from __future__ import annotations

"""PIT-aware, read-only factor inputs for the whole-market scanner."""

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from stock_ai.data_platform.service import MarketDataPlatform, get_market_data_platform


_FACTOR_DOMAINS = {
    "fundamental": "financials",
    "valuation": "financials",
    "chip": "flows",
    "event": "events",
}


def attach_current_source_inputs(
    features: list[dict[str, Any]],
    *,
    revenues: list[Any],
    institutional_flows: list[Any],
) -> list[dict[str, Any]]:
    """Attach current official bulk factors without claiming PIT eligibility.

    The official OpenAPI exports are valuable for the operational whole-market
    view, but their export timestamp is not an original historical publication
    receipt.  They are therefore intentionally ``partial`` and never make a
    research replay certifiable.  A later persisted PIT revision supersedes
    the corresponding current input in :func:`enrich_features`.
    """

    revenue_by_symbol = {
        str(_item_value(item, "symbol") or "").upper(): item
        for item in revenues
        if _item_value(item, "symbol")
    }
    flow_by_symbol = {
        str(_item_value(item, "symbol") or "").upper(): item
        for item in institutional_flows
        if _item_value(item, "symbol")
    }
    enriched: list[dict[str, Any]] = []
    for feature in features:
        symbol = str(feature.get("symbol") or "").upper()
        inputs = dict(feature.get("scanner_factor_inputs") or {})
        revenue = revenue_by_symbol.get(symbol)
        flow = flow_by_symbol.get(symbol)
        if revenue is not None:
            value = _factor_value("fundamental", _item_dict(revenue))
            if value is not None:
                inputs["fundamental"] = _current_input(
                    value=value,
                    source=str(_item_value(revenue, "source") or "twse_openapi"),
                    observed_at=str(_item_value(revenue, "report_date") or "") or None,
                )
        if flow is not None:
            value = _factor_value("chip", _item_dict(flow))
            if value is not None:
                inputs["chip"] = _current_input(
                    value=value,
                    source=str(_item_value(flow, "source") or "twse_openapi"),
                    observed_at=str(_item_value(flow, "trade_date") or "") or None,
                )
        enriched.append({**feature, "scanner_factor_inputs": inputs})
    return enriched


def enrich_features(
    features: list[dict[str, Any]],
    *,
    platform: MarketDataPlatform | None = None,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Attach bounded, source-attributed warehouse factors to scanner rows.

    Each factor keeps the persisted availability decision and revision ID.  A
    missing, future, or uncontracted revision remains unavailable rather than
    becoming an implicit zero or an unverifiable historical feature.
    """

    if not features:
        return []
    service = platform or get_market_data_platform()
    cutoff = _iso_utc(as_of or datetime.now(timezone.utc).isoformat())
    rows_by_domain = {
        domain: _latest_rows_by_entity(
            service.standard_query(domain, knowledge_at=cutoff, effective_at=cutoff, limit=10_000),
            cutoff=cutoff,
        )
        for domain in sorted(set(_FACTOR_DOMAINS.values()))
    }
    enriched: list[dict[str, Any]] = []
    for feature in features:
        entity_id = str(feature.get("entity_id") or "")
        existing = dict(feature.get("scanner_factor_inputs") or {})
        warehouse_inputs = {
            name: _factor_from_rows(name, rows_by_domain[domain].get(entity_id, ()))
            for name, domain in _FACTOR_DOMAINS.items()
        }
        inputs = {
            name: value if value["status"] != "unavailable" else existing.get(name, value)
            for name, value in warehouse_inputs.items()
        }
        enriched.append({**feature, "scanner_factor_inputs": inputs})
    return enriched


def _latest_rows_by_entity(rows: list[dict[str, Any]], *, cutoff: str) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not _available_by(row, cutoff=cutoff):
            continue
        entity_id = str(row.get("entity_id") or "")
        if entity_id:
            grouped.setdefault(entity_id, []).append(row)
    return {
        entity_id: tuple(sorted(items, key=lambda item: (str(item.get("available_at") or ""), str(item.get("revision_id") or "")), reverse=True))
        for entity_id, items in grouped.items()
    }


def _factor_from_rows(name: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    for row in rows:
        value = _factor_value(name, dict(row.get("record") or {}))
        if value is None:
            continue
        decision = dict((row.get("availability_contract_snapshot") or {}).get("decision") or {})
        pit = decision.get("historical_pit_eligible") is True
        return {
            "value": value,
            "status": "ready" if pit else "partial",
            "source": str(row.get("source_id") or "warehouse"),
            "revision_id": str(row.get("revision_id") or "") or None,
            "available_at": str(row.get("available_at") or "") or None,
            "historical_pit_eligible": pit,
            "reason": None if pit else str(decision.get("reason") or "historical_pit_not_certified"),
        }
    return {
        "value": None,
        "status": "unavailable",
        "source": "not_provided",
        "revision_id": None,
        "available_at": None,
        "historical_pit_eligible": False,
        "reason": "no_available_warehouse_factor",
    }


def _factor_value(name: str, payload: dict[str, Any]) -> float | None:
    if name == "fundamental":
        return _scale(_number(payload, "revenue_yoy", "yoy_change_percent", "growth_yoy"), lower=-20, upper=40)
    if name == "valuation":
        pe = _number(payload, "pe", "price_earnings_ratio")
        return _scale(pe, lower=5, upper=40, invert=True) if pe is not None else _scale(_number(payload, "dividend_yield", "dividend_yield_percent"), lower=0, upper=8)
    if name == "chip":
        value = _number(payload, "foreign_net", "foreign_net_buy_sell", "net_buy_sell", "net_flow")
        return None if value is None else 75.0 if value > 0 else 25.0 if value < 0 else 50.0
    if name == "event":
        sentiment = str(payload.get("sentiment") or payload.get("event_sentiment") or "").casefold()
        return {"positive": 75.0, "bullish": 75.0, "negative": 25.0, "bearish": 25.0, "neutral": 50.0, "mixed": 50.0}.get(sentiment)
    return None


def _available_by(row: dict[str, Any], *, cutoff: str) -> bool:
    available_at = str(row.get("available_at") or "")
    if not available_at or _iso_utc(available_at) > cutoff:
        return False
    snapshot = dict(row.get("availability_contract_snapshot") or {})
    contract = dict(snapshot.get("contract") or {})
    decision = dict(snapshot.get("decision") or {})
    return contract.get("production_contract_covered") is True and decision.get("available_at") == available_at


def _iso_utc(value: str) -> str:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc).isoformat()


def _number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if isfinite(value):
            return value
    return None


def _scale(value: float | None, *, lower: float, upper: float, invert: bool = False) -> float | None:
    if value is None:
        return None
    score = max(0.0, min(100.0, (value - lower) / (upper - lower) * 100))
    return round(100 - score if invert else score, 2)


def _current_input(*, value: float, source: str, observed_at: str | None) -> dict[str, Any]:
    return {
        "value": value,
        "status": "partial",
        "source": source,
        "revision_id": None,
        "available_at": observed_at,
        "historical_pit_eligible": False,
        "reason": "current_source_export_not_historical_pit_certified",
    }


def _item_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    return dict(dump(mode="json") if callable(dump) else vars(item))


def _item_value(item: Any, name: str) -> Any:
    return _item_dict(item).get(name)
