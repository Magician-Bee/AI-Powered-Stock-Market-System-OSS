"""Hosted two-runner SQLite backup and clean restore evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .sqlite_backup import SQLiteBackupReceipt


HOSTED_BACKUP_GATE_SCHEMA = "open_stock_ai.hosted_backup_gate_receipt.v1"


def build_hosted_backup_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted backup gate requires the exact candidate commit SHA")
    blockers = []
    if not _valid_evidence(evidence):
        blockers.append("off_host_backup_restore_invariant_failed")
    payload = {
        "schema_version": HOSTED_BACKUP_GATE_SCHEMA,
        "commit_sha": commit,
        "run_id": str(run_id),
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "backup_job": str(evidence.get("backup_job") or ""),
        "restore_job": str(evidence.get("restore_job") or ""),
        "off_host_artifact": str(evidence.get("off_host_artifact") or ""),
        "policy_decision": dict(evidence.get("policy_decision") or {}),
        "backup_receipt": dict(evidence.get("backup_receipt") or {}),
        "restoration": dict(evidence.get("restoration") or {}),
        "source_content_sha256": str(evidence.get("source_content_sha256") or ""),
        "restored_content_sha256": str(evidence.get("restored_content_sha256") or ""),
        "existing_target_refused": evidence.get("existing_target_refused") is True,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_backup_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "run_id", "captured_at", "backup_job", "restore_job",
        "off_host_artifact", "policy_decision", "backup_receipt", "restoration",
        "source_content_sha256", "restored_content_sha256", "existing_target_refused",
        "blockers", "passed", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_BACKUP_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    valid = _valid_evidence(receipt)
    expected_passed = valid and not receipt.get("blockers")
    return receipt.get("passed") is expected_passed


def _valid_evidence(evidence: Mapping[str, Any]) -> bool:
    try:
        backup = SQLiteBackupReceipt.from_dict(dict(evidence.get("backup_receipt") or {}))
    except (TypeError, ValueError, RuntimeError):
        return False
    policy = evidence.get("policy_decision") or {}
    policy_payload = {
        key: policy.get(key)
        for key in ("schema_version", "interval_hours", "retention_count", "destination_uri", "off_host_required", "owner")
    }
    policy_hash = hashlib.sha256(
        json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    restoration = evidence.get("restoration") or {}
    content_hash = str(evidence.get("source_content_sha256") or "")
    return (
        backup.integrity_check == "ok"
        and policy.get("policy_sha256") == policy_hash
        and policy.get("due") is True
        and policy.get("off_host_required") is True
        and str(policy.get("destination_uri") or "").startswith("github-actions-artifact://")
        and policy.get("retention_count", 0) >= 2
        and restoration.get("verified") is True
        and restoration.get("restored") is True
        and restoration.get("restored_integrity_check") == "ok"
        and restoration.get("schema_sha256") == backup.schema_sha256
        and len(content_hash) == 64
        and evidence.get("restored_content_sha256") == content_hash
        and evidence.get("existing_target_refused") is True
        and evidence.get("backup_job") == "create-backup"
        and evidence.get("restore_job") == "clean-runner-restore"
        and str(evidence.get("off_host_artifact") or "").startswith("stock-ai-sqlite-backup-")
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["HOSTED_BACKUP_GATE_SCHEMA", "build_hosted_backup_gate_receipt", "verify_hosted_backup_gate_receipt"]
