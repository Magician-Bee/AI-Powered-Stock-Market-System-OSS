import pytest

from open_stock_ai.governance.performance_regression import (
    PerformanceBaselineStore,
    PerformanceBudget,
    evaluate_performance_regression,
    verify_performance_report,
)


_BUDGET = PerformanceBudget("api.snapshot", max_p95_ms=250, max_error_rate=0.01, min_throughput=20)


def test_performance_report_passes_within_explicit_thresholds() -> None:
    report = evaluate_performance_regression(
        _BUDGET,
        baseline={"p95_ms": 100, "error_rate": 0.001, "throughput": 30},
        candidate={"p95_ms": 105, "error_rate": 0.002, "throughput": 29},
    )
    assert report.status == "evaluated"
    assert report.passed is True
    assert report.verify() is True
    assert verify_performance_report(report.as_dict()) is True


def test_performance_regression_and_missing_samples_fail_closed() -> None:
    slow = evaluate_performance_regression(
        _BUDGET,
        baseline={"p95_ms": 100, "error_rate": 0.001, "throughput": 30},
        candidate={"p95_ms": 130, "error_rate": 0.02, "throughput": 10},
    )
    assert slow.passed is False
    assert set(slow.blockers) == {"performance_regression:p95_ms", "performance_regression:error_rate", "performance_regression:throughput"}
    missing = evaluate_performance_regression(_BUDGET, baseline=None, candidate={})
    assert missing.status == "no_data"
    assert missing.passed is False


def test_p99_budget_is_enforced_when_declared() -> None:
    budget = PerformanceBudget(
        "api.tail",
        max_p95_ms=250,
        max_error_rate=0.01,
        min_throughput=20,
        allowed_regression=0.10,
        max_p99_ms=300,
    )
    report = evaluate_performance_regression(
        budget,
        baseline={"p95_ms": 100, "p99_ms": 150, "error_rate": 0, "throughput": 30},
        candidate={"p95_ms": 105, "p99_ms": 170, "error_rate": 0, "throughput": 29},
    )
    assert report.passed is False
    assert report.blockers == ("performance_regression:p99_ms",)
    assert report.comparisons["p99_ms"]["limit"] == pytest.approx(165)


def test_tampered_performance_report_is_rejected() -> None:
    report = evaluate_performance_regression(
        _BUDGET,
        baseline={"p95_ms": 100, "error_rate": 0.001, "throughput": 30},
        candidate={"p95_ms": 105, "error_rate": 0.002, "throughput": 29},
    ).as_dict()
    report["comparisons"]["p95_ms"]["candidate"] = 999
    assert verify_performance_report(report) is False


def test_performance_baseline_store_is_content_addressed_and_restart_safe(tmp_path) -> None:
    store = PerformanceBaselineStore(tmp_path / "baselines")
    artifact = store.record(
        "api.snapshot",
        {"samples": 1200, "p95_ms": 105, "error_rate": 0.002, "throughput": 29},
        environment={"python": "3.12", "runner": "local"},
        captured_at="2026-08-26T00:00:00+00:00",
    )
    assert artifact.verify() is True
    assert store.read(artifact.artifact_sha256) == artifact
    assert PerformanceBaselineStore(store.root).latest("api.snapshot") == artifact

    same = store.record(
        "api.snapshot",
        {"samples": 1200, "p95_ms": 105, "error_rate": 0.002, "throughput": 29},
        environment={"python": "3.12", "runner": "local"},
        captured_at="2026-08-26T00:00:00+00:00",
    )
    assert same == artifact


def test_performance_baseline_store_rejects_invalid_or_tampered_artifact(tmp_path) -> None:
    store = PerformanceBaselineStore(tmp_path / "baselines")
    with pytest.raises(ValueError, match="positive integer"):
        store.record(
            "api.snapshot",
            {"samples": 0, "p95_ms": 100, "error_rate": 0.001, "throughput": 20},
        )
    artifact = store.record(
        "api.snapshot",
        {"samples": 10, "p95_ms": 100, "error_rate": 0.001, "throughput": 20},
    )
    path = next(store.root.glob("*.json"))
    payload = path.read_text(encoding="utf-8").replace('"p95_ms":100', '"p95_ms":999')
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read(artifact.artifact_sha256)
