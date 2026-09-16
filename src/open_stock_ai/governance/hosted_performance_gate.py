"""Hosted T-008 performance evidence for runtime surfaces that do not use a model."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .performance_regression import (
    PerformanceBaselineStore,
    PerformanceBudget,
    evaluate_performance_regression,
    verify_performance_report,
)


HOSTED_PERFORMANCE_GATE_SCHEMA = "open_stock_ai.hosted_performance_gate.v1"
NON_MODEL_SURFACES = ("database", "api", "backtest")
DEFERRED_MODEL_SURFACE = "model_runtime"


@dataclass(frozen=True, slots=True)
class PerformanceSurfaceProfile:
    surface: str
    samples: int
    budget: PerformanceBudget

    def __post_init__(self) -> None:
        if self.surface not in NON_MODEL_SURFACES:
            raise ValueError(f"unsupported non-model performance surface: {self.surface}")
        if self.samples < 2:
            raise ValueError("performance surface requires at least two samples")
        if self.budget.operation != self.surface:
            raise ValueError("performance budget operation must match the surface")

    def as_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "samples": self.samples,
            "budget": {
                "operation": self.budget.operation,
                "max_p95_ms": self.budget.max_p95_ms,
                "max_p99_ms": self.budget.max_p99_ms,
                "max_error_rate": self.budget.max_error_rate,
                "min_throughput": self.budget.min_throughput,
                "allowed_regression": self.budget.allowed_regression,
            },
        }


def collect_surface_metrics(
    profile: PerformanceSurfaceProfile,
    operation: Callable[[], Any],
    *,
    timer: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    latencies: list[float] = []
    errors = 0
    started = timer()
    for _ in range(profile.samples):
        sample_started = timer()
        try:
            operation()
        except Exception:  # The receipt records failure counts without leaking error text.
            errors += 1
        elapsed_ms = max(0.0, (timer() - sample_started) * 1000.0)
        latencies.append(elapsed_ms)
    duration = max(timer() - started, 1e-9)
    ordered = sorted(latencies)
    return {
        "samples": profile.samples,
        "p95_ms": round(_percentile(ordered, 0.95), 6),
        "p99_ms": round(_percentile(ordered, 0.99), 6),
        "error_rate": round(errors / profile.samples, 8),
        "throughput": round(profile.samples / duration, 6),
    }


def run_hosted_performance_gate(
    profiles: tuple[PerformanceSurfaceProfile, ...],
    operations: dict[str, Callable[[], Any]],
    *,
    output_dir: str | Path,
    commit_sha: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture two real intervals per non-model surface and retain hash-bound evidence."""

    normalized_commit = str(commit_sha).strip().lower()
    if len(normalized_commit) != 40 or any(char not in "0123456789abcdef" for char in normalized_commit):
        raise ValueError("hosted performance gate requires the exact 40-character commit SHA")
    profile_by_surface = {profile.surface: profile for profile in profiles}
    if set(profile_by_surface) != set(NON_MODEL_SURFACES) or set(operations) != set(NON_MODEL_SURFACES):
        raise ValueError("database, api and backtest surfaces are all required")

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    captured_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    environment = {
        "runner": "github-hosted" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
        "commit_sha": normalized_commit,
        "model_execution": "disabled",
    }
    surfaces: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    store = PerformanceBaselineStore(destination / "baselines")
    for surface in NON_MODEL_SURFACES:
        profile = profile_by_surface[surface]
        baseline_metrics = collect_surface_metrics(profile, operations[surface])
        baseline = store.record(
            surface,
            baseline_metrics,
            environment=environment,
            captured_at=captured_at,
        )
        candidate_metrics = collect_surface_metrics(profile, operations[surface])
        report = evaluate_performance_regression(
            profile.budget,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
        )
        if not report.passed:
            blockers.extend(f"{surface}:{item}" for item in report.blockers)
        surfaces[surface] = {
            "profile": profile.as_dict(),
            "baseline_artifact": baseline.as_dict(),
            "candidate_metrics": candidate_metrics,
            "report": report.as_dict(),
        }

    payload = {
        "schema_version": HOSTED_PERFORMANCE_GATE_SCHEMA,
        "commit_sha": normalized_commit,
        "captured_at": captured_at,
        "environment": environment,
        "covered_surfaces": list(NON_MODEL_SURFACES),
        "deferred_surfaces": [DEFERRED_MODEL_SURFACE],
        "surfaces": surfaces,
        "blockers": sorted(set(blockers)),
        "passed": not blockers,
    }
    receipt = {**payload, "receipt_sha256": _digest(payload)}
    _write_immutable(destination / "performance-gate-receipt.json", receipt)
    return receipt


def verify_hosted_performance_gate_receipt(receipt: dict[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "captured_at", "environment",
        "covered_surfaces", "deferred_surfaces", "surfaces", "blockers",
        "passed", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_PERFORMANCE_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    if receipt.get("covered_surfaces") != list(NON_MODEL_SURFACES):
        return False
    if receipt.get("deferred_surfaces") != [DEFERRED_MODEL_SURFACE]:
        return False
    environment = receipt.get("environment")
    if not isinstance(environment, dict) or environment.get("model_execution") != "disabled":
        return False
    surfaces = receipt.get("surfaces")
    if not isinstance(surfaces, dict) or set(surfaces) != set(NON_MODEL_SURFACES):
        return False
    derived_blockers: list[str] = []
    for surface in NON_MODEL_SURFACES:
        item = surfaces.get(surface)
        if not isinstance(item, dict) or set(item) != {"profile", "baseline_artifact", "candidate_metrics", "report"}:
            return False
        baseline = item["baseline_artifact"]
        if not isinstance(baseline, dict) or not _verify_baseline_payload(baseline):
            return False
        report = item["report"]
        if not isinstance(report, dict) or not verify_performance_report(report):
            return False
        if report.get("operation") != surface:
            return False
        comparisons = report.get("comparisons")
        candidate = item.get("candidate_metrics")
        if not isinstance(comparisons, dict) or not isinstance(candidate, dict):
            return False
        baseline_metrics = baseline.get("metrics")
        if not isinstance(baseline_metrics, dict):
            return False
        for metric in ("p95_ms", "p99_ms", "error_rate", "throughput"):
            comparison = comparisons.get(metric)
            if not isinstance(comparison, dict):
                return False
            if comparison.get("baseline") != baseline_metrics.get(metric):
                return False
            if comparison.get("candidate") != candidate.get(metric):
                return False
        if report.get("passed") is not True:
            derived_blockers.extend(f"{surface}:{blocker}" for blocker in report.get("blockers", []))
    expected_blockers = sorted(set(derived_blockers))
    return receipt.get("blockers") == expected_blockers and receipt.get("passed") is (not expected_blockers)


def _verify_baseline_payload(payload: dict[str, Any]) -> bool:
    required = {"schema_version", "operation", "metrics", "captured_at", "environment", "artifact_sha256"}
    if set(payload) != required or payload.get("schema_version") != "open_stock_ai.performance_baseline.v1":
        return False
    supplied = str(payload.get("artifact_sha256") or "")
    document = {key: payload[key] for key in required if key != "artifact_sha256"}
    return hmac.compare_digest(_digest(document), supplied)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return math.nan
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return values[index]


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _write_immutable(path: Path, payload: dict[str, Any]) -> None:
    encoded = _canonical(payload).decode("utf-8") + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"refusing to overwrite different performance evidence: {path}")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "DEFERRED_MODEL_SURFACE",
    "HOSTED_PERFORMANCE_GATE_SCHEMA",
    "NON_MODEL_SURFACES",
    "PerformanceSurfaceProfile",
    "collect_surface_metrics",
    "run_hosted_performance_gate",
    "verify_hosted_performance_gate_receipt",
]
