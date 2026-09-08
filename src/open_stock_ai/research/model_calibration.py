from __future__ import annotations

"""Point-in-time empirical calibration for otherwise advisory rule scores."""

import hashlib
import json
import math
from datetime import datetime
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def fit_empirical_score_calibration(
    observations: list[dict[str, Any]], *, as_of: str, minimum_samples: int = 20
) -> dict[str, Any]:
    """Fit an isotonic probability mapping using only outcomes known at ``as_of``.

    A score is not allowed to become a probability merely because it is between
    zero and one.  Every calibration observation declares both the score's
    availability and its realised-outcome availability; late outcomes are
    excluded instead of being silently used as historical knowledge.
    """

    cutoff = _timestamp(as_of)
    eligible: list[tuple[float, int]] = []
    rejected: list[str] = []
    for item in observations:
        if not isinstance(item, dict):
            rejected.append("observation_not_mapping")
            continue
        score = _number(item.get("score"))
        outcome = item.get("outcome")
        available_at = item.get("available_at")
        outcome_available_at = item.get("outcome_available_at")
        if score is None or score < -1 or score > 1 or outcome not in {0, 1, False, True}:
            rejected.append("observation_value_invalid")
            continue
        try:
            known_at = max(_timestamp(str(available_at)), _timestamp(str(outcome_available_at)))
        except (TypeError, ValueError):
            rejected.append("observation_timestamp_invalid")
            continue
        if known_at > cutoff:
            rejected.append("outcome_not_known_at_as_of")
            continue
        eligible.append((abs(score), int(bool(outcome))))
    eligible.sort(key=lambda item: item[0])
    if len(eligible) < minimum_samples or len({outcome for _, outcome in eligible}) < 2:
        return _unavailable(as_of, len(eligible), rejected, minimum_samples)

    blocks = _pav(eligible)
    predictions = [_predict(abs(score), blocks) for score, _ in eligible]
    outcomes = [outcome for _, outcome in eligible]
    report = {
        "schema_version": "open_stock_ai.empirical_score_calibration.v1",
        "status": "calibrated",
        "method": "point_in_time_isotonic_regression",
        "as_of": as_of,
        "minimum_samples": minimum_samples,
        "sample_count": len(eligible),
        "positive_count": sum(outcomes),
        "score_semantics": "absolute_rule_score_to_empirical_success_probability",
        "blocks": blocks,
        "brier_score": round(sum((prediction - outcome) ** 2 for prediction, outcome in zip(predictions, outcomes)) / len(outcomes), 8),
        "expected_calibration_error": round(_ece(predictions, outcomes), 8),
        "rejected_observations": sorted(set(rejected)),
    }
    report["input_sha256"] = _sha256({"as_of": as_of, "observations": observations, "minimum_samples": minimum_samples})
    report["receipt_id"] = f"CAL-{_sha256(report)[:20]}"
    return report


def calibrate_rule_score(score: float | None, receipt: dict[str, Any]) -> dict[str, Any]:
    """Apply a verified empirical receipt without changing a signal's direction."""

    if score is None or receipt.get("status") != "calibrated" or not _valid_receipt(receipt):
        return {
            "schema_version": "open_stock_ai.calibrated_score.v1",
            "status": "unavailable",
            "probability": None,
            "reason": "calibration_receipt_unavailable_or_invalid",
        }
    value = _number(score)
    if value is None or value < -1 or value > 1:
        return {
            "schema_version": "open_stock_ai.calibrated_score.v1",
            "status": "unavailable",
            "probability": None,
            "reason": "rule_score_invalid",
        }
    probability = _predict(abs(value), receipt.get("blocks") or [])
    return {
        "schema_version": "open_stock_ai.calibrated_score.v1",
        "status": "calibrated",
        "probability": round(probability, 8),
        "calibration_receipt_id": receipt.get("receipt_id"),
        "calibration_input_sha256": receipt.get("input_sha256"),
        "method": receipt.get("method"),
    }


def _pav(values: list[tuple[float, int]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for score, outcome in values:
        blocks.append({"min_score": score, "max_score": score, "sum": outcome, "count": 1})
        while len(blocks) > 1 and blocks[-2]["sum"] / blocks[-2]["count"] > blocks[-1]["sum"] / blocks[-1]["count"]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append({
                "min_score": left["min_score"], "max_score": right["max_score"],
                "sum": left["sum"] + right["sum"], "count": left["count"] + right["count"],
            })
    return [
        {
            "min_score": round(block["min_score"], 8), "max_score": round(block["max_score"], 8),
            "probability": round(block["sum"] / block["count"], 8), "count": block["count"],
        }
        for block in blocks
    ]


def _predict(score: float, blocks: list[dict[str, Any]]) -> float:
    if not blocks:
        raise ValueError("calibration_blocks_required")
    for block in blocks:
        if score <= float(block["max_score"]):
            return float(block["probability"])
    return float(blocks[-1]["probability"])


def _ece(predictions: list[float], outcomes: list[int], bins: int = 10) -> float:
    total = len(outcomes)
    error = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        members = [
            position for position, prediction in enumerate(predictions)
            if lower <= prediction and (prediction <= upper if index == bins - 1 else prediction < upper)
        ]
        if not members:
            continue
        confidence = sum(predictions[position] for position in members) / len(members)
        accuracy = sum(outcomes[position] for position in members) / len(members)
        error += len(members) / total * abs(confidence - accuracy)
    return error


def _unavailable(as_of: str, sample_count: int, rejected: list[str], minimum_samples: int) -> dict[str, Any]:
    return {
        "schema_version": "open_stock_ai.empirical_score_calibration.v1",
        "status": "unavailable",
        "method": "point_in_time_isotonic_regression",
        "as_of": as_of,
        "minimum_samples": minimum_samples,
        "sample_count": sample_count,
        "rejected_observations": sorted(set(rejected)),
        "blockers": ["calibration_samples_or_outcome_classes_insufficient"],
    }


def _valid_receipt(receipt: dict[str, Any]) -> bool:
    input_hash = str(receipt.get("input_sha256") or "")
    return len(input_hash) == 64 and bool(receipt.get("receipt_id")) and isinstance(receipt.get("blocks"), list)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
