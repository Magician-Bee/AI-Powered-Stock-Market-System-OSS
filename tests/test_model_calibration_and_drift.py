from __future__ import annotations

from datetime import datetime, timedelta, timezone

from open_stock_ai.research.model_calibration import calibrate_rule_score, fit_empirical_score_calibration
from open_stock_ai.research.model_drift import evaluate_model_drift
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.strategy.strategy_engine import StrategyEngine
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, ResearchResult, StockRequest


def _observations(*, future: bool = False) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "score": round(0.05 + index * 0.9 / 39, 6),
            "outcome": index >= 16,
            "available_at": (start + timedelta(days=index)).isoformat(),
            "outcome_available_at": (
                start + timedelta(days=index + (100 if future and index == 39 else 1))
            ).isoformat(),
        }
        for index in range(40)
    ]


def _monitor_payload(*, shifted: bool = False, poorer_performance: bool = False) -> dict:
    baseline = [index / 40 for index in range(40)]
    outcomes = [index >= 20 for index in range(40)]
    predictions = [0.1 if not outcome else 0.9 for outcome in outcomes]
    return {
        "baseline_features": {"momentum": baseline, "liquidity": baseline},
        "current_features": {
            "momentum": [value + 10 if shifted else value for value in baseline],
            "liquidity": baseline,
        },
        "baseline_predictions": predictions,
        "baseline_outcomes": outcomes,
        "current_predictions": [0.9 if not outcome else 0.1 for outcome in outcomes] if poorer_performance else predictions,
        "current_outcomes": outcomes,
    }


def test_empirical_isotonic_calibration_produces_pit_brier_and_ece_receipt():
    receipt = fit_empirical_score_calibration(_observations(), as_of="2026-03-31T00:00:00+00:00")

    assert receipt["status"] == "calibrated"
    assert receipt["method"] == "point_in_time_isotonic_regression"
    assert receipt["brier_score"] >= 0
    assert receipt["expected_calibration_error"] >= 0
    calibrated = calibrate_rule_score(0.8, receipt)
    assert calibrated["status"] == "calibrated"
    assert calibrated["probability"] >= 0.7


def test_calibration_rejects_outcomes_unknown_at_the_historical_cutoff():
    receipt = fit_empirical_score_calibration(_observations(future=True), as_of="2026-03-31T00:00:00+00:00")

    assert receipt["status"] == "calibrated"
    early = fit_empirical_score_calibration(_observations(future=True), as_of="2026-01-10T00:00:00+00:00")
    assert early["status"] == "unavailable"
    assert "outcome_not_known_at_as_of" in early["rejected_observations"]


def test_strategy_and_risk_use_a_verified_calibrated_probability_when_supplied():
    receipt = fit_empirical_score_calibration(_observations(), as_of="2026-03-31T00:00:00+00:00")
    snapshot = MarketSnapshot(symbol="2330.TW", market="TW", price=100.0, raw={"score_calibration": receipt})
    intelligence = IntelligenceResult(
        symbol="2330.TW", market="TW", summary="calibration fixture", sentiment_score=0.7,
        fundamental_view="positive", technical_view="uptrend",
    )
    signal = StrategyEngine().generate_signal(StockRequest(symbol="2330.TW", market="TW"), snapshot, intelligence)

    assert signal.confidence_type == "calibrated_probability"
    assert signal.confidence_calibrated is True
    assert signal.decision_schema["score_calibration"]["status"] == "calibrated"
    decision = RiskEngine(min_rule_score_threshold=0.7).evaluate(
        StockRequest(symbol="2330.TW", market="TW"), signal, ResearchResult(passed=True, summary="fixture")
    )
    gate = next(item for item in decision.gate_checks if item["code"] == "rule_score_threshold")
    assert gate["passed"] is True
    assert gate["message"].startswith("Calibrated success probability")


def test_drift_monitor_auto_disables_feature_prediction_or_performance_regressions():
    stable = evaluate_model_drift(_monitor_payload())
    assert stable["status"] == "enabled"
    assert stable["automatic_disable"] is True

    shifted = evaluate_model_drift(_monitor_payload(shifted=True))
    assert shifted["status"] == "disabled"
    assert "feature_drift" in shifted["blockers"]

    poorer = evaluate_model_drift(_monitor_payload(poorer_performance=True))
    assert poorer["status"] == "disabled"
    assert "performance_drift" in poorer["blockers"]
