"""Tool-neutral load-test profile and fail-closed measurement contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .performance_regression import (
    PerformanceBudget,
    PerformanceReport,
    evaluate_performance_regression,
    verify_performance_report,
)


@dataclass(frozen=True, slots=True)
class LoadTestProfile:
    name: str
    target_requests_per_second: float
    concurrency: int
    duration_seconds: int
    budget: PerformanceBudget

    def __post_init__(self) -> None:
        if not self.name.strip() or self.target_requests_per_second <= 0:
            raise ValueError("load profile requires a positive name and request rate")
        if self.concurrency < 1 or self.duration_seconds < 1:
            raise ValueError("load profile concurrency and duration must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.load_test_profile.v1",
            "name": self.name,
            "target_requests_per_second": self.target_requests_per_second,
            "concurrency": self.concurrency,
            "duration_seconds": self.duration_seconds,
            "budget": {
                "operation": self.budget.operation,
                "max_p95_ms": self.budget.max_p95_ms,
                "max_error_rate": self.budget.max_error_rate,
                "min_throughput": self.budget.min_throughput,
                "allowed_regression": self.budget.allowed_regression,
                "max_p99_ms": self.budget.max_p99_ms,
            },
        }


@dataclass(frozen=True, slots=True)
class LoadTestReport:
    profile: LoadTestProfile
    runner: str
    samples: int
    performance: PerformanceReport
    report_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.load_test_report.v1",
            "profile": self.profile.as_dict(),
            "runner": self.runner,
            "samples": self.samples,
            "performance": self.performance.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "report_sha256": self.report_sha256}

    def verify(self) -> bool:
        return hashlib.sha256(_canonical(self.payload())).hexdigest() == self.report_sha256


def evaluate_load_test(
    profile: LoadTestProfile,
    *,
    runner: str,
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
) -> LoadTestReport:
    sample_count = _sample_count(candidate)
    performance = evaluate_performance_regression(profile.budget, baseline=baseline, candidate=candidate)
    if sample_count <= 0:
        performance = evaluate_performance_regression(profile.budget, baseline=None, candidate=None)
    payload = {
        "schema_version": "open_stock_ai.load_test_report.v1",
        "profile": profile.as_dict(),
        "runner": str(runner),
        "samples": sample_count,
        "performance": performance.as_dict(),
    }
    return LoadTestReport(
        profile=profile,
        runner=str(runner),
        samples=sample_count,
        performance=performance,
        report_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
    )


def verify_load_test_report(report: dict[str, Any]) -> bool:
    required = {
        "schema_version",
        "profile",
        "runner",
        "samples",
        "performance",
        "report_sha256",
    }
    if set(report) != required or report.get("schema_version") != "open_stock_ai.load_test_report.v1":
        return False
    if not isinstance(report.get("performance"), dict) or not verify_performance_report(report["performance"]):
        return False
    payload = {key: report[key] for key in required if key != "report_sha256"}
    expected = hashlib.sha256(_canonical(payload)).hexdigest()
    return expected == str(report.get("report_sha256") or "")


def _sample_count(candidate: dict[str, Any] | None) -> int:
    if not isinstance(candidate, dict):
        return 0
    value = candidate.get("samples")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
