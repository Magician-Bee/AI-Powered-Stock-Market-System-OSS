"""Hash-verified envelope for the hosted strategy/model rollback drill."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .artifact_rollback import verify_rollback_receipt


HOSTED_ROLLBACK_GATE_SCHEMA = "open_stock_ai.hosted_rollback_gate_receipt.v1"
ROLLBACK_SCOPES = ("strategy", "model")


def build_hosted_rollback_gate_receipt(
    observations: Mapping[str, Any],
    *,
    commit_sha: str,
    server_pid: int,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted rollback gate requires the exact candidate commit SHA")
    scope_results = []
    blockers = []
    for scope in ROLLBACK_SCOPES:
        result = dict((observations.get("scope_results") or {}).get(scope) or {})
        scope_results.append({"scope": scope, **result})
        if not _valid_scope_result(scope, result):
            blockers.append(f"rollback_invariant_failed:{scope}")
    for name in ("immutable_conflict_rejected", "unapproved_activation_rejected", "agent_rollback_rejected"):
        if observations.get(name) is not True:
            blockers.append(f"rollback_guard_failed:{name}")
    if observations.get("database_quick_check") != "ok":
        blockers.append("rollback_database_quick_check_failed")
    payload = {
        "schema_version": HOSTED_ROLLBACK_GATE_SCHEMA,
        "commit_sha": commit,
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "server_pid": int(server_pid),
        "immutable_conflict_rejected": observations.get("immutable_conflict_rejected") is True,
        "unapproved_activation_rejected": observations.get("unapproved_activation_rejected") is True,
        "agent_rollback_rejected": observations.get("agent_rollback_rejected") is True,
        "database_quick_check": str(observations.get("database_quick_check") or ""),
        "scope_results": scope_results,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_rollback_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "captured_at", "server_pid", "immutable_conflict_rejected",
        "unapproved_activation_rejected", "agent_rollback_rejected", "database_quick_check",
        "scope_results", "blockers", "passed", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_ROLLBACK_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    results = receipt.get("scope_results")
    if not isinstance(results, list) or [item.get("scope") for item in results if isinstance(item, Mapping)] != list(ROLLBACK_SCOPES):
        return False
    valid = isinstance(receipt.get("server_pid"), int) and receipt["server_pid"] > 0
    valid = valid and receipt.get("database_quick_check") == "ok"
    valid = valid and all(receipt.get(name) is True for name in (
        "immutable_conflict_rejected", "unapproved_activation_rejected", "agent_rollback_rejected"
    ))
    valid = valid and all(_valid_scope_result(str(item.get("scope")), item) for item in results)
    expected_passed = valid and not receipt.get("blockers")
    return receipt.get("passed") is expected_passed


def _valid_scope_result(scope: str, result: Mapping[str, Any]) -> bool:
    receipt = result.get("rollback_receipt")
    return (
        scope in ROLLBACK_SCOPES
        and isinstance(receipt, dict)
        and verify_rollback_receipt(receipt)
        and receipt.get("from_artifact_id") == f"{scope}-v2"
        and receipt.get("to_artifact_id") == f"{scope}-v1"
        and result.get("before_artifact_id") == f"{scope}-v2"
        and result.get("after_artifact_id") == f"{scope}-v1"
        and result.get("durable_after_restart") == f"{scope}-v1"
        and result.get("other_lane_unchanged") is True
        and result.get("http_status") == 200
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HOSTED_ROLLBACK_GATE_SCHEMA",
    "ROLLBACK_SCOPES",
    "build_hosted_rollback_gate_receipt",
    "verify_hosted_rollback_gate_receipt",
]
