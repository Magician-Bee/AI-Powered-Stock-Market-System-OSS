"""Hosted proof that immutable audit records can reconstruct an order off host."""

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
from .security_audit import (
    AUDIT_EVENT_SCHEMA,
    verify_order_audit_reconstruction,
)


HOSTED_SECURITY_AUDIT_GATE_SCHEMA = "stock_ai.hosted_security_audit_gate.v1"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def build_hosted_security_audit_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    order_reconstruction: Mapping[str, Any],
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha or "").strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted security audit gate requires the exact candidate commit SHA")
    archive = dict(evidence.get("archive") or {})
    restore = dict(evidence.get("restore") or {})
    reconstruction = dict(order_reconstruction)
    valid = _valid(evidence, archive, restore, reconstruction)
    payload = {
        "schema_version": HOSTED_SECURITY_AUDIT_GATE_SCHEMA,
        "commit_sha": commit,
        "run_id": str(run_id or ""),
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))()
        .astimezone(timezone.utc)
        .isoformat(),
        "export_job": str(evidence.get("export_job") or ""),
        "restore_job": str(evidence.get("restore_job") or ""),
        "off_host_artifact": str(evidence.get("off_host_artifact") or ""),
        "artifact_retention_days": int(evidence.get("artifact_retention_days") or 0),
        "archive_file_sha256": str(evidence.get("archive_file_sha256") or ""),
        "archive_sha256": str(archive.get("archive_sha256") or ""),
        "restore_receipt_sha256": str(restore.get("receipt_sha256") or ""),
        "order_reconstruction": reconstruction,
        "immutable_off_host_sink_verified": valid,
        "order_reconstruction_verified": valid,
        "blockers": [] if valid else ["hosted_security_audit_invariant_failed"],
        "passed": valid,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_security_audit_gate_receipt(
    receipt: Mapping[str, Any], archive: Mapping[str, Any], restore: Mapping[str, Any]
) -> bool:
    required = {
        "schema_version",
        "commit_sha",
        "run_id",
        "captured_at",
        "export_job",
        "restore_job",
        "off_host_artifact",
        "artifact_retention_days",
        "archive_file_sha256",
        "archive_sha256",
        "restore_receipt_sha256",
        "order_reconstruction",
        "immutable_off_host_sink_verified",
        "order_reconstruction_verified",
        "blockers",
        "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_SECURITY_AUDIT_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    evidence = {
        "archive": archive,
        "restore": restore,
        "commit_sha": receipt.get("commit_sha"),
        "run_id": receipt.get("run_id"),
        "export_job": receipt.get("export_job"),
        "restore_job": receipt.get("restore_job"),
        "off_host_artifact": receipt.get("off_host_artifact"),
        "artifact_retention_days": receipt.get("artifact_retention_days"),
        "archive_file_sha256": receipt.get("archive_file_sha256"),
    }
    valid = _valid(
        evidence,
        archive,
        restore,
        receipt.get("order_reconstruction") or {},
    )
    return (
        receipt.get("archive_sha256") == archive.get("archive_sha256")
        and receipt.get("restore_receipt_sha256") == restore.get("receipt_sha256")
        and receipt.get("immutable_off_host_sink_verified") is valid
        and receipt.get("order_reconstruction_verified") is valid
        and receipt.get("passed") is (valid and not receipt.get("blockers"))
    )


def _valid(
    evidence: Mapping[str, Any],
    archive: Mapping[str, Any],
    restore: Mapping[str, Any],
    reconstruction: Mapping[str, Any],
) -> bool:
    schemas = {
        str((record.get("payload") or {}).get("schema_version") or "")
        for record in archive.get("records") or []
        if isinstance(record, Mapping)
    }
    return (
        verify_critical_retention_archive(archive)
        and verify_critical_retention_restore(restore, archive)
        and verify_order_audit_reconstruction(reconstruction, archive)
        and reconstruction.get("complete") is True
        and AUDIT_EVENT_SCHEMA in schemas
        and archive.get("source_authority")
        == f"github-actions:{evidence.get('commit_sha')}:{evidence.get('run_id')}"
        and evidence.get("export_job") == "export-critical-retention"
        and evidence.get("restore_job") == "clean-runner-restore"
        and str(evidence.get("off_host_artifact") or "").startswith(
            "stock-ai-critical-retention-"
        )
        and int(evidence.get("artifact_retention_days") or 0) >= 90
        and len(str(evidence.get("archive_file_sha256") or "")) == 64
    )


__all__ = [
    "HOSTED_SECURITY_AUDIT_GATE_SCHEMA",
    "build_hosted_security_audit_gate_receipt",
    "verify_hosted_security_audit_gate_receipt",
]
