from __future__ import annotations

from open_stock_ai.research.monte_carlo import simulate_tail_risk


def test_tail_risk_report_is_reproducible_and_includes_var_cvar_stability():
    returns = [0.002 + (index % 9 - 4) * 0.0007 for index in range(80)]

    first = simulate_tail_risk(returns)
    second = simulate_tail_risk(returns)

    assert first == second
    assert first["scenario_count"] == 2000
    assert first["block_size"] == 5
    assert first["value_at_risk_loss_pct"] >= 0
    assert first["conditional_value_at_risk_loss_pct"] >= first["value_at_risk_loss_pct"]
    assert first["tail_risk_stability"]["batch_count"] == 5
    assert len(first["input_sha256"]) == 64


def test_tail_risk_report_fails_closed_with_too_few_returns():
    result = simulate_tail_risk([0.001] * 19)

    assert result["passed"] is False
    assert result["blockers"] == ["monte_carlo_insufficient_return_observations"]
