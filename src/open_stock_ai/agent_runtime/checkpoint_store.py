from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


class CheckpointStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def create(
        self,
        *,
        session_id: str,
        run_id: str,
        plan_revision: int,
        sequence: int,
        payload: dict[str, Any],
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = f"AC-{uuid4().hex}"
        encoded = _json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        created_at = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_checkpoints(
                    checkpoint_id, session_id, run_id, plan_revision, sequence, status,
                    created_at, snapshot_hash, payload_json, rollback_json
                ) values (?, ?, ?, ?, ?, 'safe', ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    run_id,
                    plan_revision,
                    sequence,
                    created_at,
                    digest,
                    encoded,
                    _json(rollback or {}),
                ),
            )
            conn.execute(
                "update agent_runs set checkpoint_id=? where run_id=?",
                (checkpoint_id, run_id),
            )
            conn.commit()
        return {
            "checkpoint_id": checkpoint_id,
            "session_id": session_id,
            "run_id": run_id,
            "plan_revision": plan_revision,
            "sequence": sequence,
            "status": "safe",
            "created_at": created_at,
            "snapshot_hash": digest,
            "payload": payload,
            "rollback": rollback or {},
        }

    def latest(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select * from agent_checkpoints
                 where run_id=? and status='safe'
                 order by sequence desc, created_at desc limit 1
                """,
                (run_id,),
            ).fetchone()
        return _row(row) if row else None

    def list(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_checkpoints where run_id=?
                 order by sequence desc, created_at desc limit ?
                """,
                (run_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "checkpoint_id": row["checkpoint_id"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "plan_revision": row["plan_revision"],
        "sequence": row["sequence"],
        "status": row["status"],
        "created_at": row["created_at"],
        "snapshot_hash": row["snapshot_hash"],
        "payload": _decode(row["payload_json"], {}),
        "rollback": _decode(row["rollback_json"], {}),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
