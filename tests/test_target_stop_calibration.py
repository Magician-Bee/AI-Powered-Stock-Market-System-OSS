from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from open_stock_ai.strategy.strategy_engine import StrategyEngine
from open_stock_ai.research.pit_dataset import FeatureRecord, build_dataset_manifest_identity
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest


def _rows(*, latest_volume_multiplier: float = 1.0, daily_trend: float = 0.42) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict] = []
    for index in range(72):
        timestamp = start + timedelta(days=index)
        close = 130.0 + index * daily_trend + (index % 5 - 2) * 0.7
        volume = float(750_000 + (index % 9) * 150_000)
        if index == 71:
            volume *= latest_volume_multiplier
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "available_at": timestamp.isoformat(),
                "ingested_at": timestamp.isoformat(),
                "open": close - 0.4,
                "high": close + 1.2,
                "low": close - 1.1,
                "close": close,
                "volume": volume,
            }
        )
    return rows


def _snapshot(*, rows: list[dict] | None = None) -> MarketSnapshot:
    pit_rows = rows or _rows()
    features = [
        FeatureRecord(
            entity_id="EQ-2330", feature_id=f"fixture:{domain}", value={"fixture": domain},
            event_time=pit_rows[0]["timestamp"], published_at=pit_rows[0]["timestamp"],
            available_at=pit_rows[0]["timestamp"], effective_at=pit_rows[0]["timestamp"],
            ingested_at=pit_rows[0]["timestamp"], source_revision_id=f"fixture-{domain}",
            transformation_id="test.fixture.v1", transformation_sha=f"fixture-{domain}",
            dataset_version="fixture:v1", domain=domain,
        ).to_dict()
        for domain in ("prices", "financials", "flows", "events")
    ]
    manifest = build_dataset_manifest_identity(
        entity_id="EQ-2330",
        as_of=pit_rows[-1]["timestamp"],
        required_domains=("prices", "financials", "flows", "events"),
        coverage={},
        blockers=(),
        features=features,
        replay_rows=pit_rows,
    )
    manifest["exact_replay_eligible"] = True
    return MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=pit_rows[-1]["close"],
        ohlcv=[dict(row) for row in pit_rows],
        raw={
            "as_of": pit_rows[-1]["timestamp"],
            "point_in_time_dataset_manifest": manifest,
            "point_in_time_dataset": [dict(row) for row in pit_rows],
            "point_in_time_dataset_features": features,
        },
    )


def _intelligence(*, direction: str, score: float) -> IntelligenceResult:
    return IntelligenceResult(
        symbol="2330.TW",
        market="TW",
        summary="PIT calibration fixture",
        sentiment_score=score,
        fundamental_view="positive" if direction == "up" else "negative",
        technical_view="uptrend" if direction == "up" else "downtrend",
        raw={
            "fingpt": {
                "forecast_projection": {
                    "schema_version": "open_stock_ai.fingpt_forecast_projection.v1",
                    "direction": direction,
                    "bin_label": "fixture empirical distribution",
                    "forecast_score": score,
                }
            }
        },
    )


def test_target_and_stop_use_pit_empirical_receipt_not_fixed_multipliers() -> None:
    signal = StrategyEngine().generate_signal(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _snapshot(),
        _intelligence(direction="up", score=0.8),
    )

    receipt = signal.decision_schema["source_ratings"]["target_stop_calibration"]
    assert signal.action == "buy"
    assert signal.price_method_id == "empirical_pit_target_stop_calibration.v1"
    assert signal.target_price is not None and signal.target_price > signal.entry_price
    assert signal.stop_loss is not None and 0 < signal.stop_loss < signal.entry_price
    assert receipt["status"] == "calibrated"
    assert len(receipt["dataset_manifest_hash"]) == 64
    assert receipt["horizon_bars"] == 5
    assert receipt["forecast_evidence"]["direction"] == "up"
    assert receipt["liquidity"]["comparable_sample_count"] >= receipt["minimum_sample_count"]
    assert receipt["volatility"]["atr_pct"] > 0
    assert receipt["input_sha256"] and len(receipt["input_sha256"]) == 64
    assert "fixed_strategy_reference_multiplier" not in str(signal.decision_schema)


def test_target_and_stop_fail_closed_without_pit_or_compatible_forecast() -> None:
    snapshot = _snapshot()
    snapshot.raw = {}
    signal = StrategyEngine().generate_signal(
        StockRequest(symbol="2330.TW", horizon="swing"),
        snapshot,
        _intelligence(direction="up", score=0.8),
    )

    receipt = signal.decision_schema["source_ratings"]["target_stop_calibration"]
    assert signal.target_price is None
    assert signal.stop_loss is None
    assert signal.price_method_id is None
    assert receipt["status"] == "unavailable"
    assert "target_stop_pit_manifest_not_execution_eligible" in receipt["blockers"]

    future_rows = deepcopy(_rows())
    future_rows[20]["available_at"] = (datetime.fromisoformat(future_rows[20]["timestamp"]) + timedelta(days=1)).isoformat()
    signal = StrategyEngine().generate_signal(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _snapshot(rows=future_rows),
        _intelligence(direction="up", score=0.8),
    )
    receipt = signal.decision_schema["source_ratings"]["target_stop_calibration"]
    assert signal.target_price is None
    assert signal.stop_loss is None
    assert receipt["status"] == "unavailable"
    assert receipt["rejected_row_count"] == 1


def test_target_and_stop_use_short_distribution_and_liquidity_cohort() -> None:
    normal = StrategyEngine().generate_signal(
        StockRequest(symbol="2330.TW", horizon="monthly"),
        _snapshot(rows=_rows(daily_trend=-0.42)),
        _intelligence(direction="down", score=-0.9),
    )
    thin = StrategyEngine().generate_signal(
        StockRequest(symbol="2330.TW", horizon="monthly"),
        _snapshot(rows=_rows(latest_volume_multiplier=0.1, daily_trend=-0.42)),
        _intelligence(direction="down", score=-0.9),
    )

    normal_receipt = normal.decision_schema["source_ratings"]["target_stop_calibration"]
    thin_receipt = thin.decision_schema["source_ratings"]["target_stop_calibration"]
    assert normal.action == "sell"
    assert normal.target_price is not None and 0 < normal.target_price < normal.entry_price
    assert normal.stop_loss is not None and normal.stop_loss > normal.entry_price
    assert normal_receipt["horizon_bars"] == 20
    assert normal_receipt["liquidity"]["current_percentile"] != thin_receipt["liquidity"]["current_percentile"]
    assert normal_receipt["input_sha256"] != thin_receipt["input_sha256"]
