from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


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
    trading_state: str | None = None,
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
    if trading_state is not None:
        payload["trading_state"] = str(trading_state).casefold()
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
    if "trading_state" in envelope:
        signed_payload["trading_state"] = envelope["trading_state"]
    if envelope.get("signature") != _signature(signed_payload):
        blockers.append("source_envelope_signature_mismatch")

    # Reception records transport latency, not when this trade/close occurred.
    exchange_at = parse_exchange_timestamp(envelope.get("exchange_timestamp"), envelope.get("provider_id"))
    age_seconds = _age_seconds(exchange_at, observed_now)
    max_age = max(1, int(envelope.get("max_age_seconds") or 1))
    if age_seconds is None:
        blockers.append("quote_timestamp_invalid")
    elif age_seconds < -5:
        blockers.append("quote_timestamp_in_future")
    elif age_seconds > max_age:
        blockers.append("quote_expired")

    is_last_trade = envelope.get("quote_kind") == "last_trade"
    if is_last_trade and not _has_clock(envelope.get("exchange_timestamp")):
        blockers.append("last_trade_requires_exchange_time")
    trading_state = str(envelope.get("trading_state") or "unknown").casefold()
    inactive_market = trading_state not in {"unknown", "", "trading", "closing_auction", "delayed_close", "active", "open"}
    if is_last_trade and inactive_market:
        blockers.append("last_trade_market_not_open")
    realtime_last_trade = bool(
        is_last_trade
        and envelope.get("realtime") is True
        and envelope.get("delayed") is not True
        and not inactive_market
    )
    official_close = envelope.get("official_close") is True and envelope.get("quote_kind") == "official_close"
    if (
        is_last_trade and envelope.get("provider_id") not in {"twse_mis", "licensed_realtime"}
        or official_close and envelope.get("provider_id") not in {"twse_openapi", "tpex_openapi"}
    ):
        blockers.append("provider_quote_kind_not_execution_capable")

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
    current_last_trade = realtime_last_trade and not any(
        reason in blockers for reason in (
            "quote_timestamp_invalid", "quote_timestamp_in_future", "quote_expired",
            "last_trade_requires_exchange_time", "provider_quote_kind_not_execution_capable",
        )
    )
    return {
        "schema_version": "open_stock_ai.execution_eligibility.v1",
        "horizon": normalized_horizon,
        "execution_eligible": execution_eligible,
        "research_eligible": provider is not None and not any(
            reason in blockers for reason in ("source_envelope_signature_mismatch", "connector_identity_mismatch")
        ),
        "age_seconds": age_seconds,
        "freshness_basis": "exchange_timestamp",
        "exchange_timestamp": exchange_at.isoformat() if exchange_at is not None else None,
        "received_at": envelope.get("received_at"),
        "trading_state": trading_state,
        "current_last_trade": current_last_trade,
        "max_age_seconds": max_age,
        "blockers": list(dict.fromkeys(blockers)),
        "policy": {
            "intraday": "authorized fresh realtime last trade",
            "swing": "fresh realtime last trade or official close",
            "after_market": "fresh official close or realtime last trade",
            "research": "known signed source; delayed and Yahoo allowed but never executable",
        },
    }


def plan_quote_eligibility(
    envelope: dict[str, Any], *, mode: str = "live",
    paper_execution_model: str | None = None, quantity_shares: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Admit quotes for a Host-configured execution backend, not a fill.

    Public MIS is permitted only for explicit bounded board/odd-lot paper
    scenarios. The broker still checks source trade identity/size, the market
    session, participation, limits and costs. This never grants live data
    authorization or makes simulated executions qualification evidence.
    """
    strict = execution_eligibility(envelope, horizon="intraday", now=now)
    integer_quantity = isinstance(quantity_shares, int) and not isinstance(quantity_shares, bool)
    odd = bool(integer_quantity and 1 <= quantity_shares <= 999 and paper_execution_model in {
        "bounded_board_trade_odd_lot_proxy", "bounded_public_quote_paper"})
    board = bool(integer_quantity and quantity_shares >= 1000 and quantity_shares % 1000 == 0
                 and paper_execution_model in {"bounded_board_trade_paper", "bounded_public_quote_paper"})
    if mode != "paper" or not (odd or board):
        return strict
    quote = execution_eligibility(envelope, horizon="swing", now=now)
    blockers = list(quote["blockers"])
    if not quote["current_last_trade"]:
        blockers.append("paper_proxy_requires_fresh_current_last_trade")
    if envelope.get("provider_id") not in {"twse_mis", "licensed_realtime"}:
        blockers.append("paper_proxy_requires_board_trade_provider")
    return {
        **quote, "horizon": "intraday", "execution_eligible": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
        "paper_execution_model": paper_execution_model, "paper_simulation_only": True,
        "execution_evidence_eligible": False,
        "live_execution_eligible": strict["execution_eligible"],
        "live_execution_blockers": strict["blockers"],
    }


def _signature(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_exchange_timestamp(value: Any, provider_id: Any) -> datetime | None:
    """Interpret exchange-local Taiwan timestamps without relabeling them UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        if str(provider_id) not in {"twse_mis", "twse_openapi", "tpex_openapi"}:
            return None
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
    return parsed


def _has_clock(value: Any) -> bool:
    return ":" in str(value or "")


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return (now.astimezone(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()
