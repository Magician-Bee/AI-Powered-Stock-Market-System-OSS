"""Retained baseline and candidate evidence for the hosted R-009 load gate."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .load_test_contract import LoadTestProfile, evaluate_load_test, verify_load_test_report
from .load_test_runner import RequestFn, collect_load_metrics, write_report
from .performance_regression import PerformanceBaselineArtifact, PerformanceBaselineStore


HOSTED_LOAD_GATE_SCHEMA = "open_stock_ai.hosted_load_gate_receipt.v1"


def run_hosted_load_gate(
    profile: LoadTestProfile,
    *,
    url: str,
    output_dir: str | Path,
    commit_sha: str,
    service_identity: dict[str, Any],
    timeout_seconds: float = 5.0,
    request_fn: RequestFn | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture a fresh baseline, compare a second interval and retain both."""

    normalized_commit = str(commit_sha).strip().lower()
    if len(normalized_commit) != 40 or any(char not in "0123456789abcdef" for char in normalized_commit):
        raise ValueError("hosted load gate requires the exact 40-character commit SHA")
    if service_identity.get("system_id") != "stock-ai-system" or service_identity.get("status") != "ok":
        raise ValueError("load target did not identify as a healthy Stock AI service")
    service_commit = str(service_identity.get("build_commit") or "").strip().lower()
    if not service_commit or not normalized_commit.startswith(service_commit):
        raise ValueError("load target commit does not match the requested candidate")

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    environment = {
        "runner": "github-hosted" if os.getenv("GITHUB_ACTIONS") == "true" else "local",
        "commit_sha": normalized_commit,
        "service_system_id": "stock-ai-system",
    }
    baseline_metrics = collect_load_metrics(
        profile,
        url=url,
        timeout_seconds=timeout_seconds,
        request_fn=request_fn,
    )
    baseline = None
    if baseline_metrics["samples"] > 0:
        baseline = PerformanceBaselineStore(destination / "baselines").record(
            profile.name,
            baseline_metrics,
            environment=environment,
            captured_at=timestamp,
        )
    candidate_metrics = collect_load_metrics(
        profile,
        url=url,
        timeout_seconds=timeout_seconds,
        request_fn=request_fn,
    )
    report = evaluate_load_test(
        profile,
        runner="open_stock_ai.hosted_load_gate",
        baseline=baseline.metrics if baseline is not None else None,
        candidate=candidate_metrics,
    )
    write_report(report, destination / "load-report.json")
    profile_payload = profile.as_dict()
    _write_immutable(destination / "load-profile.json", profile_payload)

    blockers = list(report.performance.blockers)
    minimum_samples = max(1, int(profile.target_requests_per_second * profile.duration_seconds * 0.8))
    if baseline_metrics["samples"] < minimum_samples:
        blockers.append("baseline_sample_count_below_profile_minimum")
    if candidate_metrics["samples"] < minimum_samples:
        blockers.append("candidate_sample_count_below_profile_minimum")
    payload = {
        "schema_version": HOSTED_LOAD_GATE_SCHEMA,
        "commit_sha": normalized_commit,
        "captured_at": timestamp,
        "service_identity": {
            "status": "ok",
            "system_id": "stock-ai-system",
            "build_commit": service_commit,
        },
        "profile": profile_payload,
        "minimum_samples_per_interval": minimum_samples,
        "baseline_artifact": baseline.as_dict() if baseline is not None else None,
        "candidate_metrics": candidate_metrics,
        "load_report": report.as_dict(),
        "blockers": sorted(set(blockers)),
        "passed": report.performance.passed and not blockers,
    }
    receipt = {**payload, "receipt_sha256": _digest(payload)}
    _write_immutable(destination / "load-gate-receipt.json", receipt)
    return receipt


def verify_hosted_load_gate_receipt(receipt: dict[str, Any]) -> bool:
    required = {
        "schema_version",
        "commit_sha",
        "captured_at",
        "service_identity",
        "profile",
        "minimum_samples_per_interval",
        "baseline_artifact",
        "candidate_metrics",
        "load_report",
        "blockers",
        "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_LOAD_GATE_SCHEMA:
        return False
    supplied = str(receipt.get("receipt_sha256") or "")
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), supplied):
        return False
    baseline_payload = receipt.get("baseline_artifact")
    baseline = None
    if baseline_payload is not None:
        if not isinstance(baseline_payload, dict):
            return False
        try:
            baseline = PerformanceBaselineArtifact(
                operation=str(baseline_payload["operation"]),
                metrics=dict(baseline_payload["metrics"]),
                captured_at=str(baseline_payload["captured_at"]),
                environment=dict(baseline_payload.get("environment") or {}),
                artifact_sha256=str(baseline_payload["artifact_sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
    report = receipt.get("load_report")
    if baseline is not None and not baseline.verify():
        return False
    if baseline is None and "baseline_sample_count_below_profile_minimum" not in receipt.get("blockers", []):
        return False
    if not isinstance(report, dict) or not verify_load_test_report(report):
        return False
    expected_passed = not receipt.get("blockers") and report["performance"].get("passed") is True
    return receipt.get("passed") is expected_passed


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable(path: Path, payload: dict[str, Any]) -> None:
    encoded = _canonical(payload).decode("utf-8") + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"refusing to overwrite different hosted load evidence: {path}")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "HOSTED_LOAD_GATE_SCHEMA",
    "run_hosted_load_gate",
    "verify_hosted_load_gate_receipt",
]
