from __future__ import annotations

"""Fail-closed empirical target and stop calibration.

An advisory quote must never turn current access to a historical chart into a
claim about a historically known target.  This module only accepts the
immutable point-in-time replay rows produced by :mod:`pit_dataset`, filters
them to the declared as-of time, and records every input that influenced a
price level.  Ordinary live snapshots therefore deliberately return no target
or stop until a certified calibration dataset is supplied.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean
from typing import Any

from open_stock_ai.types import Horizon, IntelligenceResult, MarketSnapshot
from open_stock_ai.research.pit_dataset import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    verify_dataset_materialization,
)


SCHEMA_VERSION = "open_stock_ai.target_stop_calibration_receipt.v1"
METHOD_ID = "point_in_time_empirical_horizon_quantiles_atr_liquidity_forecast.v1"
PRICE_METHOD_ID = "empirical_pit_target_stop_calibration.v1"
_HORIZON_BARS: dict[Horizon, int] = {
    "intraday": 1,
    "swing": 5,
    "weekly": 10,
    "monthly": 20,
}


@dataclass(frozen=True)
class TargetStopCalibration:
    target_price: float | None
    stop_loss: float | None
    receipt: dict[str, Any]

    @property
    def calibrated(self) -> bool:
        return self.receipt.get("status") == "calibrated"


def calibrate_target_stop(
    *,
    snapshot: MarketSnapshot,
    intelligence: IntelligenceResult,
    direction: str,
    horizon: Horizon,
) -> TargetStopCalibration:
    """Return empirical levels, or an auditable unavailable receipt.

    ``direction`` is intentionally explicit (``long`` or ``short``).  The
    result is not a fixed percentage conversion: horizon returns are sampled
    from PIT rows with comparable realised liquidity; the price distribution
    is then adjusted by the observed ATR/return dispersion and a forecast
    score chooses the empirical quantile.  Missing or contradictory forecast
    evidence is a reason to withhold the levels, not to silently substitute a
    rule of thumb.
    """

    entry_price = _number(snapshot.price)
    raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
    manifest = raw.get("point_in_time_dataset_manifest")
    rows = raw.get("point_in_time_dataset")
    features = raw.get("point_in_time_dataset_features")
    forecast = _forecast_projection(intelligence)
    horizon_bars = _HORIZON_BARS[horizon]
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD_ID,
        "status": "unavailable",
        "direction": direction,
        "horizon": horizon,
        "horizon_bars": horizon_bars,
        "dataset_manifest_hash": (
            str(manifest.get("manifest_hash")) if isinstance(manifest, dict) else None
        ),
        "as_of": raw.get("as_of"),
        "forecast_evidence": forecast,
        "blockers": [],
    }
    blockers: list[str] = receipt["blockers"]
    if direction not in {"long", "short"}:
        blockers.append("target_stop_direction_not_executable")
    if entry_price is None or entry_price <= 0:
        blockers.append("target_stop_entry_price_unavailable")
    if not _manifest_is_eligible(manifest):
        blockers.append("target_stop_pit_manifest_not_execution_eligible")
    if not isinstance(rows, list):
        blockers.append("target_stop_pit_rows_missing")
    if not isinstance(features, list):
        blockers.append("target_stop_pit_features_missing")
    if not _forecast_supports_direction(forecast, direction):
        blockers.append("target_stop_forecast_distribution_missing_or_conflicting")
    if blockers:
        return _unavailable(receipt)

    integrity = verify_dataset_materialization(
        manifest,
        features=features,
        replay_rows=rows,
    )
    receipt["dataset_integrity"] = integrity
    if integrity.get("passed") is not True:
        blockers.extend(integrity.get("errors") or ["target_stop_pit_integrity_failed"])
        return _unavailable(receipt)

    normalized, rejected = _eligible_rows(rows, raw.get("as_of"))
    receipt["source_row_count"] = len(rows)
    receipt["eligible_row_count"] = len(normalized)
    receipt["rejected_row_count"] = rejected
    # A supplied PIT dataset is an evidence object, not a best-effort chart.
    # Ignoring a late/backfilled row would conceal a leakage defect and let a
    # caller retain a deceptively clean calibration subset.
    if rejected:
        blockers.append("target_stop_pit_row_temporal_contract_invalid")
        return _unavailable(receipt)
    if len(normalized) <= horizon_bars:
        blockers.append("target_stop_pit_history_shorter_than_horizon")
        return _unavailable(receipt)

    samples = _horizon_samples(normalized, horizon_bars)
    # The comparable-liquidity cohort intentionally keeps roughly half of the
    # outcomes.  Requiring two full horizons *after* that split would wrongly
    # reject a valid monthly PIT window, so one horizon plus the absolute
    # twelve-observation floor is the declared minimum.
    minimum_samples = max(12, horizon_bars)
    if len(samples) < minimum_samples:
        blockers.append("target_stop_empirical_sample_insufficient")
        receipt["sample_count"] = len(samples)
        receipt["minimum_sample_count"] = minimum_samples
        return _unavailable(receipt)

    current_notional = normalized[-1]["close"] * normalized[-1]["volume"]
    liquidity_filtered, liquidity = _liquidity_stratified_samples(samples, current_notional)
    if len(liquidity_filtered) < minimum_samples:
        blockers.append("target_stop_comparable_liquidity_sample_insufficient")
        receipt["sample_count"] = len(liquidity_filtered)
        receipt["minimum_sample_count"] = minimum_samples
        receipt["liquidity"] = liquidity
        return _unavailable(receipt)

    returns = [sample["return"] for sample in liquidity_filtered]
    atr_pct = _atr_percent(normalized)
    volatility_pct = _standard_deviation(returns)
    dispersion_pct = max(atr_pct, volatility_pct)
    if not math.isfinite(dispersion_pct) or dispersion_pct <= 0:
        blockers.append("target_stop_volatility_or_atr_unavailable")
        return _unavailable(receipt)

    forecast_score = abs(float(forecast["forecast_score"]))
    # The model only selects where in the *observed* return distribution to
    # look.  It never multiplies the entry price by a synthetic forecast rate.
    favourable_quantile = 0.55 + min(0.25, forecast_score * 0.25)
    adverse_quantile = 1.0 - favourable_quantile
    if direction == "long":
        target_return = max(_quantile(returns, favourable_quantile), dispersion_pct)
        stop_return = min(_quantile(returns, adverse_quantile), -dispersion_pct)
        target_price = _round_price(entry_price * (1.0 + target_return))
        stop_loss = _round_price(entry_price * (1.0 + stop_return))
        valid = target_price is not None and stop_loss is not None and target_price > entry_price > stop_loss > 0
    else:
        target_return = min(_quantile(returns, adverse_quantile), -dispersion_pct)
        stop_return = max(_quantile(returns, favourable_quantile), dispersion_pct)
        target_price = _round_price(entry_price * (1.0 + target_return))
        stop_loss = _round_price(entry_price * (1.0 + stop_return))
        valid = target_price is not None and stop_loss is not None and 0 < target_price < entry_price < stop_loss
    if not valid:
        blockers.append("target_stop_empirical_distribution_not_directional")
        return _unavailable(receipt)

    receipt.update(
        {
            "status": "calibrated",
            "sample_count": len(liquidity_filtered),
            "minimum_sample_count": minimum_samples,
            "liquidity": liquidity,
            "volatility": {
                "horizon_return_stddev_pct": round(volatility_pct, 8),
                "atr_pct": round(atr_pct, 8),
                "dispersion_pct": round(dispersion_pct, 8),
            },
            "empirical_distribution": {
                "favourable_quantile": round(favourable_quantile, 6),
                "adverse_quantile": round(adverse_quantile, 6),
                "target_return_pct": round(target_return, 8),
                "stop_return_pct": round(stop_return, 8),
            },
            "target_price": target_price,
            "stop_loss": stop_loss,
        }
    )
    return _finalize(target_price, stop_loss, receipt)


def _manifest_is_eligible(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == DATASET_MANIFEST_SCHEMA_VERSION
        and value.get("exact_replay_eligible") is True
        and isinstance(value.get("manifest_hash"), str)
        and len(str(value["manifest_hash"])) == 64
        and isinstance(value.get("dataset_sha256"), str)
        and len(str(value["dataset_sha256"])) == 64
    )


def _forecast_projection(intelligence: IntelligenceResult) -> dict[str, Any]:
    raw = intelligence.raw if isinstance(intelligence.raw, dict) else {}
    fingpt = raw.get("fingpt") if isinstance(raw.get("fingpt"), dict) else {}
    projection = fingpt.get("forecast_projection") if isinstance(fingpt.get("forecast_projection"), dict) else {}
    score = _number(projection.get("forecast_score"))
    return {
        "schema_version": projection.get("schema_version"),
        "direction": projection.get("direction"),
        "bin_label": projection.get("bin_label"),
        "forecast_score": round(score, 8) if score is not None else None,
    }


def _forecast_supports_direction(forecast: dict[str, Any], direction: str) -> bool:
    expected = "up" if direction == "long" else "down"
    return (
        forecast.get("schema_version") == "open_stock_ai.fingpt_forecast_projection.v1"
        and forecast.get("direction") == expected
        and isinstance(forecast.get("forecast_score"), float)
        and math.isfinite(float(forecast["forecast_score"]))
    )


def _eligible_rows(rows: list[Any], as_of: Any) -> tuple[list[dict[str, Any]], int]:
    cutoff = _time(as_of) if as_of else None
    normalized: list[dict[str, Any]] = []
    rejected = 0
    for source in rows:
        if not isinstance(source, dict):
            rejected += 1
            continue
        try:
            timestamp = _time(source.get("timestamp"))
            available_at = _time(source.get("available_at"))
            ingested_at = _time(source.get("ingested_at"))
            close = float(source["close"])
            high = float(source.get("high") or close)
            low = float(source.get("low") or close)
            volume = float(source["volume"])
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        if (
            available_at > timestamp
            or ingested_at > timestamp
            or (cutoff is not None and timestamp > cutoff)
            or not all(math.isfinite(value) and value > 0 for value in (close, high, low, volume))
            or high < low
        ):
            rejected += 1
            continue
        normalized.append(
            {
                "timestamp": timestamp,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
            }
        )
    normalized.sort(key=lambda row: row["timestamp"])
    return normalized, rejected


def _horizon_samples(rows: list[dict[str, Any]], horizon_bars: int) -> list[dict[str, float]]:
    return [
        {
            "return": (rows[index + horizon_bars]["close"] / row["close"]) - 1.0,
            "notional": row["close"] * row["volume"],
        }
        for index, row in enumerate(rows[:-horizon_bars])
    ]


def _liquidity_stratified_samples(
    samples: list[dict[str, float]], current_notional: float
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    notionals = sorted(sample["notional"] for sample in samples)
    current_percentile = sum(value <= current_notional for value in notionals) / len(notionals)
    with_percentile = [
        {
            **sample,
            "liquidity_percentile": sum(value <= sample["notional"] for value in notionals) / len(notionals),
        }
        for sample in samples
    ]
    # Keep the closest half of observed liquidity conditions.  The fraction is
    # a sample-selection policy, not a price multiplier; reported receipt data
    # makes the chosen empirical cohort reproducible.
    selected_count = max(1, math.ceil(len(with_percentile) / 2))
    selected = sorted(
        with_percentile,
        key=lambda sample: abs(sample["liquidity_percentile"] - current_percentile),
    )[:selected_count]
    return selected, {
        "current_notional": round(current_notional, 8),
        "historical_median_notional": round(_quantile(notionals, 0.5), 8),
        "current_percentile": round(current_percentile, 8),
        "comparable_sample_count": len(selected),
        "total_sample_count": len(samples),
    }


def _atr_percent(rows: list[dict[str, Any]]) -> float:
    window = rows[-min(14, len(rows)) :]
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in window:
        high, low, close = row["high"], row["low"], row["close"]
        span = high - low
        if previous_close is not None:
            span = max(span, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(span / close)
        previous_close = close
    return fmean(true_ranges) if true_ranges else 0.0


def _standard_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = fmean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _quantile(values: list[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("quantile requires values")
    index = (len(ordered) - 1) * min(1.0, max(0.0, percentile))
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _unavailable(receipt: dict[str, Any]) -> TargetStopCalibration:
    return _finalize(None, None, receipt)


def _finalize(
    target_price: float | None, stop_loss: float | None, receipt: dict[str, Any]
) -> TargetStopCalibration:
    payload = {key: value for key, value in receipt.items() if key not in {"input_sha256", "receipt_id"}}
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    receipt["input_sha256"] = digest
    receipt["receipt_id"] = f"TSC-{digest[:20]}"
    return TargetStopCalibration(target_price=target_price, stop_loss=stop_loss, receipt=receipt)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_price(value: float) -> float | None:
    return round(value, 2) if math.isfinite(value) and value > 0 else None


def _time(value: Any) -> datetime:
    if value is None:
        raise ValueError("timestamp missing")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
