from __future__ import annotations

"""Fail-closed monitoring for feature, prediction and realised performance drift."""

import math
from typing import Any


DEFAULT_THRESHOLDS = {
    "feature_psi_max": 0.20,
    "prediction_psi_max": 0.20,
    "brier_degradation_max": 0.05,
    "hit_rate_decline_max": 0.10,
}


def evaluate_model_drift(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate a bounded, non-mutating model-monitoring receipt.

    The caller owns the activation decision.  This function reports
    ``disabled`` whenever any monitored distribution exceeds its threshold;
    the research engine consumes that result as an automatic evidence-disable
    gate, never as a trading decision.
    """

    if not isinstance(payload, dict):
        return {
            "schema_version": "open_stock_ai.model_drift_monitor.v1",
            "status": "not_configured",
            "automatic_disable": True,
            "blockers": ["model_monitoring_receipt_not_supplied"],
        }
    thresholds = {**DEFAULT_THRESHOLDS, **(payload.get("thresholds") or {})}
    baseline_features = payload.get("baseline_features") if isinstance(payload.get("baseline_features"), dict) else {}
    current_features = payload.get("current_features") if isinstance(payload.get("current_features"), dict) else {}
    feature = _feature_drift(baseline_features, current_features, float(thresholds["feature_psi_max"]))
    prediction = _distribution_drift(
        payload.get("baseline_predictions"), payload.get("current_predictions"),
        float(thresholds["prediction_psi_max"]), "prediction",
    )
    performance = _performance_drift(
        payload.get("baseline_predictions"), payload.get("baseline_outcomes"),
        payload.get("current_predictions"), payload.get("current_outcomes"), thresholds,
    )
    checks = [feature, prediction, performance]
    blockers = [item["code"] for item in checks if item.get("passed") is not True]
    return {
        "schema_version": "open_stock_ai.model_drift_monitor.v1",
        "status": "enabled" if not blockers else "disabled",
        "automatic_disable": True,
        "thresholds": thresholds,
        "checks": checks,
        "blockers": blockers,
        "execution_authority": "none",
    }


def _feature_drift(baseline: dict[str, Any], current: dict[str, Any], maximum: float) -> dict[str, Any]:
    common = sorted(set(baseline) & set(current))
    missing = sorted(set(baseline) ^ set(current))
    metrics: dict[str, float] = {}
    invalid: list[str] = []
    for name in common:
        value = _psi(_numbers(baseline[name]), _numbers(current[name]))
        if value is None:
            invalid.append(name)
        else:
            metrics[name] = round(value, 8)
    observed = max(metrics.values(), default=None)
    passed = bool(metrics) and not missing and not invalid and observed is not None and observed <= maximum
    return {
        "code": "feature_drift",
        "passed": passed,
        "maximum_psi": maximum,
        "observed_max_psi": observed,
        "feature_psi": metrics,
        "missing_or_extra_features": missing,
        "invalid_features": invalid,
    }


def _distribution_drift(baseline: Any, current: Any, maximum: float, kind: str) -> dict[str, Any]:
    observed = _psi(_numbers(baseline), _numbers(current))
    return {
        "code": f"{kind}_drift",
        "passed": observed is not None and observed <= maximum,
        "maximum_psi": maximum,
        "observed_psi": round(observed, 8) if observed is not None else None,
    }


def _performance_drift(
    baseline_predictions: Any, baseline_outcomes: Any, current_predictions: Any, current_outcomes: Any,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    base_predictions, base_outcomes = _numbers(baseline_predictions), _binary(baseline_outcomes)
    current_predictions, current_outcomes = _numbers(current_predictions), _binary(current_outcomes)
    if not _paired(base_predictions, base_outcomes) or not _paired(current_predictions, current_outcomes):
        return {"code": "performance_drift", "passed": False, "reason": "outcome_pairs_insufficient"}
    base_brier, current_brier = _brier(base_predictions, base_outcomes), _brier(current_predictions, current_outcomes)
    base_hit, current_hit = _hit_rate(base_predictions, base_outcomes), _hit_rate(current_predictions, current_outcomes)
    degradation, decline = current_brier - base_brier, base_hit - current_hit
    return {
        "code": "performance_drift",
        "passed": degradation <= float(thresholds["brier_degradation_max"]) and decline <= float(thresholds["hit_rate_decline_max"]),
        "baseline_brier": round(base_brier, 8), "current_brier": round(current_brier, 8),
        "brier_degradation": round(degradation, 8), "brier_degradation_max": float(thresholds["brier_degradation_max"]),
        "baseline_hit_rate": round(base_hit, 8), "current_hit_rate": round(current_hit, 8),
        "hit_rate_decline": round(decline, 8), "hit_rate_decline_max": float(thresholds["hit_rate_decline_max"]),
    }


def _psi(baseline: list[float], current: list[float], bins: int = 10) -> float | None:
    if len(baseline) < 20 or len(current) < 20:
        return None
    low, high = min(baseline), max(baseline)
    if high <= low:
        return 0.0 if min(current) == max(current) == low else float("inf")
    width = (high - low) / bins
    result = 0.0
    for index in range(bins):
        lower, upper = low + index * width, low + (index + 1) * width
        base_share = max(sum(lower <= value <= upper if index == bins - 1 else lower <= value < upper for value in baseline) / len(baseline), 1e-6)
        current_share = max(sum(lower <= value <= upper if index == bins - 1 else lower <= value < upper for value in current) / len(current), 1e-6)
        result += (current_share - base_share) * math.log(current_share / base_share)
    return result


def _numbers(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(number):
            return []
        result.append(number)
    return result


def _binary(values: Any) -> list[int]:
    if not isinstance(values, list) or any(value not in {0, 1, False, True} for value in values):
        return []
    return [int(bool(value)) for value in values]


def _paired(predictions: list[float], outcomes: list[int]) -> bool:
    return len(predictions) >= 20 and len(predictions) == len(outcomes) and all(0 <= item <= 1 for item in predictions)


def _brier(predictions: list[float], outcomes: list[int]) -> float:
    return sum((prediction - outcome) ** 2 for prediction, outcome in zip(predictions, outcomes)) / len(outcomes)


def _hit_rate(predictions: list[float], outcomes: list[int]) -> float:
    return sum(int((prediction >= 0.5) == bool(outcome)) for prediction, outcome in zip(predictions, outcomes)) / len(outcomes)
