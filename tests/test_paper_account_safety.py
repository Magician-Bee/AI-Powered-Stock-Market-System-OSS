from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore


def _oms(tmp_path, **kwargs):
    return PaperOMS(store=SQLiteStore(tmp_path / "isolated.sqlite"), account_id="isolated", read_environment=False, **kwargs)


def _order(order_id="order"):
    return {"order_id": order_id, "symbol": "2330.TW", "market": "TW", "action": "buy", "entry_price": 100,
            "position_size_pct": 50, "risk_approved": True}


def _ticket(**changes):
    return {"order_id": "broker-order", "symbol": "2330.TW", "market": "TW", "side": "buy", "order_type": "market",
            "time_in_force": "rod", "lot_type": "odd_lot", "session": "regular", "quantity_shares": 10, **changes}


def _market(price=100):
    return {"symbol": "2330.TW", "market": "TW", "price": price, "price_source": "isolated-fixture",
            "source_timestamp": "2026-09-11T02:00:00+00:00", "is_realtime": True, "is_fallback": False,
            "odd_lot_auction_matched": True}


def test_explicit_isolated_account_and_costs_ignore_existing_environment(tmp_path, monkeypatch):
    for key, value in {
        "ACCOUNT_ID": "unrelated-existing-account", "INITIAL_CASH": "123456", "COMMISSION_BPS": "100",
        "MINIMUM_COMMISSION": "999", "SELL_TAX_BPS": "999", "SLIPPAGE_BPS": "999", "LOT_SIZE": "1000",
    }.items():
        monkeypatch.setenv("OPEN_STOCK_AI_PAPER_" + key, value)
    oms = _oms(tmp_path, initial_cash=10_000, commission_bps=10, minimum_commission=20, sell_tax_bps=30, slippage_bps=5)
    assert (oms.account_id, oms.initial_cash, oms.commission_bps, oms.minimum_commission, oms.sell_tax_bps, oms.slippage_bps, oms.lot_size) == (
        "isolated", 10_000, 10, 20, 30, 5, 1,
    )
    with oms.store._connect() as conn:
        assert conn.execute("select account_id from paper_accounts").fetchall() == [("isolated",)]
    compatible = PaperOMS(store=oms.store)
    assert compatible.account_id == "unrelated-existing-account"
    assert compatible.minimum_commission == 999


def test_partial_fills_charge_one_minimum_then_only_cumulative_increment(tmp_path):
    oms = _oms(tmp_path, initial_cash=100_000, commission_bps=10, minimum_commission=20)
    order = _order()
    first = oms.submit_partial_fill(order, fill_quantity=10, fill_id="f1")
    second = oms.submit_partial_fill(order, fill_quantity=190, fill_id="f2")
    third = oms.submit_partial_fill(order, fill_quantity=300, fill_id="f3")
    assert [first["fill"]["commission"], second["fill"]["commission"], third["fill"]["commission"]] == [20, 0, 30]
    assert third["portfolio"]["cash_balance"] == 49_950
    assert third["portfolio"]["positions"][0]["quantity"] == 500
    retried = oms.submit_partial_fill(order, fill_quantity=300, fill_id="f3")
    assert retried["idempotent"] is True
    assert retried["portfolio"]["cash_balance"] == 49_950
    with oms.store._connect() as conn:
        assert conn.execute("select sum(commission),count(*) from paper_fills").fetchone() == (50, 3)


def test_partial_commission_survives_restart_and_minimum_applies_per_order(tmp_path):
    oms = _oms(tmp_path, initial_cash=10_000, commission_bps=10, minimum_commission=20)
    oms.submit_partial_fill(_order(), fill_quantity=1, fill_id="f1")
    restarted = _oms(tmp_path, initial_cash=10_000, commission_bps=10, minimum_commission=20)
    continuation = restarted.submit_partial_fill(_order(), fill_quantity=1, fill_id="f2")
    separate = restarted.submit_partial_fill(_order("another"), fill_quantity=1, fill_id="f3")
    assert continuation["fill"]["commission"] == 0
    assert separate["fill"]["commission"] == 20


def test_preview_affordability_and_fill_agree_on_minimum_and_slippage(tmp_path):
    oms = _oms(tmp_path, initial_cash=1030, minimum_commission=20, commission_bps=10, slippage_bps=100)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    preview = broker.preview(_ticket(), _market())
    assert preview["can_submit"] is True
    assert preview["estimated_fill_price"] == 101
    assert preview["estimated_costs"]["commission"] == 20
    assert preview["estimated_costs"]["estimated_total"] == 1030
    result = broker.submit(_ticket(), _market())
    assert result["filled"] is True
    assert result["portfolio"]["cash_balance"] == 0
    assert result["portfolio"]["positions"][0]["quantity"] == 10


def test_preview_rejects_cash_shortfall_from_slippage_or_minimum(tmp_path):
    oms = _oms(tmp_path, initial_cash=1020, minimum_commission=20, commission_bps=10, slippage_bps=100)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    preview = broker.preview(_ticket(), _market())
    assert preview["can_submit"] is False
    assert preview["validation"]["estimated_required"] == 1030
    assert broker.submit(_ticket(), _market())["filled"] is False
    assert oms.portfolio_summary()["cash_balance"] == 1020


def test_frozen_broker_quantity_survives_limit_fill_reference_adjustment(tmp_path):
    oms = _oms(tmp_path, initial_cash=10_000, minimum_commission=20, slippage_bps=500)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    result = broker.submit(_ticket(order_type="limit", limit_price=102), _market())
    assert result["filled"] is True
    assert result["order"]["requested_quantity"] == 10
    assert result["portfolio"]["positions"][0]["quantity"] == 10
    assert result["portfolio"]["positions"][0]["last_price"] == 102


def test_order_identity_cannot_return_another_isolated_accounts_fill(tmp_path):
    first = _oms(tmp_path, initial_cash=10_000)
    first.submit_and_fill(_order())
    other = PaperOMS(store=first.store, account_id="other", read_environment=False, initial_cash=1000)
    rejected = other.submit_and_fill(_order())
    assert rejected["filled"] is False
    assert rejected["order"]["rejection_reason"] == "order_belongs_to_different_account"
    assert rejected["fill"] is None
    assert rejected["portfolio"]["cash_balance"] == 1000


def test_nav_daily_pnl_and_peak_persist_with_account_timezone(tmp_path, monkeypatch):
    clock = {"now": "2026-09-11T01:00:00+00:00"}
    monkeypatch.setattr(PaperOMS, "_now", lambda _self: clock["now"])
    oms = _oms(tmp_path, initial_cash=10_000, minimum_commission=20)
    initial = oms.portfolio_summary()
    assert (initial["today_pnl"], initial["peak_equity"]) == (0, 10_000)
    clock["now"] = "2026-09-11T02:00:00+00:00"
    after_fill = oms.submit_and_fill(_order())["portfolio"]
    assert after_fill["today_pnl"] == -20
    assert after_fill["current_drawdown_pct"] == pytest.approx(0.2)
    clock["now"] = "2026-09-11T03:00:00+00:00"
    with oms.store._connect() as conn:
        conn.execute("update paper_positions set last_price=110 where account_id=?", (oms.account_id,))
    marked = oms.portfolio_summary()
    assert marked["total_equity"] == 10_480
    assert marked["peak_equity"] == 10_480
    assert marked["today_pnl"] == 480
    restarted = _oms(tmp_path, initial_cash=10_000)
    assert restarted.portfolio_summary()["peak_equity"] == 10_480
    clock["now"] = "2026-09-11T16:05:00+00:00"  # already September 12 in Taipei
    next_day = restarted.portfolio_summary()
    assert next_day["today_pnl"] == 0
    assert next_day["nav_risk_state"]["local_day"] == "2026-09-12"
    assert next_day["nav_risk_state"]["day_baseline_equity"] == 10_480
    assert next_day["nav_risk_state"]["day_baseline_basis"] == "latest_observed_prior_day_nav"
    with pytest.raises(ValueError, match="nav_observation_time_regressed"):
        restarted.portfolio_summary(as_of="2026-09-11T02:00:00+00:00")


def test_legacy_account_without_nav_baseline_does_not_invent_zero_loss(tmp_path):
    oms = _oms(tmp_path, initial_cash=10_000)
    oms.submit_and_fill(_order())
    with oms.store._connect() as conn:
        conn.execute("delete from paper_nav_observations")
    restarted = _oms(tmp_path)
    summary = restarted.portfolio_summary()
    assert summary["today_pnl"] is None
    assert summary["peak_equity"] is None
    assert summary["current_drawdown_pct"] is None
    assert summary["nav_risk_state"]["status"] == "missing_baseline"
    assert summary["nav_risk_state"]["observed_peak_equity"] == summary["total_equity"]


def test_reservations_are_complete_account_scoped_and_do_not_subtract_cash(tmp_path):
    oms = _oms(tmp_path, initial_cash=1_000_000, minimum_commission=20, slippage_bps=100)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    broker.submit(_ticket(order_type="limit", limit_price=80), {**_market(), "industry": "semiconductor"})
    with oms.store._connect() as conn:
        conn.row_factory = sqlite3.Row
        seed = dict(conn.execute("select * from paper_broker_orders where account_id=?", (oms.account_id,)).fetchone())
        columns = list(seed)
        for index in range(500):
            item = {**seed, "order_id": f"pending-{index}"}
            conn.execute(f"insert into paper_broker_orders ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
                         [item[key] for key in columns])
    other = PaperOMS(store=oms.store, account_id="other", read_environment=False)
    PaperBrokerSimulator(store=oms.store, oms=other).submit(_ticket(order_id="other-order", order_type="limit", limit_price=80), _market())
    rows = broker.open_order_reservations()
    assert len(rows) == 501
    assert all(row["account_id"] == oms.account_id and row["valid"] for row in rows)
    assert all(row["reservation_price"] == 80 and row["estimated_remaining_cost"] == 20 for row in rows)
    assert all(row["industry"] == "semiconductor" for row in rows)
    assert oms.portfolio_summary()["cash_balance"] == 1_000_000
    broker.cancel("pending-0")
    assert len(broker.open_order_reservations()) == 500


def test_partial_reservation_uses_remaining_quantity_and_paid_order_commission(tmp_path):
    oms = _oms(tmp_path, initial_cash=10_000, minimum_commission=20, commission_bps=10, slippage_bps=100)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    result = broker.submit(_ticket(), {**_market(), "available_quantity": 4})
    assert result["order"]["status"] == "partially_filled"
    row, = broker.open_order_reservations()
    assert row["remaining_quantity"] == 6
    assert row["reservation_price"] == 101
    assert row["estimated_remaining_cost"] == 0
    assert row["price_basis"] == "latest_market_mark_with_slippage"
    assert oms.portfolio_summary()["cash_balance"] == 9576


def test_stop_reservation_uses_buy_trigger_and_missing_price_remains_blocking(tmp_path):
    oms = _oms(tmp_path, initial_cash=10_000, minimum_commission=20, slippage_bps=100)
    broker = PaperBrokerSimulator(store=oms.store, oms=oms)
    broker.submit(_ticket(order_type="stop", stop_price=110), _market())
    row, = broker.open_order_reservations()
    assert row["reservation_price"] == pytest.approx(111.1)
    assert row["estimated_remaining_cost"] == 20
    with oms.store._connect() as conn:
        conn.execute("update paper_broker_orders set last_market_price=null where account_id=?", (oms.account_id,))
    row, = broker.open_order_reservations()
    assert row["valid"] is False
    assert row["reservation_price"] is None
    assert row["estimated_remaining_cost"] is None
    assert "open_order_reservation_price_missing" in row["blockers"]
