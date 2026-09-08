"""GitHub-hosted verification receipt for critical retention archives."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .retention_archive import (
    verify_critical_retention_archive,
    verify_critical_retention_restore,
)


HOSTED_RETENTION_GATE_SCHEMA = "open_stock_ai.hosted_critical_retention_gate.v1"
REQUIRED_CRITICAL_SCHEMAS = (
    "stock_ai.paper_oms_execution_snapshot.v1",
    "stock_ai.paper_broker_execution_snapshot.v1",
    "stock_ai.broker_oms_execution_state.v1",
)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def build_hosted_retention_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha or "").strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted retention gate requires the exact candidate commit SHA")
    archive = dict(evidence.get("archive") or {})
    restore = dict(evidence.get("restore") or {})
    valid = _valid_evidence(evidence, archive=archive, restore=restore)
    payload = {
        "schema_version": HOSTED_RETENTION_GATE_SCHEMA,
        "commit_sha": commit,
        "run_id": str(run_id or ""),
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))()
        .astimezone(timezone.utc)
        .isoformat(),
        "export_job": str(evidence.get("export_job") or ""),
        "restore_job": str(evidence.get("restore_job") or ""),
        "off_host_artifact": str(evidence.get("off_host_artifact") or ""),
        "artifact_retention_days": int(evidence.get("artifact_retention_days") or 0),
        "production_years_satisfied": evidence.get("production_years_satisfied") is True,
        "scope": str(evidence.get("scope") or ""),
        "archive_file_sha256": str(evidence.get("archive_file_sha256") or ""),
        "archive": archive,
        "restore": restore,
        "required_critical_schemas": list(REQUIRED_CRITICAL_SCHEMAS),
        "blockers": [] if valid else ["hosted_critical_retention_archive_invariant_failed"],
        "passed": valid,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_retention_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version",
        "commit_sha",
        "run_id",
        "captured_at",
        "export_job",
        "restore_job",
        "off_host_artifact",
        "artifact_retention_days",
        "production_years_satisfied",
        "scope",
        "archive_file_sha256",
        "archive",
        "restore",
        "required_critical_schemas",
        "blockers",
        "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_RETENTION_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    archive = receipt.get("archive")
    restore = receipt.get("restore")
    if not isinstance(archive, Mapping) or not isinstance(restore, Mapping):
        return False
    valid = _valid_evidence(receipt, archive=archive, restore=restore)
    return (
        receipt.get("required_critical_schemas") == list(REQUIRED_CRITICAL_SCHEMAS)
        and receipt.get("passed") is (valid and not receipt.get("blockers"))
    )


def _valid_evidence(
    evidence: Mapping[str, Any],
    *,
    archive: Mapping[str, Any],
    restore: Mapping[str, Any],
) -> bool:
    records = archive.get("records") or []
    schemas = {
        str((record.get("payload") or {}).get("schema_version") or "")
        for record in records
        if isinstance(record, Mapping)
    }
    archive_file_hash = str(evidence.get("archive_file_sha256") or "")
    return (
        verify_critical_retention_archive(archive)
        and verify_critical_retention_restore(restore, archive)
        and set(REQUIRED_CRITICAL_SCHEMAS).issubset(schemas)
        and len(archive_file_hash) == 64
        and evidence.get("export_job") == "export-critical-retention"
        and evidence.get("restore_job") == "clean-runner-restore"
        and str(evidence.get("off_host_artifact") or "").startswith(
            "stock-ai-critical-retention-"
        )
        and int(evidence.get("artifact_retention_days") or 0) >= 90
        and evidence.get("scope") == "hosted_drill_only"
        and evidence.get("production_years_satisfied") is False
    )


__all__ = [
    "HOSTED_RETENTION_GATE_SCHEMA",
    "REQUIRED_CRITICAL_SCHEMAS",
    "build_hosted_retention_gate_receipt",
    "verify_hosted_retention_gate_receipt",
]
