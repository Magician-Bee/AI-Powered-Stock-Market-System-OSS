from __future__ import annotations

import numpy as np
import pytest

from open_stock_ai.research.performance_metrics import calculate_performance_metrics


def test_hand_calculated_golden_metrics_match_the_sequential_return_contract():
    # Independent arithmetic: terminal equity = 1.10 * 0.95 * 1.02 * 0.99
    # = 1.055241.  With four observations/year, CAGR is therefore 5.5241%;
    # the largest peak-to-trough loss is 5%, and only the first of two active
    # periods wins.  These fixed values catch formula drift independently of
    # the strategy replay implementation.
    result = calculate_performance_metrics(
        [0.10, -0.05, 0.02, -0.01],
        position_weights=[1.0, 1.0, 0.0, 0.0],
        periods_per_year=4,
    )

    assert result["compounded_return_pct"] == pytest.approx(5.5241)
    assert result["cagr_pct"] == pytest.approx(5.5241)
    assert result["sharpe"] == pytest.approx(0.545455)
    assert result["sortino"] == pytest.approx(1.176697)
    assert result["max_drawdown_pct"] == pytest.approx(5.0)
    assert result["calmar"] == pytest.approx(1.10482)
    assert result["exposure_pct"] == pytest.approx(50.0)
    assert result["total_turnover_pct"] == pytest.approx(200.0)
    assert result["average_turnover_pct"] == pytest.approx(50.0)
    assert result["hit_ratio_pct"] == pytest.approx(50.0)
    assert result["profit_factor"] == pytest.approx(2.0)
    assert result["tail_loss_pct"] == pytest.approx(5.0)
    assert result["tail_observation_count"] == 1


def test_metrics_match_an_independent_numpy_reference_calculation():
    """Keep the research receipt aligned with an independent numeric engine."""
    returns = np.asarray([0.03, -0.02, 0.01, -0.04, 0.025, 0.015], dtype=float)
    weights = np.asarray([0.0, 0.6, 0.6, 0.2, 0.9, 0.0], dtype=float)
    periods_per_year = 6

    result = calculate_performance_metrics(
        returns.tolist(),
        position_weights=weights.tolist(),
        periods_per_year=periods_per_year,
    )

    equity = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    drawdowns = equity / peaks - 1.0
    cagr = equity[-1] ** (periods_per_year / len(returns)) - 1.0
    volatility = np.std(returns, ddof=0)
    downside = np.minimum(returns, 0.0)
    downside_deviation = np.sqrt(np.mean(np.square(downside)))
    turnover = np.abs(np.diff(np.concatenate(([0.0], weights))))
    active_returns = returns[weights != 0.0]

    assert result["compounded_return_pct"] == pytest.approx((equity[-1] - 1.0) * 100.0, abs=1e-6)
    assert result["cagr_pct"] == pytest.approx(cagr * 100.0, abs=1e-6)
    assert result["sharpe"] == pytest.approx(np.mean(returns) / volatility * np.sqrt(periods_per_year), abs=1e-6)
    assert result["sortino"] == pytest.approx(np.mean(returns) / downside_deviation * np.sqrt(periods_per_year), abs=1e-6)
    assert result["max_drawdown_pct"] == pytest.approx(abs(np.min(drawdowns)) * 100.0, abs=1e-6)
    assert result["calmar"] == pytest.approx(cagr * 100.0 / (abs(np.min(drawdowns)) * 100.0), abs=1e-6)
    assert result["exposure_pct"] == pytest.approx(np.mean(weights != 0.0) * 100.0, abs=1e-6)
    assert result["total_turnover_pct"] == pytest.approx(np.sum(turnover) * 100.0, abs=1e-6)
    assert result["hit_ratio_pct"] == pytest.approx(np.mean(active_returns > 0.0) * 100.0, abs=1e-6)
    assert result["profit_factor"] == pytest.approx(
        np.sum(returns[returns > 0.0]) / abs(np.sum(returns[returns < 0.0])),
        abs=1e-6,
    )
    assert result["tail_loss_pct"] == pytest.approx(abs(np.min(returns)) * 100.0, abs=1e-6)


def test_metrics_do_not_hide_undefined_ratios_or_missing_exposure():
    result = calculate_performance_metrics([0.01, 0.02], periods_per_year=252)

    assert result["sortino"] is None
    assert result["profit_factor"] is None
    assert result["exposure_pct"] is None
    assert result["hit_ratio_pct"] is None
    assert "profit_factor_unavailable_no_losing_periods" in result["warnings"]
    assert "position_weights_unavailable" in result["warnings"]


def test_metrics_reject_misaligned_or_invalid_metric_inputs():
    with pytest.raises(ValueError, match="match returns length"):
        calculate_performance_metrics([0.01], position_weights=[])
    with pytest.raises(ValueError, match="tail_probability"):
        calculate_performance_metrics([0.01], tail_probability=0.0)
