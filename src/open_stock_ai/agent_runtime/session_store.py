from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


class AgentSessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migrations(conn)

    def create(
        self,
        *,
        title: str,
        namespace: str,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        active_id = session_id or f"AS-{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            resolved_title = title.strip() or "Stock AI Agent"
            conn.execute(
                """
                insert into agent_sessions(
                    session_id, namespace, title, status, created_at, updated_at, metadata_json
                ) values (?, ?, ?, 'active', ?, ?, ?)
                """,
                (active_id, namespace, resolved_title, now, now, _json(metadata or {})),
            )
            title_history_id = f"ATH-{uuid4().hex}"
            title_payload = {"temporary": True, "source": "session.create"}
            conn.execute(
                """
                insert into agent_session_titles(
                    session_id, current_revision, title, updated_at, payload_json
                ) values (?, 1, ?, ?, ?)
                """,
                (active_id, resolved_title, now, _json(title_payload)),
            )
            conn.execute(
                """
                insert into agent_session_title_history(
                    title_history_id, session_id, revision, title, reason, created_at, payload_json
                ) values (?, ?, 1, ?, 'temporary_title', ?, ?)
                """,
                (title_history_id, active_id, resolved_title, now, _json(title_payload)),
            )
            conn.commit()
        return self.get(active_id) or {}

    def ensure(self, session_id: str, *, namespace: str, title: str = "Stock AI Agent") -> dict[str, Any]:
        existing = self.get(session_id)
        return existing or self.create(
            session_id=session_id,
            namespace=namespace,
            title=title,
            metadata={"created_by": "runtime_compatibility"},
        )

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select s.*,
                       (select count(*) from agent_runs r where r.session_id=s.session_id) run_count,
                       (select count(*) from agent_messages m where m.session_id=s.session_id) message_count
                  from agent_sessions s where session_id=?
                """,
                (session_id,),
            ).fetchone()
            active_run_id = _root_run_id(conn, row["last_run_id"]) if row else None
        return _session_row(row, active_run_id=active_run_id) if row else None

    def list(self, *, namespace: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select s.*,
                       (select count(*) from agent_runs r where r.session_id=s.session_id) run_count,
                       (select count(*) from agent_messages m where m.session_id=s.session_id) message_count
                  from agent_sessions s where namespace=?
                 order by updated_at desc limit ?
                """,
                (namespace, max(1, min(int(limit), 200))),
            ).fetchall()
            active_run_ids = {
                str(row["session_id"]): _root_run_id(conn, row["last_run_id"])
                for row in rows
            }
        return [
            _session_row(row, active_run_id=active_run_ids[str(row["session_id"])])
            for row in rows
        ]

    def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: Any,
        run_id: str | None = None,
        source: dict[str, Any] | None = None,
        kind: str = "text",
        status: str = "completed",
        set_active_run: bool = True,
    ) -> dict[str, Any]:
        message_id = f"AM-{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_messages(
                    message_id, session_id, run_id, role, created_at, content_json,
                    source_json, kind, status
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    run_id,
                    role,
                    now,
                    _json(content),
                    _json(source or {}),
                    kind,
                    status,
                ),
            )
            conn.execute(
                "update agent_sessions set updated_at=?, last_run_id=coalesce(?, last_run_id) where session_id=?",
                (now, run_id if set_active_run else None, session_id),
            )
            conn.commit()
        return {
            "message_id": message_id,
            "session_id": session_id,
            "run_id": run_id,
            "role": role,
            "created_at": now,
            "content": content,
            "source": source or {},
            "kind": kind,
            "status": status,
        }

    def set_active_run(self, session_id: str, run_id: str) -> bool:
        """Select a foreground Run without allowing a child Run to steal focus."""

        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_sessions
                   set updated_at=?, last_run_id=?
                 where session_id=?
                   and exists(
                       select 1 from agent_runs
                        where run_id=? and session_id=?
                   )
                """,
                (now, run_id, session_id, run_id, session_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_messages where session_id=?
                 order by created_at desc limit ?
                """,
                (session_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [
            {
                "message_id": row["message_id"],
                "session_id": row["session_id"],
                "run_id": row["run_id"],
                "role": row["role"],
                "created_at": row["created_at"],
                "content": _decode(row["content_json"], {}),
                "source": _decode(row["source_json"], {}),
                "kind": row["kind"],
                "status": row["status"],
            }
            for row in reversed(rows)
        ]

    def archive(self, session_id: str, *, archived: bool = True) -> dict[str, Any] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_sessions
                   set archived=?, status=?, updated_at=?
                 where session_id=?
                """,
                (
                    int(archived),
                    "archived" if archived else "active",
                    _now(),
                    session_id,
                ),
            )
            conn.commit()
        return self.get(session_id) if cursor.rowcount else None

    def update_title(
        self,
        session_id: str,
        *,
        title: str,
        reason: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = title.strip()
        if not resolved:
            raise ValueError("Session title cannot be empty")
        now = _now()
        with self._connect() as conn:
            current = conn.execute(
                "select current_revision, title from agent_session_titles where session_id=?",
                (session_id,),
            ).fetchone()
            if current is None:
                raise KeyError(session_id)
            if str(current[1]) == resolved:
                return self.get(session_id) or {}
            revision = int(current[0]) + 1
            payload = {"source": source or {}, "reason": reason}
            conn.execute(
                "update agent_sessions set title=?, updated_at=? where session_id=?",
                (resolved, now, session_id),
            )
            conn.execute(
                """
                update agent_session_titles
                   set current_revision=?, title=?, updated_at=?, payload_json=?
                 where session_id=?
                """,
                (revision, resolved, now, _json(payload), session_id),
            )
            conn.execute(
                """
                insert into agent_session_title_history(
                    title_history_id, session_id, revision, title, reason, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (f"ATH-{uuid4().hex}", session_id, revision, resolved, reason, now, _json(payload)),
            )
            conn.commit()
        return self.get(session_id) or {}

    def title_history(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_session_title_history
                 where session_id=? order by revision
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "title_history_id": row["title_history_id"],
                "session_id": row["session_id"],
                "revision": int(row["revision"]),
                "title": row["title"],
                "reason": row["reason"],
                "created_at": row["created_at"],
                "metadata": _decode(row["payload_json"], {}),
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _session_row(row: sqlite3.Row, *, active_run_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "open_stock_ai.agent_session.v1",
        "session_id": row["session_id"],
        "namespace": row["namespace"],
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_run_id": row["last_run_id"],
        "metadata": _decode(row["metadata_json"], {}),
        "run_count": int(row["run_count"]),
        "message_count": int(row["message_count"]),
        "archived": bool(row["archived"]),
        "active_run_id": active_run_id,
    }


def _root_run_id(conn: sqlite3.Connection, run_id: str | None) -> str | None:
    """Resolve historical child-focused Sessions back to their root Run."""

    current = str(run_id or "").strip()
    if not current:
        return None
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        row = conn.execute(
            "select parent_run_id from agent_runs where run_id=?",
            (current,),
        ).fetchone()
        if row is None:
            return current
        parent_run_id = str(row[0] or "").strip()
        if not parent_run_id:
            return current
        current = parent_run_id
    return str(run_id or "").strip() or None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
