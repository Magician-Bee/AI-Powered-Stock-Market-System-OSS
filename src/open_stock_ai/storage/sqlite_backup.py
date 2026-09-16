"""Verified SQLite backup and clean-target restore drills.

The application keeps authoritative ledgers in SQLite.  Copying only the main
``.sqlite`` file is not a safe backup when a WAL is active, so this module uses
SQLite's online backup API and records enough evidence to verify a later
restore without silently overwriting an existing database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "open_stock_ai.sqlite_backup_receipt.v1"


class SQLiteBackupError(RuntimeError):
    """Raised when a backup or restore cannot be verified safely."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        select type, name, tbl_name, coalesce(sql, '')
        from sqlite_master
        where name not like 'sqlite_%'
        order by type, name, tbl_name, sql
        """
    ).fetchall()
    payload = {
        "user_version": connection.execute("pragma user_version").fetchone()[0],
        "objects": [list(row) for row in rows],
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _database_health(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SQLiteBackupError("sqlite_database_path_is_missing_or_symlinked")
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SQLiteBackupError("sqlite_database_cannot_be_opened") from exc
    try:
        integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise SQLiteBackupError(f"sqlite_integrity_check_failed:{integrity}")
        return integrity, _schema_fingerprint(connection)
    except sqlite3.Error as exc:
        raise SQLiteBackupError("sqlite_database_integrity_check_failed") from exc
    finally:
        connection.close()


def _ensure_distinct_paths(source: Path, destination: Path, *, target_name: str) -> None:
    if source.resolve() == destination.resolve():
        raise SQLiteBackupError(f"backup_{target_name}_must_differ_from_source")
    if destination.exists() or destination.is_symlink():
        raise SQLiteBackupError(f"backup_{target_name}_already_exists")


@dataclass(frozen=True)
class SQLiteBackupReceipt:
    """Content-addressed evidence for one verified SQLite backup."""

    source_name: str
    backup_name: str
    source_size_bytes: int
    backup_size_bytes: int
    backup_sha256: str
    schema_sha256: str
    integrity_check: str
    created_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_name": self.source_name,
            "backup_name": self.backup_name,
            "source_size_bytes": self.source_size_bytes,
            "backup_size_bytes": self.backup_size_bytes,
            "backup_sha256": self.backup_sha256,
            "schema_sha256": self.schema_sha256,
            "integrity_check": self.integrity_check,
            "created_at": self.created_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify_receipt_hash(self) -> None:
        expected = hashlib.sha256(_canonical_json(self.payload())).hexdigest()
        if expected != self.receipt_sha256:
            raise SQLiteBackupError("sqlite_backup_receipt_hash_mismatch")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SQLiteBackupReceipt":
        required = {
            "schema_version",
            "source_name",
            "backup_name",
            "source_size_bytes",
            "backup_size_bytes",
            "backup_sha256",
            "schema_sha256",
            "integrity_check",
            "created_at",
            "receipt_sha256",
        }
        if set(payload) != required or payload.get("schema_version") != SCHEMA_VERSION:
            raise SQLiteBackupError("sqlite_backup_receipt_schema_mismatch")
        receipt = cls(
            source_name=str(payload["source_name"]),
            backup_name=str(payload["backup_name"]),
            source_size_bytes=int(payload["source_size_bytes"]),
            backup_size_bytes=int(payload["backup_size_bytes"]),
            backup_sha256=str(payload["backup_sha256"]),
            schema_sha256=str(payload["schema_sha256"]),
            integrity_check=str(payload["integrity_check"]),
            created_at=str(payload["created_at"]),
            receipt_sha256=str(payload["receipt_sha256"]),
        )
        receipt.verify_receipt_hash()
        return receipt


def _write_file_fsync(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def create_sqlite_backup(source_path: str | Path, backup_path: str | Path) -> SQLiteBackupReceipt:
    """Create and verify an online backup, refusing to overwrite a target."""

    source = Path(source_path)
    backup = Path(backup_path)
    if not source.is_file() or source.is_symlink():
        raise SQLiteBackupError("sqlite_source_database_is_missing_or_symlinked")
    _ensure_distinct_paths(source, backup, target_name="destination")
    backup.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{backup.name}.", suffix=".tmp", dir=backup.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.execute("pragma busy_timeout = 5000")
            source_connection.backup(destination_connection)
            destination_connection.commit()
        except sqlite3.Error as exc:
            raise SQLiteBackupError("sqlite_online_backup_failed") from exc
        finally:
            destination_connection.close()
            source_connection.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, backup)
        integrity, schema_sha256 = _database_health(backup)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_name": source.name,
            "backup_name": backup.name,
            "source_size_bytes": source.stat().st_size,
            "backup_size_bytes": backup.stat().st_size,
            "backup_sha256": _sha256_file(backup),
            "schema_sha256": schema_sha256,
            "integrity_check": integrity,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt = SQLiteBackupReceipt(
            source_name=payload["source_name"],
            backup_name=payload["backup_name"],
            source_size_bytes=payload["source_size_bytes"],
            backup_size_bytes=payload["backup_size_bytes"],
            backup_sha256=payload["backup_sha256"],
            schema_sha256=payload["schema_sha256"],
            integrity_check=payload["integrity_check"],
            created_at=payload["created_at"],
            receipt_sha256=hashlib.sha256(_canonical_json(payload)).hexdigest(),
        )
        receipt.verify_receipt_hash()
        return receipt
    except SQLiteBackupError:
        if temporary.exists():
            temporary.unlink()
        if backup.exists() and not temporary.exists():
            # The destination is only removed when this invocation created it
            # and verification failed; an existing caller-owned file was
            # rejected before the temporary file was created.
            backup.unlink()
        raise
    except (OSError, sqlite3.Error) as exc:
        if temporary.exists():
            temporary.unlink()
        if backup.exists():
            backup.unlink()
        raise SQLiteBackupError("sqlite_backup_failed") from exc


def verify_sqlite_backup(backup_path: str | Path, receipt: SQLiteBackupReceipt | dict[str, Any]) -> dict[str, Any]:
    """Verify receipt, bytes, schema and SQLite integrity before restore."""

    backup = Path(backup_path)
    verified_receipt = SQLiteBackupReceipt.from_dict(receipt) if isinstance(receipt, dict) else receipt
    verified_receipt.verify_receipt_hash()
    if not backup.is_file() or backup.is_symlink():
        raise SQLiteBackupError("sqlite_backup_file_is_missing_or_symlinked")
    if backup.name != verified_receipt.backup_name:
        raise SQLiteBackupError("sqlite_backup_name_mismatch")
    actual_hash = _sha256_file(backup)
    if actual_hash != verified_receipt.backup_sha256:
        raise SQLiteBackupError("sqlite_backup_hash_mismatch")
    integrity, schema_sha256 = _database_health(backup)
    if schema_sha256 != verified_receipt.schema_sha256:
        raise SQLiteBackupError("sqlite_backup_schema_hash_mismatch")
    return {
        "verified": True,
        "backup_sha256": actual_hash,
        "schema_sha256": schema_sha256,
        "integrity_check": integrity,
        "receipt_sha256": verified_receipt.receipt_sha256,
    }


def restore_sqlite_backup(
    backup_path: str | Path,
    restore_path: str | Path,
    receipt: SQLiteBackupReceipt | dict[str, Any],
) -> dict[str, Any]:
    """Restore into a clean target and refuse to overwrite existing data."""

    backup = Path(backup_path)
    restore = Path(restore_path)
    if backup.resolve() == restore.resolve():
        raise SQLiteBackupError("sqlite_restore_target_must_differ_from_backup")
    if restore.exists() or restore.is_symlink():
        raise SQLiteBackupError("sqlite_restore_target_already_exists")
    verification = verify_sqlite_backup(backup, receipt)
    restore.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{restore.name}.", suffix=".tmp", dir=restore.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(backup)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        except sqlite3.Error as exc:
            raise SQLiteBackupError("sqlite_restore_failed") from exc
        finally:
            destination_connection.close()
            source_connection.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, restore)
        integrity, schema_sha256 = _database_health(restore)
        if schema_sha256 != verification["schema_sha256"]:
            raise SQLiteBackupError("sqlite_restored_schema_hash_mismatch")
        return {
            **verification,
            "restored": True,
            "restored_path": restore.name,
            "restored_sha256": _sha256_file(restore),
            "restored_integrity_check": integrity,
        }
    except SQLiteBackupError:
        if temporary.exists():
            temporary.unlink()
        if restore.exists() and not temporary.exists():
            restore.unlink()
        raise
    except (OSError, sqlite3.Error) as exc:
        if temporary.exists():
            temporary.unlink()
        if restore.exists():
            restore.unlink()
        raise SQLiteBackupError("sqlite_restore_failed") from exc


def run_sqlite_restore_drill(
    source_path: str | Path,
    backup_path: str | Path,
    restore_path: str | Path,
) -> dict[str, Any]:
    """Create a verified backup and restore it into a clean target."""

    receipt = create_sqlite_backup(source_path, backup_path)
    restoration = restore_sqlite_backup(backup_path, restore_path, receipt)
    return {"receipt": receipt.as_dict(), "restoration": restoration}
