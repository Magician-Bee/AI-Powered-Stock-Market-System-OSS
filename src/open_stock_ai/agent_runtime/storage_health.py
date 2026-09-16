from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection


_COMPACTIBLE_RUN_STATUSES = ("completed",)


class AgentDatabaseMaintenance:
    """Bounded health checks and explicit maintenance for durable Agent state.

    Runtime event compaction deliberately leaves SQLite free pages behind: that
    preserves idempotency tombstones while the desktop process is running, but
    the operating system cannot reuse those pages until an operator performs a
    ``VACUUM``.  Reclaiming space is therefore never part of startup or the
    scheduler.  It is an explicit, backed-up operation that refuses to run
    while the durable runtime inbox still has work to process.
    """

    def __init__(self, database: Path, backup_root: Path, *, reserve_bytes: int = 64 * 1024 * 1024) -> None:
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be nonnegative")
        self.reserve_bytes = int(reserve_bytes)
        self.database = database.resolve()
        self.backup_root = backup_root.resolve()
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.chmod(0o700)

    def startup_check(self) -> dict[str, Any]:
        """Verify that the runtime schema is readable without scanning every data page.

        ``pragma quick_check`` walks the complete database.  That is useful as
        an explicit maintenance operation, but a multi-gigabyte market-data
        database can take longer than the desktop launcher's readiness
        timeout.  Startup only needs a bounded fail-closed check that SQLite
        can open the file and read the migration catalog.
        """
        checks: list[str] = []
        try:
            with self._read_connection() as conn:
                conn.execute("pragma query_only = on")
                schema_version = int(conn.execute("pragma schema_version").fetchone()[0])
                migration_row = conn.execute(
                    "select max(version), count(*) from schema_migrations"
                ).fetchone()
            migration_version = int(migration_row[0] or 0)
            migration_count = int(migration_row[1] or 0)
            checks.extend(
                (
                    "database_open",
                    f"schema_version:{schema_version}",
                    f"migration_version:{migration_version}",
                    f"migration_count:{migration_count}",
                )
            )
            healthy = schema_version > 0 and migration_count > 0
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            healthy = False
            checks.append(f"{type(exc).__name__}:{exc}")
        return {
            "schema_version": "open_stock_ai.agent_database_health.v1",
            "healthy": healthy,
            "mode": "startup",
            "checks": checks,
            "database": str(self.database),
        }

    def quick_check(self) -> dict[str, Any]:
        rows = self._quick_check_path(self.database)
        healthy = rows == ["ok"]
        return {
            "schema_version": "open_stock_ai.agent_database_health.v1",
            "healthy": healthy,
            "mode": "full",
            "checks": rows,
            "database": str(self.database),
        }

    def storage_report(self) -> dict[str, Any]:
        """Return a bounded, read-only storage preflight for the Agent Dock.

        This intentionally uses SQLite metadata and indexed counts only.  It
        does not execute ``quick_check``, ``dbstat`` or any table scan, because
        the report is loaded by the desktop UI and must remain cheap even for a
        multi-gigabyte runtime database.
        """

        main_bytes = self._file_size(self.database)
        wal_bytes = self._file_size(self.database.with_name(f"{self.database.name}-wal"))
        shm_bytes = self._file_size(self.database.with_name(f"{self.database.name}-shm"))
        runtime_events: dict[str, Any] = {"available": False}
        try:
            with self._read_connection() as conn:
                conn.execute("pragma query_only = on")
                page_count = int(conn.execute("pragma page_count").fetchone()[0])
                page_size = int(conn.execute("pragma page_size").fetchone()[0])
                freelist_count = int(conn.execute("pragma freelist_count").fetchone()[0])
                journal_mode = str(conn.execute("pragma journal_mode").fetchone()[0])
                has_runtime_events = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='agent_runtime_events'"
                ).fetchone() is not None
                if has_runtime_events:
                    row = conn.execute(
                        """
                        select
                            count(*) as total,
                            sum(case when processed_at is null then 1 else 0 end) as pending,
                            sum(case when processed_at is not null then 1 else 0 end) as processed
                          from agent_runtime_events
                        """
                    ).fetchone()
                    runtime_events = {
                        "available": True,
                        "total": int(row[0] or 0),
                        "pending": int(row[1] or 0),
                        "processed": int(row[2] or 0),
                    }
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            return {
                "schema_version": "open_stock_ai.agent_database_storage.v1",
                "healthy": False,
                "database": {"path": str(self.database), "main_bytes": main_bytes},
                "error": f"{type(exc).__name__}:{exc}",
                "maintenance": {
                    "mode": "manual",
                    "ready": False,
                    "blockers": ["database_metadata_unavailable"],
                },
            }

        pending = int(runtime_events.get("pending") or 0)
        blockers = ["runtime_events_pending"] if pending else []
        disk_space = self.space_preflight(rewrite=True)
        blockers.extend(disk_space["blockers"])
        reclaimable_bytes = freelist_count * page_size
        return {
            "schema_version": "open_stock_ai.agent_database_storage.v1",
            "healthy": True,
            "database": {
                "path": str(self.database),
                "main_bytes": main_bytes,
                "wal_bytes": wal_bytes,
                "shm_bytes": shm_bytes,
                "total_bytes": main_bytes + wal_bytes + shm_bytes,
                "page_count": page_count,
                "page_size": page_size,
                "freelist_count": freelist_count,
                "reclaimable_bytes": reclaimable_bytes,
                "journal_mode": journal_mode,
            },
            "runtime_events": runtime_events,
            "disk_space": disk_space,
            "maintenance": {
                "mode": "manual",
                "ready": not blockers,
                "blockers": blockers,
                "requires_online_backup": True,
                "requires_full_integrity_check": True,
                "operation": "reclaim_unallocated_pages",
            },
        }

    def reclaim_unallocated_pages(self) -> dict[str, Any]:
        """Safely return already-unallocated SQLite pages to the filesystem.

        The operation is intentionally not scheduled automatically.  It never
        deletes durable business records: it only rewrites the same database
        after verifying it, taking an online backup and confirming that the
        runtime inbox has no unprocessed events.  SQLite itself obtains the
        exclusive lock required by ``VACUUM``; a busy database fails closed.
        """

        before = self.storage_report()
        blockers = list((before.get("maintenance") or {}).get("blockers") or [])
        if before.get("healthy") is not True:
            blockers.append("database_metadata_unavailable")
        if blockers:
            return {
                "schema_version": "open_stock_ai.agent_database_maintenance.v1",
                "status": "blocked",
                "operation": "reclaim_unallocated_pages",
                "preflight": before,
                "blockers": sorted(set(blockers)),
            }
        source_check = self.quick_check()
        if source_check["healthy"] is not True:
            return {
                "schema_version": "open_stock_ai.agent_database_maintenance.v1",
                "status": "blocked",
                "operation": "reclaim_unallocated_pages",
                "preflight": before,
                "blockers": ["source_integrity_check_failed"],
                "integrity_check": source_check,
            }
        backup = self.backup()
        if backup.get("verified") is not True:
            return {
                "schema_version": "open_stock_ai.agent_database_maintenance.v1",
                "status": "blocked",
                "operation": "reclaim_unallocated_pages",
                "blockers": ["online_backup_integrity_check_failed"],
                "backup": backup,
                "preflight": before,
            }
        backup_check = self._quick_check_path(Path(backup["path"]))
        if backup_check != ["ok"]:
            return {
                "schema_version": "open_stock_ai.agent_database_maintenance.v1",
                "status": "blocked",
                "operation": "reclaim_unallocated_pages",
                "preflight": before,
                "backup": backup,
                "blockers": ["online_backup_integrity_check_failed"],
                "backup_integrity_check": backup_check,
            }
        try:
            with sqlite3.connect(self.database, timeout=5, factory=ManagedSQLiteConnection) as conn:
                conn.execute("pragma busy_timeout = 5000")
                conn.execute("vacuum")
        except (OSError, sqlite3.DatabaseError) as exc:
            return {
                "schema_version": "open_stock_ai.agent_database_maintenance.v1",
                "status": "blocked",
                "operation": "reclaim_unallocated_pages",
                "preflight": before,
                "backup": backup,
                "blockers": ["sqlite_vacuum_failed"],
                "error": f"{type(exc).__name__}:{exc}",
            }
        wal_checkpoint = self._truncate_wal()
        after_check = self.quick_check()
        after = self.storage_report()
        return {
            "schema_version": "open_stock_ai.agent_database_maintenance.v1",
            "status": (
                "completed"
                if after_check["healthy"] and not wal_checkpoint["busy"]
                else "completed_with_wal_pending"
                if after_check["healthy"]
                else "failed"
            ),
            "operation": "reclaim_unallocated_pages",
            "preflight": before,
            "backup": backup,
            "backup_integrity_check": backup_check,
            "wal_checkpoint": wal_checkpoint,
            "integrity_check": after_check,
            "result": after,
            "reclaimed_bytes": max(
                0,
                int((before.get("database") or {}).get("main_bytes") or 0)
                - int((after.get("database") or {}).get("main_bytes") or 0),
            ),
        }

    def compact_terminal_checkpoint_history(self, *, keep_per_run: int = 1) -> dict[str, Any]:
        """Remove replay duplicates from terminal Runs without touching live work.

        Only successfully completed Runs are eligible. Failed Runs have an
        explicit retry path, and cancelled/interrupted Runs retain their full
        evidence for recovery or investigation. Waiting, active and bounded
        partial Runs likewise remain byte-for-byte untouched. For an eligible
        Run, the newest checkpoint (and the checkpoint explicitly linked by
        ``agent_runs.checkpoint_id``) remains recoverable; older checkpoints
        are redundant cumulative transcript/trace copies. Matching activity
        events remain, but their copied checkpoint body is reduced to the
        original checkpoint audit reference.
        """

        keep = max(1, min(int(keep_per_run), 8))
        before = self.storage_report()
        if before.get("healthy") is not True or not (before.get("maintenance") or {}).get("ready"):
            return {
                "schema_version": "open_stock_ai.agent_checkpoint_compaction.v1",
                "status": "blocked",
                "blockers": (before.get("maintenance") or {}).get("blockers") or ["database_not_ready"],
                "preflight": before,
            }
        source_check = self.quick_check()
        if source_check["healthy"] is not True:
            return {
                "schema_version": "open_stock_ai.agent_checkpoint_compaction.v1",
                "status": "blocked",
                "blockers": ["source_integrity_check_failed"],
                "preflight": before,
                "integrity_check": source_check,
            }
        backup = self.backup()
        if backup.get("verified") is not True:
            return {
                "schema_version": "open_stock_ai.agent_checkpoint_compaction.v1",
                "status": "blocked",
                "blockers": ["online_backup_integrity_check_failed"],
                "backup": backup,
                "preflight": before,
            }
        compacted_events = 0
        deleted_checkpoints = 0
        with sqlite3.connect(self.database, timeout=10, factory=ManagedSQLiteConnection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma busy_timeout = 10000")
            before_rows = _checkpoint_compaction_row_counts(conn)
            run_rows = conn.execute(
                "select run_id, checkpoint_id from agent_runs where status in ({})".format(
                    ", ".join("?" for _ in _COMPACTIBLE_RUN_STATUSES)
                ),
                _COMPACTIBLE_RUN_STATUSES,
            ).fetchall()
            for run in run_rows:
                run_id = str(run["run_id"])
                checkpoints = conn.execute(
                    """select checkpoint_id from agent_checkpoints where run_id=?
                       order by sequence desc, created_at desc, checkpoint_id desc""",
                    (run_id,),
                ).fetchall()
                retained = {str(item["checkpoint_id"]) for item in checkpoints[:keep]}
                if run["checkpoint_id"]:
                    retained.add(str(run["checkpoint_id"]))
                stale = [str(item["checkpoint_id"]) for item in checkpoints if str(item["checkpoint_id"]) not in retained]
                if stale:
                    placeholders = ", ".join("?" for _ in stale)
                    deleted_checkpoints += conn.execute(
                        f"delete from agent_checkpoints where checkpoint_id in ({placeholders})", stale
                    ).rowcount
                event_rows = conn.execute(
                    """select id, payload_json from agent_events
                       where run_id=? and event_type='checkpoint.created'""",
                    (run_id,),
                ).fetchall()
                for event in event_rows:
                    compacted = _checkpoint_event_reference(str(event["payload_json"]))
                    if compacted is None:
                        continue
                    if compacted == str(event["payload_json"]):
                        continue
                    conn.execute(
                        "update agent_events set payload_json=? where id=?",
                        (compacted, int(event["id"])),
                    )
                    compacted_events += 1
            after_rows = _checkpoint_compaction_row_counts(conn)
            row_check = {
                "before": before_rows,
                "after": after_rows,
                "expected_deleted_checkpoints": deleted_checkpoints,
                "runs_unchanged": after_rows["runs"] == before_rows["runs"],
                "events_unchanged": after_rows["events"] == before_rows["events"],
                "protected_checkpoints_unchanged": (
                    after_rows["protected_checkpoints"] == before_rows["protected_checkpoints"]
                ),
                "checkpoint_delta_matches": (
                    before_rows["checkpoints"] - after_rows["checkpoints"]
                    == deleted_checkpoints
                ),
            }
            row_check["passed"] = all(
                row_check[key]
                for key in (
                    "runs_unchanged",
                    "events_unchanged",
                    "protected_checkpoints_unchanged",
                    "checkpoint_delta_matches",
                )
            )
            if not row_check["passed"]:
                conn.rollback()
                return {
                    "schema_version": "open_stock_ai.agent_checkpoint_compaction.v1",
                    "status": "blocked",
                    "blockers": ["checkpoint_compaction_row_check_failed"],
                    "backup": backup,
                    "preflight": before,
                    "row_check": row_check,
                }
            conn.commit()
        after_check = self.quick_check()
        return {
            "schema_version": "open_stock_ai.agent_checkpoint_compaction.v1",
            "status": "completed" if after_check["healthy"] else "failed",
            "keep_per_run": keep,
            "deleted_checkpoints": deleted_checkpoints,
            "compacted_events": compacted_events,
            "eligible_run_statuses": list(_COMPACTIBLE_RUN_STATUSES),
            "backup": backup,
            "integrity_check": after_check,
            "row_check": row_check,
            "preflight": before,
            "result": self.storage_report(),
        }

    def backup(self, *, retain: int = 2) -> dict[str, Any]:
        """Create a verified online backup before pruning older runtime copies.

        Two verified generations are retained as the minimum recovery window:
        the fresh backup made for this maintenance operation and its immediate
        predecessor. A failed validation is fail-closed: no earlier backup is
        removed, so an operator still has the known-good copies to restore.
        """

        retention = max(2, int(retain))
        preflight = self.space_preflight()
        if not preflight["ready"]:
            return {
                "schema_version": "open_stock_ai.agent_database_backup.v1",
                "verified": False,
                "status": "blocked",
                "blockers": preflight["blockers"],
                "preflight": preflight,
                "deleted": [],
            }
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final_target = self.backup_root / f"agent-runtime-{timestamp}-{uuid4().hex}.sqlite"
        target = final_target.with_suffix(".sqlite.partial")
        # Reserve a private, exclusive destination; two backups in the same
        # second must never open and overwrite the previous generation.
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with self._read_connection() as source:
                with sqlite3.connect(target, factory=ManagedSQLiteConnection) as destination:
                    source.backup(destination)
        except (OSError, sqlite3.DatabaseError) as exc:
            return {
                "schema_version": "open_stock_ai.agent_database_backup.v1",
                "path": str(target),
                "verified": False,
                "status": "failed",
                "blockers": ["online_backup_failed"],
                "error": type(exc).__name__,
                "deleted": [],
            }
        target.chmod(0o600)
        integrity_check = self._quick_check_path(target)
        backups = sorted(self.backup_root.glob("agent-runtime-*.sqlite"), reverse=True)
        if integrity_check != ["ok"]:
            return {
                "schema_version": "open_stock_ai.agent_database_backup.v1",
                "path": str(target),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "verified": False,
                "integrity_check": integrity_check,
                "retained": len(backups),
                "deleted": [],
            }
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            os.fsync(handle.fileno())
        target.rename(final_target)
        target = final_target
        backups = [target, *backups]
        expired = backups[retention:]
        receipt = {
            "schema_version": "open_stock_ai.agent_database_backup.v1",
            "path": str(target),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "verified": True,
            "backup_sha256": digest.hexdigest(),
            "backup_size_bytes": target.stat().st_size,
            "preflight": preflight,
            "integrity_check": integrity_check,
            "retained": min(len(backups), retention),
            "deleted": [str(item) for item in expired],
        }
        receipt_path = target.with_suffix(".receipt.json")
        # Persist proof of the new backup before pruning. Do not record the
        # planned deletions as completed in this durable integrity receipt.
        backup_proof = {key: value for key, value in receipt.items() if key not in {"retained", "deleted"}}
        with receipt_path.open("x", encoding="utf-8") as handle:
            receipt_path.chmod(0o600)
            json.dump(backup_proof, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        for expired_backup in expired:
            expired_backup.unlink(missing_ok=True)
            expired_backup.with_suffix(".receipt.json").unlink(missing_ok=True)
        return receipt

    def _read_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.database.as_uri() + "?mode=ro", uri=True, timeout=5,
            factory=ManagedSQLiteConnection,
        )

    @staticmethod
    def _filesystem_space(path: Path) -> tuple[int, int]:
        return path.stat().st_dev, shutil.disk_usage(path).free

    def space_preflight(self, *, rewrite: bool = False) -> dict[str, Any]:
        """Estimate peak allocation per filesystem without creating a backup.

        WAL growth counts toward backup size; rewrite reserves two additional
        copies on the database filesystem. Shared destinations must add their
        requirements, not each independently compare to the same free bytes.
        This is an estimate, not a disk reservation against concurrent writers.
        """
        try:
            with self._read_connection() as conn:
                logical_bytes = int(conn.execute("pragma page_count").fetchone()[0]) * int(
                    conn.execute("pragma page_size").fetchone()[0]
                )
            estimated_bytes = max(logical_bytes, self._file_size(self.database)) + self._file_size(
                self.database.with_name(f"{self.database.name}-wal")
            )
            allocations = [(self.backup_root, estimated_bytes, "backup")]
            if rewrite:
                allocations.append((self.database.parent, estimated_bytes * 2, "rewrite"))
            volumes: dict[int, dict[str, Any]] = {}
            for path, amount, purpose in allocations:
                device, free = self._filesystem_space(path)
                volume = volumes.setdefault(device, {
                    "device": device, "free_bytes": free,
                    "required_bytes": self.reserve_bytes, "purposes": [],
                })
                volume["free_bytes"] = min(volume["free_bytes"], free)
                volume["required_bytes"] += amount
                volume["purposes"].append(purpose)
            checks = list(volumes.values())
            for volume in checks:
                volume["sufficient"] = volume["free_bytes"] >= volume["required_bytes"]
            blockers = [] if all(volume["sufficient"] for volume in checks) else ["insufficient_disk_space"]
            return {
                "ready": not blockers, "blockers": blockers,
                "estimated_database_bytes": estimated_bytes,
                "reserve_bytes": self.reserve_bytes, "filesystems": checks,
            }
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return {"ready": False, "blockers": ["storage_preflight_unavailable"], "filesystems": []}

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    @staticmethod
    def _quick_check_path(database: Path) -> list[str]:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, factory=ManagedSQLiteConnection) as conn:
            return [str(row[0]) for row in conn.execute("pragma quick_check")]

    def _truncate_wal(self) -> dict[str, int | bool]:
        """Checkpoint committed WAL frames after VACUUM releases free pages.

        SQLite's WAL mode can otherwise leave the rewritten pages in a large
        ``-wal`` file even though the database itself is compact.  A busy
        reader is not interrupted: the receipt reports it so a later explicit
        maintenance pass can retry without risking live work.
        """

        with sqlite3.connect(self.database, timeout=5, factory=ManagedSQLiteConnection) as conn:
            conn.execute("pragma busy_timeout = 5000")
            row = conn.execute("pragma wal_checkpoint(truncate)").fetchone()
        return {
            "busy": bool(int(row[0] or 0)),
            "log_frames": int(row[1] or 0),
            "checkpointed_frames": int(row[2] or 0),
        }


def _checkpoint_event_reference(encoded: str) -> str | None:
    """Replace a historical copied checkpoint body with its audit reference."""

    try:
        event = json.loads(encoded)
        payload = dict(event.get("payload") or {})
        checkpoint = dict(payload.get("checkpoint") or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not checkpoint.get("checkpoint_id"):
        return None
    payload["checkpoint"] = {
        key: checkpoint[key]
        for key in (
            "checkpoint_id", "session_id", "run_id", "plan_revision", "sequence",
            "status", "created_at", "snapshot_hash",
        )
        if checkpoint.get(key) is not None
    }
    event["payload"] = payload
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)


def _checkpoint_compaction_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Capture transaction-local invariants for destructive checkpoint pruning."""

    compactible = ", ".join("?" for _ in _COMPACTIBLE_RUN_STATUSES)
    protected = conn.execute(
        f"""select count(*) from agent_checkpoints c
              join agent_runs r on r.run_id=c.run_id
             where r.status not in ({compactible})""",
        _COMPACTIBLE_RUN_STATUSES,
    ).fetchone()
    return {
        "runs": int(conn.execute("select count(*) from agent_runs").fetchone()[0]),
        "events": int(conn.execute("select count(*) from agent_events").fetchone()[0]),
        "checkpoints": int(conn.execute("select count(*) from agent_checkpoints").fetchone()[0]),
        "protected_checkpoints": int(protected[0] or 0),
    }
