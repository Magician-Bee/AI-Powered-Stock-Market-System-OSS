from __future__ import annotations

import json

import pytest

from open_stock_ai.governance import (
    LoadTestProfile,
    PerformanceBudget,
    RequestSample,
    run_load_test,
    summarize_samples,
    write_report,
)


def _profile() -> LoadTestProfile:
    return LoadTestProfile(
        "runner-test",
        target_requests_per_second=4,
        concurrency=2,
        duration_seconds=1,
        budget=PerformanceBudget("runner-test", max_p95_ms=250, max_error_rate=0.5, min_throughput=1),
    )


def test_summarize_samples_uses_nearest_rank_p95_and_success_metrics() -> None:
    metrics = summarize_samples(
        [
            RequestSample(10, True),
            RequestSample(20, True),
            RequestSample(30, False),
            RequestSample(40, True),
        ],
        elapsed_seconds=2,
    )

    assert metrics == {
        "samples": 4,
        "p95_ms": 40.0,
        "p99_ms": 40.0,
        "error_rate": 0.25,
        "throughput": 2.0,
    }


def test_runner_evaluates_injected_requests_without_network() -> None:
    calls = 0

    def request(_url: str, _timeout: float) -> RequestSample:
        nonlocal calls
        calls += 1
        return RequestSample(5, True)

    report = run_load_test(_profile(), url="http://example.invalid", baseline={"p95_ms": 5, "error_rate": 0, "throughput": 1}, request_fn=request)

    assert calls >= 2
    assert report.samples == calls
    assert report.performance.passed is True
    assert report.verify() is True


def test_runner_without_completed_requests_fails_closed() -> None:
    report = run_load_test(
        _profile(),
        url="http://example.invalid",
        baseline={"p95_ms": 5, "error_rate": 0, "throughput": 1},
        request_fn=lambda _url, _timeout: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert report.samples == 0
    assert report.performance.passed is False
    assert "baseline_or_candidate_metrics_missing" in report.performance.blockers


def test_write_report_is_verified_and_immutable(tmp_path) -> None:
    report = run_load_test(
        _profile(),
        url="http://example.invalid",
        baseline={"p95_ms": 5, "error_rate": 0, "throughput": 1},
        request_fn=lambda _url, _timeout: RequestSample(5, True),
    )
    path = tmp_path / "report.json"
    write_report(report, path)
    assert json.loads(path.read_text(encoding="utf-8"))["report_sha256"] == report.as_dict()["report_sha256"]
    write_report(report, path)
    path.write_text(path.read_text(encoding="utf-8").replace('"samples":', '"samples":999, "ignored":'), encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_report(report, path)
