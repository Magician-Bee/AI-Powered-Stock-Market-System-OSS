"""Explicit offline board-lot scenarios; simulated fills never prove live EV."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json

import pytest

from open_stock_ai.data.execution_quote import plan_quote_eligibility
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import TradingPlan
from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from test_autonomous_trading_plans import evidence
from test_paper_odd_lot_proxy import NOW, _market
from product_admission_fixtures import admitted_product, product_resolver


def market(*, at=NOW, price=20, size=100_000, received_at=None):
    return {**_market(at=at, price=price, size=size, received_at=received_at), "limit_up": 25, "limit_down": 15}


def ticket(order_id="board-one", *, quantity=1000, limit=20.1):
    return {"order_id": order_id, "symbol": "2330.TW", "market": "TW", "side": "buy",
            "order_type": "limit", "limit_price": limit, "time_in_force": "rod",
            "lot_type": "board_lot", "session": "regular", "quantity_shares": quantity}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = {"now": NOW-timedelta(hours=1)}
    monkeypatch.setattr(PaperOMS, "_now", lambda _: clock["now"].isoformat())
    monkeypatch.setattr(PaperBrokerSimulator, "_now", lambda _: clock["now"].isoformat())
    store = SQLiteStore(tmp_path/"isolated-board-scenario.sqlite")
    oms = PaperOMS(store, account_id="board-scenario-fixture", initial_cash=1_000_000,
                   commission_bps=14.25, minimum_commission=20, slippage_bps=5, sell_tax_bps=30, read_environment=False)
    broker = PaperBrokerSimulator(store, oms)
    port = PaperBrokerPort(broker, public_board_quote_simulation=True)
    clock["now"] = NOW
    return store, port, clock


def test_real_plan_risk_board_broker_waits_fills_and_exits_without_odd_lot_proxy(setup):
    store, port, clock = setup
    plans = TradingPlanStore(store)
    definition = TradingPlan(symbol="2330.TW", strategy_id="offline_board", strategy_version="a"*64,
        evidence_ids=("fixture-history",), reference_price=20, position_size_pct=2, stop_loss=18,
        target_price=22, quantity_shares=1000, cash_budget=20100, not_before=(NOW+timedelta(seconds=5)).isoformat(),
        metadata={"product_admission": admitted_product("2330.TW", now=NOW)})
    record = plans.create(account_id=port.account_id, plan=definition, idempotency_key="board-plan", now=NOW)
    engine = TradingPlanExecutor(store=plans, broker=port, risk=RiskEngine(), evidence_resolver=evidence,
                                 product_resolver=product_resolver)

    def tick(at, price):
        clock["now"] = at
        return asyncio.run(engine.tick(record["plan_id"], market=market(at=at, price=price), now=at))

    assert tick(NOW, 20)["state"]["wait_reason"] == "waiting_time"
    submitted = tick(NOW+timedelta(seconds=5), 20)
    assert submitted["state"]["status"] == "entry_submitted", submitted["state"]
    entry = submitted["state"]["entry_order_id"]
    assert port.broker.get_order(entry)["order"]["filled_quantity"] == 0  # 20.05 adverse quote exceeds limit 20
    opened = tick(NOW+timedelta(seconds=10), 19.9)
    assert opened["state"]["status"] == "open", opened["state"]
    result = port.broker.get_order(entry)
    assert result["order"]["filled_quantity"] == 1000
    execution = json.loads(result["events"][-1]["payload_json"])["execution_model"]
    assert "paper_odd_lot_simulation" not in execution
    assert execution["paper_board_lot_simulation"]["model"] == "bounded_board_trade_paper"
    assert execution["execution_evidence_eligible"] is False
    pending_exit = tick(NOW+timedelta(seconds=15), 22)
    assert pending_exit["state"]["status"] == "exit_submitted"
    closed = tick(NOW+timedelta(seconds=20), 22.1)
    assert closed["state"]["status"] == "closed", closed["state"]
    assert port.broker.oms.portfolio_summary()["positions"] == []


def test_source_liquidity_is_shared_across_orders_refetches_and_restarts(setup):
    store, port, clock = setup
    first = port.broker.submit(ticket(), market())
    assert first["order"]["filled_quantity"] == 1000
    second = port.broker.submit(ticket("board-two"), market(received_at=NOW+timedelta(seconds=1)))
    assert second["order"]["filled_quantity"] == 0
    restarted = PaperBrokerPort(PaperBrokerSimulator(store, port.broker.oms), public_board_quote_simulation=True)
    restarted.broker.process_market_tick("2330.TW", market(received_at=NOW+timedelta(seconds=2)))
    assert restarted.broker.get_order("board-two")["order"]["filled_quantity"] == 0
    changed = market(size=1_000_000)
    restarted.broker.process_market_tick("2330.TW", changed)
    assert restarted.broker.get_order("board-two")["order"]["filled_quantity"] == 0
    clock["now"] = NOW+timedelta(seconds=5)
    restarted.broker.process_market_tick("2330.TW", market(at=clock["now"]))
    assert restarted.broker.get_order("board-two")["order"]["filled_quantity"] == 1000


@pytest.mark.parametrize("size", [None, 0, 99999, 1000])
def test_unknown_or_sub_board_participation_volume_waits_instead_of_legacy_unlimited_fill(setup, size):
    _, port, _ = setup
    result = port.broker.submit(ticket(), market(size=size))
    assert result["order"]["filled_quantity"] == 0
    simulation = result["market_rules"]["paper_board_lot_simulation"]
    assert simulation["eligible"] is False


def test_partial_fill_rounds_down_to_whole_board_lots(setup):
    _, port, _ = setup
    result = port.broker.submit(ticket(quantity=2000), market(size=150_000))
    assert result["order"]["filled_quantity"] == 1000
    assert result["order"]["remaining_quantity"] == 1000


def test_opt_in_is_account_scoped_and_cannot_authorize_live_or_odd_lots(setup):
    store, port, _ = setup
    source = market()["source_envelope"]
    assert port.paper_quote_execution_model == "bounded_board_trade_paper"
    for mode, model, quantity in [("live", port.paper_quote_execution_model, 1000),
                                 ("paper", None, 1000), ("paper", port.paper_quote_execution_model, 10)]:
        assert not plan_quote_eligibility(source, mode=mode, paper_execution_model=model, quantity_shares=quantity, now=NOW)["execution_eligible"]
    other = PaperBrokerPort(PaperBrokerSimulator(store, PaperOMS(store, account_id="independent-strict", read_environment=False)))
    assert other.paper_quote_execution_model is None
    assert port.broker.odd_lot_execution_model is None
    assert source["authorized"] is False


def test_closed_stale_and_wrong_symbol_sources_cannot_use_board_scenario(setup):
    _, port, _ = setup
    for index, bad in enumerate([market(at=NOW-timedelta(hours=1)), {**market(), "source_trade": {**market()["source_trade"], "symbol": "2303.TW"}}]):
        result = port.broker.submit(ticket(f"bad-{index}"), bad)
        assert result["order"]["filled_quantity"] == 0


def test_fresh_quote_outside_regular_board_session_does_not_match(setup):
    _, port, clock = setup
    clock["now"] = NOW.replace(hour=6)  # 14:00 Taiwan; same-day fresh is not continuous trading
    result = port.broker.submit(ticket(), market(at=clock["now"]))
    assert result["order"]["filled_quantity"] == 0
    assert result["order"]["status"] == "rejected"
    assert port.broker.preview(ticket(), market(at=clock["now"]))["market_rules"]["matching_allowed"] is False


def test_fresh_last_trade_cannot_carry_continuous_matching_across_closing_boundary(setup):
    _, port, clock = setup
    source_at = NOW.replace(hour=5, minute=24, second=58)
    clock["now"] = source_at+timedelta(seconds=4)  # quote is fresh, but closing auction has started
    result = port.broker.submit(ticket(), market(at=source_at, received_at=clock["now"]))
    assert result["order"]["filled_quantity"] == 0
    assert result["market_rules"]["matching_allowed"] is False
