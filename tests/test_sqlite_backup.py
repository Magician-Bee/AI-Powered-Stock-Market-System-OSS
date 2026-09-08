from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.storage.sqlite_backup import (
    SQLiteBackupError,
    create_sqlite_backup,
    restore_sqlite_backup,
    run_sqlite_restore_drill,
    verify_sqlite_backup,
)


def _database(path):
    connection = sqlite3.connect(path)
    connection.execute("create table decisions (id integer primary key, symbol text not null)")
    connection.executemany("insert into decisions(symbol) values (?)", [("2330.TW",), ("0050.TW",)])
    connection.commit()
    connection.close()


def test_sqlite_restore_drill_records_hashes_and_rehydrates_clean_target(tmp_path):
    source = tmp_path / "authoritative.sqlite"
    backup = tmp_path / "backups" / "authoritative.sqlite"
    restored = tmp_path / "clean-machine" / "restored.sqlite"
    _database(source)

    result = run_sqlite_restore_drill(source, backup, restored)

    receipt = result["receipt"]
    assert receipt["schema_version"] == "open_stock_ai.sqlite_backup_receipt.v1"
    assert len(receipt["backup_sha256"]) == 64
    assert len(receipt["schema_sha256"]) == 64
    assert len(receipt["receipt_sha256"]) == 64
    assert result["restoration"]["verified"] is True
    assert result["restoration"]["restored_integrity_check"] == "ok"
    with sqlite3.connect(restored) as connection:
        assert connection.execute("select symbol from decisions order by id").fetchall() == [
            ("2330.TW",),
            ("0050.TW",),
        ]


def test_sqlite_backup_rejects_tampered_bytes_and_existing_restore_target(tmp_path):
    source = tmp_path / "authoritative.sqlite"
    backup = tmp_path / "backup.sqlite"
    restored = tmp_path / "restored.sqlite"
    _database(source)
    receipt = create_sqlite_backup(source, backup)

    with backup.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(SQLiteBackupError, match="hash_mismatch"):
        verify_sqlite_backup(backup, receipt)

    backup.unlink()
    receipt = create_sqlite_backup(source, backup)
    restored.write_bytes(b"caller-owned")
    with pytest.raises(SQLiteBackupError, match="target_already_exists"):
        restore_sqlite_backup(backup, restored, receipt)


def test_sqlite_backup_refuses_overwrite_of_existing_backup(tmp_path):
    source = tmp_path / "authoritative.sqlite"
    backup = tmp_path / "backup.sqlite"
    _database(source)
    backup.write_bytes(b"caller-owned")

    with pytest.raises(SQLiteBackupError, match="destination_already_exists"):
        create_sqlite_backup(source, backup)
