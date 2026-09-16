from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import (
    ManagedSQLiteConnection,
    apply_migrations,
    configure_connection,
)

from .checkpoints import CheckpointLevel, CheckpointRecord, _checkpoint_identity


_EVENT_TYPE = "agent.checkpoint"
_SCHEMA_VERSION = "open_stock_ai.scoped_checkpoint.v1"


class DurableCheckpointStore:
    """Persist scoped checkpoints in the existing durable generic event table.

    Records are inserted as already-processed envelopes, so scheduler inbox
    workers never claim them. The existing dedup key constraint provides an
    atomic, process-safe idempotency boundary without a schema migration.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migrations(conn)

    def save(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        envelope = _envelope(checkpoint)
        encoded = _json(envelope)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        dedup_key = _dedup_key(checkpoint.checkpoint_id)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            conn.execute(
                """
                insert or ignore into agent_runtime_events(
                    event_id, event_type, status, payload_json, payload_hash,
                    dedup_key, occurred_at, created_at, processed_at
                ) values (?, ?, 'processed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    _event_id(checkpoint.checkpoint_id),
                    _EVENT_TYPE,
                    encoded,
                    digest,
                    dedup_key,
                    checkpoint.created_at,
                    checkpoint.created_at,
                    checkpoint.created_at,
                ),
            )
            row = conn.execute(
                """
                select event_type, payload_json, payload_hash
                  from agent_runtime_events where dedup_key=?
                """,
                (dedup_key,),
            ).fetchone()
            if row is None or row["event_type"] != _EVENT_TYPE:
                conn.rollback()
                raise ValueError(
                    f"Checkpoint id {checkpoint.checkpoint_id!r} already has different content"
                )
            persisted = _record(row["payload_json"])
            if _checkpoint_identity(persisted) != _checkpoint_identity(checkpoint):
                conn.rollback()
                raise ValueError(
                    f"Checkpoint id {checkpoint.checkpoint_id!r} already has different content"
                )
            conn.commit()
        return persisted

    def list_for_session(self, session_id: str) -> list[CheckpointRecord]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select payload_json
                  from agent_runtime_events
                 where event_type=?
                   and json_extract(payload_json, '$.record.session_id')=?
                 order by
                    cast(json_extract(payload_json, '$.record.sequence') as integer),
                    created_at,
                    event_id
                """,
                (_EVENT_TYPE, str(session_id)),
            ).fetchall()
        return [_record(row["payload_json"]) for row in rows]

    def latest_by_scope(
        self,
        *,
        level: CheckpointLevel,
        session_id: str,
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> CheckpointRecord | None:
        scope_field, scope_value = _scope_identity(
            level=level,
            run_id=run_id,
            branch_id=branch_id,
            step_id=step_id,
            decision_id=decision_id,
            automation_id=automation_id,
        )
        clauses = [
            "event_type=?",
            "json_extract(payload_json, '$.record.session_id')=?",
            "json_extract(payload_json, '$.record.level')=?",
        ]
        parameters: list[Any] = [_EVENT_TYPE, str(session_id), level.value]
        if scope_field is not None:
            clauses.append(f"json_extract(payload_json, '$.record.{scope_field}')=?")
            parameters.append(scope_value)
        for field, value in (
            ("run_id", run_id),
            ("branch_id", branch_id),
            ("step_id", step_id),
            ("decision_id", decision_id),
            ("automation_id", automation_id),
        ):
            if value is not None and field != scope_field:
                clauses.append(f"json_extract(payload_json, '$.record.{field}')=?")
                parameters.append(str(value))
        query = f"""
            select payload_json
              from agent_runtime_events
             where {' and '.join(clauses)}
             order by
                cast(json_extract(payload_json, '$.record.sequence') as integer) desc,
                created_at desc,
                event_id desc
             limit 1
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(query, parameters).fetchone()
        return _record(row["payload_json"]) if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _scope_identity(
    *,
    level: CheckpointLevel,
    run_id: str | None,
    branch_id: str | None,
    step_id: str | None,
    decision_id: str | None,
    automation_id: str | None,
) -> tuple[str | None, str | None]:
    identities = {
        CheckpointLevel.SESSION: (None, None),
        CheckpointLevel.RUN: ("run_id", run_id),
        CheckpointLevel.BRANCH: ("branch_id", branch_id),
        CheckpointLevel.STEP: ("step_id", step_id),
        CheckpointLevel.DECISION: ("decision_id", decision_id),
        CheckpointLevel.AUTOMATION: ("automation_id", automation_id),
    }
    field, value = identities[level]
    if field is not None and not value:
        raise ValueError(f"{level.value} checkpoint lookup requires {field}")
    return field, str(value) if value is not None else None


def _envelope(checkpoint: CheckpointRecord) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "record": {
            "checkpoint_id": checkpoint.checkpoint_id,
            "level": checkpoint.level.value,
            "session_id": checkpoint.session_id,
            "run_id": checkpoint.run_id,
            "branch_id": checkpoint.branch_id,
            "step_id": checkpoint.step_id,
            "decision_id": checkpoint.decision_id,
            "automation_id": checkpoint.automation_id,
            "sequence": checkpoint.sequence,
            "created_at": checkpoint.created_at,
            "payload": checkpoint.payload,
        },
    }


def _record(encoded: str) -> CheckpointRecord:
    try:
        envelope = json.loads(encoded)
        if envelope.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("Unsupported scoped checkpoint schema")
        item = envelope["record"]
        return CheckpointRecord(
            checkpoint_id=str(item["checkpoint_id"]),
            level=CheckpointLevel(str(item["level"])),
            session_id=str(item["session_id"]),
            run_id=_optional_text(item.get("run_id")),
            branch_id=_optional_text(item.get("branch_id")),
            step_id=_optional_text(item.get("step_id")),
            decision_id=_optional_text(item.get("decision_id")),
            automation_id=_optional_text(item.get("automation_id")),
            sequence=int(item.get("sequence") or 0),
            created_at=str(item["created_at"]),
            payload=dict(item.get("payload") or {}),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid durable scoped checkpoint envelope") from exc


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _event_id(checkpoint_id: str) -> str:
    digest = hashlib.sha256(checkpoint_id.encode("utf-8")).hexdigest()
    return f"AEVT-CP-{digest[:32]}"


def _dedup_key(checkpoint_id: str) -> str:
    return f"agent-checkpoint:{checkpoint_id}"


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = ["DurableCheckpointStore"]
