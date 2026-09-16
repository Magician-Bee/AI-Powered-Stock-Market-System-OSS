"""Offline source/clock fixtures against a real isolated PaperOMS and evidence DB."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.autonomous_campaign import AutonomousCampaign
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.forward_daily_mark import FIXTURE, retain_forward_daily_mark
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore


NOW = datetime(2026, 9, 11, 6, 35, tzinfo=timezone.utc)


def quote(symbol="2330.TW", *, day_offset=0, price=120, kind="official_close", at=None):
    stamp = at or NOW.replace(hour=5, minute=30)+timedelta(days=day_offset)
    provider = "twse_openapi" if kind == "official_close" else "twse_mis"
    return {"symbol": symbol, "market": "TW", "exchange": "TWSE", "price": price,
            "source_timestamp": stamp.isoformat(), "price_source": "explicit_fixture_official",
            "is_realtime": kind == "last_trade", "is_fallback": kind == "official_close", FIXTURE: True,
            "source_envelope": quote_envelope(provider_id=provider,
                connector_id=("stock_ai.taiwan_official.official_summary_payload" if kind == "official_close"
                              else "stock_ai.realtime_quotes.fetch_twse_mis_quote"),
                quote_kind=kind, exchange_timestamp=stamp.isoformat(), received_at=NOW.isoformat(),
                max_age_seconds=15, authorized=False, realtime=kind == "last_trade", delayed=False,
                official_close=kind == "official_close", trading_state="closed" if kind == "official_close" else "trading")}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(PaperOMS, "_now", lambda _: NOW.isoformat())
    store = SQLiteStore(tmp_path/"daily-mark-fixture.sqlite")
    oms = PaperOMS(store, account_id="isolated-daily-mark", initial_cash=100_000,
                   commission_bps=0, minimum_commission=0, slippage_bps=0, read_environment=False)
    oms.ensure_account()
    broker = PaperBrokerPort(PaperBrokerSimulator(store, oms))
    quotes = {"2330.TW": quote(), "2303.TW": quote("2303.TW", price=140)}

    async def load(symbol):
        value = quotes[symbol]
        if isinstance(value, Exception):
            raise value
        return value

    async def forbidden(*args, **kwargs):
        raise AssertionError("daily NAV helper must not match, submit, or call models")

    broker.observe = broker.submit = forbidden
    campaign = AutonomousCampaign(plans=TradingPlanStore(store), broker=broker, risk=RiskEngine(),
        scanner=forbidden, history_loader=forbidden, quote_loader=load,
        calendar=SimpleNamespace(day_status=lambda day: {"trading_day": day.weekday()<5}))

    def seed(symbol="2330.TW"):
        result = oms.submit_and_fill({"order_id": "seed-"+symbol, "symbol": symbol, "market": "TW",
            "action": "buy", "entry_price": 100, "position_size_pct": 1,
            "quantity_shares": 10, "risk_approved": True})
        assert result["filled"] is True

    return campaign, quotes, seed


def collect(campaign, *, now=NOW, scheduled_at=NOW):
    return asyncio.run(retain_forward_daily_mark(campaign, scheduled_at=scheduled_at, now=now))


def test_all_positions_marked_to_same_day_official_close_without_fills(setup):
    campaign, _, seed = setup
    seed()
    seed("2303.TW")
    result = collect(campaign)
    assert result["status"] == "ready", result
    snapshot = result["snapshot"]
    assert {row["symbol"]: row["last_price"] for row in snapshot["positions"]} == {"2330.TW": 120, "2303.TW": 140}
    receipt = campaign._evidence(result["evidence_ids"][0], "forward_daily_mark")
    assert receipt["snapshot"] == snapshot
    assert receipt["account_id"] == campaign.broker.account_id
    assert receipt["source_provenance_verified"] is True and receipt["source_kind"] == "exchange_official"
    assert all(row["valuation_basis"] == "same_day_official_close" for row in receipt["quotes"])
    assert receipt[FIXTURE] is True  # this offline receipt must not qualify an EV observation
    assert receipt["execution_evidence_eligible"] is False
    with campaign.plans.store._connect() as conn:
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 2  # setup only
        assert conn.execute("select count(*) from paper_price_marks").fetchone()[0] == 2


@pytest.mark.parametrize("bad,reason", [
    (quote("2303.TW", day_offset=-1), "same_trading_date"),
    (quote(symbol="2454.TW"), "identity"),
    (quote("2303.TW", kind="last_trade", at=NOW-timedelta(minutes=2)), "official_close_or_fresh_trade"),
    (quote("2303.TW", at=NOW+timedelta(minutes=2)), "time_not_available"),
    ({**quote("2303.TW"), "price": 0}, "positive"),
    ({**quote("2303.TW"), "source_envelope": {**quote("2303.TW")["source_envelope"], "authorized": True}}, "integrity"),
    (ConnectionError("official_source_unavailable"), "official_source_unavailable"),
])
def test_source_failure_is_retryable_and_marks_none_of_the_positions(setup, bad, reason):
    campaign, quotes, seed = setup
    seed()
    seed("2303.TW")
    quotes["2303.TW"] = bad
    result = collect(campaign)
    assert result["status"] == "retry"
    assert result["evidence_ids"] == [] and result["snapshot"] is None
    assert reason in " ".join(result["blockers"])
    with campaign.plans.store._connect() as conn:
        assert conn.execute("select count(*) from paper_price_marks").fetchone()[0] == 0


def test_fresh_current_trade_is_usable_for_valuation_without_live_authorization(setup):
    campaign, quotes, seed = setup
    seed()
    quotes["2330.TW"] = quote(kind="last_trade", at=NOW-timedelta(seconds=2))
    result = collect(campaign)
    assert result["status"] == "ready", result
    receipt = campaign._evidence(result["evidence_ids"][0], "forward_daily_mark")
    assert receipt["quotes"][0]["valuation_basis"] == "fresh_current_last_trade"
    assert receipt["quotes"][0]["source_envelope"]["authorized"] is False


@pytest.mark.parametrize("scheduled,now,reason", [
    (NOW, NOW-timedelta(seconds=1), "not_due"),
    (NOW, NOW+timedelta(hours=4, seconds=1), "expired"),
    (NOW-timedelta(minutes=1), NOW, "1435_slot"),
    (NOW+timedelta(days=1), NOW+timedelta(days=1), "trading_day"),
])
def test_only_fixed_due_trading_day_slot_can_observe(setup, scheduled, now, reason):
    campaign, _, _ = setup
    result = collect(campaign, now=now, scheduled_at=scheduled)
    assert result["status"] in {"retry", "rejected"}
    assert reason in " ".join(result["blockers"])


def retain_cycle(campaign, *, day_offset=0, unavailable_first=False):
    feature = {"symbol": "2303.TW", "source": "TWSE_ALL_QUOTES", "close": 142.5,
               "data_as_of": (NOW+timedelta(days=day_offset)).date().isoformat(), FIXTURE: True}
    bulk = campaign._retain("market_screen", {"features": ([{**feature, "close": None}] if unavailable_first else [])+[feature]})
    body = {"account_id": campaign.broker.account_id, "created_at": (NOW-timedelta(minutes=1)).isoformat(),
            "bulk_evidence_id": bulk}
    cycle_id = "AC-"+content_hash(body)
    with campaign.plans.store._connect() as conn:
        conn.execute("insert into autonomous_research_cycles values (?,?,?,?)",
                     (cycle_id, campaign.broker.account_id, body["created_at"], json.dumps({**body, "cycle_id": cycle_id})))
        conn.commit()
    return bulk


def test_cash_only_requires_same_day_official_market_evidence(setup):
    campaign, _, _ = setup
    missing = collect(campaign)
    assert missing["status"] == "retry" and "cycle_unavailable" in str(missing["blockers"])
    bulk = retain_cycle(campaign)
    result = collect(campaign)
    assert result["status"] == "ready", result
    receipt = campaign._evidence(result["evidence_ids"][0], "forward_daily_mark")
    assert receipt["quotes"] == []
    assert receipt["market_evidence_ids"] == [bulk]
    assert receipt["snapshot"]["total_equity"] == 100_000


def test_cash_only_cannot_use_yesterdays_bulk_refetched_today(setup):
    campaign, _, _ = setup
    retain_cycle(campaign, day_offset=-1)
    result = collect(campaign)
    assert result["status"] == "retry"
    assert "same_day_official_market_evidence" in str(result["blockers"])


def test_cash_only_skips_one_unavailable_symbol_when_other_same_day_source_exists(setup):
    campaign, _, _ = setup
    retain_cycle(campaign, unavailable_first=True)
    assert collect(campaign)["status"] == "ready"


def test_account_lease_prevents_daily_mark_racing_plan_submission(setup):
    campaign, _, seed = setup
    seed()
    with campaign.plans.account_lease(campaign.broker.account_id, now=NOW):
        result = collect(campaign)
    assert result["status"] == "retry" and result["blockers"] == ["daily_mark_account_busy"]
    assert result["evidence_ids"] == []


def test_source_fetch_account_change_requires_retry_before_any_mark(setup):
    campaign, _, seed = setup
    seed()
    loader = campaign.quote_loader

    async def racing_loader(symbol):
        seed("2303.TW")  # concurrent fixture event, outside this helper
        return await loader(symbol)

    campaign.quote_loader = racing_loader
    result = collect(campaign)
    assert result["status"] == "retry" and result["blockers"] == ["daily_mark_account_changed_during_source_fetch"]
    with campaign.plans.store._connect() as conn:
        assert conn.execute("select count(*) from paper_price_marks").fetchone()[0] == 0


def test_a_noop_mark_cannot_validate_old_nav(setup):
    campaign, _, seed = setup
    seed()

    async def noop(_market):
        return None

    campaign.broker.mark = noop
    result = collect(campaign)
    assert result["status"] == "retry"
    assert "position_price_does_not_match_source" in str(result["blockers"])
    assert result["evidence_ids"] == []


def test_failed_partial_mark_never_retains_observation_and_can_retry(setup):
    campaign, _, seed = setup
    seed()
    seed("2303.TW")
    original = campaign.broker.mark
    calls = []

    async def interrupted(market):
        calls.append(market["symbol"])
        if len(calls) == 2:
            raise RuntimeError("fixture_mark_interrupted")
        await original(market)

    campaign.broker.mark = interrupted
    failed = collect(campaign)
    assert failed["status"] == "retry" and failed["evidence_ids"] == []
    assert "fixture_mark_interrupted" in str(failed["blockers"])
    campaign.broker.mark = original
    assert collect(campaign)["status"] == "ready"


def test_cancellation_is_not_converted_to_a_retry_receipt(setup):
    campaign, _, seed = setup
    seed()

    async def cancelled(symbol):
        raise asyncio.CancelledError()

    campaign.quote_loader = cancelled
    with pytest.raises(asyncio.CancelledError):
        collect(campaign)
