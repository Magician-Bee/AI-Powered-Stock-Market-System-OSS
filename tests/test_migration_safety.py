from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from copy import deepcopy

import pytest

import open_stock_ai.storage.sqlite_store as sqlite_store_module
from open_stock_ai.storage.migration_safety import (
    MigrationSafetyError,
    cleanup_migration_backups,
    load_migration_safety_receipt,
    migration_manifest,
    verify_migration_cleanup_report,
)
from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION
from open_stock_ai.storage.migrations import apply_migrations as canonical_apply_migrations
from open_stock_ai.storage.sqlite_backup import verify_sqlite_backup
from open_stock_ai.storage.sqlite_store import SQLiteStore


def _legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "create table signals (id integer primary key autoincrement, created_at text not null, symbol text, market text, horizon text, action text, confidence real, risk_approved integer not null default 0, executed integer not null default 0, payload_json text not null)"
        )
        connection.execute(
            "insert into signals(created_at, symbol, payload_json) values (?, ?, ?)",
            ("2026-07-15T00:00:00+00:00", "2330.TW", "{}"),
        )
        connection.commit()


def test_migration_manifest_is_contiguous_and_content_hashed():
    manifest = migration_manifest()

    assert manifest["latest_schema_version"] == LATEST_SCHEMA_VERSION
    assert [item["version"] for item in manifest["migrations"]] == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert all(len(item["source_sha256"]) == 64 for item in manifest["migrations"])
    assert len(manifest["manifest_sha256"]) == 64


def test_legacy_startup_creates_completed_backup_receipt_before_migration(tmp_path):
    path = tmp_path / "legacy.sqlite"
    _legacy_database(path)

    SQLiteStore(db_path=path)

    receipt_paths = list(tmp_path.glob(".legacy.sqlite.migration-v*.receipt.json"))
    assert len(receipt_paths) == 1
    receipt = load_migration_safety_receipt(receipt_paths[0])
    assert receipt.status == "completed"
    assert receipt.from_version == 0
    assert receipt.to_version == LATEST_SCHEMA_VERSION
    assert receipt.migration_manifest_sha256 == migration_manifest()["manifest_sha256"]
    backup = tmp_path / receipt.backup_name
    verification = verify_sqlite_backup(backup, receipt.backup_receipt)
    assert verification["verified"] is True
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select symbol from signals").fetchone() == ("2330.TW",)


def test_tampered_migration_receipt_fails_closed(tmp_path):
    path = tmp_path / "legacy.sqlite"
    _legacy_database(path)
    SQLiteStore(db_path=path)
    receipt_path = next(tmp_path.glob(".legacy.sqlite.migration-v*.receipt.json"))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["status"] = "prepared"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationSafetyError, match="hash_mismatch"):
        load_migration_safety_receipt(receipt_path)


def test_failed_startup_migration_rolls_back_and_keeps_receipt_prepared(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    _legacy_database(path)

    def fail_migration(_connection):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(sqlite_store_module, "apply_migrations", fail_migration)
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        sqlite_store_module.SQLiteStore(db_path=path)

    receipt_path = next(tmp_path.glob(".legacy.sqlite.migration-v*.receipt.json"))
    receipt = load_migration_safety_receipt(receipt_path)
    assert receipt.status == "prepared"
    assert list(tmp_path.glob(".legacy.sqlite.failed-migration-*.sqlite"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("select symbol from signals").fetchone() == ("2330.TW",)
        with pytest.raises(sqlite3.OperationalError, match="no such table: schema_migrations"):
            connection.execute("select * from schema_migrations")

    monkeypatch.setattr(sqlite_store_module, "apply_migrations", canonical_apply_migrations)
    sqlite_store_module.SQLiteStore(db_path=path)
    assert load_migration_safety_receipt(receipt_path).status == "completed"


def test_migration_cleanup_dry_run_keeps_prepared_and_unknown_files(tmp_path):
    path = tmp_path / "legacy.sqlite"
    _legacy_database(path)
    SQLiteStore(db_path=path)
    receipt_path = next(tmp_path.glob(".legacy.sqlite.migration-v*.receipt.json"))
    receipt = load_migration_safety_receipt(receipt_path)
    backup = tmp_path / receipt.backup_name

    # A completed receipt is only eligible once it is outside the newest two
    # copies for its database.  Keep the real pair and add three verified
    # copies with distinct receipt timestamps to exercise ordering.
    for index in range(3):
        extra_backup = tmp_path / f".legacy-extra-{index}.sqlite"
        extra_receipt = tmp_path / f".legacy.sqlite.migration-v{index}.receipt.json"
        source = backup
        shutil.copy2(source, extra_backup)
        payload = deepcopy(receipt.as_dict())
        payload["backup_name"] = extra_backup.name
        payload["backup_receipt"]["backup_name"] = extra_backup.name
        backup_payload = {
            key: value for key, value in payload["backup_receipt"].items() if key != "receipt_sha256"
        }
        payload["backup_receipt"]["receipt_sha256"] = hashlib.sha256(
            json.dumps(backup_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload["prepared_at"] = f"2026-08-{10 + index:02d}T00:00:00+00:00"
        payload["completed_at"] = f"2026-08-{10 + index:02d}T00:01:00+00:00"
        canonical = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        extra_receipt.write_text(json.dumps(payload), encoding="utf-8")

    prepared_backup = tmp_path / ".legacy-prepared.sqlite"
    shutil.copy2(backup, prepared_backup)
    prepared_receipt_path = tmp_path / ".legacy-prepared.sqlite.migration-v9.receipt.json"
    prepared_payload = deepcopy(receipt.as_dict())
    prepared_payload["backup_name"] = prepared_backup.name
    prepared_payload["backup_receipt"]["backup_name"] = prepared_backup.name
    backup_payload = {
        key: value for key, value in prepared_payload["backup_receipt"].items() if key != "receipt_sha256"
    }
    prepared_payload["backup_receipt"]["receipt_sha256"] = hashlib.sha256(
        json.dumps(backup_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prepared_payload["status"] = "prepared"
    prepared_payload["completed_at"] = None
    prepared_payload["database_name"] = "legacy-prepared.sqlite"
    canonical = {key: value for key, value in prepared_payload.items() if key != "receipt_sha256"}
    prepared_payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prepared_receipt_path.write_text(json.dumps(prepared_payload), encoding="utf-8")

    unknown = tmp_path / "not-a-migration-backup.sqlite"
    shutil.copy2(backup, unknown)
    report = cleanup_migration_backups(tmp_path, retention_count=2)

    assert len(report["planned"]) == 2
    assert report["deleted"] == []
    assert any(item["reason"] == "prepared_receipt" for item in report["preserved"])
    assert unknown.exists()
    assert len(report["report_sha256"]) == 64
    assert verify_migration_cleanup_report(report) is True

    applied = cleanup_migration_backups(tmp_path, retention_count=2, execute=True)
    assert verify_migration_cleanup_report(applied) is True
    assert len(applied["deleted"]) == 2
    assert unknown.exists()
    assert receipt_path.exists()
    assert backup.exists()
    assert prepared_receipt_path.exists()
    assert prepared_backup.exists()
