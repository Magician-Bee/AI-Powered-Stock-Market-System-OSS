"""Source-bound daily paper NAV observation; never submits or matches orders."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import math
import time as clock
from typing import Any
from zoneinfo import ZoneInfo

from open_stock_ai.data.execution_quote import execution_eligibility, parse_exchange_timestamp
from .trading_plan import utc_time


TAIPEI = ZoneInfo("Asia/Taipei")
SCHEMA = "open_stock_ai.forward_daily_mark.v1"
KIND = "forward_daily_mark"
FIXTURE = "fixture_only_not_market_or_ev_proof"


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("finite_positive_number_required")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("finite_positive_number_required")
    return number


def _positions(snapshot: dict, account_id: str) -> dict[str, float]:
    if snapshot.get("account_id") != account_id or not isinstance(snapshot.get("positions"), list):
        raise ValueError("daily_mark_account_snapshot_identity_invalid")
    result = {}
    for row in snapshot["positions"]:
        symbol = str(row.get("symbol") or "")
        if not symbol or symbol in result:
            raise ValueError("daily_mark_position_identity_invalid")
        result[symbol] = _positive(row.get("quantity"))
    cash = float(snapshot.get("cash_balance", float("nan")))
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("daily_mark_cash_invalid")
    return result


def _quote_evidence(market: dict, *, symbol: str, quantity: float, day, now: datetime) -> dict:
    envelope = dict(market.get("source_envelope") or {})
    gate = execution_eligibility(envelope, horizon="swing", now=now)
    provider = envelope.get("provider_id")
    source_at = parse_exchange_timestamp(envelope.get("exchange_timestamp"), provider)
    available_at = parse_exchange_timestamp(envelope.get("received_at"), provider)
    market_at = parse_exchange_timestamp(market.get("source_timestamp"), provider)
    if not gate["research_eligible"] or not source_at or source_at != market_at or market.get("symbol") != symbol:
        raise ValueError("daily_mark_source_identity_or_integrity_invalid")
    if source_at.astimezone(TAIPEI).date() != day:
        raise ValueError("daily_mark_requires_same_trading_date")
    if (source_at > now or not available_at or available_at > now+timedelta(seconds=5)
            or available_at < source_at-timedelta(seconds=5)):
        raise ValueError("daily_mark_source_time_not_available")
    official = (provider in {"twse_openapi", "tpex_openapi"}
                and envelope.get("quote_kind") == "official_close" and envelope.get("official_close") is True)
    fresh_trade = bool(gate["execution_eligible"] and gate["current_last_trade"])
    if not (official or fresh_trade):
        raise ValueError("daily_mark_requires_same_day_official_close_or_fresh_trade")
    return {"symbol": symbol, "quantity": quantity, "price": _positive(market.get("price")),
            "exchange_timestamp": source_at.isoformat(), "available_at": available_at.isoformat(),
            "source_provenance_verified": True, FIXTURE: market.get(FIXTURE) is True,
            "source_kind": "licensed_vendor" if provider == "licensed_realtime" else "exchange_official",
            "source_envelope": envelope,
            "valuation_basis": "same_day_official_close" if official else "fresh_current_last_trade"}


def _cash_market_evidence(campaign, *, day, now: datetime) -> dict:
    cycle_id = campaign.status().get("latest_cycle_id")
    if not cycle_id:
        raise ValueError("daily_mark_same_day_market_cycle_unavailable")
    cycle = campaign.cycle(cycle_id)
    created = utc_time(cycle["created_at"])
    if (cycle.get("account_id") != campaign.broker.account_id or created > now
            or created.astimezone(TAIPEI).date() != day
            or created.astimezone(TAIPEI).time().replace(tzinfo=None) < time(14, 30)):
        raise ValueError("daily_mark_same_day_postclose_cycle_required")
    evidence_id = str(cycle.get("bulk_evidence_id") or "")
    payload = campaign._evidence(evidence_id, "market_screen")
    for feature in payload.get("features", []):
        quality = feature.get("data_quality") or {}
        if (feature.get("source") in {"TWSE_ALL_QUOTES", "TPEX_DAILY_QUOTES"}
                and str(feature.get("data_as_of")) == day.isoformat()
                and quality.get("fallback") is not True and feature.get("fallback") is not True):
            try:
                price = _positive(feature.get("close"))
            except (ValueError, TypeError, OverflowError):
                continue
            if not str(feature.get("symbol") or ""):
                continue
            return {"cycle_id": cycle_id, "evidence_id": evidence_id,
                    "symbol": feature["symbol"], "price": price,
                    "data_as_of": day.isoformat(), "source": feature["source"],
                    "source_provenance_verified": True, "source_kind": "exchange_official",
                    FIXTURE: feature.get(FIXTURE) is True or payload.get(FIXTURE) is True or cycle.get(FIXTURE) is True}
    raise ValueError("daily_mark_same_day_official_market_evidence_unavailable")


async def retain_forward_daily_mark(campaign, *, scheduled_at: str | datetime,
                                   now: datetime | None = None) -> dict[str, Any]:
    """Mark at the 14:35 Taipei slot and retain an account-bound source receipt.

    Returns ``ready`` with evidence_ids/snapshot, or ``retry`` with precise
    blockers and no qualifying snapshot. Late attempts have a four-hour limit.
    All quotes are checked before any mark. The existing account lease excludes
    plan submissions during marks, and the account is verified again afterward.
    A failed partial mark can be retried; it never becomes a daily observation.
    """
    instant = utc_time(now or datetime.now(timezone.utc))
    started = clock.monotonic()

    def observed() -> datetime:
        return instant+timedelta(seconds=clock.monotonic()-started)

    account_id = campaign.broker.account_id
    result = {"schema_version": SCHEMA, "account_id": account_id, "status": "retry",
              "evidence_ids": [], "snapshot": None, "blockers": [], "model_calls": 0}
    try:
        slot = utc_time(scheduled_at)
        local = slot.astimezone(TAIPEI)
        if (campaign.broker.mode != "paper" or local.time().replace(tzinfo=None) != time(14, 35)
                or not campaign.calendar.day_status(local.date())["trading_day"]):
            return {**result, "status": "rejected", "blockers": ["daily_mark_paper_trading_day_1435_slot_required"]}
        if instant < slot:
            return {**result, "blockers": ["daily_mark_observation_not_due"]}
        if instant > slot+timedelta(hours=4):
            return {**result, "status": "rejected", "blockers": ["daily_mark_observation_window_expired"]}
        before = await campaign.broker.account(now=instant)
        positions = _positions(before, account_id)
        markets: dict[str, dict] = {}
        quotes = []
        semaphore = asyncio.Semaphore(4)

        async def load(symbol: str) -> None:
            async with semaphore:
                markets[symbol] = await campaign.quote_loader(symbol)

        await asyncio.wait_for(asyncio.gather(*(load(symbol) for symbol in positions)), timeout=40)
        for symbol, quantity in sorted(positions.items()):
            try:
                quotes.append(_quote_evidence(markets[symbol], symbol=symbol, quantity=quantity,
                                              day=local.date(), now=observed()))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{symbol}:{exc}") from exc
        market_observation = None if positions else _cash_market_evidence(campaign, day=local.date(), now=observed())
        with campaign.plans.account_lease(account_id, now=observed(), seconds=120) as owner:
            if owner is None:
                return {**result, "blockers": ["daily_mark_account_busy"]}
            current = await campaign.broker.account(now=observed())
            if _positions(current, account_id) != positions or current["cash_balance"] != before["cash_balance"]:
                return {**result, "blockers": ["daily_mark_account_changed_during_source_fetch"]}

            async def mark_all() -> dict:
                for symbol in sorted(markets):
                    await campaign.broker.mark(markets[symbol])
                return await campaign.broker.account(now=observed())

            snapshot = await asyncio.wait_for(mark_all(), timeout=45)
            collected_at = observed()
            if collected_at > slot+timedelta(hours=4):
                raise ValueError("daily_mark_observation_window_expired")
            for symbol, quantity in sorted(positions.items()):
                _quote_evidence(markets[symbol], symbol=symbol, quantity=quantity, day=local.date(), now=collected_at)
            if _positions(snapshot, account_id) != positions or snapshot["cash_balance"] != before["cash_balance"]:
                raise ValueError("daily_mark_account_changed_during_mark")
            by_symbol = {row["symbol"]: row for row in snapshot["positions"]}
            for row in quotes:
                actual = _positive(by_symbol[row["symbol"]].get("last_price"))
                if not math.isclose(actual, row["price"], rel_tol=0, abs_tol=0.000001):
                    raise ValueError("daily_mark_position_price_does_not_match_source")
            expected_nav = snapshot["cash_balance"]+sum(row["price"]*row["quantity"] for row in quotes)
            if not math.isclose(float(snapshot.get("total_equity", float("nan"))), expected_nav, rel_tol=0, abs_tol=0.02):
                raise ValueError("daily_mark_nav_does_not_match_source_prices")
            source_kinds = {row["source_kind"] for row in quotes} if quotes else {market_observation["source_kind"]}
            payload = {"schema_version": SCHEMA, "account_id": account_id,
                       "scheduled_at": slot.isoformat(), "observed_at": collected_at.isoformat(),
                       "trading_date": local.date().isoformat(), "quotes": quotes, "snapshot": snapshot,
                       "market_observation": market_observation,
                       "market_evidence_ids": [market_observation["evidence_id"]] if market_observation else [],
                       "source_provenance_verified": True,
                       "source_kind": next(iter(source_kinds)) if len(source_kinds) == 1 else "mixed_verified_market",
                       FIXTURE: any(row[FIXTURE] for row in quotes) or bool(market_observation and market_observation[FIXTURE]),
                       "execution_evidence_eligible": False}
            evidence_id = campaign._retain(KIND, payload)
            return {**result, "status": "ready", "evidence_ids": [evidence_id], "snapshot": snapshot,
                    "scheduled_at": slot.isoformat(), "observed_at": collected_at.isoformat()}
    except Exception as exc:
        return {**result, "blockers": [f"{type(exc).__name__}:{exc}" if str(exc) else "daily_mark_source_or_mark_timeout"]}
