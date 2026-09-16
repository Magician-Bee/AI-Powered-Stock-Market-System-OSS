from __future__ import annotations

"""Deterministic, ledger-derived research performance metrics.

The replay and the advisory baseline intentionally share this implementation.
Keeping separate copies of Sharpe/drawdown formulas is a subtle way for a UI
preview to drift away from the event ledger used for evidence.  Inputs are
period returns and realised position weights only; no metric manufactures a
trade or treats an unavailable value as a successful result.
"""

from math import ceil, isfinite, sqrt
from statistics import mean, pstdev
from typing import Iterable


def calculate_performance_metrics(
    returns: Iterable[float],
    *,
    position_weights: Iterable[float] | None = None,
    periods_per_year: int = 252,
    tail_probability: float = 0.05,
) -> dict[str, object]:
    """Calculate auditable performance metrics from a sequential return path.

    ``position_weights`` must be contemporaneous with the return path and is
    used only for turnover/exposure.  When it is omitted, those two values are
    explicitly unavailable rather than inferred from returns.
    """

    values = [float(item) for item in returns]
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if not 0.0 < tail_probability <= 1.0:
        raise ValueError("tail_probability must be in (0, 1]")
    if any(not isfinite(item) for item in values):
        raise ValueError("returns must be finite")

    weights = None if position_weights is None else [float(item) for item in position_weights]
    if weights is not None and len(weights) != len(values):
        raise ValueError("position_weights must match returns length")
    if weights is not None and any(not isfinite(item) for item in weights):
        raise ValueError("position_weights must be finite")

    warnings: list[str] = []
    if not values:
        warnings.append("insufficient_return_observations")
    if weights is None:
        warnings.append("position_weights_unavailable")

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, equity / peak - 1.0)

    compounded_return_pct = (equity - 1.0) * 100.0
    cagr_pct: float | None
    if values and equity > 0.0:
        cagr_pct = ((equity ** (periods_per_year / len(values))) - 1.0) * 100.0
    else:
        cagr_pct = None
        if values:
            warnings.append("cagr_unavailable_non_positive_terminal_equity")

    volatility = pstdev(values) if len(values) >= 2 else 0.0
    sharpe = mean(values) / volatility * sqrt(periods_per_year) if volatility else None
    if sharpe is None and values:
        warnings.append("sharpe_unavailable_zero_volatility")

    downside = [min(value, 0.0) for value in values]
    downside_deviation = sqrt(sum(value * value for value in downside) / len(values)) if values else 0.0
    sortino = mean(values) / downside_deviation * sqrt(periods_per_year) if downside_deviation else None
    if sortino is None and values:
        warnings.append("sortino_unavailable_zero_downside_deviation")

    max_drawdown_pct = abs(max_drawdown) * 100.0
    calmar = (cagr_pct / max_drawdown_pct) if cagr_pct is not None and max_drawdown_pct > 0 else None
    if calmar is None and values:
        warnings.append("calmar_unavailable_zero_drawdown_or_cagr")

    gross_profit = sum(value for value in values if value > 0.0)
    gross_loss = abs(sum(value for value in values if value < 0.0))
    profit_factor = gross_profit / gross_loss if gross_loss else None
    if profit_factor is None and values:
        warnings.append("profit_factor_unavailable_no_losing_periods")

    tail_count = min(len(values), max(1, ceil(len(values) * tail_probability))) if values else 0
    tail_loss_pct = abs(mean(sorted(values)[:tail_count])) * 100.0 if tail_count else None

    exposure_pct: float | None = None
    total_turnover_pct: float | None = None
    average_turnover_pct: float | None = None
    hit_ratio_pct: float | None = None
    active_period_count = 0
    if weights is not None:
        active = [weight != 0.0 for weight in weights]
        active_period_count = sum(active)
        exposure_pct = active_period_count / len(weights) * 100.0 if weights else None
        turnover = 0.0
        previous_weight = 0.0
        for weight in weights:
            turnover += abs(weight - previous_weight)
            previous_weight = weight
        total_turnover_pct = turnover * 100.0
        average_turnover_pct = total_turnover_pct / len(weights) if weights else None
        active_returns = [value for value, is_active in zip(values, active) if is_active]
        hit_ratio_pct = (sum(value > 0.0 for value in active_returns) / len(active_returns) * 100.0) if active_returns else None
        if hit_ratio_pct is None and values:
            warnings.append("hit_ratio_unavailable_no_exposure")

    return {
        "schema_version": "open_stock_ai.performance_metrics.v1",
        "method": "sequential_ledger_return_metrics",
        "periods_per_year": periods_per_year,
        "tail_probability": tail_probability,
        "sample_size": len(values),
        "active_period_count": active_period_count,
        "compounded_return_pct": _round(compounded_return_pct),
        "cagr_pct": _round_or_none(cagr_pct),
        "sharpe": _round_or_none(sharpe),
        "sortino": _round_or_none(sortino),
        "max_drawdown_pct": _round(max_drawdown_pct),
        "calmar": _round_or_none(calmar),
        "exposure_pct": _round_or_none(exposure_pct),
        "total_turnover_pct": _round_or_none(total_turnover_pct),
        "average_turnover_pct": _round_or_none(average_turnover_pct),
        "hit_ratio_pct": _round_or_none(hit_ratio_pct),
        "profit_factor": _round_or_none(profit_factor),
        "gross_profit_return_pct": _round(gross_profit * 100.0),
        "gross_loss_return_pct": _round(gross_loss * 100.0),
        "tail_loss_pct": _round_or_none(tail_loss_pct),
        "tail_observation_count": tail_count,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _round(value: float) -> float:
    return round(value, 6)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else _round(value)
