from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from open_stock_ai.governance import (
    PerformanceBudget,
    PerformanceSurfaceProfile,
    collect_surface_metrics,
    run_hosted_performance_gate,
    verify_hosted_performance_gate_receipt,
)


def _profiles(samples: int = 3):
    return tuple(
        PerformanceSurfaceProfile(
            surface,
            samples,
            PerformanceBudget(surface, 1000, 0, 0.01, allowed_regression=10, max_p99_ms=1000),
        )
        for surface in ("database", "api", "backtest")
    )


def test_surface_metrics_capture_latency_errors_and_throughput() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture")

    metrics = collect_surface_metrics(_profiles()[0], operation)
    assert metrics["samples"] == 3
    assert metrics["error_rate"] == pytest.approx(1 / 3)
    assert metrics["p99_ms"] >= metrics["p95_ms"] >= 0
    assert metrics["throughput"] > 0


def test_hosted_gate_retains_each_non_model_surface_and_defers_model(tmp_path) -> None:
    receipt = run_hosted_performance_gate(
        _profiles(),
        {"database": lambda: None, "api": lambda: None, "backtest": lambda: None},
        output_dir=tmp_path,
        commit_sha="a" * 40,
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    assert receipt["passed"] is True
    assert receipt["covered_surfaces"] == ["database", "api", "backtest"]
    assert receipt["deferred_surfaces"] == ["model_runtime"]
    assert receipt["environment"]["model_execution"] == "disabled"
    assert verify_hosted_performance_gate_receipt(receipt) is True
    assert len(list((tmp_path / "baselines").glob("*.json"))) == 3
    assert json.loads((tmp_path / "performance-gate-receipt.json").read_text()) == receipt

    receipt["surfaces"]["api"]["candidate_metrics"]["p95_ms"] = 999
    assert verify_hosted_performance_gate_receipt(receipt) is False


def test_hosted_gate_fails_closed_when_a_surface_is_missing(tmp_path) -> None:
    try:
        run_hosted_performance_gate(
            _profiles(),
            {"database": lambda: None, "api": lambda: None},
            output_dir=tmp_path,
            commit_sha="b" * 40,
        )
    except ValueError as exc:
        assert "all required" in str(exc)
    else:
        raise AssertionError("missing surface must fail closed")


def test_workflow_retains_real_non_model_surface_evidence() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/performance-regression.yml").read_text()
    assert "scripts/run_hosted_performance_gate.py" in workflow
    assert "--samples 100" in workflow
    assert "retention-days: 90" in workflow
    assert "verify_hosted_performance_gate_receipt" in workflow
    assert 'receipt["deferred_surfaces"] != ["model_runtime"]' in workflow
    assert "ollama" not in workflow.casefold()
    assert "openai" not in workflow.casefold()
