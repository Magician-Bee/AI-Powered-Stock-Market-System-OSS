"""Offline opening-session fixtures; no fixture is evidence of a real fill."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.data.execution_quote import plan_quote_eligibility, quote_envelope
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import TradingPlan
from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai import paper_training_api, services
from stock_ai.realtime_quotes import _parse_mis_row
from product_admission_fixtures import admitted_product, product_resolver


NOW = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)
PROXY = "bounded_board_trade_odd_lot_proxy"


def envelope(*, at=NOW, received_at=NOW, state="trading", kind="last_trade", provider="twse_mis"):
    return quote_envelope(
        provider_id=provider,
        connector_id=("stock_ai.realtime_quotes.fetch_twse_mis_quote" if provider == "twse_mis"
                      else "stock_ai.taiwan_official.official_summary_payload"),
        quote_kind=kind, exchange_timestamp=at.isoformat(), received_at=received_at.isoformat(),
        max_age_seconds=15, authorized=False, realtime=kind == "last_trade",
        delayed=False, official_close=kind == "official_close", trading_state=state,
    )


@pytest.mark.parametrize("mode,model,quantity", [
    ("live", PROXY, 10), ("paper", None, 10), ("paper", "untrusted_model", 10),
    ("paper", PROXY, 1000), ("paper", PROXY, True), ("paper", PROXY, None),
])
def test_public_quote_cannot_bypass_generic_or_live_intraday_authorization(mode, model, quantity):
    result = plan_quote_eligibility(envelope(), mode=mode, paper_execution_model=model,
                                    quantity_shares=quantity, now=NOW)
    assert result["execution_eligible"] is False
    assert "intraday_requires_authorized_realtime" in result["blockers"]


def test_explicit_paper_scenario_admits_quote_without_inventing_data_authorization():
    source = envelope()
    result = plan_quote_eligibility(source, mode="paper", paper_execution_model=PROXY,
                                    quantity_shares=10, now=NOW)
    assert result["execution_eligible"] is True
    assert result["paper_simulation_only"] is True
    assert result["execution_evidence_eligible"] is False
    assert result["live_execution_eligible"] is False
    assert source["authorized"] is False


@pytest.mark.parametrize("source,reason", [
    (envelope(at=NOW-timedelta(hours=12)), "quote_expired"),
    (envelope(state="closed"), "last_trade_market_not_open"),
    (envelope(kind="official_close", provider="twse_openapi"), "paper_proxy_requires_fresh_current_last_trade"),
    (envelope(at=NOW+timedelta(minutes=1)), "quote_timestamp_in_future"),
    ({**envelope(), "authorized": True}, "source_envelope_signature_mismatch"),
])
def test_paper_scenario_retains_freshness_kind_and_integrity_gates(source, reason):
    result = plan_quote_eligibility(source, mode="paper", paper_execution_model=PROXY,
                                    quantity_shares=10, now=NOW)
    assert result["execution_eligible"] is False
    assert reason in result["blockers"]


@pytest.fixture
def fixture_path(tmp_path, monkeypatch):
    """Use actual connector/projection code with an explicitly synthetic MIS row."""
    clock = {"now": NOW-timedelta(hours=2)}
    monkeypatch.setattr(PaperOMS, "_now", lambda self: clock["now"].isoformat())
    monkeypatch.setattr(PaperBrokerSimulator, "_now", lambda self: clock["now"].isoformat())
    store = SQLiteStore(tmp_path / "isolated-opening-scenario.sqlite")
    oms = PaperOMS(store=store, account_id="offline-quote-reachability", initial_cash=1_000_000,
                   commission_bps=14.25, minimum_commission=20, slippage_bps=5,
                   sell_tax_bps=30, read_environment=False)
    broker = PaperBrokerSimulator(store=store, oms=oms, odd_lot_execution_model=OddLotBoardProxyModel())
    port = PaperBrokerPort(broker)
    plans = TradingPlanStore(store)
    definition = TradingPlan(symbol="2303.TW", strategy_id="offline_fixture", strategy_version="a"*64,
                             evidence_ids=("fixture-history",), reference_price=142.5,
                             position_size_pct=0.15, stop_loss=135, target_price=150,
                             quantity_shares=10, cash_budget=1500, not_before=NOW.isoformat(),
                             metadata={"product_admission": admitted_product("2303.TW", now=NOW)})
    record = plans.create(account_id=oms.account_id, plan=definition, idempotency_key="offline-plan", now=clock["now"])

    def evidence(_plan):
        return {"required_evidence": ["price_history", "cost_model"],
                "receipts": {"price_history": order_risk_evidence_receipt(kind="price_history",
                    payload={"symbol": "2303.TW", "bars": 100}, source="explicit_offline_fixture", passed=True)},
                "experiment_limits": {"max_order_notional_pct": 5, "max_total_exposure_pct": 20}}

    executor = TradingPlanExecutor(store=plans, broker=port, risk=RiskEngine(), evidence_resolver=evidence,
                                   product_resolver=product_resolver)
    monkeypatch.setattr(services, "_debug_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(services, "_linked_factors_for", lambda *_: [])
    monkeypatch.setattr(services, "_observe_independent_same_day_quote", lambda *args, **kwargs: {})
    monkeypatch.setattr(services, "_attach_market_summary_quality_receipt", lambda summary, **kwargs: summary)
    monkeypatch.setattr(paper_training_api, "_record_runtime_slo", lambda *args, **kwargs: {})

    def quote(*, at, price=142.5, size="1", received_at=None):
        row = {"c": "2303", "n": "Fixture 聯電", "ex": "tse", "z": str(price), "y": "142",
               "d": at.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d"),
               "tlong": int(at.timestamp()*1000), "tv": size, "u": "156", "w": "128"}
        parsed = _parse_mis_row(row, {"userDelay": 5000}, received_at=received_at or at)

        async def fetch(_symbol):
            return {"data": parsed}

        monkeypatch.setattr(services, "fetch_twse_mis_quote", fetch)
        summary = services.get_market_summary("2303.TW", include_events=False)
        monkeypatch.setattr(paper_training_api, "get_execution_price_summary", lambda _symbol: summary)
        market = paper_training_api._verified_price("2303.TW", require_execution_quote=False)
        assert market["source_envelope"]["authorized"] is False
        assert "odd_lot_auction_matched" not in market
        return market

    def tick(market, now):
        clock["now"] = now
        return asyncio.run(executor.tick(record["plan_id"], market=market, now=now))

    return broker, port, plans, record["plan_id"], quote, tick


def test_closed_saved_plan_reaches_real_paper_backend_then_fills_only_at_valid_limit(fixture_path):
    broker, port, plans, pid, quote, tick = fixture_path
    assert plans.get(pid)["state"]["status"] == "waiting_entry"
    assert port.paper_quote_execution_model == PROXY
    closed = quote(at=NOW-timedelta(days=1), received_at=NOW-timedelta(hours=2))
    waiting = tick(closed, NOW-timedelta(hours=2))
    assert waiting["state"]["wait_reason"] == "quote_ineligible"
    assert broker.open_order_reservations() == []
    opening = quote(at=NOW)
    accepted = tick(opening, NOW)
    assert accepted["state"]["status"] == "entry_submitted", accepted["state"]
    order_id = accepted["state"]["entry_order_id"]
    assert broker.get_order(order_id)["order"]["filled_quantity"] == 0  # no opening-auction match yet
    auction = NOW+timedelta(minutes=10)
    still_pending = tick(quote(at=auction), auction)
    assert broker.get_order(order_id)["order"]["filled_quantity"] == 0  # adverse price exceeds limit
    assert still_pending["state"]["status"] == "entry_submitted"
    later = auction+timedelta(seconds=5)
    filled = tick(quote(at=later, price=142), later)
    assert filled["state"]["status"] == "open", filled["state"]
    receipt = broker.get_order(order_id)
    assert receipt["order"]["filled_quantity"] == 10
    simulation = json.loads(receipt["events"][-1]["payload_json"])["execution_model"]["paper_odd_lot_simulation"]
    assert simulation["execution_evidence_eligible"] is False
    assert simulation["simulated_fill_price"] <= 142.5
    target_at = later+timedelta(seconds=5)
    exit_pending = tick(quote(at=target_at, price=150), target_at)
    assert exit_pending["state"]["status"] == "exit_submitted", exit_pending["state"]
    exit_id = exit_pending["state"]["exit_order_id"]
    assert broker.get_order(exit_id)["order"]["filled_quantity"] == 0
    exit_at = target_at+timedelta(seconds=5)
    closed = tick(quote(at=exit_at, price=150.5), exit_at)
    assert closed["state"]["status"] == "closed", closed["state"]
    assert broker.get_order(exit_id)["order"]["filled_quantity"] == 10
    assert broker.oms.portfolio_summary()["positions"] == []


def test_missing_true_trade_size_keeps_opted_in_order_pending(fixture_path):
    broker, _, _, _, quote, tick = fixture_path
    accepted = tick(quote(at=NOW, size="-"), NOW)
    assert accepted["state"]["status"] == "entry_submitted", accepted["state"]
    later = NOW+timedelta(minutes=10, seconds=5)
    pending = tick(quote(at=later, price=142, size="-"), later)
    assert pending["state"]["status"] == "entry_submitted"
    order = broker.get_order(pending["state"]["entry_order_id"])
    assert order["order"]["filled_quantity"] == 0


def test_strict_paper_port_cannot_take_the_proxy_quote_route(fixture_path):
    broker, _, _, _, quote, tick = fixture_path
    broker.odd_lot_execution_model = None
    rejected = tick(quote(at=NOW), NOW)
    assert rejected["state"]["wait_reason"] == "quote_ineligible"
    assert "intraday_requires_authorized_realtime" in rejected["state"]["quote_gate"]["blockers"]
    assert broker.open_order_reservations() == []
