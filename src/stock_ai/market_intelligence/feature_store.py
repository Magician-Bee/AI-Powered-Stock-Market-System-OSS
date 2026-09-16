from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from stock_ai.phase1_data import (
    _float,
    _int,
    list_securities_master,
    list_institutional_flows,
    list_monthly_revenues,
    roc_to_iso,
    tpex_quotes,
    twse_quotes,
)

from .scanner_factor_enrichment import attach_current_source_inputs


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = _float(value)
    except (TypeError, ValueError):
        return None
    return float(parsed) if isfinite(float(parsed)) else None


def _quality(
    *,
    close: float | None,
    volume: int,
    data_as_of: str | None,
    source: str,
) -> dict[str, Any]:
    # A single official bulk quote remains useful operational input, but it is
    # not enough to certify a market decision as ready for a new position.  A
    # second, independent same-day observation must be attached by the market
    # data pipeline before that stronger state is used.  Keeping this explicit
    # prevents the whole-market screen from presenting a one-source daily
    # ranking as an immediately executable recommendation.
    source_observation = {
        "status": "not_observed",
        "reason": "independent_bulk_quote_observation_not_connected",
    }
    missing = []
    if close is None or close <= 0:
        missing.append("latest_price")
    if volume <= 0:
        missing.append("volume")
    if not data_as_of:
        missing.append("data_as_of")
    if not missing:
        status, score = "partial", 0.75
    elif close is not None and close > 0:
        status, score = "partial", 0.65
    else:
        status, score = "insufficient", 0.15
    return {
        "status": status,
        "score": score,
        "source": source,
        "data_as_of": data_as_of,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        "fallback": False,
        "missing_fields": missing,
        "quality_flags": (
            ["cross_source_observation_not_available"]
            if not missing
            else [
                "cross_source_observation_not_available",
                *[f"missing:{field}" for field in missing],
            ]
        ),
        "source_observation": source_observation,
    }


def _feature(
    *,
    symbol: str,
    entity_id: str | None = None,
    name: str,
    exchange: str,
    industry: str | None,
    is_etf: bool,
    is_warrant: bool,
    is_managed_stock: bool = False,
    is_special_security: bool = False,
    product_classification: dict[str, Any] | None = None,
    lifecycle_status: str | None = None,
    source: str,
    raw_date: Any,
    open_price: Any,
    high: Any,
    low: Any,
    close: Any,
    change: Any,
    volume: Any,
    trade_value: Any,
    transactions: Any,
    source_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    close_value = None if source_error else _safe_float(close)
    change_value = _safe_float(change) or 0.0
    open_value = None if source_error else _safe_float(open_price)
    high_value = None if source_error else _safe_float(high)
    low_value = None if source_error else _safe_float(low)
    volume_value = max(0, _int(volume))
    previous_close = (
        close_value - change_value if close_value is not None else None
    )
    change_percent = (
        change_value / previous_close * 100
        if previous_close not in (None, 0)
        else None
    )
    price_range = (
        high_value - low_value
        if high_value is not None and low_value is not None
        else None
    )
    range_position = (
        min(1.0, max(0.0, (close_value - low_value) / price_range))
        if close_value is not None
        and low_value is not None
        and price_range is not None
        and price_range > 0
        else 0.5
    )
    raw_date_text = str(raw_date or "").strip()
    data_as_of = (
        roc_to_iso(raw_date_text)
        if raw_date_text and raw_date_text.isdigit()
        else raw_date_text or None
    )
    quality = _quality(
        close=close_value,
        volume=volume_value,
        data_as_of=data_as_of,
        source=source,
    )
    if source_error:
        quality["source_observation"]["primary_source_error"] = dict(source_error)
        quality["quality_flags"].append("primary_bulk_quote_load_failed")
    return {
        "symbol": symbol,
        "entity_id": entity_id or symbol,
        "name": name,
        "exchange": exchange,
        "industry": industry,
        "is_etf": is_etf,
        "is_warrant": is_warrant,
        "is_managed_stock": is_managed_stock,
        "is_special_security": is_special_security,
        "product_classification": dict(product_classification or {}),
        "lifecycle_status": lifecycle_status,
        "source": source,
        "data_as_of": data_as_of,
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "previous_close": previous_close,
        "change": change_value,
        "change_percent": round(change_percent, 4) if change_percent is not None else None,
        "volume": volume_value,
        "trade_value": max(0.0, _safe_float(trade_value) or 0.0),
        "transactions": max(0, _int(transactions)),
        "range_position": range_position,
        "data_quality": quality,
        "evidence": [{
            "evidence_id": f"quote_source_failure:{symbol}:{quality['acquired_at']}",
            "source": source,
            "statement": (
                "官方批次報價載入失敗；未取得本次行情，價格與行情日期維持缺漏。"
                f" {source_error['type']}: {source_error['message']}"
            ),
            "observed_at": None,
            "acquired_at": quality["acquired_at"],
            "quality": "invalid",
            "fallback": False,
        }] if source_error else [
            {
                "evidence_id": f"quote:{symbol}:{data_as_of or 'unknown'}",
                "source": source,
                "statement": "官方每日報價提供開高低收、漲跌、成交量與成交金額。",
                "observed_at": data_as_of,
                "acquired_at": quality["acquired_at"],
                "quality": "valid" if quality["status"] == "ready" else "partial",
                "fallback": False,
            }
        ],
    }


def load_all_taiwan_features() -> list[dict[str, Any]]:
    """Load deterministic research rows from a bounded current master batch.

    This layer intentionally does not call an LLM and does not issue one remote
    request per symbol. TWSE and TPEx official bulk quote datasets are joined to
    the official security master in memory. The 100,000-row read limit and
    unresolved listing identities leave coverage gaps; this batch is not a
    complete market inventory or proof of per-symbol research completeness.
    """

    securities = list_securities_master(
        market="taiwan",
        limit=100000,
        include_lifecycle=True,
    )
    master: dict[str, list[Any]] = {}
    for item in securities:
        if item.trading_status in {"expired", "delisted"} or item.lifecycle_status in {"expired", "delisted"}:
            continue
        symbol = item.symbol.strip().upper()
        if symbol:
            master.setdefault(symbol, []).append(item)
    quote_rows: dict[str, tuple[str, dict[str, Any]]] = {}
    source_errors: dict[str, dict[str, Any]] = {}
    for venue, source, suffix, code_key, loader in (
        ("TWSE", "TWSE_ALL_QUOTES", "TW", "Code", twse_quotes),
        ("TPEx", "TPEX_DAILY_QUOTES", "TWO", "SecuritiesCompanyCode", tpex_quotes),
    ):
        try:
            # Publish a venue only after its existing bulk loader finishes.
            # A truncated iteration must not make a partial payload usable.
            venue_rows = {}
            for row in loader():
                code = str(row.get(code_key) or "").strip()
                if code:
                    venue_rows[f"{code}.{suffix}"] = (source, row)
            quote_rows.update(venue_rows)
        except Exception as exc:
            source_errors[venue.upper()] = {
                "status": "failed", "source": source, "venue": venue,
                "type": type(exc).__name__, "message": str(exc)[:512],
            }
    if len(source_errors) == 2:
        raise RuntimeError("official_bulk_quotes_unavailable: " + "; ".join(
            f"{error['source']}: {error['type']}: {error['message']}"
            for error in source_errors.values()
        ))

    # These are two official bulk payloads, not one call per security.  Their
    # precise historical availability is not attested by the current OpenAPI,
    # so the attached factor receipts remain operational-only / partial.
    try:
        revenues = list_monthly_revenues(limit=5_000, allow_network=False)
    except Exception:
        revenues = []
    try:
        institutional_flows = list_institutional_flows(limit=5_000, allow_network=False)
    except Exception:
        institutional_flows = []

    features: list[dict[str, Any]] = []
    for symbol, items in sorted(master.items()):
        # The downstream snapshot is keyed by symbol. Never silently pick an
        # issuance when several current or future entities share that label.
        candidates = sorted({
            (item.entity_id or "", item.exchange, item.trading_status, item.listing_identifier_status)
            for item in items
        })
        ambiguous = len(candidates) != 1
        item = items[0]
        lifecycle = "unknown" if ambiguous else (
            "pre_listing" if item.trading_status == "listed_pending_quote" else item.trading_status)
        quote_binding_reason = (
            "ambiguous_research_listing_identity" if ambiguous
            else "research_listing_identity_unverified" if not item.entity_id or item.listing_identifier_status == "unavailable_or_ambiguous"
            else "research_listing_not_active" if lifecycle != "active"
            else "research_listing_quote_venue_unsupported" if item.exchange not in {"TWSE", "TPEx"}
            else None
        )
        if quote_binding_reason == "research_listing_identity_unverified":
            lifecycle = "unknown"
        source_error = source_errors.get(item.exchange.upper())
        source, row = ("OFFICIAL_SECURITY_MASTER", {}) if quote_binding_reason else quote_rows.get(symbol, (
            source_error["source"] if source_error else "OFFICIAL_SECURITY_MASTER", {}
        ))
        if item.exchange.upper() == "TWSE":
            values = {
                "raw_date": row.get("Date"),
                "open_price": row.get("OpeningPrice"),
                "high": row.get("HighestPrice"),
                "low": row.get("LowestPrice"),
                "close": row.get("ClosingPrice"),
                "change": row.get("Change"),
                "volume": row.get("TradeVolume"),
                "trade_value": row.get("TradeValue"),
                "transactions": row.get("Transaction"),
            }
        else:
            values = {
                "raw_date": row.get("Date"),
                "open_price": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "change": row.get("Change"),
                "volume": row.get("TradingShares"),
                "trade_value": row.get("TransactionAmount"),
                "transactions": row.get("TransactionNumber"),
            }
        feature = _feature(
                symbol=symbol,
                entity_id=item.entity_id,
                name=f"{symbol}（名錄識別待釐清）" if ambiguous else item.name,
                exchange=item.exchange if len({candidate[1] for candidate in candidates}) == 1 else "unknown",
                industry=None if ambiguous else item.industry,
                is_etf=False if ambiguous else item.is_etf,
                is_warrant=False if ambiguous else item.is_warrant,
                is_managed_stock=getattr(item, "entity_type", "stock") in {"managed_stock", "fund"},
                is_special_security=getattr(item, "entity_type", "stock") not in {"stock", "etf", "warrant"},
                product_classification=item.product_classification,
                lifecycle_status=lifecycle,
                source=source,
                source_error=source_error,
                **values,
            )
        if not item.entity_id:
            feature["entity_id"] = None
        if ambiguous:
            feature["entity_id"] = None
            feature["product_classification"] = {
                "schema_version": "stock_ai.product_classification.v1", "status": "conflict",
                "product_type": "unknown", "symbol": symbol,
                "reasons": [quote_binding_reason],
            }
        if quote_binding_reason:
            observation = {"status": "not_observed", "reason": quote_binding_reason,
                "candidate_count": len(candidates), "candidates": [
                    {"entity_id": entity or None, "exchange": venue, "trading_status": status,
                     "listing_identifier_status": identifier_status}
                    for entity, venue, status, identifier_status in candidates],
                "master_read_limit": 100000,
                "master_read_limit_reached": len(securities) >= 100000}
            feature["data_quality"]["source_observation"]["primary_quote_binding"] = observation
            feature["data_quality"]["quality_flags"].append(quote_binding_reason)
            feature["evidence"] = [{
                "evidence_id": f"research_listing:{symbol}:{feature['data_quality']['acquired_at']}",
                "source": "OFFICIAL_SECURITY_MASTER",
                "statement": f"名錄保留研究用途；未綁定本次行情。{quote_binding_reason}；候選實體："
                             + ", ".join(entity or "unknown" for entity, *_ in candidates),
                "observed_at": None, "acquired_at": feature["data_quality"]["acquired_at"],
                "quality": "invalid", "fallback": False,
            }]
        features.append(feature)
    # Symbol-only current factors have the same reuse risk as quotes. Missing
    # lifecycle or listing identity must not attach an older issuer's inputs.
    observed_features = [feature for feature in features if not (
        feature["data_quality"]["source_observation"].get("primary_quote_binding"))]
    enriched = attach_current_source_inputs(
        observed_features,
        revenues=revenues,
        institutional_flows=institutional_flows,
    )
    by_symbol = {feature["symbol"]: feature for feature in enriched}
    return [by_symbol.get(feature["symbol"], feature) for feature in features]
