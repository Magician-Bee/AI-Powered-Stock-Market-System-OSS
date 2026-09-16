"""Every-leg conservative scenario parity, not broker execution certification."""
import pytest

from open_stock_ai.research.strategy_replay import ExactStrategyReplay
from open_stock_ai.research.cost_model import UnsupportedCostSchedule
from open_stock_ai.research.candle_qualification import evaluate_candidate
from open_stock_ai.strategy.candle_candidates import CandleCandidate
from open_stock_ai.types import StockRequest
from test_candle_qualification import _rows as qualification_rows
from test_exact_strategy_replay import _rows, _ExplicitPolicyFixture


@pytest.mark.parametrize("date", ["2025-01-02", "2026-08-14"])
@pytest.mark.parametrize("side,expected", [("buy", 100.5), ("sell", 99.7)])
def test_floor_applies_to_both_sides_before_and_after_dated_rule_coverage(date, side, expected):
    replay = ExactStrategyReplay(historical_impact_sensitivity_bps=0, impact_coefficient_bps=0,
                                 slippage_bps=0, adverse_execution_floor_bps=25)
    price, receipt = replay._fill_price(reference_price=100, side=side, quantity=10, volume=100000,
        row={"venue":"TWSE", "product_type":"stock", "timestamp":date,
             "spread_bps":0, "realized_volatility_bps":0, "adv_volume_shares":100000})
    assert price == expected
    assert receipt["total_slippage_bps"] >= 25
    assert receipt["adverse_execution_floor_bps"] == 25
    assert receipt["adverse_floor_tick_policy"] == "shared_taiwan_equity_tick_adverse_rounding"
    assert receipt["execution_evidence_eligible"] is False
    assert receipt["calibration_execution_evidence_eligible"] is False
    assert receipt["calibration_status"] == "execution_scenario_adverse_floor"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_floor_never_reduces_larger_observed_spread_or_liquidity_impact(side):
    inputs = dict(reference_price=100, side=side, quantity=500, volume=1000,
                  row={"venue":"TWSE", "product_type":"stock", "timestamp":"2026-08-14",
                       "spread_bps":200, "realized_volatility_bps":100, "adv_volume_shares":10000})
    original, _ = ExactStrategyReplay()._fill_price(**inputs)
    bounded, receipt = ExactStrategyReplay(adverse_execution_floor_bps=25)._fill_price(**inputs)
    assert bounded >= original if side == "buy" else bounded <= original
    assert receipt["total_slippage_bps"] > 25


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 10000, "25"])
def test_invalid_floor_rejected_before_any_evaluation(value):
    with pytest.raises(ValueError, match="invalid_adverse_execution_floor"):
        ExactStrategyReplay(adverse_execution_floor_bps=value)


def test_floor_is_version_bound_and_cannot_create_zero_price_sell():
    plain = ExactStrategyReplay()
    bounded = ExactStrategyReplay(adverse_execution_floor_bps=25)
    assert plain.strategy_version_hash() != bounded.strategy_version_hash()
    assert bounded.strategy_version_manifest()["replay_configuration"]["adverse_execution_floor_bps"] == 25
    assert "execution/taiwan_market_rules.py" in bounded.strategy_version_manifest()["replay_sources"]
    with pytest.raises(UnsupportedCostSchedule, match="no_positive_executable_price"):
        bounded._fill_price(reference_price=.01, side="sell", quantity=10, volume=100000,
            row={"venue":"TWSE", "product_type":"stock", "timestamp":"2026-08-14",
                 "spread_bps":0, "realized_volatility_bps":0, "adv_volume_shares":100000})


def test_entry_and_exit_ledgers_charge_floor_and_remain_cash_quantity_reconciled():
    rows = _rows(9)
    for row in rows:
        row.update(open=100.,high=100.,low=100.,close=100.,volume=100000,spread_bps=0.,
                   broker_commission_bps=0.,broker_minimum_commission_twd=0.)
    strategy = _ExplicitPolicyFixture({rows[3]["timestamp"]: {"action":"buy","position_size_pct":10,"stop_loss":90},
                                       rows[5]["timestamp"]: {"action":"sell","position_size_pct":100}})
    replay = ExactStrategyReplay(strategy=strategy,initial_cash=10000,feature_lookback=3,
                                 impact_coefficient_bps=0,slippage_bps=0,adverse_execution_floor_bps=25)
    normalized, blockers = replay._normalize_rows(rows)
    assert not blockers
    result = replay._replay_slice(StockRequest(symbol="2330.TW"),normalized,3,9)
    assert [fill["side"] for fill in result["fill_ledger"]] == ["buy","sell"]
    assert [fill["fill_price"] for fill in result["fill_ledger"]] == [100.5,99.7]
    assert result["accounting_verified"] and result["position_ledger"][-1]["quantity"] == 0
    assert result["cash_ledger"][-1]["balance"] < 10000


def test_qualification_retains_floor_protocol_and_separate_replay_identity():
    kwargs = {"data_evidence":{"source_kind":"exchange_official","source_id":"offline_fixture"}}
    plain = evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),qualification_rows(),**kwargs)
    bounded = evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),qualification_rows(),
                                  cost_assumptions={"adverse_execution_floor_bps":25},**kwargs)
    assert bounded["evaluation_manifest"]["cost_assumptions"]["adverse_execution_floor_bps"] == 25
    assert bounded["replay_version_hash"] != plain["replay_version_hash"]
    assert not bounded["positive_ev_qualified"]
