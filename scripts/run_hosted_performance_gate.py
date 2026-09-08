#!/usr/bin/env python3
"""Capture T-008 DB, API and backtest performance evidence without running a model."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from open_stock_ai.governance import (
    PerformanceBudget,
    PerformanceSurfaceProfile,
    run_hosted_performance_gate,
    verify_hosted_performance_gate_receipt,
)
from open_stock_ai.research.benchmark_metrics import evaluate_benchmark_metrics


def _profiles(samples: int) -> tuple[PerformanceSurfaceProfile, ...]:
    return (
        PerformanceSurfaceProfile(
            "database",
            samples,
            PerformanceBudget("database", 25, 0, 20, allowed_regression=1.0, max_p99_ms=50),
        ),
        PerformanceSurfaceProfile(
            "api",
            samples,
            PerformanceBudget("api", 250, 0, 10, allowed_regression=1.0, max_p99_ms=500),
        ),
        PerformanceSurfaceProfile(
            "backtest",
            samples,
            PerformanceBudget("backtest", 100, 0, 5, allowed_regression=1.0, max_p99_ms=200),
        ),
    )


def _database_operation(root: Path):
    path = root / "performance.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode=wal")
    connection.execute("create table samples (id integer primary key, value text not null)")
    counter = 0

    def operation() -> None:
        nonlocal counter
        counter += 1
        connection.execute("insert into samples(value) values (?)", (f"sample-{counter}",))
        row = connection.execute("select value from samples where id = last_insert_rowid()").fetchone()
        connection.commit()
        if row != (f"sample-{counter}",):
            raise RuntimeError("database round trip mismatch")

    return connection, operation


def _api_operation(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("performance API target must be loopback HTTP")
    if parsed.path != "/health" or parsed.query or parsed.fragment:
        raise ValueError("performance API target must be the Stock AI /health endpoint")

    def operation() -> None:
        with urlopen(url, timeout=5) as response:
            payload = json.load(response)
        if payload.get("status") != "ok" or payload.get("system_id") != "stock-ai-system":
            raise RuntimeError("performance API target identity mismatch")

    operation()
    return operation


def _backtest_operation():
    count = 64
    timestamps = [f"2026-01-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00" for index in range(count)]
    benchmark = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.0005 + (index % 5) * 0.0001,
            "benchmark_id": "T008-FIXTURE",
            "source_id": "t008_hosted_fixture",
            "dataset_id": "t008_benchmark_v1",
            "revision_id": "a" * 64,
        }
        for index, timestamp in enumerate(timestamps)
    ]
    cash = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.00002,
            "cash_rate_id": "TWD-FIXTURE",
            "source_id": "t008_hosted_fixture",
            "dataset_id": "t008_cash_v1",
            "revision_id": "b" * 64,
        }
        for timestamp in timestamps
    ]
    strategy = [row["return"] * 1.2 + ((index % 3) - 1) * 0.00003 for index, row in enumerate(benchmark)]

    def operation() -> None:
        report = evaluate_benchmark_metrics(
            strategy,
            benchmark,
            cash,
            evaluation_timestamps=timestamps,
        )
        if report.get("passed") is not True or report.get("sample_size") != count:
            raise RuntimeError("backtest calculation did not produce a verified report")

    operation()
    return operation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if args.samples < 2:
        raise SystemExit("--samples must be at least 2")

    with tempfile.TemporaryDirectory(prefix="stock-ai-t008-") as temporary:
        connection, database = _database_operation(Path(temporary))
        try:
            receipt = run_hosted_performance_gate(
                _profiles(args.samples),
                {
                    "database": database,
                    "api": _api_operation(args.url),
                    "backtest": _backtest_operation(),
                },
                output_dir=args.output_dir,
                commit_sha=args.commit_sha,
            )
        finally:
            connection.close()
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["passed"] and verify_hosted_performance_gate_receipt(receipt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
