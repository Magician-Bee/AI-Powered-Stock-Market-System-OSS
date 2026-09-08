from __future__ import annotations

"""Point-in-time benchmark, cash-rate and active-return research receipts."""

import hashlib
import json
from datetime import datetime, timezone
from math import isfinite, sqrt
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping


def evaluate_benchmark_metrics(
    strategy_returns: Iterable[float],
    benchmark_observations: Iterable[Mapping[str, Any]] | None,
    risk_free_observations: Iterable[Mapping[str, Any]] | None,
    *,
    evaluation_timestamps: Iterable[str] | None,
    periods_per_year: int = 252,
) -> dict[str, object]:
    """Calculate alpha, beta and information ratio from aligned PIT inputs.

    A same-symbol close series is not an acceptable implicit benchmark.  Each
    return must instead identify its benchmark/cash-rate source, version and
    availability time.  Missing or later-known inputs produce an explicit
    unavailable receipt rather than a fabricated zero risk-free rate.
    """

    strategy = [float(value) for value in strategy_returns]
    benchmark_rows = list(benchmark_observations or [])
    cash_rows = list(risk_free_observations or [])
    timestamps = list(evaluation_timestamps or [])
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if any(not isfinite(value) for value in strategy):
        raise ValueError("strategy_returns must be finite")
    blockers: list[str] = []
    if len(strategy) < 2:
        blockers.append("benchmark_metrics_insufficient_strategy_returns")
    if len(timestamps) != len(strategy):
        blockers.append("benchmark_metrics_evaluation_timestamps_misaligned")
    if len(benchmark_rows) != len(strategy):
        blockers.append("benchmark_observations_misaligned")
    if len(cash_rows) != len(strategy):
        blockers.append("risk_free_observations_misaligned")
    if blockers:
        return _unavailable(strategy, periods_per_year, blockers)

    benchmark_values, benchmark_identity, benchmark_blockers = _normalize_observations(
        benchmark_rows,
        timestamps,
        identity_key="benchmark_id",
        kind="benchmark",
    )
    cash_values, cash_identity, cash_blockers = _normalize_observations(
        cash_rows,
        timestamps,
        identity_key="cash_rate_id",
        kind="risk_free",
    )
    blockers.extend(benchmark_blockers)
    blockers.extend(cash_blockers)
    if blockers:
        return _unavailable(strategy, periods_per_year, list(dict.fromkeys(blockers)))

    benchmark_excess = [value - cash for value, cash in zip(benchmark_values, cash_values)]
    strategy_excess = [value - cash for value, cash in zip(strategy, cash_values)]
    benchmark_variance = mean([(value - mean(benchmark_excess)) ** 2 for value in benchmark_excess])
    if benchmark_variance <= 0.0:
        return _unavailable(strategy, periods_per_year, ["benchmark_variance_zero"])
    covariance = mean(
        [
            (left - mean(strategy_excess)) * (right - mean(benchmark_excess))
            for left, right in zip(strategy_excess, benchmark_excess)
        ]
    )
    beta = covariance / benchmark_variance
    alpha_period = mean(strategy_excess) - beta * mean(benchmark_excess)
    active_returns = [left - right for left, right in zip(strategy, benchmark_values)]
    tracking_error = pstdev(active_returns)
    information_ratio = (
        mean(active_returns) / tracking_error * sqrt(periods_per_year)
        if tracking_error > 0.0
        else None
    )
    alignment_document = {
        "timestamps": [_timestamp(value).isoformat() for value in timestamps],
        "strategy_returns": strategy,
        "benchmark_observations": benchmark_rows,
        "risk_free_observations": cash_rows,
        "periods_per_year": periods_per_year,
    }
    alignment_sha256 = hashlib.sha256(
        json.dumps(alignment_document, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "open_stock_ai.benchmark_metrics.v1",
        "method": "point_in_time_aligned_strategy_benchmark_cash_rate_metrics",
        "passed": information_ratio is not None,
        "alignment_verified": True,
        "alignment_sha256": alignment_sha256,
        "sample_size": len(strategy),
        "periods_per_year": periods_per_year,
        "benchmark": benchmark_identity,
        "risk_free": cash_identity,
        "strategy_return_pct": _compound_return(strategy),
        "benchmark_return_pct": _compound_return(benchmark_values),
        "risk_free_return_pct": _compound_return(cash_values),
        "excess_return_pct": _round((_compound(strategy) - _compound(benchmark_values)) * 100.0),
        "beta": _round(beta),
        "alpha_annualized_pct": _round(alpha_period * periods_per_year * 100.0),
        "tracking_error_annualized_pct": _round(tracking_error * sqrt(periods_per_year) * 100.0),
        "information_ratio": _round_or_none(information_ratio),
        "blockers": [] if information_ratio is not None else ["information_ratio_unavailable_zero_tracking_error"],
    }


def _normalize_observations(
    rows: list[Mapping[str, Any]],
    timestamps: list[str],
    *,
    identity_key: str,
    kind: str,
) -> tuple[list[float], dict[str, str], list[str]]:
    values: list[float] = []
    identities: list[dict[str, str]] = []
    blockers: list[str] = []
    for index, (row, timestamp) in enumerate(zip(rows, timestamps)):
        if not isinstance(row, Mapping):
            blockers.append(f"{kind}_observation_invalid:{index}")
            continue
        try:
            observed_at = _timestamp(row.get("timestamp"))
            available_at = _timestamp(row.get("available_at"))
            evaluation_at = _timestamp(timestamp)
            value = float(row.get("return"))
        except (TypeError, ValueError):
            blockers.append(f"{kind}_observation_temporal_or_return_missing:{index}")
            continue
        if not isfinite(value):
            blockers.append(f"{kind}_observation_return_not_finite:{index}")
        if observed_at != evaluation_at:
            blockers.append(f"{kind}_observation_timestamp_misaligned:{index}")
        if available_at > evaluation_at:
            blockers.append(f"{kind}_observation_available_after_evaluation:{index}")
        identity = {
            key: str(row.get(key) or "").strip()
            for key in (identity_key, "source_id", "dataset_id", "revision_id")
        }
        if any(not value for value in identity.values()):
            blockers.append(f"{kind}_observation_lineage_missing:{index}")
        values.append(value)
        identities.append(identity)
    if blockers:
        return values, {}, blockers
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        return values, {}, [f"{kind}_identity_changes_within_replay"]
    return values, first, []


def _unavailable(
    strategy_returns: list[float], periods_per_year: int, blockers: list[str]
) -> dict[str, object]:
    return {
        "schema_version": "open_stock_ai.benchmark_metrics.v1",
        "method": "point_in_time_aligned_strategy_benchmark_cash_rate_metrics",
        "passed": False,
        "alignment_verified": False,
        "sample_size": len(strategy_returns),
        "periods_per_year": periods_per_year,
        "benchmark": {},
        "risk_free": {},
        "strategy_return_pct": _compound_return(strategy_returns) if strategy_returns else None,
        "benchmark_return_pct": None,
        "risk_free_return_pct": None,
        "excess_return_pct": None,
        "beta": None,
        "alpha_annualized_pct": None,
        "tracking_error_annualized_pct": None,
        "information_ratio": None,
        "blockers": list(dict.fromkeys(blockers)),
    }


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _compound(values: list[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= 1.0 + value
    return equity


def _compound_return(values: list[float]) -> float:
    return _round((_compound(values) - 1.0) * 100.0)


def _round(value: float) -> float:
    return round(value, 6)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else _round(value)
