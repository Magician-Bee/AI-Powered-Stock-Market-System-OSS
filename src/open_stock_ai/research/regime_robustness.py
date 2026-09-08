from __future__ import annotations

"""Point-in-time regime robustness receipts for out-of-sample research.

Regime labels are calculated from each observation and its preceding history.
They never use a full-sample percentile, which would leak later market state
into an earlier out-of-sample label.
"""

import hashlib
import json
from datetime import datetime, timezone
from math import isfinite
from statistics import median, pstdev
from typing import Any, Iterable

from .performance_metrics import calculate_performance_metrics


REQUIRED_REGIMES = ("bull", "bear", "high_volatility", "low_liquidity", "earnings")


def evaluate_regime_robustness(
    returns: Iterable[float],
    observations: Iterable[dict[str, Any]],
    *,
    position_weights: Iterable[float] | None = None,
    min_samples_per_regime: int = 5,
    trend_window: int = 20,
    state_window: int = 10,
    max_regime_drawdown_pct: float = 20.0,
) -> dict[str, object]:
    """Create independent, ledger-derived OOS metrics for required regimes.

    Observations must align one-to-one with period returns and represent only
    information available at that period's decision timestamp.  A missing or
    future-dated earnings marker remains unavailable; it is not assumed false.
    """

    values = [float(item) for item in returns]
    rows = [dict(item) for item in observations]
    weights = None if position_weights is None else [float(item) for item in position_weights]
    if min_samples_per_regime < 1 or trend_window < 2 or state_window < 2:
        raise ValueError("regime robustness windows and minimum samples must be positive")
    if len(values) != len(rows):
        return _unavailable(
            values,
            _hash(values),
            min_samples_per_regime,
            trend_window,
            state_window,
            ["returns_observations_alignment_missing"],
        )
    if weights is not None and len(weights) != len(values):
        raise ValueError("position_weights must match returns")
    if any(not isfinite(value) for value in values):
        raise ValueError("returns must be finite")

    normalized, blockers = _normalize_observations(rows)
    period_returns_hash = _hash(values)
    if blockers:
        return _unavailable(values, period_returns_hash, min_samples_per_regime, trend_window, state_window, blockers)

    memberships = _classify(normalized, trend_window=trend_window, state_window=state_window)
    regimes: dict[str, dict[str, object]] = {}
    for name in REQUIRED_REGIMES:
        indices = [index for index, labels in enumerate(memberships) if name in labels]
        regime_returns = [values[index] for index in indices]
        regime_weights = None if weights is None else [weights[index] for index in indices]
        metrics = calculate_performance_metrics(regime_returns, position_weights=regime_weights)
        coverage = len(indices) >= min_samples_per_regime
        metrics_passed = (
            metrics["sharpe"] is not None
            and float(metrics["sharpe"]) >= 0.0
            and float(metrics["max_drawdown_pct"]) <= max_regime_drawdown_pct
        )
        regime_blockers: list[str] = []
        if not coverage:
            regime_blockers.append("insufficient_oos_regime_samples")
        if coverage and not metrics_passed:
            regime_blockers.append("regime_performance_threshold_not_met")
        regimes[name] = {
            "regime": name,
            "sample_size": len(indices),
            "return_indices": indices,
            "timestamps": [normalized[index]["timestamp"] for index in indices],
            "performance_metrics": metrics,
            "coverage_passed": coverage,
            "metrics_passed": metrics_passed,
            "passed": coverage and metrics_passed,
            "blockers": regime_blockers,
        }

    coverage_passed = all(item["coverage_passed"] is True for item in regimes.values())
    performance_passed = all(item["metrics_passed"] is True for item in regimes.values())
    return {
        "schema_version": "open_stock_ai.regime_robustness.v1",
        "method": "point_in_time_regime_stratified_oos_ledger_metrics",
        "required_regimes": list(REQUIRED_REGIMES),
        "period_returns_hash": period_returns_hash,
        "sample_size": len(values),
        "min_samples_per_regime": min_samples_per_regime,
        "trend_window": trend_window,
        "state_window": state_window,
        "max_regime_drawdown_pct": max_regime_drawdown_pct,
        "regimes": regimes,
        "memberships": [list(labels) for labels in memberships],
        "coverage_passed": coverage_passed,
        "performance_passed": performance_passed,
        "passed": coverage_passed and performance_passed,
        "blockers": [
            f"regime:{name}:{blocker}"
            for name, item in regimes.items()
            for blocker in item["blockers"]
        ],
        "point_in_time_verified": True,
    }


def _normalize_observations(rows: list[dict[str, Any]]) -> tuple[list[dict[str, object]], list[str]]:
    normalized: list[dict[str, object]] = []
    blockers: list[str] = []
    previous_time: datetime | None = None
    for index, source in enumerate(rows):
        try:
            timestamp = _timestamp(source.get("timestamp"))
            close = float(source.get("close"))
            volume = float(source.get("volume"))
        except (TypeError, ValueError):
            blockers.append(f"invalid_observation:{index}")
            continue
        if close <= 0.0 or volume < 0.0:
            blockers.append(f"invalid_market_state:{index}")
            continue
        if previous_time is not None and timestamp <= previous_time:
            blockers.append("observations_not_strictly_ordered")
        previous_time = timestamp
        earnings, earnings_blocker = _earnings_available(source, timestamp)
        if earnings_blocker:
            blockers.append(f"earnings_temporal_contract:{index}:{earnings_blocker}")
        normalized.append({"timestamp": timestamp.isoformat(), "close": close, "volume": volume, "earnings": earnings})
    if len(normalized) != len(rows):
        blockers.append("incomplete_regime_observations")
    return normalized, list(dict.fromkeys(blockers))


def _earnings_available(source: dict[str, Any], timestamp: datetime) -> tuple[bool, str | None]:
    marker = source.get("earnings_event")
    if marker is None:
        marker = source.get("earnings")
    if marker is None:
        return False, None
    if isinstance(marker, bool):
        return False, "boolean_marker_requires_available_at"
    if not isinstance(marker, dict):
        return False, "invalid_marker"
    try:
        available_at = _timestamp(marker.get("available_at"))
    except (TypeError, ValueError):
        return False, "available_at_missing"
    if available_at > timestamp:
        return False, "available_after_decision_time"
    event_type = str(marker.get("event_type") or marker.get("type") or "earnings").lower()
    return event_type in {"earnings", "earnings_release", "financial_results"}, None


def _classify(rows: list[dict[str, object]], *, trend_window: int, state_window: int) -> list[tuple[str, ...]]:
    labels: list[tuple[str, ...]] = []
    rolling_volatility: list[float | None] = []
    for index, row in enumerate(rows):
        current: list[str] = []
        trend_prices = [float(item["close"]) for item in rows[max(0, index - trend_window + 1) : index + 1]]
        if len(trend_prices) == trend_window:
            trend_return = trend_prices[-1] / trend_prices[0] - 1.0
            if trend_return > 0.0:
                current.append("bull")
            elif trend_return < 0.0:
                current.append("bear")

        state_rows = rows[max(0, index - state_window + 1) : index + 1]
        close_returns = [float(state_rows[item]["close"]) / float(state_rows[item - 1]["close"]) - 1.0 for item in range(1, len(state_rows))]
        volatility = pstdev(close_returns) if len(close_returns) >= 2 else None
        rolling_volatility.append(volatility)
        historical_volatility = [item for item in rolling_volatility[:index] if item is not None]
        if volatility is not None and len(historical_volatility) >= state_window - 1 and volatility >= median(historical_volatility):
            current.append("high_volatility")

        historical_volumes = [float(item["volume"]) for item in rows[max(0, index - state_window) : index]]
        if len(historical_volumes) >= state_window - 1 and float(row["volume"]) < median(historical_volumes):
            current.append("low_liquidity")
        if row["earnings"] is True:
            current.append("earnings")
        labels.append(tuple(current))
    return labels


def _unavailable(values: list[float], period_returns_hash: str, min_samples: int, trend_window: int, state_window: int, blockers: list[str]) -> dict[str, object]:
    return {
        "schema_version": "open_stock_ai.regime_robustness.v1",
        "method": "point_in_time_regime_stratified_oos_ledger_metrics",
        "required_regimes": list(REQUIRED_REGIMES),
        "period_returns_hash": period_returns_hash,
        "sample_size": len(values),
        "min_samples_per_regime": min_samples,
        "trend_window": trend_window,
        "state_window": state_window,
        "regimes": {},
        "memberships": [],
        "coverage_passed": False,
        "performance_passed": False,
        "passed": False,
        "blockers": list(dict.fromkeys(blockers)),
        "point_in_time_verified": False,
    }


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _hash(values: list[float]) -> str:
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
