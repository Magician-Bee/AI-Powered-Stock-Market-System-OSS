from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


KNOWN_PROVIDERS = {
    "twse_mis": {"connector_id": "stock_ai.realtime_quotes.fetch_twse_mis_quote"},
    "twse_openapi": {"connector_id": "stock_ai.taiwan_official.official_summary_payload"},
    "tpex_openapi": {"connector_id": "stock_ai.taiwan_official.official_summary_payload"},
    "licensed_realtime": {"connector_id": "stock_ai.realtime_quotes.fetch_licensed_quote"},
    "yahoo": {"connector_id": "stock_ai.yahoo_data.fetch_yahoo_summary"},
}


def quote_envelope(
    *,
    provider_id: str,
    connector_id: str,
    quote_kind: str,
    exchange_timestamp: str | None,
    received_at: str | None,
    max_age_seconds: int,
    authorized: bool,
    realtime: bool,
    delayed: bool,
    official_close: bool,
) -> dict[str, Any]:
    payload = {
        "provider_id": str(provider_id),
        "connector_id": str(connector_id),
        "quote_kind": str(quote_kind),
        "exchange_timestamp": exchange_timestamp,
        "received_at": received_at,
        "max_age_seconds": max(1, int(max_age_seconds)),
        "authorized": bool(authorized),
        "realtime": bool(realtime),
        "delayed": bool(delayed),
        "official_close": bool(official_close),
    }
    return {
        "schema_version": "open_stock_ai.source_envelope.v3",
        **payload,
        "signature": _signature(payload),
    }


def execution_eligibility(
    envelope: dict[str, Any],
    *,
    horizon: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_now = now or datetime.now(timezone.utc)
    normalized_horizon = str(horizon or "research").casefold()
    provider = KNOWN_PROVIDERS.get(str(envelope.get("provider_id") or ""))
    blockers: list[str] = []
    if provider is None:
        blockers.append("unknown_provider_id")
    elif provider["connector_id"] != envelope.get("connector_id"):
        blockers.append("connector_identity_mismatch")
    signed_payload = {
        key: envelope.get(key)
        for key in (
            "provider_id", "connector_id", "quote_kind", "exchange_timestamp", "received_at",
            "max_age_seconds", "authorized", "realtime", "delayed", "official_close",
        )
    }
    if envelope.get("signature") != _signature(signed_payload):
        blockers.append("source_envelope_signature_mismatch")

    freshness_value = envelope.get("exchange_timestamp") if envelope.get("official_close") is True else envelope.get("received_at")
    age_seconds = _age_seconds(freshness_value, observed_now)
    max_age = max(1, int(envelope.get("max_age_seconds") or 1))
    if age_seconds is None:
        blockers.append("quote_timestamp_invalid")
    elif age_seconds > max_age:
        blockers.append("quote_expired")

    is_last_trade = envelope.get("quote_kind") == "last_trade"
    realtime_last_trade = bool(
        is_last_trade
        and envelope.get("realtime") is True
        and envelope.get("delayed") is not True
    )
    official_close = envelope.get("official_close") is True and envelope.get("quote_kind") == "official_close"

    if normalized_horizon == "intraday":
        if envelope.get("authorized") is not True:
            blockers.append("intraday_requires_authorized_realtime")
        if not realtime_last_trade:
            blockers.append("intraday_requires_realtime_last_trade")
    elif normalized_horizon in {"swing", "weekly", "monthly", "after_market", "after-market"}:
        if not (realtime_last_trade or official_close):
            blockers.append("horizon_requires_realtime_trade_or_official_close")
    elif normalized_horizon == "research":
        pass
    else:
        blockers.append("unsupported_execution_horizon")

    execution_eligible = normalized_horizon != "research" and not blockers
    return {
        "schema_version": "open_stock_ai.execution_eligibility.v1",
        "horizon": normalized_horizon,
        "execution_eligible": execution_eligible,
        "research_eligible": provider is not None and "source_envelope_signature_mismatch" not in blockers,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "blockers": list(dict.fromkeys(blockers)),
        "policy": {
            "intraday": "authorized fresh realtime last trade",
            "swing": "fresh realtime last trade or official close",
            "after_market": "fresh official close or realtime last trade",
            "research": "known signed source; delayed and Yahoo allowed but never executable",
        },
    }


def _signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _age_seconds(value: Any, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
