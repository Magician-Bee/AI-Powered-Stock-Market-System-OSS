"""Candidate-bound receipt for hosted Session archive recovery drills."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .session_archival import MATERIALIZE_SCHEMA_VERSION, verify_session_archive


SCHEMA_VERSION = "open_stock_ai.hosted_session_archive_gate.v1"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def build_hosted_session_archive_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha or "").strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("hosted Session archive gate requires the exact candidate commit SHA")
    valid = _valid_evidence(evidence, commit_sha=commit, run_id=str(run_id or ""))
    payload = {
        "schema_version": SCHEMA_VERSION,
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
        "archive": dict(evidence.get("archive") or {}),
        "restore": dict(evidence.get("restore") or {}),
        "restored_database_sha256": str(evidence.get("restored_database_sha256") or ""),
        "quick_check": str(evidence.get("quick_check") or ""),
        "materialized_archive_verified": evidence.get("materialized_archive_verified") is True,
        "overwrite_refused": evidence.get("overwrite_refused") is True,
        "blockers": [] if valid else ["hosted_session_archive_invariant_failed"],
        "passed": valid,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_session_archive_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "run_id", "captured_at", "export_job",
        "restore_job", "off_host_artifact", "artifact_retention_days",
        "archive_file_sha256", "archive", "restore", "restored_database_sha256",
        "quick_check", "materialized_archive_verified", "overwrite_refused",
        "blockers", "passed", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    valid = _valid_evidence(
        receipt,
        commit_sha=str(receipt.get("commit_sha") or ""),
        run_id=str(receipt.get("run_id") or ""),
    )
    return receipt.get("passed") is (valid and not receipt.get("blockers"))


def _valid_evidence(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
) -> bool:
    archive = evidence.get("archive")
    restore = evidence.get("restore")
    if not isinstance(archive, dict) or not isinstance(restore, Mapping):
        return False
    try:
        verified = verify_session_archive(archive, expected_session_id="AS-hosted-archive")
    except ValueError:
        return False
    encoded = json.dumps(archive, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    archive_text = encoded.decode("utf-8")
    return (
        len(commit_sha) == 40
        and all(character in "0123456789abcdef" for character in commit_sha)
        and bool(run_id)
        and evidence.get("commit_sha", commit_sha) == commit_sha
        and str(evidence.get("run_id") or run_id) == run_id
        and evidence.get("export_job") == "export-session-archive"
        and evidence.get("restore_job") == "clean-runner-session-restore"
        and str(evidence.get("off_host_artifact") or "").startswith("stock-ai-session-archive-")
        and int(evidence.get("artifact_retention_days") or 0) >= 90
        and hashlib.sha256(encoded).hexdigest() == evidence.get("archive_file_sha256")
        and verified["run_count"] == 1
        and verified["message_count"] == 2
        and "must-not-leak" not in archive_text
        and restore.get("schema_version") == MATERIALIZE_SCHEMA_VERSION
        and restore.get("restore_mode") == "fresh_sqlite_authoritative"
        and restore.get("restored") is True
        and restore.get("read_only") is False
        and restore.get("archive_sha256") == verified["archive_sha256"]
        and len(str(evidence.get("restored_database_sha256") or "")) == 64
        and evidence.get("quick_check") == "ok"
        and evidence.get("materialized_archive_verified") is True
        and evidence.get("overwrite_refused") is True
    )


__all__ = [
    "SCHEMA_VERSION",
    "build_hosted_session_archive_gate_receipt",
    "verify_hosted_session_archive_gate_receipt",
]
