from __future__ import annotations

from dataclasses import replace

import pytest

from open_stock_ai.strategy.execution_policy import plan_signal_order, protective_exit
from open_stock_ai.types import TradingSignal


def _signal(**changes):
    return replace(TradingSignal(symbol="2330.TW", market="TW", action="buy", confidence=None,
                                 horizon="swing", reason="fixture", position_size_pct=5,
                                 entry_price=100, stop_loss=90, target_price=120), **changes)


def _plan(signal, **changes):
    return plan_signal_order(signal, **{"equity": 10000, "cash": 10000,
                                       "reference_price": 100, "position_quantity": 0, **changes})


@pytest.mark.parametrize("size", [None, 0, -1, 101, float("nan"), float("inf")])
def test_missing_or_invalid_sizing_never_defaults_to_full_equity(size):
    result = _plan(_signal(position_size_pct=size))
    assert result["eligible"] is False
    assert result["quantity"] == 0


def test_five_percent_is_a_five_share_order_and_add_is_incremental():
    assert _plan(_signal())["quantity"] == 5
    add = _plan(_signal(action="add"), position_quantity=20, cash=8000)
    assert add["quantity"] == 5
    assert add["cash_budget"] == 500


def test_hold_does_not_rebalance_and_reduce_is_not_liquidation():
    assert _plan(_signal(action="hold"), position_quantity=20)["quantity"] == 0
    reduced = _plan(_signal(action="reduce"), position_quantity=20)
    assert reduced["quantity"] == 5
    assert reduced["side"] == "sell"
    assert _plan(_signal(action="sell", position_size_pct=100), position_quantity=20)["quantity"] == 20


def test_sizing_charges_costs_and_minimum_commission_before_rounding():
    plan = _plan(_signal(position_size_pct=10), commission_bps=10, minimum_commission=20)
    assert plan["quantity"] == 9
    assert plan["cash_budget"] == 1000


@pytest.mark.parametrize("stop,target", [(None,120),(100,120),(101,120),(90,100),(90,99)])
def test_buy_requires_a_valid_long_bracket(stop,target):
    assert not _plan(_signal(stop_loss=stop,target_price=target))["eligible"]


def test_protective_stop_is_conservative_on_gaps_and_ambiguous_bars():
    assert protective_exit(open_price=85,high=95,low=80,stop_loss=90,target_price=120) == {
        "reason":"stop_loss","reference_price":85,"ambiguous_bar":False}
    both=protective_exit(open_price=100,high=125,low=85,stop_loss=90,target_price=120)
    assert both == {"reason":"stop_loss","reference_price":90,"ambiguous_bar":True}
    target=protective_exit(open_price=130,high=135,low=125,stop_loss=90,target_price=120)
    assert target["reference_price"] == 120  # no favorable gap fill invented


def test_no_trigger_preserves_existing_bracket():
    assert protective_exit(open_price=100,high=110,low=95,stop_loss=90,target_price=120) is None


def test_taiwan_single_order_rounding_keeps_residual_for_a_separate_order():
    buy=_plan(_signal(position_size_pct=100),equity=122500,cash=122500)
    assert (buy["quantity"],buy["lot_size"],buy["lot_type"],buy["unsubmitted_quantity"]) == (1000,1000,"board_lot",225)
    sale=_plan(_signal(action="sell",position_size_pct=100),equity=122500,cash=0,position_quantity=1225)
    assert sale["quantity"] == 1000 and sale["unsubmitted_quantity"] == 225
    residual=_plan(_signal(action="sell",position_size_pct=100),equity=22500,cash=0,position_quantity=225)
    assert (residual["quantity"],residual["lot_size"],residual["lot_type"]) == (225,1,"odd_lot")
