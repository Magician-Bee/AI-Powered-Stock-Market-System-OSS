from __future__ import annotations

import sqlite3
import hashlib
import json
from pathlib import Path
from shutil import copy2

import pytest

from open_stock_ai.agent_runtime.storage_health import AgentDatabaseMaintenance
from stock_ai.agent_run_store import AgentRunStore


def _maintenance(tmp_path: Path) -> AgentDatabaseMaintenance:
    database = tmp_path / "agent-runtime.db"
    AgentRunStore(database)
    return AgentDatabaseMaintenance(database, tmp_path / "backups")


def test_startup_check_reads_schema_without_running_full_quick_check(
    tmp_path: Path,
    monkeypatch,
):
    maintenance = _maintenance(tmp_path)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("full quick_check must not run during startup")

    monkeypatch.setattr(maintenance, "quick_check", fail_if_called)

    result = maintenance.startup_check()

    assert result["healthy"] is True
    assert result["mode"] == "startup"
    assert any(item.startswith("migration_version:") for item in result["checks"])
    assert any(item.startswith("migration_count:") for item in result["checks"])


def test_startup_check_fails_closed_when_schema_catalog_is_missing(tmp_path: Path):
    database = tmp_path / "agent-runtime.db"
    sqlite3.connect(database).close()
    maintenance = AgentDatabaseMaintenance(database, tmp_path / "backups")

    result = maintenance.startup_check()

    assert result["healthy"] is False
    assert result["mode"] == "startup"
    assert any("OperationalError" in item for item in result["checks"])


def test_full_quick_check_remains_available_for_explicit_maintenance(tmp_path: Path):
    maintenance = _maintenance(tmp_path)

    result = maintenance.quick_check()

    assert result["healthy"] is True
    assert result["mode"] == "full"
    assert result["checks"] == ["ok"]


def test_agent_service_uses_bounded_startup_check():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "stock_ai" / "agent_service.py"
    ).read_text(encoding="utf-8")

    assert "database_maintenance.startup_check()" in source
    assert "database_maintenance.quick_check()" not in source


def test_storage_report_is_bounded_and_blocks_reclaim_while_runtime_events_are_pending(tmp_path: Path):
    maintenance = _maintenance(tmp_path)
    store = AgentRunStore(maintenance.database)
    store.publish_runtime_event("ui.state.updated", {"large": "x" * 20_000})

    report = maintenance.storage_report()
    blocked = maintenance.reclaim_unallocated_pages()

    assert report["healthy"] is True
    assert report["runtime_events"] == {"available": True, "total": 1, "pending": 1, "processed": 0}
    assert report["maintenance"]["ready"] is False
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["runtime_events_pending"]
    assert not list((tmp_path / "backups").glob("agent-runtime-*.sqlite"))


def test_reclaim_unallocated_pages_verifies_backup_and_preserves_runtime_inbox(tmp_path: Path):
    maintenance = _maintenance(tmp_path)
    store = AgentRunStore(maintenance.database)
    event = store.publish_runtime_event("ui.state.updated", {"large": "x" * 20_000}, dedup_key="one")
    claimed = store.claim_runtime_events(
        owner="test",
        at="2026-08-28T00:00:00+00:00",
        lease_expires_at="2026-08-28T00:01:00+00:00",
    )
    assert claimed[0]["event_id"] == event["event_id"]
    assert store.complete_runtime_event(event["event_id"], owner="test")
    store.compact_processed_runtime_events(keep_recent=0)
    with sqlite3.connect(maintenance.database) as conn:
        conn.execute("create table disposable_payloads(value text not null)")
        conn.executemany("insert into disposable_payloads(value) values (?)", [("x" * 20_000,)] * 80)
        conn.execute("delete from disposable_payloads")
        conn.commit()

    result = maintenance.reclaim_unallocated_pages()

    assert result["status"] == "completed"
    assert result["backup_integrity_check"] == ["ok"]
    assert result["wal_checkpoint"]["busy"] is False
    assert result["integrity_check"]["healthy"] is True
    # WAL can defer physical main-file growth until checkpoint, so the durable
    # proof of a successful VACUUM is the authoritative free-page count rather
    # than an OS file-size decrease in every SQLite journal configuration.
    assert result["result"]["database"]["freelist_count"] == 0
    assert result["reclaimed_bytes"] >= 0
    assert result["result"]["runtime_events"]["pending"] == 0
    assert result["result"]["runtime_events"]["processed"] == 1


def test_terminal_checkpoint_compaction_keeps_the_latest_replay_and_event_reference(tmp_path: Path):
    maintenance = _maintenance(tmp_path)
    store = AgentRunStore(maintenance.database)
    run = store.create_run("AR-terminal", {"objective": "test", "session_id": "AS-terminal"})
    del run
    with sqlite3.connect(maintenance.database) as conn:
        conn.execute("insert into agent_sessions(session_id, namespace, title, status, created_at, updated_at, metadata_json) values ('AS-terminal','test','test','active','now','now','{}')")
        for sequence in (1, 2, 3):
            checkpoint_id = f"AC-{sequence}"
            payload = '{"transcript":["' + ("x" * 1000) + '"]}'
            conn.execute("insert into agent_checkpoints(checkpoint_id,session_id,run_id,plan_revision,sequence,status,created_at,snapshot_hash,payload_json,rollback_json) values (?,?,?,1,?,'safe','now',?,?, '{}')", (checkpoint_id, "AS-terminal", "AR-terminal", sequence, "a" * 64, payload))
            event = '{"type":"checkpoint.created","payload":{"checkpoint":{"checkpoint_id":"' + checkpoint_id + '","snapshot_hash":"' + ("a" * 64) + '","payload":' + payload + '}}}'
            conn.execute("insert into agent_events(run_id,sequence,event_type,payload_json,created_at) values (?,?,?,?, 'now')", ("AR-terminal", sequence, "checkpoint.created", event))
        conn.execute("update agent_runs set status='completed', checkpoint_id='AC-3' where run_id='AR-terminal'")
        conn.commit()

    result = maintenance.compact_terminal_checkpoint_history()

    assert result["status"] == "completed"
    assert result["deleted_checkpoints"] == 2
    assert result["eligible_run_statuses"] == ["completed"]
    assert result["row_check"]["passed"] is True
    assert result["row_check"]["runs_unchanged"] is True
    assert result["row_check"]["events_unchanged"] is True
    assert result["row_check"]["protected_checkpoints_unchanged"] is True
    assert result["row_check"]["checkpoint_delta_matches"] is True
    with sqlite3.connect(maintenance.database) as conn:
        assert conn.execute("select count(*) from agent_checkpoints where run_id='AR-terminal'").fetchone()[0] == 1
        payload = conn.execute("select payload_json from agent_events where run_id='AR-terminal' and sequence=1").fetchone()[0]
    assert '"checkpoint_id":"AC-1"' in payload
    assert "transcript" not in payload


def test_checkpoint_compaction_preserves_resumable_run_recovery_chains(tmp_path: Path):
    maintenance = _maintenance(tmp_path)
    store = AgentRunStore(maintenance.database)
    statuses = (
        "partially_completed",
        "max_steps_reached",
        "failed",
        "cancelled",
        "interrupted",
        "suspended",
        "waiting_user_input",
        "waiting_decision",
        "waiting_approval",
        "queued",
        "running",
    )
    for status in statuses:
        store.create_run(
            f"AR-{status}",
            {"objective": "resume", "session_id": f"AS-{status}"},
        )
    with sqlite3.connect(maintenance.database) as conn:
        for status in statuses:
            run_id = f"AR-{status}"
            session_id = f"AS-{status}"
            conn.execute(
                "insert into agent_sessions(session_id, namespace, title, status, created_at, updated_at, metadata_json) values (?, 'test', 'test', 'active', 'now', 'now', '{}')",
                (session_id,),
            )
            for sequence in (1, 2):
                checkpoint_id = f"AC-{status}-{sequence}"
                payload = '{"transcript":["' + ("x" * 1000) + '"]}'
                conn.execute(
                    "insert into agent_checkpoints(checkpoint_id,session_id,run_id,plan_revision,sequence,status,created_at,snapshot_hash,payload_json,rollback_json) values (?,?,?,1,?,'safe','now',?,?, '{}')",
                    (checkpoint_id, session_id, run_id, sequence, "b" * 64, payload),
                )
                event = '{"type":"checkpoint.created","payload":{"checkpoint":{"checkpoint_id":"' + checkpoint_id + '","payload":' + payload + '}}}'
                conn.execute(
                    "insert into agent_events(run_id,sequence,event_type,payload_json,created_at) values (?,?,?,?, 'now')",
                    (run_id, sequence, "checkpoint.created", event),
                )
            conn.execute(
                "update agent_runs set status=?, checkpoint_id=? where run_id=?",
                (status, f"AC-{status}-2", run_id),
            )
        conn.commit()

    with sqlite3.connect(maintenance.database) as conn:
        protected_before = {
            status: {
                "checkpoints": conn.execute(
                    "select checkpoint_id,payload_json,rollback_json from agent_checkpoints where run_id=? order by sequence",
                    (f"AR-{status}",),
                ).fetchall(),
                "events": conn.execute(
                    "select sequence,payload_json from agent_events where run_id=? order by sequence",
                    (f"AR-{status}",),
                ).fetchall(),
            }
            for status in statuses
        }

    result = maintenance.compact_terminal_checkpoint_history()

    assert result["status"] == "completed"
    assert result["deleted_checkpoints"] == 0
    assert result["compacted_events"] == 0
    assert result["eligible_run_statuses"] == ["completed"]
    assert result["row_check"]["passed"] is True
    assert result["row_check"]["protected_checkpoints_unchanged"] is True
    with sqlite3.connect(maintenance.database) as conn:
        for status in statuses:
            run_id = f"AR-{status}"
            assert conn.execute(
                "select count(*) from agent_checkpoints where run_id=?", (run_id,)
            ).fetchone()[0] == 2
            payload = conn.execute(
                "select payload_json from agent_events where run_id=? and sequence=1", (run_id,)
            ).fetchone()[0]
            assert "transcript" in payload
            assert conn.execute(
                "select checkpoint_id,payload_json,rollback_json from agent_checkpoints where run_id=? order by sequence",
                (run_id,),
            ).fetchall() == protected_before[status]["checkpoints"]
            assert conn.execute(
                "select sequence,payload_json from agent_events where run_id=? order by sequence",
                (run_id,),
            ).fetchall() == protected_before[status]["events"]


def test_backup_retains_two_verified_generations_and_prunes_only_after_validation(tmp_path: Path):
    maintenance = _maintenance(tmp_path)
    backup_root = tmp_path / "backups"
    older = backup_root / "agent-runtime-20200101T000000Z.sqlite"
    previous = backup_root / "agent-runtime-20210101T000000Z.sqlite"
    copy2(maintenance.database, older)
    copy2(maintenance.database, previous)

    receipt = maintenance.backup(retain=1)

    retained = sorted(backup_root.glob("agent-runtime-*.sqlite"))
    assert receipt["verified"] is True
    assert receipt["integrity_check"] == ["ok"]
    assert receipt["retained"] == 2
    assert older.exists() is False
    assert previous.exists() is True
    assert len(retained) == 2
    assert Path(receipt["path"]).exists()


def test_backup_does_not_prune_existing_generations_when_verification_fails(tmp_path: Path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    backup_root = tmp_path / "backups"
    existing = backup_root / "agent-runtime-20210101T000000Z.sqlite"
    copy2(maintenance.database, existing)
    monkeypatch.setattr(maintenance, "_quick_check_path", lambda _path: ["corrupt"])

    receipt = maintenance.backup()

    assert receipt["verified"] is False
    assert receipt["deleted"] == []
    assert existing.exists() is True


def test_terminal_checkpoint_compaction_fails_closed_when_backup_is_unverified(tmp_path: Path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    monkeypatch.setattr(
        maintenance,
        "backup",
        lambda: {"path": str(tmp_path / "unverified.sqlite"), "verified": False},
    )

    result = maintenance.compact_terminal_checkpoint_history()

    assert result["status"] == "blocked"
    assert result["blockers"] == ["online_backup_integrity_check_failed"]


def test_storage_preflight_sums_allocations_on_shared_filesystem(tmp_path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    estimate = maintenance.space_preflight()["estimated_database_bytes"]
    # Each individual operation fits; their simultaneous peak does not.
    monkeypatch.setattr(maintenance, "_filesystem_space", lambda path: (1, estimate * 2 + maintenance.reserve_bytes))
    report = maintenance.space_preflight(rewrite=True)
    assert report["ready"] is False
    assert report["blockers"] == ["insufficient_disk_space"]
    assert report["filesystems"][0]["required_bytes"] == estimate * 3 + maintenance.reserve_bytes
    assert set(report["filesystems"][0]["purposes"]) == {"backup", "rewrite"}


def test_storage_preflight_checks_separate_destinations_independently(tmp_path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    estimate = maintenance.space_preflight()["estimated_database_bytes"]
    def space(path):
        return (1, estimate + maintenance.reserve_bytes) if path == maintenance.backup_root else (2, estimate * 2 + maintenance.reserve_bytes)
    monkeypatch.setattr(maintenance, "_filesystem_space", space)
    report = maintenance.space_preflight(rewrite=True)
    assert report["ready"] is True
    assert len(report["filesystems"]) == 2
    monkeypatch.setattr(maintenance, "_filesystem_space", lambda path: (1, 0) if path == maintenance.backup_root else (2, 10**12))
    assert maintenance.space_preflight(rewrite=True)["ready"] is False


@pytest.mark.parametrize("operation", ["backup", "reclaim_unallocated_pages", "compact_terminal_checkpoint_history"])
def test_disk_pressure_blocks_before_integrity_scan_backup_or_data_changes(tmp_path, monkeypatch, operation):
    maintenance = _maintenance(tmp_path)
    existing = maintenance.backup_root / "agent-runtime-20200101T000000Z.sqlite"
    copy2(maintenance.database, existing)
    original = maintenance.database.read_bytes()
    monkeypatch.setattr(maintenance, "_filesystem_space", lambda path: (1, 0))
    def forbidden():
        raise AssertionError("Do not scan a large database when preflight already blocks")
    monkeypatch.setattr(maintenance, "quick_check", forbidden)
    result = getattr(maintenance, operation)()
    assert result["status"] == "blocked"
    assert result["blockers"] == ["insufficient_disk_space"]
    assert existing.exists()
    assert list(maintenance.backup_root.iterdir()) == [existing]
    assert maintenance.database.read_bytes() == original


def test_missing_database_preflight_and_startup_do_not_create_source(tmp_path):
    source = tmp_path / "missing?#.sqlite"
    maintenance = AgentDatabaseMaintenance(source, tmp_path / "backups")
    assert maintenance.space_preflight()["ready"] is False
    assert maintenance.storage_report()["healthy"] is False
    assert maintenance.startup_check()["healthy"] is False
    assert maintenance.backup()["verified"] is False
    assert not source.exists()
    assert not list(maintenance.backup_root.iterdir())


def test_storage_preflight_fails_closed_when_disk_information_is_unavailable(tmp_path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    def unavailable(path):
        raise OSError("filesystem offline")
    monkeypatch.setattr(maintenance, "_filesystem_space", unavailable)
    assert maintenance.storage_report()["maintenance"]["blockers"] == ["storage_preflight_unavailable"]
    assert maintenance.backup()["verified"] is False


def test_same_time_backups_have_distinct_verified_hashes_and_preserve_previous_bytes(tmp_path, monkeypatch):
    import open_stock_ai.agent_runtime.storage_health as module
    from datetime import datetime, timezone
    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", FrozenClock)
    maintenance = _maintenance(tmp_path)
    first = maintenance.backup()
    original = Path(first["path"]).read_bytes()
    AgentRunStore(maintenance.database).create_run("AR-next", {"objective": "next snapshot"})
    second = maintenance.backup()
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == original
    assert first["backup_sha256"] != second["backup_sha256"]
    for receipt in (first, second):
        data = Path(receipt["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == receipt["backup_sha256"]
        assert len(data) == receipt["backup_size_bytes"]
        proof = json.loads(Path(receipt["path"]).with_suffix(".receipt.json").read_text())
        assert proof == {key: value for key, value in receipt.items() if key not in {"retained", "deleted"}}
    assert not list(maintenance.backup_root.glob("*.partial"))


def test_failed_backup_is_not_counted_as_a_verified_generation(tmp_path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    original_check = maintenance._quick_check_path
    first = maintenance.backup()
    monkeypatch.setattr(maintenance, "_quick_check_path", lambda path: ["corrupt"])
    failed = maintenance.backup()
    assert failed["verified"] is False
    assert Path(failed["path"]).suffix == ".partial"
    monkeypatch.setattr(maintenance, "_quick_check_path", original_check)
    second = maintenance.backup()
    assert Path(first["path"]).exists()
    assert second["retained"] == 2


def test_preflight_accounts_for_uncheckpointed_wal(tmp_path):
    maintenance = _maintenance(tmp_path)
    with sqlite3.connect(maintenance.database) as conn:
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma wal_autocheckpoint=0")
        conn.execute("create table wal_growth(value blob)")
        conn.execute("insert into wal_growth values (zeroblob(1048576))")
        conn.commit()
        wal = maintenance.database.with_name(maintenance.database.name + "-wal")
        assert wal.stat().st_size > 0
        required = maintenance.space_preflight()["estimated_database_bytes"]
        assert required >= maintenance.database.stat().st_size + wal.stat().st_size


def test_receipt_failure_preserves_previous_verified_backups(tmp_path, monkeypatch):
    maintenance = _maintenance(tmp_path)
    first = maintenance.backup()
    second = maintenance.backup()
    original = Path.open
    def fail_receipt(path, *args, **kwargs):
        if path.name.endswith('.receipt.json') and args and args[0] == 'x':
            raise OSError('receipt disk full')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_receipt)
    with pytest.raises(OSError, match='receipt disk full'):
        maintenance.backup()
    assert Path(first['path']).exists()
    assert Path(second['path']).exists()
