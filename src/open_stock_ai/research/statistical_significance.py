from __future__ import annotations

"""Deterministic statistical evidence for research promotion.

These statistics are deliberately a gate, not a prediction.  They make the
number of observations, bootstrap seed, competing hypotheses and correction
method part of the evidence receipt so a later promotion review can reproduce
the result.
"""

import hashlib
import json
from math import sqrt
from random import Random
from statistics import NormalDist, mean, pstdev
from typing import Iterable


def evaluate_statistical_significance(
    returns: Iterable[float],
    *,
    periods_per_year: int = 252,
    bootstrap_samples: int = 2_000,
    candidate_count: int = 1,
    confidence_level: float = 0.95,
) -> dict[str, object]:
    values = [float(item) for item in returns]
    if periods_per_year <= 0 or bootstrap_samples < 100 or candidate_count < 1:
        raise ValueError("invalid significance evaluation configuration")
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be in (0, 1)")

    sample_size = len(values)
    if sample_size < 2:
        return _unavailable(sample_size, candidate_count, "insufficient_return_observations")
    volatility = pstdev(values)
    if volatility == 0.0:
        return _unavailable(sample_size, candidate_count, "zero_return_volatility")

    sharpe = mean(values) / volatility * sqrt(periods_per_year)
    daily_t = mean(values) / (volatility / sqrt(sample_size))
    normal = NormalDist()
    unadjusted_p_value = 1.0 - normal.cdf(daily_t)
    holm_adjusted_p_value = min(1.0, unadjusted_p_value * candidate_count)
    seed_material = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    bootstrap_seed = int(hashlib.sha256(seed_material).hexdigest()[:16], 16)
    bootstrap_sharpes = _bootstrap_sharpes(values, bootstrap_samples, periods_per_year, bootstrap_seed)
    alpha = (1.0 - confidence_level) / 2.0
    lower = _quantile(bootstrap_sharpes, alpha)
    upper = _quantile(bootstrap_sharpes, 1.0 - alpha)

    # Bailey & López de Prado's DSR adjusts observed Sharpe for the expected
    # maximum among competing trials.  The sampling-error approximation is
    # explicit here and is conservative for this lightweight local runtime.
    sharpe_standard_error = sqrt((1.0 + 0.5 * sharpe * sharpe) / sample_size)
    expected_max_sharpe = _expected_max_sharpe(candidate_count, sharpe_standard_error, normal)
    deflated_sharpe_probability = normal.cdf((sharpe - expected_max_sharpe) / sharpe_standard_error)
    passed = (
        sample_size >= 40
        and lower > 0.0
        and holm_adjusted_p_value <= 0.05
        and deflated_sharpe_probability >= 0.95
    )
    blockers = [] if passed else [
        code
        for code, condition in (
            ("significance_sample_size_below_40", sample_size < 40),
            ("bootstrap_sharpe_ci_not_strictly_positive", lower <= 0.0),
            ("holm_adjusted_p_value_above_0_05", holm_adjusted_p_value > 0.05),
            ("deflated_sharpe_probability_below_0_95", deflated_sharpe_probability < 0.95),
        )
        if condition
    ]
    return {
        "schema_version": "open_stock_ai.statistical_significance.v1",
        "method": "iid_bootstrap_sharpe_with_deflated_sharpe_and_holm_bonferroni",
        "sample_size": sample_size,
        "periods_per_year": periods_per_year,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": bootstrap_seed,
        "confidence_level": confidence_level,
        "candidate_count": candidate_count,
        "sharpe": round(sharpe, 6),
        "bootstrap_sharpe_ci": {"lower": round(lower, 6), "upper": round(upper, 6)},
        "unadjusted_p_value": round(unadjusted_p_value, 8),
        "multiple_testing": {
            "method": "holm_bonferroni_single_family",
            "adjusted_p_value": round(holm_adjusted_p_value, 8),
        },
        "deflated_sharpe": {
            "expected_max_sharpe": round(expected_max_sharpe, 6),
            "probability": round(deflated_sharpe_probability, 8),
        },
        "passed": passed,
        "blockers": blockers,
    }


def _bootstrap_sharpes(values: list[float], count: int, periods: int, seed: int) -> list[float]:
    random = Random(seed)
    outcomes: list[float] = []
    for _ in range(count):
        sample = [values[random.randrange(len(values))] for _ in values]
        deviation = pstdev(sample)
        outcomes.append(0.0 if deviation == 0.0 else mean(sample) / deviation * sqrt(periods))
    return sorted(outcomes)


def _quantile(values: list[float], probability: float) -> float:
    index = max(0, min(len(values) - 1, round((len(values) - 1) * probability)))
    return values[index]


def _expected_max_sharpe(count: int, standard_error: float, normal: NormalDist) -> float:
    if count == 1:
        return 0.0
    gamma = 0.5772156649
    first = normal.inv_cdf(1.0 - 1.0 / count)
    second = normal.inv_cdf(1.0 - 1.0 / (count * 2.718281828))
    return standard_error * ((1.0 - gamma) * first + gamma * second)


def _unavailable(sample_size: int, candidate_count: int, blocker: str) -> dict[str, object]:
    return {
        "schema_version": "open_stock_ai.statistical_significance.v1",
        "method": "iid_bootstrap_sharpe_with_deflated_sharpe_and_holm_bonferroni",
        "sample_size": sample_size,
        "candidate_count": candidate_count,
        "passed": False,
        "blockers": [blocker],
    }
