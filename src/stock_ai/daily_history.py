from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .data_platform.contracts import EntityRecord
from .data_platform.service import get_market_data_platform, stable_entity_id
from .models import PricePoint
from .price_adjustments import (
    PriceAdjustmentError,
    normalize_price_basis,
    query_price_basis,
)
from .realtime_data import normalize_symbol
from .taiwan_official import (
    normalize_taiwan_code,
    tpex_history_range,
    twse_history_range,
)


class DailyHistoryQueryError(ValueError):
    pass


def _iso_date(value: str | None, *, name: str) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise DailyHistoryQueryError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def _months_between(start_iso: str, end_iso: str) -> list[str]:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    result: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        result.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def _source_context(symbol: str) -> tuple[str, str, str]:
    normalized = normalize_symbol(symbol)
    if normalized.endswith(".TWO"):
        return normalized, "TPEx", "tpex_official_web"
    if normalized.endswith(".TW"):
        return normalized, "TWSE", "twse_official_web"
    raise DailyHistoryQueryError(
        "complete official daily history currently requires a Taiwan .TW or .TWO symbol"
    )


def _ensure_entity(
    *,
    symbol: str,
    exchange: str,
    source_id: str,
) -> str:
    platform = get_market_data_platform()
    resolution = platform.resolve_entity(symbol, identifier_type="display_symbol")
    if resolution["status"] == "resolved":
        return str(resolution["entity"]["entity_id"])
    if resolution["status"] == "ambiguous":
        raise DailyHistoryQueryError(f"ambiguous display symbol: {symbol}")
    code = symbol.split(".", 1)[0]
    entity_id = stable_entity_id(
        market="taiwan",
        exchange=exchange,
        source_code=code,
    )
    platform.warehouse.upsert_entity(
        EntityRecord(
            entity_id=entity_id,
            entity_type="stock",
            canonical_name=symbol,
            market="taiwan",
            exchange=exchange,
            currency="TWD",
            lifecycle_status="unknown",
            metadata={"display_symbol": symbol, "provisional": True},
        ),
        identifiers=(
            {
                "source_id": source_id,
                "identifier_type": "display_symbol",
                "identifier_value": symbol,
                "confidence": 0.9,
                "is_primary": True,
                "metadata": {"provisional": True},
            },
        ),
    )
    return entity_id


def _persist_points(
    *,
    symbol: str,
    entity_id: str,
    source_id: str,
    points: list[PricePoint],
    is_fallback: bool,
    requested_start: str,
    requested_end: str,
) -> None:
    if not points:
        return
    records = [
        {
            "symbol": symbol,
            **point.model_dump(mode="json"),
            "source_id": source_id,
            "price_basis": "unadjusted",
        }
        for point in points
    ]
    platform = get_market_data_platform()
    dataset_id = {
        "tpex_official_web": "tpex_trading_stock",
        "twse_official_web": "twse_stock_day",
        "yahoo_finance": "yahoo_chart",
    }[source_id]
    acquired_at = datetime.now(timezone.utc).isoformat()
    request_url = platform.source_registry_service.endpoint(dataset_id, symbol=symbol)
    parser_id = (
        "tpex.trading_stock.monthly.v1"
        if source_id == "tpex_official_web"
        else "yahoo.chart.daily.v1"
        if source_id == "yahoo_finance"
        else "twse.stock_day.monthly.v1"
    )
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id=source_id,
        payload=records,
        request_url=request_url,
        requested_at=acquired_at,
        received_at=acquired_at,
        parser_id=parser_id,
        metadata={
            "dataset": "prices_daily",
            "record_count": len(records),
            "requested_start": requested_start,
            "requested_end": requested_end,
            "transformation_id": "stock_ai.daily_history_normalizer.v1",
        },
    )
    batch = platform.warehouse.write_daily_price_revision_batch(
        entity_id=entity_id,
        source_id=source_id,
        records=records,
        raw_payload_id=raw_payload_id,
        acquired_at=acquired_at,
        is_fallback=is_fallback,
        transformation_id="stock_ai.daily_history_normalizer.v1",
        code_version=platform.code_version,
    )
    platform.warehouse.save_checkpoint(
        source_id=source_id,
        dataset="prices_daily",
        partition_key=f"{symbol}:range",
        cursor_value=requested_end,
        status="succeeded",
        metadata={
            "requested_start": requested_start,
            "requested_end": requested_end,
            "record_count": len(records),
            **batch,
            "price_basis": "unadjusted",
            "raw_payload_id": raw_payload_id,
        },
    )


def _persist_coverage(
    *,
    symbol: str,
    source_id: str,
    coverage: list[dict[str, Any]],
) -> None:
    warehouse = get_market_data_platform().warehouse
    warehouse.save_checkpoints_batch(
        source_id=source_id,
        dataset="prices_daily",
        checkpoints=[
            {
                "partition_key": f"{symbol}:{item['month']}",
                "cursor_value": str(item["month"]),
                "status": str(item["status"]),
                "error": (
                    {"type": str(item.get("error") or "upstream_error")}
                    if item["status"] != "succeeded"
                    else None
                ),
                "metadata": {
                    "symbol": symbol,
                    "month": item["month"],
                    "row_count": int(item.get("row_count") or 0),
                    "coverage_kind": "official_month",
                },
            }
            for item in coverage
        ],
    )


def _cached_coverage(
    *,
    symbol: str,
    source_id: str,
    months: list[str],
) -> list[dict[str, Any]]:
    warehouse = get_market_data_platform().warehouse
    partition_keys = [f"{symbol}:{month}" for month in months]
    checkpoints = warehouse.get_checkpoints(
        source_id=source_id,
        dataset="prices_daily",
        partition_keys=partition_keys,
    )
    result: list[dict[str, Any]] = []
    for month in months:
        checkpoint = checkpoints.get(f"{symbol}:{month}")
        result.append(
            {
                "month": month,
                "status": checkpoint["status"] if checkpoint else "not_fetched",
                "row_count": int((checkpoint or {}).get("metadata", {}).get("row_count") or 0),
                "error": (checkpoint or {}).get("error"),
            }
        )
    return result


def _fetch_yahoo_range(symbol: str, start_iso: str, end_iso: str) -> list[PricePoint]:
    try:
        import yfinance as yf
    except Exception:
        return []
    inclusive_end = date.fromisoformat(end_iso) + timedelta(days=1)
    try:
        history = yf.Ticker(symbol).history(
            start=start_iso,
            end=inclusive_end.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
        )
    except Exception:
        return []
    if history is None or history.empty:
        return []
    points: list[PricePoint] = []
    for index, row in history.iterrows():
        day = index.date().isoformat() if hasattr(index, "date") else str(index)[:10]
        if not (start_iso <= day <= end_iso):
            continue
        try:
            values = {
                field: float(row.get(field.title()))
                for field in ("open", "high", "low", "close")
            }
            if any(value != value for value in values.values()):
                continue
            volume = int(row.get("Volume") or 0)
        except (TypeError, ValueError):
            continue
        points.append(
            PricePoint(
                date=day,
                open=round(values["open"], 4),
                high=round(values["high"], 4),
                low=round(values["low"], 4),
                close=round(values["close"], 4),
                volume=volume,
                turnover=None,
            )
        )
    return points


def query_daily_history(
    symbol: str,
    *,
    start: str,
    end: str,
    cursor: str | None = None,
    limit: int = 5000,
    refresh: bool = True,
    allow_fallback: bool = True,
    price_basis: str = "unadjusted",
    refresh_adjustments: bool | None = None,
    require_complete_adjustment: bool = True,
) -> dict[str, Any]:
    start_iso = _iso_date(start, name="start")
    end_iso = _iso_date(end, name="end")
    if start_iso > end_iso:
        raise DailyHistoryQueryError("start must not be after end")
    cursor_iso = _iso_date(cursor, name="cursor") if cursor else None
    if cursor_iso and not (start_iso <= cursor_iso <= end_iso):
        raise DailyHistoryQueryError("cursor must be inside the requested date range")
    page_limit = max(1, min(int(limit), 5000))
    try:
        selected_price_basis = normalize_price_basis(price_basis)
    except PriceAdjustmentError as exc:
        raise DailyHistoryQueryError(str(exc)) from exc
    normalized, exchange, source_id = _source_context(symbol)
    entity_id = _ensure_entity(
        symbol=normalized,
        exchange=exchange,
        source_id=source_id,
    )
    code = normalize_taiwan_code(normalized)
    errors: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]]
    if refresh:
        if exchange == "TPEx":
            official_points, coverage = tpex_history_range(code, start_iso, end_iso)
        else:
            official_points, coverage = twse_history_range(code, start_iso, end_iso)
        _persist_coverage(symbol=normalized, source_id=source_id, coverage=coverage)
        _persist_points(
            symbol=normalized,
            entity_id=entity_id,
            source_id=source_id,
            points=official_points,
            is_fallback=False,
            requested_start=start_iso,
            requested_end=end_iso,
        )
        failed_months = [item for item in coverage if item["status"] != "succeeded"]
        errors.extend(
            {
                "source_id": source_id,
                "month": item["month"],
                "type": item.get("error") or "upstream_error",
            }
            for item in failed_months
        )
        if allow_fallback and (failed_months or not official_points):
            fallback_points = _fetch_yahoo_range(normalized, start_iso, end_iso)
            _persist_points(
                symbol=normalized,
                entity_id=entity_id,
                source_id="yahoo_finance",
                points=fallback_points,
                is_fallback=True,
                requested_start=start_iso,
                requested_end=end_iso,
            )
    coverage = _cached_coverage(
        symbol=normalized,
        source_id=source_id,
        months=_months_between(start_iso, end_iso),
    )
    known_errors = {
        (str(item.get("source_id")), str(item.get("month")), str(item.get("type")))
        for item in errors
    }
    for item in coverage:
        if item["status"] == "succeeded":
            continue
        error_payload = item.get("error") or {}
        error_type = (
            str(error_payload.get("type") or "not_fetched")
            if isinstance(error_payload, dict)
            else str(error_payload)
        )
        key = (source_id, str(item["month"]), error_type)
        if key not in known_errors:
            errors.append(
                {
                    "source_id": source_id,
                    "month": item["month"],
                    "type": error_type,
                }
            )
            known_errors.add(key)
    page = get_market_data_platform().warehouse.daily_price_history(
        entity_id=entity_id,
        start_date=start_iso,
        end_date=end_iso,
        after=cursor_iso,
        limit=page_limit,
    )
    items = page["items"]
    points = [PricePoint.model_validate(item["record"]) for item in items]
    source_ids = sorted({str(item["source_id"]) for item in items})
    fallback_count = sum(bool(item["is_fallback"]) for item in items)
    turnover_count = sum(point.turnover is not None for point in points)
    range_complete = bool(coverage) and all(
        item["status"] == "succeeded" for item in coverage
    )
    total_count = int(page["total_count"])
    adjustment: dict[str, Any] = {
        "adjustment_schema_version": "stock_ai.price_adjustment.v1",
        "adjustment_complete": None,
        "factor_set_id": None,
        "factor_set_revision_id": None,
        "factor_source_id": None,
        "factor_method": None,
        "factor_anchor_start": None,
        "factor_anchor_end": None,
        "adjustment_event_count": 0,
        "adjustment_coverage": [],
        "available_price_bases": [
            "backward_adjusted",
            "forward_adjusted",
            "unadjusted",
        ],
        "volume_basis": "raw_shares",
        "turnover_basis": "raw_twd",
    }
    if selected_price_basis != "unadjusted":
        if require_complete_adjustment and not range_complete:
            raise DailyHistoryQueryError(
                "official raw daily coverage must be complete before adjusted prices can be used"
            )
        try:
            adjusted = query_price_basis(
                symbol=normalized,
                entity_id=entity_id,
                start=start_iso,
                end=end_iso,
                price_basis=selected_price_basis,
                cursor=cursor_iso,
                limit=page_limit,
                refresh=(
                    True
                    if refresh_adjustments is None
                    else bool(refresh_adjustments)
                ),
                require_complete=require_complete_adjustment,
            )
        except PriceAdjustmentError as exc:
            raise DailyHistoryQueryError(str(exc)) from exc
        points = adjusted["points"]
        total_count = int(adjusted["total_point_count"])
        source_ids = sorted(
            {
                *source_ids,
                str(adjusted["factor_source_id"]),
            }
        )
        page["has_more"] = adjusted["has_more"]
        page["next_cursor"] = adjusted["next_cursor"]
        adjustment = {
            key: value
            for key, value in adjusted.items()
            if key
            in {
                "adjustment_schema_version",
                "adjustment_complete",
                "factor_set_id",
                "factor_set_revision_id",
                "factor_source_id",
                "factor_method",
                "factor_anchor_start",
                "factor_anchor_end",
                "adjustment_event_count",
                "adjustment_coverage",
                "available_price_bases",
                "volume_basis",
                "turnover_basis",
            }
        }
    indicator_count = total_count
    indicators = {
        name: indicator_count >= window
        for name, window in {
            "MA5": 5,
            "MA10": 10,
            "MA20": 20,
            "MA60": 60,
            "BOLL(20,2)": 20,
            "MACD": 35,
        }.items()
    }
    return {
        "schema_version": "stock_ai.daily_history.v1",
        "symbol": normalized,
        "requested_start": start_iso,
        "requested_end": end_iso,
        "cursor": cursor_iso,
        "limit": page_limit,
        "points": points,
        "point_count": len(points),
        "total_point_count": total_count,
        "history_start": points[0].date if points else None,
        "history_end": points[-1].date if points else None,
        "next_cursor": page["next_cursor"],
        "has_more": bool(page["has_more"]),
        "range_complete": range_complete,
        "coverage": coverage,
        "source_ids": source_ids,
        "fallback_count": fallback_count,
        "is_fallback": bool(fallback_count),
        "price_basis": selected_price_basis,
        "field_coverage": {
            "open": len(points),
            "high": len(points),
            "low": len(points),
            "close": len(points),
            "volume": len(points),
            "turnover": turnover_count,
        },
        "turnover_complete": bool(points) and turnover_count == len(points),
        **adjustment,
        "available_indicators": indicators,
        "quality": (
            "complete"
            if (
                range_complete
                and total_count
                and not fallback_count
                and (
                    selected_price_basis == "unadjusted"
                    or adjustment["adjustment_complete"] is True
                )
            )
            else "partial"
            if total_count
            else "unavailable"
        ),
        "note": (
            (
                "指定日期範圍已由官方月資料完整查詢；價格為原始未復權日 K。"
                if selected_price_basis == "unadjusted"
                else (
                    "指定日期範圍已由官方原始日 K 與官方權息參考價完整重建；"
                    + (
                        "前復權以查詢終點為錨。"
                        if selected_price_basis == "forward_adjusted"
                        else "後復權以查詢起點為錨。"
                    )
                )
            )
            if range_complete
            and total_count
            and not fallback_count
            and (
                selected_price_basis == "unadjusted"
                or adjustment["adjustment_complete"] is True
            )
            else "資料覆蓋不完整或含研究用途 fallback；缺口與來源已逐月揭露，不會補造 K 線。"
        ),
        "errors": errors,
    }
