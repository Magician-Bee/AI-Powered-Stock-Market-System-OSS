"""Fail-closed performance baseline and regression threshold contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "open_stock_ai.performance_regression_report.v1"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    operation: str
    max_p95_ms: float
    max_error_rate: float
    min_throughput: float
    allowed_regression: float = 0.10
    max_p99_ms: float | None = None


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    operation: str
    status: str
    passed: bool
    blockers: tuple[str, ...]
    comparisons: dict[str, dict[str, Any]]
    report_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "status": self.status,
            "passed": self.passed,
            "blockers": list(self.blockers),
            "comparisons": self.comparisons,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "report_sha256": self.report_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.report_sha256)


@dataclass(frozen=True, slots=True)
class PerformanceBaselineArtifact:
    """Content-addressed baseline metrics retained for later comparisons."""

    operation: str
    metrics: dict[str, Any]
    captured_at: str
    environment: dict[str, Any]
    artifact_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.performance_baseline.v1",
            "operation": self.operation,
            "metrics": self.metrics,
            "captured_at": self.captured_at,
            "environment": self.environment,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "artifact_sha256": self.artifact_sha256}

    def verify(self) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(_canonical(self.payload())).hexdigest(), self.artifact_sha256
        )


class PerformanceBaselineStore:
    """Retain verified baselines without allowing an existing artifact to change."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        operation: str,
        metrics: dict[str, Any],
        *,
        environment: dict[str, Any] | None = None,
        captured_at: str | None = None,
    ) -> PerformanceBaselineArtifact:
        normalized_operation = str(operation).strip()
        if not normalized_operation:
            raise ValueError("baseline operation is required")
        normalized_metrics = _validate_baseline_metrics(metrics)
        timestamp = str(captured_at or datetime.now(timezone.utc).isoformat())
        artifact = PerformanceBaselineArtifact(
            operation=normalized_operation,
            metrics=normalized_metrics,
            captured_at=timestamp,
            environment=dict(environment or {}),
            artifact_sha256="",
        )
        digest = hashlib.sha256(_canonical(artifact.payload())).hexdigest()
        artifact = PerformanceBaselineArtifact(
            operation=artifact.operation,
            metrics=artifact.metrics,
            captured_at=artifact.captured_at,
            environment=artifact.environment,
            artifact_sha256=digest,
        )
        destination = self.root / f"{_safe_name(normalized_operation)}-{digest}.json"
        encoded = _canonical(artifact.as_dict()).decode("utf-8") + "\n"
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if destination.read_text(encoding="utf-8") != encoded:
                raise ValueError("baseline artifact path is bound to different evidence")
            return artifact
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return artifact

    def read(self, artifact_sha256: str) -> PerformanceBaselineArtifact:
        digest = str(artifact_sha256).strip()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid baseline artifact hash")
        matches = list(self.root.glob(f"*-{digest}.json"))
        if len(matches) != 1:
            raise FileNotFoundError(f"baseline artifact not found: {digest}")
        payload = json.loads(matches[0].read_text(encoding="utf-8"))
        artifact = PerformanceBaselineArtifact(
            operation=str(payload["operation"]),
            metrics=dict(payload["metrics"]),
            captured_at=str(payload["captured_at"]),
            environment=dict(payload.get("environment") or {}),
            artifact_sha256=str(payload["artifact_sha256"]),
        )
        if payload.get("schema_version") != "open_stock_ai.performance_baseline.v1" or not artifact.verify():
            raise ValueError("baseline artifact hash mismatch")
        return artifact

    def latest(self, operation: str) -> PerformanceBaselineArtifact | None:
        candidates: list[PerformanceBaselineArtifact] = []
        prefix = f"{_safe_name(str(operation).strip())}-"
        for path in self.root.glob(f"{prefix}*.json"):
            try:
                candidates.append(self.read(path.stem.rsplit("-", 1)[-1]))
            except (ValueError, FileNotFoundError, json.JSONDecodeError):
                continue
        return max(candidates, key=lambda item: item.captured_at) if candidates else None


def evaluate_performance_regression(
    budget: PerformanceBudget,
    *,
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
) -> PerformanceReport:
    required = ["p95_ms", "error_rate", "throughput"]
    if budget.max_p99_ms is not None:
        required.append("p99_ms")
    base = baseline if isinstance(baseline, dict) else {}
    current = candidate if isinstance(candidate, dict) else {}
    if any(not _finite_number(base.get(key)) or not _finite_number(current.get(key)) for key in required):
        return _report(budget.operation, "no_data", False, ("baseline_or_candidate_metrics_missing",), {})
    allowed_p95 = min(float(budget.max_p95_ms), float(base["p95_ms"]) * (1.0 + float(budget.allowed_regression)))
    minimum_throughput = max(float(budget.min_throughput), float(base["throughput"]) * (1.0 - float(budget.allowed_regression)))
    comparisons = {
        "p95_ms": {"baseline": base["p95_ms"], "candidate": current["p95_ms"], "limit": allowed_p95, "passed": float(current["p95_ms"]) <= allowed_p95},
        "error_rate": {"baseline": base["error_rate"], "candidate": current["error_rate"], "limit": float(budget.max_error_rate), "passed": float(current["error_rate"]) <= float(budget.max_error_rate)},
        "throughput": {"baseline": base["throughput"], "candidate": current["throughput"], "limit": minimum_throughput, "passed": float(current["throughput"]) >= minimum_throughput},
    }
    if budget.max_p99_ms is not None:
        allowed_p99 = min(
            float(budget.max_p99_ms),
            float(base["p99_ms"]) * (1.0 + float(budget.allowed_regression)),
        )
        comparisons["p99_ms"] = {
            "baseline": base["p99_ms"],
            "candidate": current["p99_ms"],
            "limit": allowed_p99,
            "passed": float(current["p99_ms"]) <= allowed_p99,
        }
    blockers = tuple(f"performance_regression:{key}" for key, item in comparisons.items() if item["passed"] is not True)
    return _report(budget.operation, "evaluated", not blockers, blockers, comparisons)


def _report(operation: str, status: str, passed: bool, blockers: tuple[str, ...], comparisons: dict[str, dict[str, Any]]) -> PerformanceReport:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": status,
        "passed": passed,
        "blockers": list(blockers),
        "comparisons": comparisons,
    }
    return PerformanceReport(operation, status, passed, blockers, comparisons, hashlib.sha256(_canonical(payload)).hexdigest())


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def verify_performance_report(report: dict[str, Any]) -> bool:
    required = {"schema_version", "operation", "status", "passed", "blockers", "comparisons", "report_sha256"}
    if set(report) != required or report.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = hashlib.sha256(_canonical({key: report[key] for key in required if key != "report_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(report.get("report_sha256") or ""))


def _validate_baseline_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise ValueError("baseline metrics must be an object")
    required = ("p95_ms", "error_rate", "throughput", "samples")
    if any(not _finite_number(metrics.get(key)) for key in required):
        raise ValueError("baseline metrics require finite p95_ms, error_rate, throughput and samples")
    if int(metrics["samples"]) != metrics["samples"] or int(metrics["samples"]) <= 0:
        raise ValueError("baseline samples must be a positive integer")
    if float(metrics["p95_ms"]) < 0 or float(metrics["error_rate"]) < 0 or float(metrics["throughput"]) < 0:
        raise ValueError("baseline metrics cannot be negative")
    return dict(metrics)


def _safe_name(value: str) -> str:
    result = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return result.strip("._-") or "operation"
