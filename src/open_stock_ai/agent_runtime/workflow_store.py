from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .plan_graph import PlanGraph


class WorkflowStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def save(
        self,
        *,
        namespace: str,
        name: str,
        plan: PlanGraph,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(version), 0) from agent_workflows where namespace=? and name=?",
                (namespace, name),
            ).fetchone()
            version = int(row[0]) + 1
            workflow_id = f"AWF-{uuid4().hex}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                insert into agent_workflows(
                    workflow_id, namespace, name, version, status, created_at,
                    updated_at, plan_json, metadata_json
                ) values (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    namespace,
                    name,
                    version,
                    now,
                    now,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        return self.get(workflow_id) or {}

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_workflows where workflow_id=?", (workflow_id,)
            ).fetchone()
        return _row(row) if row else None

    def list(self, namespace: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_workflows where namespace=? and status='active'
                 order by name, version desc
                """,
                (namespace,),
            ).fetchall()
        return [_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "workflow_id": row["workflow_id"],
        "namespace": row["namespace"],
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "plan": json.loads(row["plan_json"]),
        "metadata": json.loads(row["metadata_json"]),
    }
