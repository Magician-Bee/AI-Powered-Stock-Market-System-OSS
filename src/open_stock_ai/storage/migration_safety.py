"""Startup migration checksums and pre-migration recovery receipts."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .sqlite_backup import (
    SQLiteBackupReceipt,
    SQLiteBackupError,
    create_sqlite_backup,
    restore_sqlite_backup,
    verify_sqlite_backup,
)


SCHEMA_VERSION = "open_stock_ai.migration_safety_receipt.v1"
CLEANUP_SCHEMA_VERSION = "open_stock_ai.migration_backup_cleanup.v1"


class MigrationSafetyError(RuntimeError):
    """Raised when a migration cannot be protected by verified evidence."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def migration_manifest() -> dict[str, Any]:
    """Return a contiguous, hashed manifest of every versioned migration."""

    from . import migrations

    entries: list[dict[str, Any]] = []
    for version in range(1, migrations.LATEST_SCHEMA_VERSION + 1):
        migration = getattr(migrations, f"_migration_{version}", None)
        if not callable(migration):
            raise MigrationSafetyError(f"migration_manifest_missing_version:{version}")
        try:
            source = inspect.getsource(migration)
        except (OSError, TypeError) as exc:
            raise MigrationSafetyError(f"migration_source_unavailable:{version}") from exc
        entries.append(
            {
                "version": version,
                "name": migration.__name__,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "latest_schema_version": migrations.LATEST_SCHEMA_VERSION,
        "migrations": entries,
    }
    return {**payload, "manifest_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest()}


def _current_schema_version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            row = connection.execute("select max(version) from schema_migrations").fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] or 0)
    finally:
        connection.close()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class MigrationSafetyReceipt:
    database_name: str
    from_version: int
    to_version: int
    migration_manifest_sha256: str
    backup_name: str
    backup_receipt: dict[str, Any]
    status: str
    prepared_at: str
    completed_at: str | None
    receipt_sha256: str
    receipt_path: Path | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "database_name": self.database_name,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "migration_manifest_sha256": self.migration_manifest_sha256,
            "backup_name": self.backup_name,
            "backup_receipt": self.backup_receipt,
            "status": self.status,
            "prepared_at": self.prepared_at,
            "completed_at": self.completed_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify(self) -> None:
        if self.status not in {"prepared", "completed"}:
            raise MigrationSafetyError("migration_receipt_status_invalid")
        expected = hashlib.sha256(_canonical_json(self.payload())).hexdigest()
        if expected != self.receipt_sha256:
            raise MigrationSafetyError("migration_receipt_hash_mismatch")
        SQLiteBackupReceipt.from_dict(self.backup_receipt)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, receipt_path: Path | None = None) -> "MigrationSafetyReceipt":
        required = {
            "schema_version", "database_name", "from_version", "to_version",
            "migration_manifest_sha256", "backup_name", "backup_receipt", "status",
            "prepared_at", "completed_at", "receipt_sha256",
        }
        if set(payload) != required or payload.get("schema_version") != SCHEMA_VERSION:
            raise MigrationSafetyError("migration_receipt_schema_mismatch")
        receipt = cls(
            database_name=str(payload["database_name"]),
            from_version=int(payload["from_version"]),
            to_version=int(payload["to_version"]),
            migration_manifest_sha256=str(payload["migration_manifest_sha256"]),
            backup_name=str(payload["backup_name"]),
            backup_receipt=dict(payload["backup_receipt"]),
            status=str(payload["status"]),
            prepared_at=str(payload["prepared_at"]),
            completed_at=str(payload["completed_at"]) if payload["completed_at"] is not None else None,
            receipt_sha256=str(payload["receipt_sha256"]),
            receipt_path=receipt_path,
        )
        receipt.verify()
        return receipt


def _build_receipt(
    *, database_name: str, from_version: int, to_version: int, manifest_sha256: str,
    backup_receipt: SQLiteBackupReceipt, status: str, prepared_at: str,
    completed_at: str | None, receipt_path: Path,
) -> MigrationSafetyReceipt:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "database_name": database_name,
        "from_version": from_version,
        "to_version": to_version,
        "migration_manifest_sha256": manifest_sha256,
        "backup_name": backup_receipt.backup_name,
        "backup_receipt": backup_receipt.as_dict(),
        "status": status,
        "prepared_at": prepared_at,
        "completed_at": completed_at,
    }
    return MigrationSafetyReceipt(
        database_name=database_name, from_version=from_version, to_version=to_version,
        migration_manifest_sha256=manifest_sha256, backup_name=backup_receipt.backup_name,
        backup_receipt=backup_receipt.as_dict(), status=status, prepared_at=prepared_at,
        completed_at=completed_at,
        receipt_sha256=hashlib.sha256(_canonical_json(payload)).hexdigest(),
        receipt_path=receipt_path,
    )


def prepare_startup_migration_backup(database_path: str | Path) -> MigrationSafetyReceipt | None:
    """Prepare a verified backup only when an existing DB needs migration."""

    database = Path(database_path)
    if not database.is_file() or database.is_symlink() or database.stat().st_size == 0:
        return None
    from .migrations import LATEST_SCHEMA_VERSION

    current = _current_schema_version(database)
    if current >= LATEST_SCHEMA_VERSION:
        return None
    manifest = migration_manifest()
    backup = database.with_name(
        f".{database.name}.migration-v{current}-to-{LATEST_SCHEMA_VERSION}-{manifest['manifest_sha256'][:12]}.bak"
    )
    receipt_path = Path(f"{backup}.receipt.json")
    if backup.exists() or receipt_path.exists():
        if not backup.is_file() or not receipt_path.is_file():
            raise MigrationSafetyError("migration_backup_pair_incomplete")
        receipt = MigrationSafetyReceipt.from_dict(
            json.loads(receipt_path.read_text(encoding="utf-8")), receipt_path=receipt_path
        )
        if receipt.from_version != current or receipt.to_version != LATEST_SCHEMA_VERSION:
            raise MigrationSafetyError("migration_backup_version_mismatch")
        if receipt.migration_manifest_sha256 != manifest["manifest_sha256"]:
            raise MigrationSafetyError("migration_manifest_hash_mismatch")
        verify_sqlite_backup(backup, receipt.backup_receipt)
        return receipt
    backup_receipt = create_sqlite_backup(database, backup)
    receipt = _build_receipt(
        database_name=database.name, from_version=current, to_version=LATEST_SCHEMA_VERSION,
        manifest_sha256=manifest["manifest_sha256"], backup_receipt=backup_receipt,
        status="prepared", prepared_at=datetime.now(timezone.utc).isoformat(),
        completed_at=None, receipt_path=receipt_path,
    )
    receipt.verify()
    _write_json(receipt_path, receipt.as_dict())
    return receipt


def complete_startup_migration(receipt: MigrationSafetyReceipt) -> MigrationSafetyReceipt:
    """Mark a prepared receipt complete only after migrations commit."""

    if receipt.receipt_path is None:
        raise MigrationSafetyError("migration_receipt_path_missing")
    completed = _build_receipt(
        database_name=receipt.database_name, from_version=receipt.from_version,
        to_version=receipt.to_version, manifest_sha256=receipt.migration_manifest_sha256,
        backup_receipt=SQLiteBackupReceipt.from_dict(receipt.backup_receipt), status="completed",
        prepared_at=receipt.prepared_at, completed_at=datetime.now(timezone.utc).isoformat(),
        receipt_path=receipt.receipt_path,
    )
    completed.verify()
    _write_json(receipt.receipt_path, completed.as_dict())
    return completed


def rollback_startup_migration_backup(
    database_path: str | Path,
    receipt: MigrationSafetyReceipt,
) -> dict[str, Any]:
    """Restore the exact pre-migration database after a failed startup.

    The prepared receipt is intentionally left untouched.  The failed
    database (and any WAL/SHM companions) is moved aside for investigation,
    while the verified backup is restored atomically into the original path.
    This gives the next process start the same schema it had before the
    migration attempt without silently destroying forensic evidence.
    """

    if receipt.status != "prepared":
        raise MigrationSafetyError("migration_rollback_requires_prepared_receipt")
    database = Path(database_path).expanduser().resolve()
    backup = database.with_name(receipt.backup_name)
    if not database.is_file() or database.is_symlink():
        raise MigrationSafetyError("migration_rollback_database_missing_or_symlinked")
    try:
        verification = verify_sqlite_backup(backup, receipt.backup_receipt)
        temporary = database.with_name(f".{database.name}.rollback-{uuid4().hex}.sqlite")
        restoration = restore_sqlite_backup(backup, temporary, receipt.backup_receipt)
    except (OSError, SQLiteBackupError) as exc:
        raise MigrationSafetyError("migration_rollback_backup_verification_failed") from exc

    failed = database.with_name(f".{database.name}.failed-migration-{uuid4().hex}.sqlite")
    moved_companions: list[tuple[Path, Path]] = []
    try:
        os.replace(database, failed)
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{database}{suffix}")
            if companion.exists() or companion.is_symlink():
                failed_companion = Path(f"{failed}{suffix}")
                os.replace(companion, failed_companion)
                moved_companions.append((companion, failed_companion))
        os.replace(temporary, database)
        _fsync_directory(database.parent)
    except (OSError, SQLiteBackupError) as exc:
        if not database.exists() and failed.exists():
            os.replace(failed, database)
        for original, preserved in reversed(moved_companions):
            if not original.exists() and preserved.exists():
                os.replace(preserved, original)
        if temporary.exists():
            temporary.unlink()
        raise MigrationSafetyError("migration_rollback_replace_failed") from exc

    return {
        "rolled_back": True,
        "database_name": database.name,
        "preserved_failed_database": failed.name,
        "receipt_status": receipt.status,
        "backup_verification": verification,
        "restoration": restoration,
    }


def _fsync_directory(path: Path) -> None:
    """Persist an atomic rename where the host filesystem supports it."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def load_migration_safety_receipt(path: str | Path) -> MigrationSafetyReceipt:
    receipt_path = Path(path)
    return MigrationSafetyReceipt.from_dict(
        json.loads(receipt_path.read_text(encoding="utf-8")), receipt_path=receipt_path
    )


def cleanup_migration_backups(
    directory: str | Path,
    *,
    retention_count: int = 2,
    execute: bool = False,
) -> dict[str, Any]:
    """Plan, and optionally execute, safe cleanup of old migration backups.

    Only a verified ``completed`` receipt and its matching backup can be
    considered for removal.  ``prepared`` receipts are always retained as
    rollback/forensic evidence, as are malformed, missing, or tampered pairs.
    The default is a dry-run so callers must explicitly opt into deletion.
    """

    candidate_root = Path(directory).expanduser()
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        raise MigrationSafetyError("migration_cleanup_directory_missing_or_symlinked")
    root = candidate_root.resolve()
    if retention_count < 2:
        raise MigrationSafetyError("migration_cleanup_retention_count_must_keep_at_least_two")

    grouped: dict[str, list[tuple[Path, MigrationSafetyReceipt]]] = {}
    preserved: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    receipt_paths = sorted(root.glob("*.migration-v*.receipt.json")) + sorted(
        root.glob(".migration-v*.receipt.json")
    )
    seen: set[Path] = set()
    for receipt_path in receipt_paths:
        if receipt_path.is_symlink() or not receipt_path.is_file():
            blockers.append({"receipt": receipt_path.name, "reason": "migration_cleanup_receipt_missing_or_symlinked"})
            continue
        receipt_path = receipt_path.resolve()
        if receipt_path in seen:
            continue
        seen.add(receipt_path)
        try:
            receipt = load_migration_safety_receipt(receipt_path)
            backup = root / receipt.backup_name
            if Path(receipt.backup_name).name != receipt.backup_name or backup.parent != root:
                raise MigrationSafetyError("migration_cleanup_backup_path_invalid")
            if not backup.is_file() or backup.is_symlink():
                raise MigrationSafetyError("migration_cleanup_backup_missing_or_symlinked")
            verify_sqlite_backup(backup, receipt.backup_receipt)
        except (OSError, ValueError, json.JSONDecodeError, MigrationSafetyError, SQLiteBackupError) as exc:
            blockers.append({"receipt": receipt_path.name, "reason": str(exc)})
            continue
        if receipt.status != "completed":
            preserved.append({"receipt": receipt_path.name, "backup": backup.name, "reason": "prepared_receipt"})
            continue
        grouped.setdefault(receipt.database_name, []).append((receipt_path, receipt))

    planned: list[dict[str, Any]] = []
    for database_name, entries in sorted(grouped.items()):
        entries.sort(key=lambda item: (item[1].completed_at or item[1].prepared_at, item[0].name), reverse=True)
        for receipt_path, receipt in entries[retention_count:]:
            backup = root / receipt.backup_name
            planned.append(
                {
                    "database_name": database_name,
                    "receipt": receipt_path.name,
                    "backup": backup.name,
                    "receipt_sha256": receipt.receipt_sha256,
                }
            )

    deleted: list[dict[str, Any]] = []
    if execute:
        for item in planned:
            receipt_path = root / item["receipt"]
            backup = root / item["backup"]
            try:
                # Re-verify immediately before deletion to close the TOCTOU
                # window between a dry-run plan and an explicit cleanup.
                receipt = load_migration_safety_receipt(receipt_path)
                if receipt.status != "completed":
                    raise MigrationSafetyError("migration_cleanup_receipt_no_longer_completed")
                verify_sqlite_backup(backup, receipt.backup_receipt)
                receipt_path.unlink()
                backup.unlink()
                deleted.append(item)
            except (OSError, MigrationSafetyError, SQLiteBackupError, ValueError, json.JSONDecodeError) as exc:
                blockers.append({"receipt": item["receipt"], "reason": str(exc)})

    report_payload = {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "directory": str(root),
        "retention_count": retention_count,
        "execute": execute,
        "planned": planned,
        "deleted": deleted,
        "preserved": preserved,
        "blockers": blockers,
    }
    report_payload["report_sha256"] = hashlib.sha256(_canonical_json(report_payload)).hexdigest()
    return report_payload


def verify_migration_cleanup_report(report: dict[str, Any]) -> bool:
    """Verify a cleanup report without trusting its execution outcome."""

    required = {
        "schema_version",
        "directory",
        "retention_count",
        "execute",
        "planned",
        "deleted",
        "preserved",
        "blockers",
        "report_sha256",
    }
    if set(report) != required or report.get("schema_version") != CLEANUP_SCHEMA_VERSION:
        return False
    payload = {key: report[key] for key in required if key != "report_sha256"}
    expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(expected, str(report.get("report_sha256") or "")):
        return False
    return (
        isinstance(report.get("retention_count"), int)
        and report["retention_count"] >= 2
        and isinstance(report.get("execute"), bool)
        and all(isinstance(report.get(key), list) for key in ("planned", "deleted", "preserved", "blockers"))
    )
