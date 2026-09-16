from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from open_stock_ai.agent_runtime.transport_guard import default_external_transport_guard
from .data_platform.contracts import TemporalCoordinates
from .data_platform.service import get_market_data_platform
from .data_platform.source_registry import get_source_registry, source_endpoint
from .data_platform.warehouse import content_hash
from .models import AdjustedPricePoint
from .realtime_data import normalize_symbol


SCHEMA_VERSION = "stock_ai.price_adjustment.v1"
FACTOR_METHOD = "official_reference_price_ratio"
SUPPORTED_PRICE_BASES = frozenset(
    {"unadjusted", "forward_adjusted", "backward_adjusted"}
)
SOURCE_START = {
    "twse_official_web": date(2003, 5, 5),
    "tpex_official_web": date(2008, 1, 2),
}
_TRANSPORT_GUARD = default_external_transport_guard()


class PriceAdjustmentError(ValueError):
    pass


def normalize_price_basis(value: str | None) -> str:
    basis = str(value or "unadjusted").strip().casefold()
    aliases = {
        "raw": "unadjusted",
        "original": "unadjusted",
        "front": "forward_adjusted",
        "forward": "forward_adjusted",
        "back": "backward_adjusted",
        "backward": "backward_adjusted",
    }
    basis = aliases.get(basis, basis)
    if basis not in SUPPORTED_PRICE_BASES:
        raise PriceAdjustmentError(
            "price_basis must be unadjusted, forward_adjusted, or backward_adjusted"
        )
    return basis


def _decimal(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", "").replace("+", "")
    if text.casefold() in {"", "-", "--", "none", "null", "nan", "n/a"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _number(value: Decimal, places: str = "0.000000000001") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _price(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _official_date(value: Any) -> str:
    text = (
        str(value or "")
        .strip()
        .replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
        .replace(".", "/")
        .replace("-", "/")
    )
    parts = [part for part in text.split("/") if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise PriceAdjustmentError(f"unsupported official event date: {value}")
    year, month, day = (int(part) for part in parts)
    if year < 1911:
        year += 1911
    return date(year, month, day).isoformat()


def _source_context(symbol: str) -> tuple[str, str, str, str]:
    normalized = normalize_symbol(symbol)
    if normalized.endswith(".TWO"):
        return (
            normalized,
            "tpex_official_web",
            "tpex_exright_results",
            "TPEx",
        )
    if normalized.endswith(".TW"):
        return (
            normalized,
            "twse_official_web",
            "twse_exright_results",
            "TWSE",
        )
    raise PriceAdjustmentError(
        "official price adjustments currently require a Taiwan .TW or .TWO symbol"
    )


def _request_json(
    dataset_id: str,
    url: str,
    *,
    form: dict[str, str] | None = None,
) -> Any:
    dataset = get_source_registry().dataset(dataset_id)
    strategy = dataset.failure_strategy
    body = urlencode(form or {}).encode("utf-8") if form is not None else None
    last_error: Exception | None = None
    for attempt in range(strategy.max_attempts):
        request = Request(
            url,
            data=body,
            headers={
                "User-Agent": "Mozilla/5.0 StockAI/0.1",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            response = _TRANSPORT_GUARD.call_sync(
                f"source:{dataset_id}",
                lambda: urlopen(request, timeout=strategy.timeout_seconds),
            )
            with response:
                return json.loads(response.read().decode("utf-8-sig"))
        except Exception as exc:
            last_error = exc
            if attempt < strategy.max_attempts - 1:
                delay = (
                    strategy.backoff_seconds[
                        min(attempt, len(strategy.backoff_seconds) - 1)
                    ]
                    if strategy.backoff_seconds
                    else 0
                )
                if delay:
                    time.sleep(delay)
    assert last_error is not None
    raise last_error


def _event(
    *,
    event_date: str,
    symbol: str,
    name: str,
    event_type: str,
    previous_close: Any,
    reference_price: Any,
    adjustment_value: Any,
    source_id: str,
    dataset_id: str,
    source_record: Any,
) -> dict[str, Any] | None:
    previous = _decimal(previous_close)
    reference = _decimal(reference_price)
    value = _decimal(adjustment_value)
    if previous is None or previous <= 0:
        return None
    theoretical = previous - value if value is not None else reference
    if theoretical is None or theoretical <= 0:
        return None
    ratio = theoretical / previous
    return {
        "event_date": event_date,
        "symbol": symbol,
        "name": name,
        "event_type": event_type,
        "previous_close": _price(previous),
        "reference_price": _price(reference or theoretical),
        "theoretical_reference_price": _price(theoretical),
        "adjustment_value": _price(value or (previous - theoretical)),
        "event_ratio": _number(ratio),
        "source_id": source_id,
        "dataset_id": dataset_id,
        "factor_method": FACTOR_METHOD,
        "source_record": source_record,
    }


def parse_twse_adjustment_payload(
    payload: dict[str, Any],
    *,
    code: str,
) -> list[dict[str, Any]]:
    if str(payload.get("stat") or "").casefold() != "ok":
        raise PriceAdjustmentError(str(payload.get("stat") or "TWSE query failed"))
    output: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) < 11:
            continue
        if str(row[1]).strip().upper() != code.upper():
            continue
        item = _event(
            event_date=_official_date(row[0]),
            symbol=f"{code.upper()}.TW",
            name=str(row[2]).strip(),
            event_type=str(row[6]).strip() or "ex_right_dividend",
            previous_close=row[3],
            reference_price=row[4],
            adjustment_value=row[5],
            source_id="twse_official_web",
            dataset_id="twse_exright_results",
            source_record=row,
        )
        if item is not None:
            output.append(item)
    return output


def parse_tpex_adjustment_payload(
    payload: dict[str, Any],
    *,
    code: str,
) -> list[dict[str, Any]]:
    if str(payload.get("stat") or "").casefold() != "ok":
        raise PriceAdjustmentError(str(payload.get("stat") or "TPEx query failed"))
    tables = payload.get("tables") or []
    rows = tables[0].get("data") if tables and isinstance(tables[0], dict) else []
    output: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 9:
            continue
        if str(row[1]).strip().upper() != code.upper():
            continue
        rights = _decimal(row[5]) or Decimal("0")
        dividend = _decimal(row[6]) or Decimal("0")
        item = _event(
            event_date=_official_date(row[0]),
            symbol=f"{code.upper()}.TWO",
            name=str(row[2]).strip(),
            event_type=str(row[8]).strip() or "ex_right_dividend",
            previous_close=row[3],
            reference_price=row[4],
            adjustment_value=rights + dividend,
            source_id="tpex_official_web",
            dataset_id="tpex_exright_results",
            source_record=row,
        )
        if item is not None:
            output.append(item)
    return output


def _year_ranges(start_iso: str, end_iso: str) -> list[tuple[int, str, str]]:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    return [
        (
            year,
            max(start, date(year, 1, 1)).isoformat(),
            min(end, date(year, 12, 31)).isoformat(),
        )
        for year in range(start.year, end.year + 1)
    ]


def _roc_slash(iso_value: str) -> str:
    value = date.fromisoformat(iso_value)
    return f"{value.year - 1911:03d}/{value.month:02d}/{value.day:02d}"


def fetch_official_adjustment_events(
    symbol: str,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    normalized, source_id, dataset_id, exchange = _source_context(symbol)
    code = normalized.split(".", 1)[0]
    supported_start = SOURCE_START[source_id]

    def fetch_year(year_range: tuple[int, str, str]) -> dict[str, Any]:
        year, segment_start, segment_end = year_range
        if date.fromisoformat(segment_end) < supported_start:
            return {
                "year": year,
                "status": "unsupported",
                "row_count": 0,
                "upstream_row_count": 0,
                "error": f"official history starts {supported_start.isoformat()}",
                "payload": None,
                "events": [],
            }
        request_start = max(
            date.fromisoformat(segment_start), supported_start
        ).isoformat()
        try:
            if exchange == "TWSE":
                url = source_endpoint(dataset_id) + "?" + urlencode(
                    {
                        "response": "json",
                        "startDate": request_start.replace("-", ""),
                        "endDate": segment_end.replace("-", ""),
                    }
                )
                payload = _request_json(dataset_id, url)
                events = parse_twse_adjustment_payload(payload, code=code)
                upstream_count = len(payload.get("data") or [])
                request_details = {"method": "GET", "url": url}
            else:
                url = source_endpoint(dataset_id)
                form = {
                    "startDate": _roc_slash(request_start),
                    "endDate": _roc_slash(segment_end),
                    "response": "json",
                }
                payload = _request_json(dataset_id, url, form=form)
                events = parse_tpex_adjustment_payload(payload, code=code)
                tables = payload.get("tables") or []
                upstream_count = len(
                    tables[0].get("data") or []
                    if tables and isinstance(tables[0], dict)
                    else []
                )
                request_details = {"method": "POST", "url": url, "form": form}
            return {
                "year": year,
                "status": "succeeded",
                "row_count": len(events),
                "upstream_row_count": upstream_count,
                "error": None,
                "payload": {
                    "request": request_details,
                    "response": payload,
                },
                "events": events,
            }
        except Exception as exc:
            return {
                "year": year,
                "status": "failed",
                "row_count": 0,
                "upstream_row_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "payload": None,
                "events": [],
            }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_year, item): item[0]
            for item in _year_ranges(start, end)
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["year"])
    events_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        for item in result["events"]:
            events_by_key[(item["event_date"], item["event_type"])] = item
    events = sorted(
        events_by_key.values(),
        key=lambda item: (item["event_date"], item["event_type"]),
    )
    return {
        "symbol": normalized,
        "source_id": source_id,
        "dataset_id": dataset_id,
        "supported_start": supported_start.isoformat(),
        "coverage": [
            {
                key: value
                for key, value in item.items()
                if key not in {"payload", "events"}
            }
            for item in results
        ],
        "events": events,
        "source_payloads": [
            item["payload"] for item in results if item["payload"] is not None
        ],
    }


def _all_raw_price_items(
    *,
    entity_id: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    warehouse = get_market_data_platform().warehouse
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = warehouse.daily_price_history(
            entity_id=entity_id,
            start_date=start,
            end_date=end,
            after=cursor,
            limit=5000,
        )
        items.extend(page["items"])
        if not page["has_more"]:
            break
        next_cursor = page["next_cursor"]
        if not next_cursor or next_cursor == cursor:
            raise PriceAdjustmentError("raw daily history pagination did not advance")
        cursor = str(next_cursor)
    return items


def _persist_factor_set(
    *,
    entity_id: str,
    symbol: str,
    start: str,
    end: str,
    fetched: dict[str, Any],
) -> dict[str, Any]:
    platform = get_market_data_platform()
    warehouse = platform.warehouse
    source_id = str(fetched["source_id"])
    acquired_at = datetime.now(timezone.utc).isoformat()
    raw_payload_id = warehouse.record_raw_payload(
        source_id=source_id,
        payload={
            "symbol": symbol,
            "requested_start": start,
            "requested_end": end,
            "source_payloads": fetched["source_payloads"],
        },
        request_url=source_endpoint(str(fetched["dataset_id"])),
        requested_at=acquired_at,
        received_at=acquired_at,
        parser_id=f"{fetched['dataset_id']}.v1",
        metadata={
            "dataset": "price_adjustment_events",
            "requested_start": start,
            "requested_end": end,
            "source_payload_count": len(fetched["source_payloads"]),
        },
    )
    warehouse.save_checkpoints_batch(
        source_id=source_id,
        dataset="price_adjustment_events",
        checkpoints=[
            {
                "partition_key": f"{symbol}:{item['year']}",
                "cursor_value": str(item["year"]),
                "status": (
                    "succeeded"
                    if item["status"] == "succeeded"
                    else "failed"
                ),
                "error": (
                    None
                    if item["status"] == "succeeded"
                    else {
                        "type": item["status"],
                        "message": item.get("error"),
                    }
                ),
                "metadata": {
                    "symbol": symbol,
                    "year": item["year"],
                    "row_count": item["row_count"],
                    "upstream_row_count": item["upstream_row_count"],
                    "supported_start": fetched["supported_start"],
                },
            }
            for item in fetched["coverage"]
        ],
    )
    event_revisions: list[dict[str, Any]] = []
    for item in fetched["events"]:
        event_date = str(item["event_date"])
        envelope = warehouse.write_revision(
            dataset="price_adjustment_events",
            entity_id=entity_id,
            observation_key=f"{event_date}:{item['event_type']}",
            source_id=source_id,
            temporal=TemporalCoordinates(
                time_basis="event_time",
                trade_date=event_date,
                observed_at=event_date,
                published_at=event_date,
                available_at=acquired_at,
                acquired_at=acquired_at,
                effective_at=event_date,
            ),
            payload=item,
            raw_payload_id=raw_payload_id,
            transformation_id="stock_ai.official_adjustment_event.v1",
            code_version=platform.code_version,
        )
        event_revisions.append(
            {
                **item,
                "revision_id": envelope.revision_id,
                "payload_hash": envelope.payload_hash,
            }
        )
    complete = bool(fetched["coverage"]) and all(
        item["status"] == "succeeded" for item in fetched["coverage"]
    )
    factor_document = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "source_id": source_id,
        "factor_method": FACTOR_METHOD,
        "anchor_start": start,
        "anchor_end": end,
        "adjustment_complete": complete,
        "coverage": fetched["coverage"],
        "events": [
            {
                "event_date": item["event_date"],
                "event_type": item["event_type"],
                "event_ratio": item["event_ratio"],
                "revision_id": item["revision_id"],
                "payload_hash": item["payload_hash"],
            }
            for item in event_revisions
        ],
    }
    factor_set_id = f"PAF-{content_hash(factor_document)[:32]}"
    factor_document["factor_set_id"] = factor_set_id
    factor_set = warehouse.write_revision(
        dataset="price_adjustment_factor_sets",
        entity_id=entity_id,
        observation_key=f"{symbol}:{start}:{end}",
        source_id=source_id,
        temporal=TemporalCoordinates(
            time_basis="event_time",
            period_start=start,
            period_end=end,
            observed_at=end,
            available_at=acquired_at,
            acquired_at=acquired_at,
            effective_at=end,
        ),
        payload=factor_document,
        raw_payload_id=raw_payload_id,
        transformation_id="stock_ai.cumulative_adjustment_factor.v1",
        code_version=platform.code_version,
        input_revision_ids=[item["revision_id"] for item in event_revisions],
    )
    return {
        **factor_document,
        "factor_set_revision_id": factor_set.revision_id,
        "event_revisions": event_revisions,
        "acquired_at": acquired_at,
    }


def _cached_factor_set(
    *,
    entity_id: str,
    symbol: str,
    source_id: str,
    start: str,
    end: str,
) -> dict[str, Any] | None:
    history = get_market_data_platform().warehouse.revision_history(
        dataset="price_adjustment_factor_sets",
        entity_id=entity_id,
        observation_key=f"{symbol}:{start}:{end}",
        source_id=source_id,
    )
    items = history.get("items") or []
    if not items:
        return None
    latest = items[-1]
    payload = dict(latest["payload"])
    payload["factor_set_revision_id"] = latest["revision_id"]
    payload["event_revisions"] = payload.get("events") or []
    payload["acquired_at"] = latest["temporal"]["acquired_at"]
    return payload


def _adjusted_record(
    *,
    symbol: str,
    raw_item: dict[str, Any],
    factor_set: dict[str, Any],
    forward_factor: Decimal,
    backward_factor: Decimal,
) -> dict[str, Any]:
    raw = raw_item["record"]

    def prices(factor: Decimal) -> dict[str, float]:
        return {
            field: _price(Decimal(str(raw[field])) * factor)
            for field in ("open", "high", "low", "close")
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "date": str(raw["date"]),
        "unadjusted": prices(Decimal("1")),
        "forward_adjusted": prices(forward_factor),
        "backward_adjusted": prices(backward_factor),
        "forward_factor": _number(forward_factor),
        "backward_factor": _number(backward_factor),
        "factor_set_id": factor_set["factor_set_id"],
        "factor_source_id": factor_set["source_id"],
        "factor_method": FACTOR_METHOD,
        "factor_anchor_start": factor_set["anchor_start"],
        "factor_anchor_end": factor_set["anchor_end"],
        "adjustment_complete": factor_set["adjustment_complete"],
        "volume": int(raw.get("volume") or 0),
        "turnover": raw.get("turnover"),
        "volume_basis": "raw_shares",
        "turnover_basis": "raw_twd",
        "raw_price_revision_id": raw_item["revision_id"],
        "_input_revision_ids": [
            raw_item["revision_id"],
            factor_set["factor_set_revision_id"],
        ],
    }


def build_adjusted_price_history(
    *,
    symbol: str,
    entity_id: str,
    start: str,
    end: str,
    refresh: bool = True,
    require_complete: bool = True,
) -> dict[str, Any]:
    normalized, source_id, _, _ = _source_context(symbol)
    if refresh:
        fetched = fetch_official_adjustment_events(
            normalized,
            start=start,
            end=end,
        )
        factor_set = _persist_factor_set(
            entity_id=entity_id,
            symbol=normalized,
            start=start,
            end=end,
            fetched=fetched,
        )
    else:
        factor_set = _cached_factor_set(
            entity_id=entity_id,
            symbol=normalized,
            source_id=source_id,
            start=start,
            end=end,
        )
        if factor_set is None:
            raise PriceAdjustmentError(
                "no cached adjustment factor set exists for the requested range"
            )
    if require_complete and not factor_set["adjustment_complete"]:
        failed = [
            str(item["year"])
            for item in factor_set["coverage"]
            if item["status"] != "succeeded"
        ]
        raise PriceAdjustmentError(
            "official adjustment factor coverage is incomplete for years: "
            + ", ".join(failed)
        )
    raw_items = _all_raw_price_items(
        entity_id=entity_id,
        start=start,
        end=end,
    )
    if not raw_items:
        raise PriceAdjustmentError("no raw daily prices exist for adjustment")
    event_ratios = [
        (
            str(item["event_date"]),
            Decimal(str(item["event_ratio"])),
        )
        for item in factor_set.get("event_revisions") or factor_set.get("events") or []
        if start <= str(item["event_date"]) <= end
    ]
    first_trade_date = str(raw_items[0]["record"]["date"])
    first_forward = Decimal("1")
    for event_date, ratio in event_ratios:
        if first_trade_date < event_date <= end:
            first_forward *= ratio
    records: list[dict[str, Any]] = []
    for raw_item in raw_items:
        trade_date = str(raw_item["record"]["date"])
        forward = Decimal("1")
        for event_date, ratio in event_ratios:
            if trade_date < event_date <= end:
                forward *= ratio
        backward = forward / first_forward if first_forward else Decimal("1")
        records.append(
            _adjusted_record(
                symbol=normalized,
                raw_item=raw_item,
                factor_set=factor_set,
                forward_factor=forward,
                backward_factor=backward,
            )
        )
    batch = get_market_data_platform().warehouse.write_adjusted_price_revision_batch(
        entity_id=entity_id,
        source_id=source_id,
        records=records,
        acquired_at=str(factor_set["acquired_at"]),
        transformation_id="stock_ai.daily_price_adjuster.v1",
        code_version=get_market_data_platform().code_version,
    )
    return {
        "factor_set": factor_set,
        "projection": batch,
        "raw_point_count": len(raw_items),
    }


def query_price_basis(
    *,
    symbol: str,
    entity_id: str,
    start: str,
    end: str,
    price_basis: str,
    cursor: str | None,
    limit: int,
    refresh: bool,
    require_complete: bool,
) -> dict[str, Any]:
    basis = normalize_price_basis(price_basis)
    built = build_adjusted_price_history(
        symbol=symbol,
        entity_id=entity_id,
        start=start,
        end=end,
        refresh=refresh,
        require_complete=require_complete,
    )
    factor_set = built["factor_set"]
    page = get_market_data_platform().warehouse.adjusted_price_history(
        entity_id=entity_id,
        source_id=str(factor_set["source_id"]),
        factor_set_id=str(factor_set["factor_set_id"]),
        start_date=start,
        end_date=end,
        after=cursor,
        limit=limit,
    )
    points: list[AdjustedPricePoint] = []
    for item in page["items"]:
        record = item["record"]
        selected = record[basis]
        points.append(
            AdjustedPricePoint(
                date=record["date"],
                open=selected["open"],
                high=selected["high"],
                low=selected["low"],
                close=selected["close"],
                volume=record["volume"],
                turnover=record.get("turnover"),
                price_basis=basis,
                adjustment_factor=record[
                    "forward_factor"
                    if basis == "forward_adjusted"
                    else "backward_factor"
                    if basis == "backward_adjusted"
                    else "forward_factor"
                ]
                if basis != "unadjusted"
                else 1.0,
                raw_open=record["unadjusted"]["open"],
                raw_high=record["unadjusted"]["high"],
                raw_low=record["unadjusted"]["low"],
                raw_close=record["unadjusted"]["close"],
                forward_adjusted_open=record["forward_adjusted"]["open"],
                forward_adjusted_high=record["forward_adjusted"]["high"],
                forward_adjusted_low=record["forward_adjusted"]["low"],
                forward_adjusted_close=record["forward_adjusted"]["close"],
                backward_adjusted_open=record["backward_adjusted"]["open"],
                backward_adjusted_high=record["backward_adjusted"]["high"],
                backward_adjusted_low=record["backward_adjusted"]["low"],
                backward_adjusted_close=record["backward_adjusted"]["close"],
                factor_set_id=record["factor_set_id"],
                factor_source_id=record["factor_source_id"],
            )
        )
    return {
        "points": points,
        "point_count": len(points),
        "total_point_count": page["total_count"],
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "price_basis": basis,
        "adjustment_schema_version": SCHEMA_VERSION,
        "adjustment_complete": bool(factor_set["adjustment_complete"]),
        "factor_set_id": factor_set["factor_set_id"],
        "factor_set_revision_id": factor_set["factor_set_revision_id"],
        "factor_source_id": factor_set["source_id"],
        "factor_method": factor_set["factor_method"],
        "factor_anchor_start": factor_set["anchor_start"],
        "factor_anchor_end": factor_set["anchor_end"],
        "adjustment_event_count": len(factor_set.get("events") or []),
        "adjustment_coverage": factor_set["coverage"],
        "volume_basis": "raw_shares",
        "turnover_basis": "raw_twd",
        "available_price_bases": sorted(SUPPORTED_PRICE_BASES),
        "projection": built["projection"],
    }


def backtest_price_series(
    *,
    symbol: str,
    entity_id: str,
    start: str,
    end: str,
    price_basis: str,
    refresh: bool = True,
) -> dict[str, Any]:
    """Return an explicit, reproducible basis for a backtest dataset."""

    result = query_price_basis(
        symbol=symbol,
        entity_id=entity_id,
        start=start,
        end=end,
        price_basis=price_basis,
        cursor=None,
        limit=5000,
        refresh=refresh,
        require_complete=True,
    )
    points = list(result["points"])
    cursor = result["next_cursor"]
    while result["has_more"]:
        if not cursor:
            raise PriceAdjustmentError(
                "adjusted backtest pagination did not provide a next cursor"
            )
        previous_cursor = cursor
        page = query_price_basis(
            symbol=symbol,
            entity_id=entity_id,
            start=start,
            end=end,
            price_basis=price_basis,
            cursor=cursor,
            limit=5000,
            refresh=False,
            require_complete=True,
        )
        if page["factor_set_id"] != result["factor_set_id"]:
            raise PriceAdjustmentError(
                "adjustment factor set changed during backtest pagination"
            )
        points.extend(page["points"])
        result = page
        cursor = page["next_cursor"]
        if page["has_more"] and cursor == previous_cursor:
            raise PriceAdjustmentError(
                "adjusted backtest pagination did not advance"
            )
    return {
        "schema_version": "stock_ai.backtest_price_series.v1",
        "symbol": normalize_symbol(symbol),
        "requested_start": start,
        "requested_end": end,
        "price_basis": normalize_price_basis(price_basis),
        "factor_set_id": result["factor_set_id"],
        "factor_source_id": result["factor_source_id"],
        "adjustment_complete": result["adjustment_complete"],
        "total_point_count": len(points),
        "points": points,
    }
