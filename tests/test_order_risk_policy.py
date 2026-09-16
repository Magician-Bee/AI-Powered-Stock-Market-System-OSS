from __future__ import annotations

import pytest

from open_stock_ai.risk.kill_switch import DurableRiskControlStore
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.risk.risk_engine import RiskEngine


def _inputs(**intent_changes):
    intent = {"symbol": "2330.TW", "side": "buy", "quantity_shares": 10, "reference_price": 100,
              "stop_loss": 95, "industry": "semiconductor", "strategy_id": "candle", "strategy_version_hash": "a" * 64,
              "account_id": "isolated", **intent_changes}
    account = {"account_id": "isolated", "total_equity": 100_000, "cash_balance": 100_000, "available_cash": 100_000,
               "today_pnl": 0, "peak_equity": 100_000, "positions": []}
    history = order_risk_evidence_receipt(kind="price_history", payload={"symbol": "2330.TW", "completed_bars": 100}, source="retained_history_fixture", passed=True)
    cost = order_risk_evidence_receipt(kind="cost_model", payload={
        "symbol": intent["symbol"], "side": intent["side"], "quantity_shares": intent["quantity_shares"],
        "reference_price": intent["reference_price"], "estimated_total_cost": 5,
        "execution_evidence_eligible": False,
    }, source="retained_cost_fixture", passed=True)
    evidence = {"required_evidence": ["price_history", "cost_model"], "receipts": {"price_history": history, "cost_model": cost},
                "experiment_limits": {"max_order_notional_pct": 2, "max_total_exposure_pct": 10}}
    return intent, account, evidence


def _evaluate(inputs, **kwargs):
    intent, account, evidence = inputs
    engine = kwargs.pop("engine", RiskEngine())
    return engine.evaluate_order_intent(intent=intent, account_summary=account, evidence=evidence, **kwargs)


def test_candle_experiment_uses_declared_evidence_without_unrelated_frameworks():
    result = _evaluate(_inputs())
    assert result["approved"] is True
    assert result["positive_ev_qualified"] is False
    assert {item["kind"] for item in result["evidence_dependencies"]} == {"price_history", "cost_model"}
    assert not any("finrl" in item["code"] or "qlib" in item["code"] for item in result["gate_checks"])


def test_lower_exit_floor_counts_against_existing_loss_budget_before_entry():
    engine = RiskEngine(max_daily_loss_pct=.075)
    inputs = _inputs()
    assert _evaluate(inputs, engine=engine)["approved"] is True
    inputs[0]["exit_minimum_limit_price"] = 90
    result = _evaluate(inputs, engine=engine)
    assert result["approved"] is False
    assert "stop_loss_exposure" in result["blockers"]
    assert result["metrics"]["estimated_stop_loss_pct"] == pytest.approx(.105)
    assert result["metrics"]["risk_exit_price"] == 90
    assert inputs[0]["stop_loss"] == 95


@pytest.mark.parametrize("floor", [None, True, 0, -1, 96, float("nan"), "90"])
def test_invalid_exit_floor_cannot_bypass_entry_risk(floor):
    result = _evaluate(_inputs(exit_minimum_limit_price=floor))
    assert result["approved"] is False
    assert "exit_price_floor" in result["blockers"]


def test_existing_position_can_reduce_despite_loss_exceeding_exit_floor_budget():
    inputs = _inputs(side="sell", reduce_only=True, exit_minimum_limit_price=90)
    inputs[1]["positions"] = [{"symbol": "2330.TW", "quantity": 10, "market_value": 1000}]
    result = _evaluate(inputs, engine=RiskEngine(max_daily_loss_pct=.001))
    assert result["approved"] is True


@pytest.mark.parametrize("change,blocker", [
    ({"quantity_shares": 0}, "order_input"), ({"quantity_shares": -1}, "order_input"),
    ({"quantity_shares": None}, "order_input"), ({"quantity_shares": float("nan")}, "order_input"),
    ({"reference_price": float("inf")}, "order_input"), ({"reference_price": 0}, "order_input"),
    ({"stop_loss": None}, "protective_stop"), ({"stop_loss": 100}, "protective_stop"),
    ({"stop_loss": 101}, "protective_stop"), ({"stop_loss": -1}, "protective_stop"),
    ({"side": "short_sell"}, "order_input"), ({"reduce_only": True}, "reduce_only"),
])
def test_invalid_order_or_stop_never_receives_a_size_or_price_fallback(change, blocker):
    inputs = _inputs()
    inputs[0].update(change)
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert blocker in result["blockers"]


@pytest.mark.parametrize("field", ["today_pnl", "peak_equity"])
def test_missing_account_loss_state_blocks_new_exposure(field):
    inputs = _inputs()
    inputs[1].pop(field)
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert "account_loss_state" in result["blockers"]


def test_cash_uses_available_settled_capacity_and_includes_costs():
    inputs = _inputs()
    inputs[1]["available_cash"] = 1_003
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert "cash_after_costs" in result["blockers"]
    assert "cash_available" not in result["blockers"]


@pytest.mark.parametrize("change,blocker", [
    ({"today_pnl": -4000}, "daily_loss"), ({"peak_equity": 130_000}, "account_drawdown"),
    ({"total_equity": 0}, "account_state"), ({"positions": None}, "account_state"),
])
def test_authoritative_account_limits_cannot_be_replaced_by_research_passed(change, blocker):
    inputs = _inputs()
    inputs[1].update(change)
    inputs[2]["research"] = {"passed": True}
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert blocker in result["blockers"]


def test_dependencies_must_be_retained_intact_and_cost_must_match_order():
    inputs = _inputs()
    inputs[2]["receipts"]["price_history"]["payload"]["completed_bars"] = 500
    assert "evidence:price_history" in _evaluate(inputs)["blockers"]
    inputs = _inputs()
    inputs[0]["quantity_shares"] = 20
    assert "order_cost_binding" in _evaluate(inputs)["blockers"]
    inputs = _inputs()
    inputs[2]["required_evidence"].append("financial_statements")
    assert "evidence:financial_statements" in _evaluate(inputs)["blockers"]


def test_bounded_experiment_needs_an_explicit_budget_and_cannot_be_live():
    inputs = _inputs()
    inputs[2].pop("experiment_limits")
    assert "bounded_experiment" in _evaluate(inputs)["blockers"]
    assert "bounded_experiment" in _evaluate(_inputs(), mode="live", engine=RiskEngine(live_trading_enabled=True))["blockers"]
    inputs = _inputs()
    inputs[2]["experiment_limits"]["max_order_notional_pct"] = 0.5
    assert "bounded_experiment" in _evaluate(inputs)["blockers"]


def test_qualified_requires_real_matching_qualification_not_passed_flag():
    inputs = _inputs()
    inputs[2]["qualification"] = {"passed": True, "positive_ev_qualified": True, "receipt_sha256": "a" * 64}
    result = _evaluate(inputs, eligibility="qualified")
    assert result["approved"] is False
    assert "strategy_qualification" in result["blockers"]


def _reduction():
    inputs = _inputs(side="sell", quantity_shares=500, reduce_only=True, stop_loss=None)
    inputs[1].update(today_pnl=-5000, peak_equity=140_000, positions=[{
        "symbol": "2330.TW", "quantity": 500, "market_value": 50_000, "industry": "semiconductor",
    }])
    inputs[2]["receipts"].pop("price_history")
    return inputs


def test_owned_position_reduction_survives_entry_exposure_and_loss_limits():
    result = _evaluate(_reduction())
    assert result["approved"] is True
    assert result["reduce_only"] is True
    assert {item["kind"] for item in result["evidence_dependencies"]} == {"cost_model"}
    inputs = _reduction()
    inputs[0]["quantity_shares"] = 501
    assert "reduce_only" in _evaluate(inputs)["blockers"]


def test_durable_manual_stop_remains_authoritative_but_loss_stop_allows_reduction():
    control = DurableRiskControlStore(":memory:")
    engine = RiskEngine(risk_control=control)
    control.activate("account", "isolated", "loss stop", "loss_limit_monitor")
    assert "durable_risk_control" in _evaluate(_inputs(), engine=engine)["blockers"]
    assert _evaluate(_reduction(), engine=engine)["approved"] is True
    control.activate("global", "global", "operator stop", "owner")
    assert "durable_risk_control" in _evaluate(_reduction(), engine=engine)["blockers"]


def _reservation(**changes):
    return {"order_id": "resting-order", "symbol": "2330.TW", "side": "buy", "remaining_quantity": 200,
            "reservation_price": 100, "estimated_remaining_cost": 5, "industry": "semiconductor", **changes}


def test_pending_buy_reserves_cash_and_exposure_without_becoming_a_holding():
    inputs = _inputs()
    inputs[1]["available_cash"] = 20_500
    inputs[1]["open_order_reservations"] = [_reservation()]
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert "cash_after_costs" in result["blockers"]
    assert "symbol_exposure" in result["blockers"]
    assert result["metrics"]["open_buy_reserved_cash"] == 20_005
    inputs[0].update(side="sell", quantity_shares=1)
    assert "position_after_reservations" in _evaluate(inputs)["blockers"]


def test_pending_sell_reserves_owned_quantity_without_anticipating_execution():
    inputs = _reduction()
    inputs[1]["open_order_reservations"] = [_reservation(side="sell", remaining_quantity=1)]
    result = _evaluate(inputs)
    assert result["approved"] is False
    assert "position_after_reservations" in result["blockers"]
    assert result["metrics"]["symbol_sellable_quantity_after_reservations"] == 499


def test_malformed_or_duplicate_reservations_cannot_hide_commitments():
    inputs = _inputs()
    inputs[1]["open_order_reservations"] = [_reservation(), _reservation()]
    assert "open_order_reservations" in _evaluate(inputs)["blockers"]
    inputs[1]["open_order_reservations"] = [_reservation(reservation_price=None)]
    assert "open_order_reservations" in _evaluate(inputs)["blockers"]
    inputs[1]["open_order_reservations"] = [_reservation(valid=False, blockers=["open_order_stop_price_missing"])]
    assert "open_order_reservations" in _evaluate(inputs)["blockers"]


def test_malformed_cost_payload_is_rejected_without_crashing_live_policy():
    inputs = _inputs()
    inputs[2]["receipts"]["cost_model"]["payload"] = ["invalid"]
    result = _evaluate(inputs, mode="live", eligibility="qualified")
    assert result["approved"] is False
    assert "order_cost_binding" in result["blockers"]
    assert "live_cost_evidence" in result["blockers"]
