"""Content-addressed Session archive/export/restore boundaries.

The live SQLite stores remain authoritative.  This module deliberately keeps
archive creation and restore validation side-effect free so an exported
Session can be verified and inspected on a cold machine without overwriting
an existing Session or Run identifier.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import redact_runtime_value
from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


SCHEMA_VERSION = "open_stock_ai.session_archive.v1"
RESTORE_SCHEMA_VERSION = "open_stock_ai.session_restore.v1"
MATERIALIZE_SCHEMA_VERSION = "open_stock_ai.session_materialize.v1"
MATERIALIZED_ARCHIVE_TABLE = "agent_session_archive_imports"


def build_session_archive(
    *,
    session: dict[str, Any],
    title_history: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a secret-minimized, content-addressed archive bundle."""

    safe_session = _safe(session)
    safe_titles = _safe(title_history)
    safe_messages = _safe(messages)
    safe_runs = _safe(runs)
    session_id = _required_id(safe_session, "session_id", "session")
    run_ids = [
        _required_id(snapshot.get("run") or {}, "run_id", "run")
        for snapshot in safe_runs
    ]
    message_ids = [
        _required_id(message, "message_id", "message")
        for message in safe_messages
    ]
    lineage = _lineage(
        session_id=session_id,
        run_ids=run_ids,
        message_ids=message_ids,
        runs=safe_runs,
    )
    archive: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "archive_id": f"SA-{uuid4().hex}",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "session": safe_session,
        "title_history": safe_titles,
        "messages": safe_messages,
        "runs": safe_runs,
        "lineage": lineage,
    }
    archive["archive_sha256"] = _digest(archive)
    return archive


def verify_session_archive(
    archive: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Verify hash, schema, and cross-record lineage before restore."""

    if not isinstance(archive, dict):
        raise ValueError("Session archive must be an object")
    if archive.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported Session archive schema")
    supplied_digest = str(archive.get("archive_sha256") or "").strip()
    if len(supplied_digest) != 64 or supplied_digest != _digest(archive):
        raise ValueError("Session archive content hash mismatch")

    session = archive.get("session")
    titles = archive.get("title_history")
    messages = archive.get("messages")
    runs = archive.get("runs")
    if not isinstance(session, dict) or not isinstance(titles, list):
        raise ValueError("Session archive is missing its Session records")
    if not isinstance(messages, list) or not isinstance(runs, list):
        raise ValueError("Session archive is missing its lineage records")
    session_id = _required_id(session, "session_id", "session")
    if expected_session_id and session_id != expected_session_id:
        raise ValueError("Session archive belongs to a different Session")

    for title in titles:
        if not isinstance(title, dict) or title.get("session_id") != session_id:
            raise ValueError("Session title lineage does not match the Session")
    for message in messages:
        if not isinstance(message, dict) or message.get("session_id") != session_id:
            raise ValueError("Session message lineage does not match the Session")
    run_ids: list[str] = []
    for snapshot in runs:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("run"), dict):
            raise ValueError("Session archive contains an invalid Run snapshot")
        run = snapshot["run"]
        if run.get("session_id") != session_id:
            raise ValueError("Run lineage does not match the Session")
        run_id = _required_id(run, "run_id", "run")
        run_ids.append(run_id)
        for event in snapshot.get("events") or []:
            if isinstance(event, dict) and event.get("run_id") not in (None, run_id):
                raise ValueError("Event lineage does not match its Run")

    expected_lineage = _lineage(
        session_id=session_id,
        run_ids=run_ids,
        message_ids=[_required_id(item, "message_id", "message") for item in messages],
        runs=runs,
    )
    if archive.get("lineage") != expected_lineage:
        raise ValueError("Session archive lineage hash mismatch")
    return {
        "archive_id": str(archive.get("archive_id") or ""),
        "session_id": session_id,
        "run_ids": run_ids,
        "message_count": len(messages),
        "run_count": len(runs),
        "lineage_sha256": str(expected_lineage["lineage_sha256"]),
        "archive_sha256": supplied_digest,
    }


def restore_session_archive(
    archive: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Return a verified cold-restore projection without mutating live stores.

    The projection is intentionally complete for the exported decision
    lineage.  A future migration can rehydrate it into a fresh database while
    preserving the original IDs; this release does not overwrite an existing
    authoritative runtime database.
    """

    verification = verify_session_archive(
        archive,
        expected_session_id=expected_session_id,
    )
    return {
        "schema_version": RESTORE_SCHEMA_VERSION,
        "restore_mode": "cold_projection",
        "restored": True,
        "read_only": True,
        **verification,
        "session": archive["session"],
        "title_history": archive["title_history"],
        "messages": archive["messages"],
        "runs": archive["runs"],
        "lineage": archive["lineage"],
    }


def materialize_session_archive(
    archive: dict[str, Any],
    target_db: str | Path,
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Restore a verified archive into a new SQLite runtime database.

    The function never updates or deletes an existing row. A target containing
    any archived Session, Run, or Message ID is rejected before the first
    insert, so a caller can safely point it at an existing database.
    """

    verification = verify_session_archive(archive, expected_session_id=expected_session_id)
    path = Path(target_db).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    session = archive["session"]
    title_history = archive["title_history"]
    messages = archive["messages"]
    runs = [snapshot["run"] for snapshot in archive["runs"]]
    with sqlite3.connect(path, factory=ManagedSQLiteConnection) as conn:
        configure_connection(conn)
        apply_migrations(conn)
        _init_materialized_archive_schema(conn)
        _reject_existing_ids(conn, "agent_sessions", "session_id", [verification["session_id"]])
        _reject_existing_ids(conn, "agent_runs", "run_id", verification["run_ids"])
        _reject_existing_ids(conn, "agent_messages", "message_id", [str(item["message_id"]) for item in messages])
        _reject_existing_ids(conn, MATERIALIZED_ARCHIVE_TABLE, "archive_id", [verification["archive_id"]])
        _reject_existing_ids(conn, MATERIALIZED_ARCHIVE_TABLE, "session_id", [verification["session_id"]])
        conn.execute(
            """
            insert into agent_sessions(
                session_id, namespace, title, status, created_at, updated_at,
                last_run_id, metadata_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(session["session_id"]), str(session.get("namespace") or "stock-ai"),
                str(session.get("title") or "Stock AI Agent"), str(session.get("status") or "active"),
                str(session.get("created_at") or archive["exported_at"]),
                str(session.get("updated_at") or archive["exported_at"]), session.get("last_run_id"),
                _json(session.get("metadata") or {}),
            ),
        )
        _insert_titles(conn, str(session["session_id"]), title_history, str(session.get("title") or "Stock AI Agent"))
        for run in runs:
            _insert_run(conn, run, session_id=str(session["session_id"]))
        for message in messages:
            _insert_message(conn, message)
        conn.execute(
            f"""insert into {MATERIALIZED_ARCHIVE_TABLE}(
                archive_id, session_id, archive_sha256, archive_json, materialized_at
            ) values (?, ?, ?, ?, ?)""",
            (
                verification["archive_id"],
                verification["session_id"],
                verification["archive_sha256"],
                _json(archive),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    return {
        "schema_version": MATERIALIZE_SCHEMA_VERSION,
        "restore_mode": "fresh_sqlite_authoritative",
        "restored": True,
        "read_only": False,
        "target_db": str(path),
        "archive_envelope": {
            "table": MATERIALIZED_ARCHIVE_TABLE,
            "archive_id": verification["archive_id"],
            "archive_sha256": verification["archive_sha256"],
            "preserves_full_run_snapshots": True,
        },
        **verification,
    }


def load_materialized_session_archive(
    target_db: str | Path,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    """Read and re-verify the full archive held by a fresh restored authority.

    Normalized Session/Run rows make the restored session browsable by the
    runtime. The immutable envelope retains the complete export, including
    each Run's events, evidence, artifacts and other snapshot-only fields.
    """

    path = Path(target_db).expanduser().resolve()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path, factory=ManagedSQLiteConnection) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""select archive_id, session_id, archive_sha256, archive_json
                    from {MATERIALIZED_ARCHIVE_TABLE}
                    where session_id=? limit 1""",
                (session_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    archive = _load_object(row["archive_json"], label="materialized Session archive")
    verification = verify_session_archive(archive, expected_session_id=session_id)
    if (
        verification["archive_id"] != str(row["archive_id"])
        or verification["archive_sha256"] != str(row["archive_sha256"])
    ):
        raise ValueError("materialized Session archive identity mismatch")
    return archive


def _lineage(
    *,
    session_id: str,
    run_ids: list[str],
    message_ids: list[str],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    event_ids = sorted(
        str(event.get("event_id"))
        for snapshot in runs
        for event in snapshot.get("events") or []
        if isinstance(event, dict) and str(event.get("event_id") or "")
    )
    artifact_ids = sorted(
        str(artifact.get("artifact_id"))
        for snapshot in runs
        for artifact in snapshot.get("artifacts") or []
        if isinstance(artifact, dict) and str(artifact.get("artifact_id") or "")
    )
    identity = {
        "session_id": session_id,
        "run_ids": sorted(run_ids),
        "message_ids": sorted(message_ids),
        "event_ids": event_ids,
        "artifact_ids": artifact_ids,
    }
    return {**identity, "lineage_sha256": _digest(identity)}


def _reject_existing_ids(conn: sqlite3.Connection, table: str, column: str, identifiers: list[str]) -> None:
    for identifier in identifiers:
        if conn.execute(f"select 1 from {table} where {column}=? limit 1", (identifier,)).fetchone():
            raise ValueError(f"cold restore refuses to overwrite existing {table} ID:{identifier}")


def _init_materialized_archive_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        create table if not exists {MATERIALIZED_ARCHIVE_TABLE} (
            archive_id text primary key,
            session_id text not null unique,
            archive_sha256 text not null,
            archive_json text not null,
            materialized_at text not null
        );
        create trigger if not exists {MATERIALIZED_ARCHIVE_TABLE}_immutable_update
            before update on {MATERIALIZED_ARCHIVE_TABLE}
            begin select raise(abort, 'materialized session archives are immutable'); end;
        create trigger if not exists {MATERIALIZED_ARCHIVE_TABLE}_immutable_delete
            before delete on {MATERIALIZED_ARCHIVE_TABLE}
            begin select raise(abort, 'materialized session archives are immutable'); end;
        """
    )


def _insert_titles(
    conn: sqlite3.Connection,
    session_id: str,
    history: list[dict[str, Any]],
    fallback_title: str,
) -> None:
    rows = history or [{
        "title_history_id": f"ATH-restore-{uuid4().hex}",
        "session_id": session_id,
        "revision": 1,
        "title": fallback_title,
        "reason": "cold_restore",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"source": "session_archive"},
    }]
    current = rows[-1]
    conn.execute(
        "insert into agent_session_titles(session_id, current_revision, title, updated_at, payload_json) values (?, ?, ?, ?, ?)",
        (session_id, int(current.get("revision") or 1), str(current.get("title") or fallback_title), str(current.get("created_at") or ""), _json(current.get("metadata") or {})),
    )
    for row in rows:
        conn.execute(
            "insert into agent_session_title_history(title_history_id, session_id, revision, title, reason, created_at, payload_json) values (?, ?, ?, ?, ?, ?, ?)",
            (str(row.get("title_history_id") or f"ATH-restore-{uuid4().hex}"), session_id, int(row.get("revision") or 1), str(row.get("title") or fallback_title), str(row.get("reason") or "cold_restore"), str(row.get("created_at") or ""), _json(row.get("metadata") or {})),
        )


def _insert_run(conn: sqlite3.Connection, run: dict[str, Any], *, session_id: str) -> None:
    request = run.get("request") if isinstance(run.get("request"), dict) else run.get("request_json")
    if not isinstance(request, dict):
        request = {}
    conn.execute(
        """
        insert into agent_runs(
            run_id, created_at, updated_at, started_at, completed_at, status,
            objective, driver, autonomy, symbols_json, max_steps, current_step,
            cancel_requested, request_json, result_json, error_json, session_id,
            parent_run_id, idempotency_key
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["run_id"]), str(run.get("created_at") or ""), str(run.get("updated_at") or run.get("created_at") or ""),
            run.get("started_at"), run.get("completed_at"), str(run.get("status") or "queued"),
            str(run.get("objective") or request.get("objective") or ""), run.get("driver") or run.get("driver_id"),
            str(run.get("autonomy") or request.get("autonomy") or "advisory"), _json(run.get("symbols") or request.get("symbols") or []),
            int(run.get("max_steps") or request.get("max_steps") or 6), int(run.get("current_step") or 0), int(bool(run.get("cancel_requested"))),
            _json(request), _json(run.get("result")) if run.get("result") is not None else None,
            _json(run.get("error")) if run.get("error") is not None else None, session_id, run.get("parent_run_id"), run.get("idempotency_key"),
        ),
    )


def _insert_message(conn: sqlite3.Connection, message: dict[str, Any]) -> None:
    columns = {str(row[1]) for row in conn.execute("pragma table_info(agent_messages)").fetchall()}
    values: dict[str, Any] = {
        "message_id": str(message["message_id"]), "session_id": str(message["session_id"]), "run_id": message.get("run_id"),
        "role": str(message.get("role") or "user"), "created_at": str(message.get("created_at") or ""),
        "content_json": _json(message.get("content") or {}), "source_json": _json(message.get("source") or {}),
        "kind": str(message.get("kind") or "text"), "status": str(message.get("status") or "completed"),
    }
    selected = [key for key in values if key in columns]
    placeholders = ", ".join("?" for _ in selected)
    conn.execute(
        f"insert into agent_messages({', '.join(selected)}) values ({placeholders})",
        tuple(values[key] for key in selected),
    )


def _safe(value: Any) -> Any:
    return redact_runtime_value(value)


def _required_id(value: dict[str, Any], key: str, label: str) -> str:
    identifier = str(value.get(key) or "").strip()
    if not identifier:
        raise ValueError(f"Session archive {label} is missing {key}")
    return identifier


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_object(value: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _digest(value: Any) -> str:
    payload = dict(value)
    payload.pop("archive_sha256", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
