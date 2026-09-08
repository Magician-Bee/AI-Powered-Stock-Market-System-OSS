from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SOURCE_TIERS = {
    "broker": 1,
    "authorized_realtime": 1,
    "twse_realtime": 1,
    "official_market_data": 2,
    "official_disclosure": 2,
    "official_announcements": 2,
    "official_financial_data": 2,
    "official_chip_data": 2,
    "auxiliary_market_data": 3,
    "market_price_history_news": 3,
    "auxiliary_news": 3,
    "news_events": 3,
    "research": 3,
}


def quality_summary(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())
        item_count = len(payload)
        empty = not bool(payload)
    elif isinstance(payload, list):
        keys = sorted({str(key) for item in payload if isinstance(item, dict) for key in item.keys()})
        item_count = len(payload)
        empty = len(payload) == 0
    else:
        keys = []
        item_count = 0 if payload is None else 1
        empty = payload is None
    return {
        "schema_version": "open_stock_ai.data_quality.v1",
        "empty": empty,
        "item_count": item_count,
        "keys": keys,
        "quality_status": "unavailable" if empty else "available",
    }


def source_envelope(
    *,
    source_key: str,
    source_name: str,
    symbol: str,
    market: str,
    role: str,
    payload: Any,
    loaded: bool | None = None,
    exchange_timestamp: str | None = None,
    published_at: str | None = None,
    available_at: str | None = None,
    received_at: str | None = None,
    is_realtime: bool = False,
    is_delayed: bool = False,
    is_fallback: bool = False,
    is_simulated: bool = False,
    revision: str | int | None = None,
) -> dict[str, Any]:
    quality = quality_summary(payload)
    received = _normalize_datetime(received_at) or datetime.now(timezone.utc)
    available = _normalize_datetime(available_at) or _normalize_datetime(published_at) or _normalize_datetime(exchange_timestamp)
    freshness_seconds = max(0.0, (received - available).total_seconds()) if available else None
    source_tier = SOURCE_TIERS.get(role)
    resolved_loaded = (not quality["empty"]) if loaded is None else loaded
    blockers: list[str] = []
    if not resolved_loaded:
        blockers.append("source_not_loaded")
    if quality["empty"]:
        blockers.append("empty_payload")
    if is_simulated:
        blockers.append("simulated_data")
    if is_fallback:
        blockers.append("fallback_data")
    if source_tier not in {1, 2}:
        blockers.append("non_primary_source")
    decision_eligible = not blockers

    return {
        # Preserve the v1 discriminator for existing API clients and reports.
        # The v2 extension fields below are additive and explicitly advertised.
        "schema_version": "open_stock_ai.data_source_envelope.v1",
        "extended_schema_version": "open_stock_ai.data_source_envelope.v2",
        "source_key": source_key,
        "source_name": source_name,
        "source_tier": source_tier,
        "symbol": symbol,
        "market": market,
        "role": role,
        "loaded": resolved_loaded,
        "exchange_timestamp": _iso(exchange_timestamp),
        "published_at": _iso(published_at),
        "available_at": _iso(available_at) or _iso(published_at) or _iso(exchange_timestamp),
        "received_at": received.isoformat(),
        "freshness_seconds": round(freshness_seconds, 3) if freshness_seconds is not None else None,
        "is_realtime": is_realtime,
        "is_delayed": is_delayed,
        "is_fallback": is_fallback,
        "is_simulated": is_simulated,
        "revision": revision,
        "decision_eligible": decision_eligible,
        "decision_blockers": blockers,
        "quality": quality,
        "payload": payload,
    }


def _normalize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: str | None) -> str | None:
    parsed = _normalize_datetime(value)
    return parsed.isoformat() if parsed else None
