from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from open_stock_ai.research.benchmark_metrics import evaluate_benchmark_metrics


def _observations(count: int = 30):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [(start + timedelta(days=index)).isoformat() for index in range(count)]
    benchmark = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.001 + (index % 4) * 0.0003,
            "benchmark_id": "TWII",
            "source_id": "twse_official_index",
            "dataset_id": "twse_index_daily",
            "revision_id": hashlib.sha256(b"twii-v1").hexdigest(),
        }
        for index, timestamp in enumerate(timestamps)
    ]
    cash = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.00004,
            "cash_rate_id": "twd_overnight_cash_rate",
            "source_id": "central_bank_taiwan",
            "dataset_id": "twd_cash_rate_daily",
            "revision_id": hashlib.sha256(b"cash-v1").hexdigest(),
        }
        for timestamp in timestamps
    ]
    return timestamps, benchmark, cash


def test_benchmark_alpha_beta_and_information_ratio_are_deterministic():
    timestamps, benchmark, cash = _observations()
    strategy = [row["return"] * 1.35 + (index % 3 - 1) * 0.00008 for index, row in enumerate(benchmark)]

    first = evaluate_benchmark_metrics(strategy, benchmark, cash, evaluation_timestamps=timestamps)
    second = evaluate_benchmark_metrics(strategy, benchmark, cash, evaluation_timestamps=timestamps)

    assert first == second
    assert first["passed"] is True
    assert first["alignment_verified"] is True
    assert first["benchmark"]["benchmark_id"] == "TWII"
    assert first["risk_free"]["cash_rate_id"] == "twd_overnight_cash_rate"
    assert first["beta"] is not None and first["beta"] > 1.0
    assert first["information_ratio"] is not None
    assert len(first["alignment_sha256"]) == 64


def test_benchmark_metrics_fail_closed_when_observation_was_not_available():
    timestamps, benchmark, cash = _observations()
    benchmark[4] = {**benchmark[4], "available_at": timestamps[5]}
    strategy = [0.002] * len(timestamps)

    result = evaluate_benchmark_metrics(strategy, benchmark, cash, evaluation_timestamps=timestamps)

    assert result["passed"] is False
    assert result["alignment_verified"] is False
    assert "benchmark_observation_available_after_evaluation:4" in result["blockers"]
