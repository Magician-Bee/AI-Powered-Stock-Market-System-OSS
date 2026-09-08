from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .policies import is_current, validate_memory


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def write(
        self,
        *,
        namespace: str,
        kind: str,
        fact_type: str,
        content: str,
        source: dict[str, Any],
        session_id: str | None = None,
        run_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        validate_memory(kind=kind, fact_type=fact_type, source=source, expires_at=expires_at)
        memory_id = f"AMEM-{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_memory(
                    memory_id, namespace, session_id, run_id, kind, fact_type, content,
                    source_json, created_at, updated_at, expires_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    namespace,
                    session_id,
                    run_id,
                    kind,
                    fact_type,
                    content,
                    _json(source),
                    now,
                    now,
                    expires_at,
                ),
            )
            conn.commit()
        return self.get(memory_id) or {}

    def get(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_memory where memory_id=?", (memory_id,)).fetchone()
        return _row(row) if row else None

    def search(
        self,
        *,
        namespace: str,
        query: str,
        kinds: list[str] | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [namespace]
        where = ["namespace=?", "archived_at is null"]
        if kinds:
            where.append("kind in (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from agent_memory where {' and '.join(where)} order by updated_at desc limit 500",
                params,
            ).fetchall()
        tokens = _search_terms(query)
        fts_ids = self._fts_matches(namespace=namespace, terms=tokens)
        scored = []
        for row in rows:
            memory = _row(row)
            if not is_current(memory):
                continue
            text = memory["content"].casefold()
            score = sum(max(1, len(token)) for token in tokens if token in text)
            if memory["memory_id"] in fts_ids:
                score += 50
            if session_id and memory.get("session_id") == session_id:
                score += 100
            if tokens and score == 0 and memory["kind"] not in {"working", "user_preference"}:
                continue
            scored.append((score, memory["updated_at"], memory))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[: max(1, min(int(limit), 100))]]

    def replace_session_working(
        self,
        *,
        namespace: str,
        session_id: str,
        run_id: str,
        content: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                update agent_memory
                   set archived_at=?, updated_at=?
                 where namespace=? and session_id=? and kind='working' and archived_at is null
                """,
                (now, now, namespace, session_id),
            )
            conn.commit()
        return self.write(
            namespace=namespace,
            session_id=session_id,
            run_id=run_id,
            kind="working",
            fact_type="temporary_state",
            content=content,
            source=source,
        )

    def update(self, memory_id: str, *, content: str, source: dict[str, Any]) -> dict[str, Any]:
        if not source or not source.get("type"):
            raise ValueError("Memory correction requires a source")
        with self._connect() as conn:
            cursor = conn.execute(
                "update agent_memory set content=?, source_json=?, updated_at=? where memory_id=?",
                (content, _json(source), _now(), memory_id),
            )
            conn.commit()
        if not cursor.rowcount:
            raise KeyError(memory_id)
        return self.get(memory_id) or {}

    def archive(self, memory_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "update agent_memory set archived_at=?, updated_at=? where memory_id=? and archived_at is null",
                (_now(), _now(), memory_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn

    def _fts_matches(self, *, namespace: str, terms: set[str]) -> set[str]:
        searchable = sorted((term for term in terms if len(term) >= 3), key=len, reverse=True)[:20]
        if not searchable:
            return set()
        query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in searchable)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    select memory_id
                      from agent_memory_fts
                     where namespace=? and agent_memory_fts match ?
                     limit 200
                    """,
                    (namespace, query),
                ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row[0]) for row in rows}


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_id": row["memory_id"],
        "namespace": row["namespace"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "kind": row["kind"],
        "fact_type": row["fact_type"],
        "content": row["content"],
        "source": json.loads(row["source_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
        "archived_at": row["archived_at"],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _search_terms(query: str) -> set[str]:
    """Tokenize Latin words and overlapping Han n-grams without a heavy NLP dependency."""
    normalized = query.casefold()
    terms = {
        item
        for item in re.findall(r"[a-z0-9_.-]{2,}", normalized)
        if len(item) > 1
    }
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(sequence) == 1:
            terms.add(sequence)
            continue
        for size in (2, 3):
            if len(sequence) >= size:
                terms.update(
                    sequence[index : index + size]
                    for index in range(len(sequence) - size + 1)
                )
    return terms


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
