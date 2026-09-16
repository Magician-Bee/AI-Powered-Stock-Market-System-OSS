"""Hosted migration retention and off-host verification receipt."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .migration_safety import (
    MigrationSafetyReceipt,
    migration_manifest,
    verify_migration_cleanup_report,
)


HOSTED_MIGRATION_GATE_SCHEMA = "open_stock_ai.hosted_migration_retention_gate.v1"


def build_hosted_migration_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted migration gate requires the exact candidate commit SHA")
    blockers = [] if _valid_evidence(evidence) else ["hosted_migration_retention_invariant_failed"]
    payload = {
        "schema_version": HOSTED_MIGRATION_GATE_SCHEMA,
        "commit_sha": commit,
        "run_id": str(run_id),
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "migration_job": str(evidence.get("migration_job") or ""),
        "verification_job": str(evidence.get("verification_job") or ""),
        "off_host_artifact": str(evidence.get("off_host_artifact") or ""),
        "artifact_retention_days": int(evidence.get("artifact_retention_days") or 0),
        "migration_manifest": dict(evidence.get("migration_manifest") or {}),
        "migration_receipt": dict(evidence.get("migration_receipt") or {}),
        "backup_verification": dict(evidence.get("backup_verification") or {}),
        "dry_run_report": dict(evidence.get("dry_run_report") or {}),
        "execution_report": dict(evidence.get("execution_report") or {}),
        "retained_pairs": [dict(item) for item in (evidence.get("retained_pairs") or [])],
        "retained_completed_count": int(evidence.get("retained_completed_count") or 0),
        "prepared_pairs_preserved": int(evidence.get("prepared_pairs_preserved") or 0),
        "unknown_files_preserved": int(evidence.get("unknown_files_preserved") or 0),
        "retained_pairs_verified": evidence.get("retained_pairs_verified") is True,
        "legacy_content_verified": evidence.get("legacy_content_verified") is True,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_migration_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "run_id", "captured_at", "migration_job",
        "verification_job", "off_host_artifact", "artifact_retention_days",
        "migration_manifest", "migration_receipt", "backup_verification",
        "dry_run_report", "execution_report", "retained_completed_count",
        "retained_pairs",
        "prepared_pairs_preserved", "unknown_files_preserved",
        "retained_pairs_verified", "legacy_content_verified", "blockers", "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_MIGRATION_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    valid = _valid_evidence(receipt)
    return receipt.get("passed") is (valid and not receipt.get("blockers"))


def _valid_evidence(evidence: Mapping[str, Any]) -> bool:
    try:
        receipt = MigrationSafetyReceipt.from_dict(dict(evidence.get("migration_receipt") or {}))
    except (TypeError, ValueError, RuntimeError):
        return False
    manifest = evidence.get("migration_manifest") or {}
    current_manifest = migration_manifest()
    dry_run = dict(evidence.get("dry_run_report") or {})
    execution = dict(evidence.get("execution_report") or {})
    backup = evidence.get("backup_verification") or {}
    planned = dry_run.get("planned") or []
    deleted = execution.get("deleted") or []
    retained_pairs = evidence.get("retained_pairs") or []
    return (
        manifest == current_manifest
        and receipt.status == "completed"
        and receipt.from_version == 0
        and receipt.to_version == current_manifest["latest_schema_version"]
        and receipt.migration_manifest_sha256 == current_manifest["manifest_sha256"]
        and backup.get("verified") is True
        and backup.get("integrity_check") == "ok"
        and backup.get("backup_sha256") == receipt.backup_receipt.get("backup_sha256")
        and verify_migration_cleanup_report(dry_run)
        and verify_migration_cleanup_report(execution)
        and dry_run.get("execute") is False
        and execution.get("execute") is True
        and dry_run.get("retention_count") == execution.get("retention_count") == 2
        and len(planned) == 2
        and dry_run.get("deleted") == []
        and execution.get("planned") == planned
        and deleted == planned
        and not dry_run.get("blockers")
        and not execution.get("blockers")
        and evidence.get("retained_completed_count") == 2
        and len(retained_pairs) == 3
        and all(isinstance(item, Mapping) for item in retained_pairs)
        and len({item.get("backup") for item in retained_pairs}) == 3
        and len({item.get("receipt") for item in retained_pairs}) == 3
        and [item.get("status") for item in retained_pairs].count("completed") == 2
        and [item.get("status") for item in retained_pairs].count("prepared") == 1
        and all(len(str(item.get("backup_sha256") or "")) == 64 for item in retained_pairs)
        and all(len(str(item.get("receipt_sha256") or "")) == 64 for item in retained_pairs)
        and evidence.get("prepared_pairs_preserved") == 1
        and evidence.get("unknown_files_preserved") == 1
        and evidence.get("retained_pairs_verified") is True
        and evidence.get("legacy_content_verified") is True
        and evidence.get("artifact_retention_days", 0) >= 90
        and evidence.get("migration_job") == "migration-and-cleanup"
        and evidence.get("verification_job") == "clean-runner-verify"
        and str(evidence.get("off_host_artifact") or "").startswith("stock-ai-migration-retention-")
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HOSTED_MIGRATION_GATE_SCHEMA",
    "build_hosted_migration_gate_receipt",
    "verify_hosted_migration_gate_receipt",
]
