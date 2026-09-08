from __future__ import annotations

"""Taiwan cash-settlement timing for the local paper ledger.

This module models the *accounting boundary* of T+2 delivery-versus-payment.
It deliberately does not claim to emulate a broker's intraday credit policy:
until a broker-specific, verified credit contract exists, sale receivables are
not spendable in the paper account.
"""

from datetime import date, datetime, time
from typing import Any

from stock_ai.market_calendar import TAIPEI_TZ, default_taiwan_market_calendar


SCHEMA_VERSION = "open_stock_ai.taiwan_paper_settlement.v1"
NORMAL_SETTLEMENT_DAYS = 2
SETTLEMENT_CUTOFF = time(11, 0)
OFFICIAL_SETTLEMENT_SOURCES = (
    "https://www.twse.com.tw/en/clearing/clearing/operations.html",
    "https://twse-regulation.twse.com.tw/ENG/EN/law/DAT0201.aspx?FLCODE=fl007304",
)


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("settlement_source_timestamp_required")
    if parsed.tzinfo is None:
        raise ValueError("settlement_timestamp_timezone_required")
    return parsed.astimezone(TAIPEI_TZ)


def settlement_timestamp(trade_at: Any) -> datetime:
    """Return the normal TWSE/TPEx T+2 settlement availability timestamp."""

    local_trade = parse_timestamp(trade_at)
    calendar = default_taiwan_market_calendar()
    day: date = local_trade.date()
    for _ in range(NORMAL_SETTLEMENT_DAYS):
        day = calendar.next_trading_day(day)
    return datetime.combine(day, SETTLEMENT_CUTOFF, tzinfo=TAIPEI_TZ)


def settlement_receipt(*, market: dict[str, Any]) -> dict[str, Any]:
    """Issue the immutable settlement contract carried by an exchange quote."""

    enforced = market.get("settlement_rules_enforced") is True
    timestamp = market.get("source_timestamp")
    if not enforced:
        return {
            "schema_version": SCHEMA_VERSION,
            "enforced": False,
            "reason": "legacy_or_non_exchange_paper_fill",
            "official_sources": list(OFFICIAL_SETTLEMENT_SOURCES),
        }
    trade_at = parse_timestamp(timestamp)
    due_at = settlement_timestamp(trade_at)
    calendar = default_taiwan_market_calendar()
    return {
        "schema_version": SCHEMA_VERSION,
        "enforced": True,
        "settlement_type": "normal_t_plus_2_dvp",
        "trade_at": trade_at.isoformat(),
        "settlement_due_at": due_at.isoformat(),
        "settlement_date": due_at.date().isoformat(),
        "currency": str(market.get("currency") or "TWD").upper(),
        "calendar_snapshot_sha256": calendar.snapshot_sha256,
        "calendar_source": calendar.source,
        "official_sources": list(OFFICIAL_SETTLEMENT_SOURCES),
        "credit_policy": "sale_receivable_not_spendable_before_settlement",
    }
