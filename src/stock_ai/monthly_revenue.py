from __future__ import annotations

from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import re

from open_stock_ai.agent_runtime import default_external_transport_guard

from .data_platform.contracts import payload_leaf_pointers, utc_now
from .data_platform.service import MarketDataPlatform, get_market_data_platform, stable_entity_id
from .data_platform.source_registry import get_source_registry, source_endpoint
from .models import RevenueItem
from .realtime_data import normalize_symbol


MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION = "stock_ai.monthly_revenue_history.v1"
MONTHLY_REVENUE_ARCHIVE_PARSER = "stock_ai.mops_monthly_revenue_archive_parser.v1"
MONTHLY_REVENUE_TRANSFORMATION = "stock_ai.monthly_revenue_history_normalizer.v1"
MONTHLY_REVENUE_DATASET = "revenues_monthly"
MONTHLY_REVENUE_SOURCE_ID = "mops_archive"
EARLIEST_ARCHIVE_PERIOD = "2010-01"
MAX_HISTORY_MONTHS = 240
ARCHIVE_MARKETS = {
    "sii": {"exchange": "TWSE", "symbol_suffix": ".TW", "label": "上市"},
    "otc": {"exchange": "TPEx", "symbol_suffix": ".TWO", "label": "上櫃"},
    "rotc": {"exchange": "TPEx-ESB", "symbol_suffix": ".TWO", "label": "興櫃"},
    "pub": {"exchange": "PUBLIC", "symbol_suffix": "", "label": "公開發行"},
}
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SECURITY_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,8}$", re.IGNORECASE)


class MonthlyRevenueHistoryError(ValueError):
    pass


class MonthlyRevenueSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MonthlyRevenueArchivePage:
    period: str
    market_segment: str
    source_url: str
    acquired_at: str
    content_type: str
    body: bytes
    items: list[RevenueItem]


def _clean_cell(value: Any) -> str:
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .replace("\u3000", " ")
        .split()
    ).strip()


def _number(value: Any) -> float | None:
    text = _clean_cell(value).replace(",", "").replace("%", "")
    if text.casefold() in {"", "-", "--", "n/a", "na", "不適用", "無"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class _MopsRevenueHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_industry: str | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.records: list[tuple[str | None, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        if lower == "tr":
            self._row = []
        elif lower in {"td", "th"} and self._row is not None:
            self._cell = []
        elif lower == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(_clean_cell("".join(self._cell)))
            self._cell = None
        elif lower == "tr" and self._row is not None:
            self._consume_row(self._row)
            self._row = None

    def _consume_row(self, row: list[str]) -> None:
        industry_cell = next(
            (cell for cell in row if cell.startswith("產業別：")),
            None,
        )
        if industry_cell:
            self.current_industry = _clean_cell(
                industry_cell.split("：", 1)[1]
            ) or None
            return
        if len(row) < 11:
            return
        code = _clean_cell(row[0])
        if not _SECURITY_CODE_PATTERN.fullmatch(code) or code == "合計":
            return
        self.records.append((self.current_industry, row[:11]))


def validate_period(period: str, *, field_name: str = "period") -> str:
    value = str(period or "").strip()
    if not _PERIOD_PATTERN.fullmatch(value):
        raise MonthlyRevenueHistoryError(
            f"{field_name} must use YYYY-MM with a valid month"
        )
    return value


def _period_index(period: str) -> int:
    value = validate_period(period)
    return int(value[:4]) * 12 + int(value[5:7]) - 1


def iter_periods(start_period: str, end_period: str) -> list[str]:
    start = validate_period(start_period, field_name="start_period")
    end = validate_period(end_period, field_name="end_period")
    start_index = _period_index(start)
    end_index = _period_index(end)
    if end_index < start_index:
        raise MonthlyRevenueHistoryError("end_period must not precede start_period")
    count = end_index - start_index + 1
    if count > MAX_HISTORY_MONTHS:
        raise MonthlyRevenueHistoryError(
            f"monthly revenue history is limited to {MAX_HISTORY_MONTHS} months per request"
        )
    return [
        f"{index // 12:04d}-{index % 12 + 1:02d}"
        for index in range(start_index, end_index + 1)
    ]


def archive_periods(start_period: str, end_period: str) -> list[str]:
    periods = iter_periods(start_period, end_period)
    if _period_index(periods[0]) < _period_index(EARLIEST_ARCHIVE_PERIOD):
        raise MonthlyRevenueHistoryError(
            f"MOPS monthly revenue archive starts at {EARLIEST_ARCHIVE_PERIOD}"
        )
    return periods


def latest_completed_period(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone()
    year = current.year
    month = current.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def _period_end(period: str) -> str:
    year, month = (int(value) for value in validate_period(period).split("-", 1))
    return f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"


def _period_start(period: str) -> str:
    return f"{validate_period(period)}-01"


def _roc_period(period: str) -> tuple[str, str]:
    year, month = (int(value) for value in validate_period(period).split("-", 1))
    if year < 1912:
        raise MonthlyRevenueHistoryError("period predates the Minguo calendar")
    return str(year - 1911), str(month)


def archive_url(period: str, market_segment: str) -> str:
    if market_segment not in ARCHIVE_MARKETS:
        raise MonthlyRevenueHistoryError(
            f"market_segment must be one of {sorted(ARCHIVE_MARKETS)}"
        )
    roc_year, month = _roc_period(period)
    return source_endpoint(
        "mops_monthly_revenue_archive",
        market=market_segment,
        roc_year=roc_year,
        month=month,
    )


def resolve_market_segment(
    symbol: str,
    *,
    platform: MarketDataPlatform | None = None,
    requested: str | None = None,
) -> tuple[str, str]:
    if requested:
        value = requested.strip().casefold()
        if value not in ARCHIVE_MARKETS:
            raise MonthlyRevenueHistoryError(
                f"market_segment must be one of {sorted(ARCHIVE_MARKETS)}"
            )
        return value, "explicit"
    normalized = normalize_symbol(symbol)
    service = platform or get_market_data_platform()
    resolution = service.resolve_entity(
        normalized,
        identifier_type="display_symbol",
    )
    if resolution.get("status") == "resolved":
        exchange = str((resolution.get("entity") or {}).get("exchange") or "")
        for segment, contract in ARCHIVE_MARKETS.items():
            if contract["exchange"] == exchange:
                return segment, "entity_registry"
    if normalized.endswith(".TW"):
        return "sii", "display_symbol_suffix_fallback"
    if normalized.endswith(".TWO"):
        return "otc", "display_symbol_suffix_fallback"
    raise MonthlyRevenueHistoryError(
        "market segment cannot be resolved; pass market_segment explicitly"
    )


def parse_archive_html(
    body: bytes,
    *,
    period: str,
    market_segment: str,
    source_url: str,
    acquired_at: str,
    symbol: str | None = None,
) -> list[RevenueItem]:
    validate_period(period)
    if market_segment not in ARCHIVE_MARKETS:
        raise MonthlyRevenueHistoryError("unknown MOPS archive market segment")
    text = body.decode("cp950", errors="replace")
    parser = _MopsRevenueHTMLParser()
    parser.feed(text)
    if "營收" not in text or not parser.records:
        raise MonthlyRevenueSourceError(
            f"MOPS archive did not contain a monthly-revenue table: {source_url}"
        )
    target_code = normalize_symbol(symbol).split(".", 1)[0] if symbol else None
    suffix = str(ARCHIVE_MARKETS[market_segment]["symbol_suffix"])
    items: list[RevenueItem] = []
    for industry, row in parser.records:
        code = _clean_cell(row[0])
        if target_code and code != target_code:
            continue
        items.append(
            RevenueItem(
                report_date=None,
                report_date_semantics=None,
                period=period,
                symbol=f"{code}{suffix}",
                name=_clean_cell(row[1]),
                industry=industry,
                current_revenue=_number(row[2]),
                previous_revenue=_number(row[3]),
                last_year_revenue=_number(row[4]),
                mom_change_percent=_number(row[5]),
                yoy_change_percent=_number(row[6]),
                ytd_revenue=_number(row[7]),
                last_ytd_revenue=_number(row[8]),
                ytd_change_percent=_number(row[9]),
                note=(
                    None
                    if _clean_cell(row[10]).casefold() in {"", "-", "無"}
                    else _clean_cell(row[10])
                ),
                source=f"MOPS monthly revenue archive ({ARCHIVE_MARKETS[market_segment]['label']})",
                source_id=MONTHLY_REVENUE_SOURCE_ID,
                source_url=source_url,
                source_market=market_segment,
                unit="thousand_twd",
                acquired_at=acquired_at,
                published_at=None,
                available_at=acquired_at,
                publication_time_status="not_provided_by_archive",
                growth_source="official_disclosed",
            )
        )
    return items


def fetch_archive_period(
    period: str,
    *,
    market_segment: str,
    symbol: str | None = None,
    timeout: float | None = None,
) -> MonthlyRevenueArchivePage:
    source_url = archive_url(period, market_segment)
    dataset = get_source_registry().dataset("mops_monthly_revenue_archive")
    attempts = max(1, dataset.failure_strategy.max_attempts)
    request_timeout = float(timeout or dataset.failure_strategy.timeout_seconds)
    backoffs = dataset.failure_strategy.backoff_seconds
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 StockAI/0.1",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            response = default_external_transport_guard().call_sync(
                f"source:mops:{dataset.dataset_id}:{period}:{market_segment}:{symbol or ''}:attempt:{attempt}",
                lambda: urlopen(request, timeout=request_timeout),
            )
            with response:
                body = response.read()
                content_type = str(
                    response.headers.get("Content-Type") or "text/html"
                )
            acquired_at = utc_now()
            items = parse_archive_html(
                body,
                period=period,
                market_segment=market_segment,
                source_url=source_url,
                acquired_at=acquired_at,
                symbol=symbol,
            )
            return MonthlyRevenueArchivePage(
                period=period,
                market_segment=market_segment,
                source_url=source_url,
                acquired_at=acquired_at,
                content_type=content_type,
                body=body,
                items=items,
            )
        except (HTTPError, URLError, TimeoutError, MonthlyRevenueSourceError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            if backoffs:
                import time

                time.sleep(float(backoffs[min(attempt, len(backoffs) - 1)]))
    raise MonthlyRevenueSourceError(
        f"MOPS monthly revenue archive failed for {period}: {last_error}"
    ) from last_error


def _entity_id_for(
    symbol: str,
    *,
    market_segment: str,
    platform: MarketDataPlatform,
) -> str:
    resolution = platform.resolve_entity(
        symbol,
        identifier_type="display_symbol",
    )
    if resolution.get("status") == "resolved":
        return str(resolution["entity"]["entity_id"])
    exchange = str(ARCHIVE_MARKETS[market_segment]["exchange"])
    return stable_entity_id(
        market="taiwan",
        exchange=exchange,
        source_code=normalize_symbol(symbol).split(".", 1)[0],
    )


def _field_provenance(
    record: dict[str, Any],
    temporal: Any,
    raw_payload_id: str,
) -> dict[str, dict[str, Any]]:
    return {
        pointer: {
            "source_id": MONTHLY_REVENUE_SOURCE_ID,
            "temporal": temporal,
            "updated_at": temporal.acquired_at,
            "raw_payload_id": raw_payload_id,
            "raw_json_pointer": f"/0{pointer}" if pointer != "/" else "/0",
            "quality_status": "valid",
            "quality_flags": [],
            "transformation_id": MONTHLY_REVENUE_TRANSFORMATION,
            "input_fields": [pointer],
        }
        for pointer in payload_leaf_pointers(record)
    }


def _persist_archive_page(
    page: MonthlyRevenueArchivePage,
    *,
    platform: MarketDataPlatform,
) -> list[Any]:
    records = [item.model_dump(mode="json") for item in page.items]
    return platform.ingest_records(
        source_id=MONTHLY_REVENUE_SOURCE_ID,
        dataset=MONTHLY_REVENUE_DATASET,
        records=records,
        entity_id_for=lambda row: _entity_id_for(
            str(row["symbol"]),
            market_segment=page.market_segment,
            platform=platform,
        ),
        observation_key_for=lambda row: str(row["period"]),
        time_basis="fiscal_period",
        fiscal_period_for=lambda row: str(row["period"]),
        period_start_for=lambda row: _period_start(str(row["period"])),
        period_end_for=lambda row: _period_end(str(row["period"])),
        observed_at_for=lambda row: _period_end(str(row["period"])),
        published_at_for=lambda row: row.get("published_at"),
        available_at_for=lambda row: str(
            row.get("available_at") or page.acquired_at
        ),
        effective_at_for=lambda row: _period_end(str(row["period"])),
        request_url=page.source_url,
        raw_response=page.body,
        raw_content_type=page.content_type,
        raw_content_encoding="cp950",
        raw_parser_id=MONTHLY_REVENUE_ARCHIVE_PARSER,
        transformation_id=MONTHLY_REVENUE_TRANSFORMATION,
        acquired_at=page.acquired_at,
        field_provenance_for=_field_provenance,
    )


def _history_entity_id(
    symbol: str,
    *,
    market_segment: str,
    platform: MarketDataPlatform,
) -> str:
    normalized = normalize_symbol(symbol)
    return _entity_id_for(
        normalized,
        market_segment=market_segment,
        platform=platform,
    )


def _history_storage_entity_ids(
    symbol: str,
    *,
    market_segment: str,
    platform: MarketDataPlatform,
) -> list[str]:
    normalized = normalize_symbol(symbol)
    canonical = _history_entity_id(
        normalized,
        market_segment=market_segment,
        platform=platform,
    )
    fallback = stable_entity_id(
        market="taiwan",
        exchange=str(ARCHIVE_MARKETS[market_segment]["exchange"]),
        source_code=normalized.split(".", 1)[0],
    )
    return list(dict.fromkeys((canonical, fallback)))


def query_monthly_revenue_history(
    symbol: str,
    *,
    start_period: str = EARLIEST_ARCHIVE_PERIOD,
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = MAX_HISTORY_MONTHS,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise MonthlyRevenueHistoryError("symbol is required")
    end = end_period or latest_completed_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    entity_id = _history_entity_id(
        normalized,
        market_segment=market,
        platform=service,
    )
    storage_entity_ids = _history_storage_entity_ids(
        normalized,
        market_segment=market,
        platform=service,
    )
    rows = [
        row
        for storage_entity_id in storage_entity_ids
        for row in service.standard_query(
            "financials",
            dataset=MONTHLY_REVENUE_DATASET,
            entity_id=storage_entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=10000,
        )
    ]
    source_rank = {"twse_openapi": 0, MONTHLY_REVENUE_SOURCE_ID: 1, "mops": 2}
    selected: dict[str, dict[str, Any]] = {}
    requested_set = set(requested_periods)
    for row in rows:
        record = dict(row.get("record") or {})
        period = str(record.get("period") or row.get("observation_key") or "")
        if period not in requested_set:
            continue
        candidate_rank = source_rank.get(str(row.get("source_id") or ""), 99)
        existing = selected.get(period)
        if (
            existing is None
            or candidate_rank < int(existing["_source_rank"])
            or (
                candidate_rank == int(existing["_source_rank"])
                and str(row.get("acquired_at") or "")
                > str(existing.get("acquired_at") or "")
            )
        ):
            selected[period] = {**row, "record": record, "_source_rank": candidate_rank}
    ordered_periods = sorted(selected, reverse=True)
    page_limit = max(1, min(int(limit), MAX_HISTORY_MONTHS))
    items = [
        RevenueItem.model_validate(selected[period]["record"]).model_dump(mode="json")
        for period in ordered_periods[:page_limit]
    ]
    stored_periods = sorted(selected)
    missing_periods = [
        period for period in requested_periods if period not in selected
    ]
    return {
        "schema_version": MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "entity_id": entity_id,
        "storage_entity_ids": storage_entity_ids,
        "market_segment": market,
        "market_resolution_basis": resolution_basis,
        "start_period": requested_periods[0],
        "end_period": requested_periods[-1],
        "unit": "thousand_twd",
        "count": len(items),
        "total_stored_period_count": len(stored_periods),
        "items": items,
        "coverage": {
            "requested_period_count": len(requested_periods),
            "stored_period_count": len(stored_periods),
            "missing_period_count": len(missing_periods),
            "missing_periods": missing_periods,
            "first_stored_period": stored_periods[0] if stored_periods else None,
            "last_stored_period": stored_periods[-1] if stored_periods else None,
            "is_complete": not missing_periods,
        },
        "source_ids": sorted(
            {
                str(selected[period].get("source_id"))
                for period in selected
                if selected[period].get("source_id")
            }
        ),
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "truthfulness": {
            "archive_publication_time": (
                "Historical MOPS archive pages do not provide an original "
                "publication timestamp; available_at is conservatively the "
                "actual acquisition time."
            ),
            "missing_values": "Missing official values remain null.",
            "growth_metrics": "MoM, YoY and YTD YoY are official disclosed fields.",
        },
    }


def backfill_monthly_revenue_history(
    symbol: str,
    *,
    start_period: str = EARLIEST_ARCHIVE_PERIOD,
    end_period: str | None = None,
    market_segment: str | None = None,
    force_refresh: bool = False,
    max_workers: int = 4,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise MonthlyRevenueHistoryError("symbol is required")
    end = end_period or latest_completed_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    partition_keys = {
        period: f"{market}:{period}:{normalized}"
        for period in requested_periods
    }
    checkpoints = service.warehouse.get_checkpoints(
        source_id=MONTHLY_REVENUE_SOURCE_ID,
        dataset=MONTHLY_REVENUE_DATASET,
        partition_keys=partition_keys.values(),
    )
    pending_periods = [
        period
        for period in requested_periods
        if force_refresh
        or checkpoints.get(partition_keys[period], {}).get("status") != "succeeded"
    ]
    failures: list[dict[str, str]] = []
    fetched_periods: list[str] = []
    found_periods: list[str] = []
    revision_count = 0
    worker_count = max(1, min(int(max_workers), 4, len(pending_periods) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                fetch_archive_period,
                period,
                market_segment=market,
                symbol=normalized,
            ): period
            for period in pending_periods
        }
        for future in as_completed(future_map):
            period = future_map[future]
            partition_key = partition_keys[period]
            try:
                page = future.result()
                envelopes = _persist_archive_page(page, platform=service)
            except Exception as exc:
                failures.append(
                    {
                        "period": period,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                service.warehouse.save_checkpoint(
                    source_id=MONTHLY_REVENUE_SOURCE_ID,
                    dataset=MONTHLY_REVENUE_DATASET,
                    partition_key=partition_key,
                    status="failed",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    metadata={"symbol": normalized, "market_segment": market},
                )
                continue
            fetched_periods.append(period)
            if page.items:
                found_periods.append(period)
            revision_count += len(envelopes)
            service.warehouse.save_checkpoint(
                source_id=MONTHLY_REVENUE_SOURCE_ID,
                dataset=MONTHLY_REVENUE_DATASET,
                partition_key=partition_key,
                status="succeeded",
                cursor_value=period,
                metadata={
                    "symbol": normalized,
                    "market_segment": market,
                    "source_url": page.source_url,
                    "record_count": len(page.items),
                    "archive_page_preserved": True,
                },
            )
    history = query_monthly_revenue_history(
        normalized,
        start_period=start_period,
        end_period=end,
        market_segment=market,
        platform=service,
    )
    return {
        **history,
        "sync": {
            "status": (
                "succeeded"
                if not failures
                else "partial"
                if fetched_periods
                else "failed"
            ),
            "requested_period_count": len(requested_periods),
            "fetched_period_count": len(fetched_periods),
            "skipped_period_count": len(requested_periods) - len(pending_periods),
            "record_period_count": len(found_periods),
            "revision_result_count": revision_count,
            "force_refresh": bool(force_refresh),
            "market_resolution_basis": resolution_basis,
            "failed_periods": sorted(failures, key=lambda item: item["period"]),
        },
    }
