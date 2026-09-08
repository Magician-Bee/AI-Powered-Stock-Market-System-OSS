"""Portable, content-addressed archives for execution-critical retention data."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .content_retention import (
    ContentAddressedRetentionLedger,
    RetentionError,
    verify_retention_receipt,
)
from .durable_store import SQLiteRetentionStore


ARCHIVE_SCHEMA = "open_stock_ai.critical_retention_archive.v1"
RESTORE_SCHEMA = "open_stock_ai.critical_retention_restore_receipt.v1"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise RetentionError("retention_archive_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise RetentionError("retention_archive_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def build_critical_retention_archive(
    store: SQLiteRetentionStore,
    *,
    source_authority: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Export only immutable critical records and the retention receipt chain."""

    authority = str(source_authority or "").strip()
    if not authority:
        raise RetentionError("retention_archive_source_authority_required")
    records = sorted(
        (dict(item) for item in store.load_records() if item.get("critical") is True),
        key=lambda item: str(item.get("record_id") or ""),
    )
    if not records:
        raise RetentionError("retention_archive_requires_critical_records")
    receipts = sorted(
        (dict(item) for item in store.load_receipts()),
        key=lambda item: str(item.get("receipt_sha256") or ""),
    )
    payload = {
        "schema_version": ARCHIVE_SCHEMA,
        "source_authority": authority,
        "created_at": _utc(created_at or datetime.now(timezone.utc).isoformat()),
        "critical_count": len(records),
        "record_ids": [str(item["record_id"]) for item in records],
        "content_sha256": [str(item["content_sha256"]) for item in records],
        "retention_receipt_sha256": [str(item["receipt_sha256"]) for item in receipts],
        "records": records,
        "retention_receipts": receipts,
    }
    archive = {**payload, "archive_sha256": _digest(payload)}
    if not verify_critical_retention_archive(archive):
        raise RetentionError("retention_archive_self_verification_failed")
    return archive


def verify_critical_retention_archive(archive: Mapping[str, Any]) -> bool:
    required = {
        "schema_version",
        "source_authority",
        "created_at",
        "critical_count",
        "record_ids",
        "content_sha256",
        "retention_receipt_sha256",
        "records",
        "retention_receipts",
        "archive_sha256",
    }
    if set(archive) != required or archive.get("schema_version") != ARCHIVE_SCHEMA:
        return False
    payload = {key: archive[key] for key in required if key != "archive_sha256"}
    if not hmac.compare_digest(_digest(payload), str(archive.get("archive_sha256") or "")):
        return False
    try:
        _utc(str(archive.get("created_at") or ""))
    except RetentionError:
        return False
    if not str(archive.get("source_authority") or "").strip():
        return False
    records = archive.get("records")
    receipts = archive.get("retention_receipts")
    if not isinstance(records, list) or not records or not isinstance(receipts, list):
        return False
    expected_record_keys = {
        "schema_version",
        "record_id",
        "kind",
        "critical",
        "content_sha256",
        "occurred_at",
        "payload",
    }
    record_ids: list[str] = []
    content_hashes: list[str] = []
    for record in records:
        if not isinstance(record, Mapping) or set(record) != expected_record_keys:
            return False
        if record.get("schema_version") != "open_stock_ai.retained_content.v1":
            return False
        if record.get("critical") is not True or not str(record.get("kind") or "").strip():
            return False
        record_id = str(record.get("record_id") or "").strip()
        content_hash = str(record.get("content_sha256") or "")
        if not record_id or len(content_hash) != 64 or _digest(record.get("payload") or {}) != content_hash:
            return False
        try:
            _utc(str(record.get("occurred_at") or ""))
        except RetentionError:
            return False
        record_ids.append(record_id)
        content_hashes.append(content_hash)
    receipt_hashes: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or not verify_retention_receipt(dict(receipt)):
            return False
        receipt_hashes.append(str(receipt["receipt_sha256"]))
    return (
        len(record_ids) == len(set(record_ids)) == archive.get("critical_count")
        and record_ids == sorted(record_ids)
        and record_ids == archive.get("record_ids")
        and content_hashes == archive.get("content_sha256")
        and receipt_hashes == sorted(receipt_hashes)
        and receipt_hashes == archive.get("retention_receipt_sha256")
    )


def restore_critical_retention_archive(
    archive: Mapping[str, Any],
    target_database: str | Path,
    *,
    restored_at: str | None = None,
) -> dict[str, Any]:
    """Materialize an archive into a new SQLite authority without overwriting."""

    if not verify_critical_retention_archive(archive):
        raise RetentionError("retention_archive_invalid")
    target = Path(target_database).expanduser().resolve()
    if target.exists():
        raise RetentionError("retention_archive_restore_target_exists")
    store = SQLiteRetentionStore(target)
    ledger = ContentAddressedRetentionLedger(store=store)
    try:
        for record in archive["records"]:
            ledger.append(
                str(record["record_id"]),
                dict(record["payload"]),
                critical=True,
                kind=str(record["kind"]),
                occurred_at=str(record["occurred_at"]),
            )
        for receipt in archive["retention_receipts"]:
            store.prune_and_save_receipt([], dict(receipt))
        reopened = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(target))
        restored_records = [
            reopened.get(record_id) for record_id in archive["record_ids"]
        ]
        if restored_records != list(archive["records"]):
            raise RetentionError("retention_archive_restore_record_mismatch")
        reopened_receipts = sorted(
            (item.as_dict() for item in reopened.receipts),
            key=lambda item: str(item.get("receipt_sha256") or ""),
        )
        if reopened_receipts != list(archive["retention_receipts"]):
            raise RetentionError("retention_archive_restore_receipt_mismatch")
        with sqlite3.connect(target) as connection:
            quick_check = str(connection.execute("pragma quick_check").fetchone()[0])
        payload = {
            "schema_version": RESTORE_SCHEMA,
            "archive_sha256": str(archive["archive_sha256"]),
            "source_authority": str(archive["source_authority"]),
            "restored_at": _utc(restored_at or datetime.now(timezone.utc).isoformat()),
            "restored_record_ids": list(archive["record_ids"]),
            "restored_content_sha256": list(archive["content_sha256"]),
            "restored_retention_receipt_sha256": list(archive["retention_receipt_sha256"]),
            "quick_check": quick_check,
        }
        receipt = {**payload, "receipt_sha256": _digest(payload)}
        if not verify_critical_retention_restore(receipt, archive):
            raise RetentionError("retention_archive_restore_self_verification_failed")
        return receipt
    except Exception:
        # A failed restore is never left behind as an apparently usable authority.
        target.unlink(missing_ok=True)
        Path(str(target) + "-wal").unlink(missing_ok=True)
        Path(str(target) + "-shm").unlink(missing_ok=True)
        raise


def verify_critical_retention_restore(
    receipt: Mapping[str, Any], archive: Mapping[str, Any]
) -> bool:
    required = {
        "schema_version",
        "archive_sha256",
        "source_authority",
        "restored_at",
        "restored_record_ids",
        "restored_content_sha256",
        "restored_retention_receipt_sha256",
        "quick_check",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != RESTORE_SCHEMA:
        return False
    if not verify_critical_retention_archive(archive):
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    try:
        _utc(str(receipt.get("restored_at") or ""))
    except RetentionError:
        return False
    return (
        hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or ""))
        and receipt.get("archive_sha256") == archive.get("archive_sha256")
        and receipt.get("source_authority") == archive.get("source_authority")
        and receipt.get("restored_record_ids") == archive.get("record_ids")
        and receipt.get("restored_content_sha256") == archive.get("content_sha256")
        and receipt.get("restored_retention_receipt_sha256")
        == archive.get("retention_receipt_sha256")
        and receipt.get("quick_check") == "ok"
    )


__all__ = [
    "ARCHIVE_SCHEMA",
    "RESTORE_SCHEMA",
    "build_critical_retention_archive",
    "restore_critical_retention_archive",
    "verify_critical_retention_archive",
    "verify_critical_retention_restore",
]
