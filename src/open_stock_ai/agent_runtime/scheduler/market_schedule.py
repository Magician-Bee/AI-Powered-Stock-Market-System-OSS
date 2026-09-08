from __future__ import annotations

"""Market-calendar-aware schedule helpers.

The scheduler remains provider agnostic: a product supplies a calendar object
with ``day_status`` and ``next_trading_day`` methods.  This keeps the generic
Agent runtime reusable while allowing the Stock AI Host to inject the checked-
in Taiwan market calendar.
"""

from datetime import date, datetime, timezone
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
SUPPORTED_MARKET_CALENDARS = frozenset({"taiwan", "twse", "taiwan_equities"})


class MarketCalendar(Protocol):
    def day_status(self, day: date) -> Mapping[str, Any]: ...

    def next_trading_day(self, day: date) -> date: ...


def normalize_market_calendar(value: Any) -> str | None:
    """Return the stable wire name for a supported market calendar."""

    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().casefold()
    if normalized not in SUPPORTED_MARKET_CALENDARS:
        raise ValueError("market_calendar must be taiwan")
    return "taiwan"


def market_calendar_name(trigger: Mapping[str, Any]) -> str | None:
    return normalize_market_calendar(trigger.get("market_calendar"))


def is_market_closed(moment: datetime, trigger: Mapping[str, Any], calendar: MarketCalendar | None) -> bool:
    """Return whether a market-calendar schedule must defer at ``moment``."""

    if market_calendar_name(trigger) is None or calendar is None:
        return False
    local_day = moment.astimezone(TAIPEI_TZ).date()
    return not bool(calendar.day_status(local_day).get("trading_day"))


def align_market_due(
    candidate: datetime,
    trigger: Mapping[str, Any],
    calendar: MarketCalendar | None,
) -> datetime:
    """Move a due timestamp to the next trading day at the same local time."""

    normalized = candidate if candidate.tzinfo else candidate.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    if market_calendar_name(trigger) is None or calendar is None:
        return normalized
    local = normalized.astimezone(TAIPEI_TZ)
    while not bool(calendar.day_status(local.date()).get("trading_day")):
        local = datetime.combine(calendar.next_trading_day(local.date()), local.timetz(), tzinfo=TAIPEI_TZ)
    return local.astimezone(timezone.utc)


def next_market_cron(
    expression: str,
    after: datetime,
    next_cron: Any,
    trigger: Mapping[str, Any],
    calendar: MarketCalendar | None,
) -> datetime:
    """Calculate a cron occurrence, skipping closed market dates."""

    candidate = next_cron(expression, after)
    if market_calendar_name(trigger) is None or calendar is None:
        return candidate
    while is_market_closed(candidate, trigger, calendar):
        candidate = next_cron(expression, candidate)
    return candidate
