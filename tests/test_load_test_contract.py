from __future__ import annotations

from open_stock_ai.governance import LoadTestProfile, PerformanceBudget, evaluate_load_test


PROFILE = LoadTestProfile(
    "agent-api-snapshot",
    target_requests_per_second=20,
    concurrency=8,
    duration_seconds=60,
    budget=PerformanceBudget("agent-api-snapshot", max_p95_ms=250, max_error_rate=0.01, min_throughput=18),
)


def test_load_profile_evaluates_tool_neutral_runner_measurements() -> None:
    report = evaluate_load_test(
        PROFILE,
        runner="k6-equivalent",
        baseline={"p95_ms": 100, "error_rate": 0.001, "throughput": 20},
        candidate={"samples": 1200, "p95_ms": 105, "error_rate": 0.002, "throughput": 19},
    )

    assert report.performance.passed is True
    assert report.samples == 1200
    assert report.runner == "k6-equivalent"
    assert report.verify() is True
    assert report.profile.as_dict()["concurrency"] == 8


def test_load_test_missing_samples_or_regression_fails_closed() -> None:
    missing = evaluate_load_test(PROFILE, runner="locust-equivalent", baseline=None, candidate={})
    slow = evaluate_load_test(
        PROFILE,
        runner="locust-equivalent",
        baseline={"p95_ms": 100, "error_rate": 0.001, "throughput": 20},
        candidate={"samples": 10, "p95_ms": 400, "error_rate": 0.02, "throughput": 2},
    )

    assert missing.performance.passed is False
    assert "baseline_or_candidate_metrics_missing" in missing.performance.blockers
    assert slow.performance.passed is False
    assert set(slow.performance.blockers) == {
        "performance_regression:p95_ms",
        "performance_regression:error_rate",
        "performance_regression:throughput",
    }
