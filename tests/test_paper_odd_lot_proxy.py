from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore


NOW = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)


def _market(*, at=NOW, price=100, size=1000, received_at=None):
    stamp = at.isoformat()
    return {"symbol": "2330.TW", "market": "TW", "exchange": "TWSE", "price": price,
        "price_source": "TWSE MIS fixture", "source_timestamp": stamp, "is_realtime": True, "is_fallback": False,
        "trading_state": "trading", "limit_up": 110, "limit_down": 90,
        "exchange_rules_enforced": True,
        "source_envelope": quote_envelope(provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
            quote_kind="last_trade", exchange_timestamp=stamp, received_at=(received_at or at).isoformat(),
            max_age_seconds=15, authorized=False, realtime=True, delayed=False, official_close=False, trading_state="trading"),
        "source_trade": {"provider_id": "twse_mis", "symbol": "2330.TW", "venue": "TWSE", "channel": "regular_lot",
            "exchange_timestamp": stamp, "trade_id": str(int(at.timestamp() * 1000)), "price": price, "size_shares": size}}


def _ticket(order_id="proxy-order", *, quantity=40, limit=101, side="buy"):
    return {"order_id": order_id, "symbol": "2330.TW", "market": "TW", "side": side,
        "order_type": "limit", "limit_price": limit, "time_in_force": "rod", "lot_type": "odd_lot",
        "session": "regular", "quantity_shares": quantity}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = {"now": NOW.replace(hour=1)}
    monkeypatch.setattr(PaperBrokerSimulator, "_now", lambda _self: clock["now"].isoformat())
    monkeypatch.setattr(PaperOMS, "_now", lambda _self: clock["now"].isoformat())
    store = SQLiteStore(tmp_path / "isolated.sqlite")
    oms = PaperOMS(store=store, account_id="proxy", initial_cash=100_000, commission_bps=10,
                   minimum_commission=20, slippage_bps=5, sell_tax_bps=30, read_environment=False)
    broker = PaperBrokerSimulator(store=store, oms=oms, odd_lot_execution_model=OddLotBoardProxyModel())
    clock["now"] = NOW
    return broker, clock


def test_strict_default_waits_for_actual_odd_lot_receipt(setup):
    configured, _ = setup
    strict = PaperBrokerSimulator(store=configured.store, oms=configured.oms)
    result = strict.submit(_ticket(), _market())
    assert result["pending"] is True
    assert result["order"]["filled_quantity"] == 0
    assert result["market_rules"]["session"]["reason"] == "awaiting_odd_lot_auction_receipt"


def test_opted_in_proxy_uses_adverse_price_one_percent_and_durable_evidence(setup):
    broker, _ = setup
    market = _market()
    original = copy.deepcopy(market)
    preview = broker.preview(_ticket(), market)
    assert preview["estimated_fill_price"] == 100.5
    result = broker.submit(_ticket(), market)
    assert result["order"]["filled_quantity"] == 10
    assert result["order"]["remaining_quantity"] == 30
    assert result["portfolio"]["cash_balance"] == 98_975
    execution = json.loads(result["events"][-1]["payload_json"])["execution_model"]
    receipt = execution["paper_odd_lot_simulation"]
    assert receipt["is_simulated"] is True
    assert receipt["execution_evidence_eligible"] is False
    assert execution["execution_evidence_eligible"] is False
    assert "no observed odd-lot fill" in receipt["assumption"]
    assert market == original
    assert "odd_lot_auction_matched" not in market
    assert "paper_training_fill_override" not in market


def test_shared_trade_and_auction_caps_survive_refetch_restart_and_other_orders(setup):
    broker, clock = setup
    first = broker.submit(_ticket("one"), _market())
    assert first["order"]["filled_quantity"] == 10
    second = broker.submit(_ticket("two"), _market(received_at=NOW + timedelta(seconds=1)))
    assert second["order"]["filled_quantity"] == 0
    restarted = PaperBrokerSimulator(store=broker.store, oms=broker.oms, odd_lot_execution_model=OddLotBoardProxyModel())
    restarted.process_market_tick("2330.TW", _market(received_at=NOW + timedelta(seconds=2)))
    assert restarted.get_order("one")["order"]["filled_quantity"] == 10
    clock["now"] = NOW + timedelta(seconds=2)
    third = restarted.submit(_ticket("three"), _market(at=clock["now"], size=100_000))
    assert third["order"]["filled_quantity"] == 15  # same 5-second slot: 10 + 15 = 25
    other_oms = PaperOMS(store=broker.store, account_id="independent-scenario", initial_cash=100_000, read_environment=False)
    other = PaperBrokerSimulator(store=broker.store, oms=other_oms, odd_lot_execution_model=OddLotBoardProxyModel())
    independent = other.submit(_ticket("other"), _market())
    assert independent["order"]["filled_quantity"] == 10
    clock["now"] = NOW + timedelta(seconds=5)
    next_auction = restarted.submit(_ticket("four"), _market(at=clock["now"], size=100_000))
    assert next_auction["order"]["filled_quantity"] == 25


def test_same_trade_identity_cannot_gain_volume_by_changing_payload(setup):
    broker, _ = setup
    broker.submit(_ticket("first"), _market(size=1000))
    conflict = broker.submit(_ticket("second"), _market(size=100_000))
    assert conflict["order"]["filled_quantity"] == 0
    execution = json.loads(conflict["events"][-1]["payload_json"])["execution_model"]
    assert "odd_lot_proxy_trade_identity_conflicts_with_persisted_source" in execution["blockers"]


def test_additional_explicit_impact_is_included_in_preview_and_fill(setup):
    broker, _ = setup
    market = {**_market(), "market_impact_bps": 100, "latency_ms": 100, "latency_impact_bps_per_100ms": 5}
    ticket = _ticket(quantity=10, limit=103)
    preview = broker.preview(ticket, market)
    assert preview["estimated_fill_price"] == 101.5
    result = broker.submit(ticket, market)
    assert result["order"]["fill"]["fill_price"] == preview["estimated_fill_price"]


def test_resting_limit_budget_does_not_charge_an_impossible_above_limit_price(setup):
    broker, _ = setup
    oms = PaperOMS(store=broker.store, account_id="tight-cash", initial_cash=1020,
                   minimum_commission=20, slippage_bps=5, read_environment=False)
    tight = PaperBrokerSimulator(store=broker.store, oms=oms, odd_lot_execution_model=OddLotBoardProxyModel())
    preview = tight.preview(_ticket("tight", quantity=10, limit=100), _market())
    assert preview["can_submit"] is True
    assert preview["marketable_now"] is False
    assert preview["estimated_costs"]["estimated_total"] == 1020
    pending = tight.submit(_ticket("tight", quantity=10, limit=100), _market())
    assert pending["pending"] is True and pending["order"]["filled_quantity"] == 0
    assert pending["portfolio"]["cash_balance"] == 1020


@pytest.mark.parametrize("side,limit,later_price", [("buy", 100, 99), ("sell", 100, 101)])
def test_adverse_price_never_crosses_limit_and_later_better_quote_can_fill(setup, side, limit, later_price):
    broker, clock = setup
    if side == "sell":
        broker.oms.submit_and_fill({"order_id": "initial-owned", "symbol": "2330.TW", "market": "TW",
            "action": "buy", "entry_price": 100, "position_size_pct": 10, "risk_approved": True})
    pending = broker.submit(_ticket(side=side, limit=limit, quantity=10), _market())
    assert pending["order"]["filled_quantity"] == 0
    assert pending["pending"] is True
    clock["now"] = NOW + timedelta(seconds=5)
    result = broker.process_market_tick("2330.TW", _market(at=clock["now"], price=later_price))["results"][0]
    assert result["filled"] is True
    fill_price = result["order"]["fill"]["fill_price"]
    assert fill_price <= limit if side == "buy" else fill_price >= limit


@pytest.mark.parametrize("mutation,blocker", [
    ("no_size", "observed_board_trade_size_required"), ("zero_size", "observed_board_trade_size_required"),
    ("tiny_size", "board_trade_participation_below_one_share"),
    ("other_symbol", "board_trade_identity_or_price_not_bound"),
    ("other_price", "board_trade_identity_or_price_not_bound"),
    ("other_timestamp", "board_trade_identity_or_price_not_bound"),
    ("expired", "quote_expired"), ("training_override", "odd_lot_proxy_cannot_use_latest_mark_training_override"),
])
def test_missing_or_invalid_source_never_invents_auction_volume(setup, mutation, blocker):
    broker, _ = setup
    market = _market(at=NOW - timedelta(seconds=30)) if mutation == "expired" else _market()
    if mutation == "no_size":
        market["source_trade"].pop("size_shares")
    elif mutation == "zero_size":
        market["source_trade"]["size_shares"] = 0
    elif mutation == "tiny_size":
        market["source_trade"]["size_shares"] = 50
    elif mutation == "other_symbol":
        market["source_trade"]["symbol"] = "2317.TW"
    elif mutation == "other_price":
        market["source_trade"]["price"] = 99
    elif mutation == "other_timestamp":
        market["source_trade"]["exchange_timestamp"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "training_override":
        market["paper_training_fill_override"] = True
    preview = broker.preview(_ticket(), market)
    assert blocker in preview["paper_odd_lot_simulation"]["blockers"]
    result = broker.submit(_ticket(), market)
    assert result["order"]["filled_quantity"] == 0
    persisted = json.loads(result["events"][-1]["payload_json"])["execution_model"]
    assert blocker in persisted["paper_odd_lot_simulation"]["blockers"]


def test_model_preserves_exchange_time_quantity_and_price_rules(setup):
    broker, clock = setup
    clock["now"] = NOW.replace(hour=1, minute=5)  # 09:05 Taipei: no odd-lot auction yet
    opening = broker.submit(_ticket("opening"), _market(at=clock["now"]))
    assert opening["pending"] and opening["order"]["filled_quantity"] == 0
    clock["now"] = NOW
    rejected = broker.submit(_ticket("wrong-quantity", quantity=1000), _market())
    assert rejected["order"]["status"] == "rejected"
    rejected = broker.submit(_ticket("wrong-limit", limit=111), _market())
    assert rejected["order"]["status"] == "rejected"


@pytest.mark.parametrize("kwargs", [{"participation_rate": 0.02}, {"max_shares_per_auction": 26}, {"adverse_impact_bps": 0}])
def test_configuration_cannot_remove_conservative_scenario_bounds(kwargs):
    with pytest.raises(ValueError):
        OddLotBoardProxyModel(**kwargs)
