"""Tamper-evident hosted failure-storm evidence for shared circuit breakers."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from open_stock_ai.agent_runtime.circuit_breaker import verify_circuit_breaker_receipt


HOSTED_CIRCUIT_GATE_SCHEMA = "open_stock_ai.hosted_circuit_gate_receipt.v1"
HOSTED_CIRCUIT_SCOPES = (
    "provider:hosted-failure-storm",
    "source:hosted-failure-storm",
    "broker:hosted-failure-storm",
)


def build_hosted_circuit_gate_receipt(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    commit_sha: str,
    server_pid: int,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted circuit gate requires the exact candidate commit SHA")
    scopes: list[dict[str, Any]] = []
    blockers: list[str] = []
    for scope in HOSTED_CIRCUIT_SCOPES:
        observation = dict(observations.get(scope) or {})
        scopes.append({"scope": scope, **observation})
        if not _valid_scope_observation(scope, observation):
            blockers.append(f"failure_storm_invariant_failed:{scope}")
    payload = {
        "schema_version": HOSTED_CIRCUIT_GATE_SCHEMA,
        "commit_sha": commit,
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "server_pid": int(server_pid),
        "scopes": scopes,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_circuit_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "captured_at", "server_pid", "scopes", "blockers", "passed", "receipt_sha256"
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_CIRCUIT_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    scopes = receipt.get("scopes")
    if not isinstance(scopes, list) or [item.get("scope") for item in scopes if isinstance(item, Mapping)] != list(HOSTED_CIRCUIT_SCOPES):
        return False
    valid = isinstance(receipt.get("server_pid"), int) and receipt["server_pid"] > 0
    valid = valid and all(_valid_scope_observation(str(item.get("scope")), item) for item in scopes)
    expected_passed = valid and not receipt.get("blockers")
    return receipt.get("passed") is expected_passed


def _valid_scope_observation(scope: str, observation: Mapping[str, Any]) -> bool:
    failures = observation.get("failure_decisions")
    if not isinstance(failures, list) or len(failures) != 3:
        return False
    receipts = failures + [
        observation.get("blocked_decision"),
        observation.get("isolation_decision"),
        observation.get("probe_decision"),
        observation.get("recovered_decision"),
    ]
    if not all(isinstance(item, dict) and verify_circuit_breaker_receipt(item) for item in receipts):
        return False
    blocked = observation["blocked_decision"]
    isolation = observation["isolation_decision"]
    probe = observation["probe_decision"]
    recovered = observation["recovered_decision"]
    return (
        all(item.get("scope") == scope for item in failures)
        and [item.get("state") for item in failures] == ["closed", "closed", "open"]
        and blocked.get("scope") == scope
        and blocked.get("allowed") is False
        and blocked.get("reason") == "open_backoff_active"
        and isolation.get("allowed") is True
        and isolation.get("scope") != scope
        and probe.get("scope") == scope
        and probe.get("state") == "half_open"
        and probe.get("reason") == "half_open_probe_allowed"
        and recovered.get("scope") == scope
        and recovered.get("state") == "closed"
        and recovered.get("reason") == "success_closed_circuit"
        and observation.get("requests_before_block") == 3
        and observation.get("requests_after_block") == 3
        and observation.get("requests_after_recovery") == 4
        and observation.get("recovery_status") == 200
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HOSTED_CIRCUIT_GATE_SCHEMA",
    "HOSTED_CIRCUIT_SCOPES",
    "build_hosted_circuit_gate_receipt",
    "verify_hosted_circuit_gate_receipt",
]
