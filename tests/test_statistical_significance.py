from __future__ import annotations

from open_stock_ai.research.statistical_significance import evaluate_statistical_significance
from open_stock_ai.learning.evaluation import PolicyEvaluation


def test_significance_receipt_is_deterministic_and_handles_multiple_testing():
    returns = [0.004 + (index % 5) * 0.0004 for index in range(80)]
    first = evaluate_statistical_significance(returns, candidate_count=1)
    second = evaluate_statistical_significance(returns, candidate_count=1)
    many_trials = evaluate_statistical_significance(returns, candidate_count=20)

    assert first == second
    assert first["passed"] is True
    assert first["bootstrap_sharpe_ci"]["lower"] > 0
    assert first["multiple_testing"]["adjusted_p_value"] <= many_trials["multiple_testing"]["adjusted_p_value"]
    assert many_trials["deflated_sharpe"]["expected_max_sharpe"] > first["deflated_sharpe"]["expected_max_sharpe"]


def test_significance_refuses_insufficient_or_negative_evidence():
    assert evaluate_statistical_significance([0.01])["passed"] is False
    negative = evaluate_statistical_significance([-0.004 + (index % 3) * 0.0001 for index in range(80)])
    assert negative["passed"] is False
    assert "bootstrap_sharpe_ci_not_strictly_positive" in negative["blockers"]


def test_policy_evaluation_cannot_promote_a_strategy_without_significant_returns():
    result = PolicyEvaluation().evaluate(
        {"period_returns": [-0.004 + (index % 3) * 0.0001 for index in range(80)]},
        risk_review={"approved": True},
    )

    gate = next(item for item in result["gates"] if item["code"] == "statistical_significance")
    assert gate["passed"] is False
    assert "statistical_significance" in result["blockers"]
