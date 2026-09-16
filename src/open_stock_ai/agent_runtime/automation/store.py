from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, configure_connection
from open_stock_ai.agent_runtime.scheduler.market_schedule import (
    align_market_due,
    is_market_closed,
    market_calendar_name,
)

from .dedup import semantic_projection
from .intent import AutomationIntent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutomationStore:
    """SQLite store using the canonical P71 Automation table layouts."""

    def __init__(self, db_path: str | Path, *, market_calendar: Any | None = None) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.market_calendar = market_calendar
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.reconcile_submission_lifecycles()

    def save_intent(self, intent: AutomationIntent, fingerprint: str, *, now: datetime | None = None) -> str:
        intent_id = f"AIT-{uuid4().hex}"
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            conn.execute(
                """insert into agent_automation_intents(
                       intent_id, session_id, run_id, branch_id, user_id, goal,
                       symbol, kind, status, semantic_fingerprint, intent_json,
                       created_at, updated_at, payload_json
                   ) values (?, ?, null, null, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)""",
                (
                    intent_id, intent.session_id, intent.user_id, intent.goal,
                    intent.symbol, intent.kind.value, fingerprint,
                    _json(intent.to_semantic_dict()), timestamp, timestamp,
                    _json(intent.to_semantic_dict()),
                ),
            )
            conn.commit()
        return intent_id

    def create_automation(
        self,
        *,
        intent_id: str,
        intent: AutomationIntent,
        fingerprint: str,
        state: str = "draft",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        automation_id = f"AUT-{uuid4().hex}"
        timestamp = _iso(now or utc_now())
        payload = {
            "user_id": intent.user_id,
            "goal": intent.goal,
            "symbol": intent.symbol,
            "kind": intent.kind.value,
            "backend_reference": None,
            "current_decision": {},
            "notification_state": {},
        }
        with self._connect() as conn:
            conn.execute(
                """insert into agent_automations(
                       automation_id, intent_id, session_id, user_id, goal, symbol,
                       kind, state, status, backend, backend_reference,
                       semantic_fingerprint, current_version, current_decision_json,
                       notification_state_json, created_at, updated_at, expires_at,
                       payload_json
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, null, null, ?, 0, null, '{}', ?, ?, null, ?)""",
                (
                    automation_id,
                    intent_id,
                    intent.session_id,
                    intent.user_id,
                    intent.goal,
                    intent.symbol,
                    intent.kind.value,
                    state,
                    state,
                    fingerprint,
                    timestamp,
                    timestamp,
                    _json(payload),
                ),
            )
            conn.commit()
        return self.get_automation(automation_id) or {}

    def save_version(
        self,
        automation_id: str,
        *,
        intent: AutomationIntent,
        compiled: Mapping[str, Any],
        artifact: Mapping[str, Any],
        backend: str,
        backend_reference: str | None = None,
        now: datetime | None = None,
    ) -> int:
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(version), 0) from agent_automation_versions where automation_id=?",
                (automation_id,),
            ).fetchone()
            version = int(row[0]) + 1
            conn.execute(
                """insert into agent_automation_versions(
                       automation_version_id, automation_id, version, status,
                       intent_json, compiled_json, artifact_json, created_at,
                       payload_json
                   ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
                (
                    f"AVR-{uuid4().hex}",
                    automation_id,
                    version,
                    _json(intent.to_semantic_dict()),
                    _json(dict(compiled)),
                    _json(dict(artifact)),
                    timestamp,
                    _json({"intent": intent.to_semantic_dict(), "compiled": dict(compiled), "artifact": dict(artifact)}),
                ),
            )
            payload = self._automation_payload(conn, automation_id)
            payload["backend_reference"] = backend_reference or payload.get("backend_reference")
            conn.execute(
                """update agent_automations
                      set current_version=?, backend=?, backend_reference=coalesce(?, backend_reference),
                          updated_at=?, payload_json=?
                    where automation_id=?""",
                (version, backend, backend_reference, timestamp, _json(payload), automation_id),
            )
            conn.commit()
        return version

    def retarget_automation(
        self,
        automation_id: str,
        *,
        intent_id: str,
        intent: AutomationIntent,
        fingerprint: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Point an existing durable Automation at a new semantic version."""
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            cursor = conn.execute(
                """update agent_automations
                      set intent_id=?, session_id=?, user_id=?, goal=?, symbol=?, kind=?,
                          semantic_fingerprint=?, updated_at=?
                    where automation_id=?""",
                (
                    intent_id,
                    intent.session_id,
                    intent.user_id,
                    intent.goal,
                    intent.symbol,
                    intent.kind.value,
                    fingerprint,
                    timestamp,
                    automation_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown automation: {automation_id}")
            payload = self._automation_payload(conn, automation_id)
            payload.update(
                {
                    "user_id": intent.user_id,
                    "goal": intent.goal,
                    "symbol": intent.symbol,
                    "kind": intent.kind.value,
                }
            )
            conn.execute(
                "update agent_automations set payload_json=? where automation_id=?",
                (_json(payload), automation_id),
            )
            conn.commit()
        return self.get_automation(automation_id) or {}

    def set_state(self, automation_id: str, state: str, *, now: datetime | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                "update agent_automations set state=?, status=?, updated_at=? where automation_id=?",
                (state, state, _iso(now or utc_now()), automation_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown automation: {automation_id}")
            if state in {"paused", "archived", "expired", "deleted"}:
                conn.execute(
                    """update agent_automation_schedules set status=?, updated_at=?
                         where automation_id=? and status in ('active', 'paused')""",
                    (state, _iso(now or utc_now()), automation_id),
                )
            elif state == "active":
                conn.execute(
                    """update agent_automation_schedules set status='active', updated_at=?
                         where automation_id=? and status='paused'""",
                    (_iso(now or utc_now()), automation_id),
                )
            conn.commit()
        return self.get_automation(automation_id) or {}

    def set_backend_reference(self, automation_id: str, reference: str, *, now: datetime | None = None) -> None:
        self._update_automation_payload(automation_id, {"backend_reference": reference}, now=now)
        with self._connect() as conn:
            conn.execute(
                "update agent_automations set backend_reference=? where automation_id=?",
                (reference, automation_id),
            )
            conn.commit()

    def finish_reanalysis_state(self, automation_id: str, state: str, *, now: datetime | None = None) -> None:
        """An in-flight result must never undo a subsequent user pause."""
        if state not in {"active", "failed"}:
            raise ValueError("reanalysis may finish only active or failed")
        with self._connect() as conn:
            conn.execute(
                """update agent_automations set state=?, status=?, updated_at=?
                     where automation_id=? and state='triggered'""",
                (state, state, _iso(now or utc_now()), automation_id),
            )
            conn.commit()

    def reserve_reanalysis(
        self, automation_id: str, execution_id: str, *,
        max_per_day: int, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically admit one candidate and charge its UTC-day AI budget.

        Failed attempts remain charged. A reservation is not automatically
        replayed after a crash: starting another provider call without a
        durable completion receipt could spend twice for the same occurrence.
        """
        if max_per_day < 1:
            raise ValueError("max_per_day must be positive")
        timestamp = _iso(now or utc_now())
        day = timestamp[:10]
        with self._connect() as conn:
            conn.execute("begin immediate")
            automation = conn.execute(
                "select state from agent_automations where automation_id=?", (automation_id,),
            ).fetchone()
            execution = conn.execute(
                "select automation_id from agent_automation_executions where execution_id=?", (execution_id,),
            ).fetchone()
            if not automation or not execution or execution[0] != automation_id:
                raise KeyError("unknown automation or mismatched execution")
            existing = conn.execute(
                "select execution_id from agent_automation_reanalysis_budget where execution_id=?", (execution_id,),
            ).fetchone()
            used = int(conn.execute(
                "select count(*) from agent_automation_reanalysis_budget where automation_id=? and budget_day=?",
                (automation_id, day),
            ).fetchone()[0])
            reason = (
                "occurrence_already_reserved" if existing else
                f"automation_{automation[0]}" if automation[0] != "active" else
                "daily_reanalysis_limit_reached" if used >= max_per_day else "admitted"
            )
            admitted = reason == "admitted"
            if admitted:
                conn.execute(
                    """insert into agent_automation_reanalysis_budget
                           (execution_id, automation_id, budget_day, reserved_at) values (?, ?, ?, ?)""",
                    (execution_id, automation_id, day, timestamp),
                )
                conn.execute(
                    """update agent_automations set state='triggered', status='triggered', updated_at=?
                         where automation_id=?""", (timestamp, automation_id),
                )
                used += 1
            conn.commit()
        return {"admitted": admitted, "reason": reason, "budget_day": day,
                "used": used, "limit": max_per_day}

    def update_decision(self, automation_id: str, decision: Mapping[str, Any], *, now: datetime | None = None) -> None:
        self._update_automation_payload(automation_id, {"current_decision": dict(decision)}, now=now)
        with self._connect() as conn:
            conn.execute(
                "update agent_automations set current_decision_json=? where automation_id=?",
                (_json(dict(decision)), automation_id),
            )
            conn.commit()

    def get_automation(self, automation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_automations where automation_id=?", (automation_id,)).fetchone()
        return _automation_row(row) if row else None

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_automation_intents where intent_id=?", (intent_id,)).fetchone()
        return {**dict(row), "intent": _load(row["intent_json"])} if row else None

    def latest_version(self, automation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_automation_versions where automation_id=? order by version desc limit 1",
                (automation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "intent": _load(row["intent_json"]),
            "compiled": _load(row["compiled_json"]),
            "artifact": _load(row["artifact_json"]),
        }

    def semantic_candidates(self, intent: AutomationIntent) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """select a.automation_id, a.semantic_fingerprint, i.intent_json
                     from agent_automations a
                     join agent_automation_intents i on i.intent_id=a.intent_id
                    where a.user_id=?
                      and a.state not in ('deleted', 'archived', 'expired')""",
                (intent.user_id,),
            ).fetchall()
        return [
            {
                "automation_id": row["automation_id"],
                "semantic_fingerprint": row["semantic_fingerprint"],
                "semantic_projection": semantic_projection(AutomationIntent.from_dict(_load(row["intent_json"]))),
            }
            for row in rows
        ]

    def save_execution(
        self,
        automation_id: str,
        *,
        stage: str,
        status: str,
        input_payload: Mapping[str, Any] | None = None,
        output_payload: Mapping[str, Any] | None = None,
        meaningful_change: bool | None = None,
        now: datetime | None = None,
    ) -> str:
        execution_id = self.begin_execution(
            automation_id,
            stage=stage,
            input_payload=input_payload,
            now=now,
        )
        self.complete_execution(
            execution_id,
            status=status,
            output_payload=output_payload,
            meaningful_change=meaningful_change,
            now=now,
        )
        return execution_id

    def begin_execution(
        self,
        automation_id: str,
        *,
        stage: str,
        input_payload: Mapping[str, Any] | None = None,
        source_event_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        normalized_event_id = str(source_event_id or "").strip() or None
        execution_id = f"AEX-{uuid4().hex}"
        timestamp = _iso(now or utc_now())
        automation = self.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        payload = {
            "stage": stage,
            "input": dict(input_payload or {}),
            "output": {},
            "stage_history": [{"stage": stage, "status": "running", "at": timestamp}],
        }
        with self._connect() as conn:
            conn.execute("begin immediate")
            if normalized_event_id:
                existing = conn.execute(
                    """select execution_id from agent_automation_executions
                         where automation_id=? and source_event_id=?""",
                    (automation_id, normalized_event_id),
                ).fetchone()
                if existing:
                    conn.commit()
                    return str(existing[0])
            conn.execute(
                """insert into agent_automation_executions(
                       execution_id, automation_id, automation_version, run_id,
                       stage, status, input_json, output_json, meaningful_change,
                       decision_changed, created_at, started_at, completed_at,
                       payload_json, source_event_id
                   ) values (?, ?, ?, null, ?, 'running', ?, '{}', null, null, ?, ?, null, ?, ?)""",
                (
                    execution_id,
                    automation_id,
                    int(automation.get("current_version") or 0),
                    stage,
                    _json(input_payload or {}),
                    timestamp,
                    timestamp,
                    _json(payload),
                    normalized_event_id,
                ),
            )
            conn.commit()
        return execution_id

    def checkpoint_execution(
        self,
        execution_id: str,
        *,
        stage: str,
        status: str,
        output_payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            row = conn.execute(
                "select payload_json from agent_automation_executions where execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown automation execution: {execution_id}")
            payload = _load(row[0])
            history = list(payload.get("stage_history") or [])
            history.append({"stage": stage, "status": status, "at": timestamp, "output": dict(output_payload or {})})
            payload.update({"stage": stage, "output": dict(output_payload or {}), "stage_history": history})
            conn.execute(
                """update agent_automation_executions
                      set stage=?, status='running', output_json=?, payload_json=?
                    where execution_id=?""",
                (stage, _json(output_payload or {}), _json(payload), execution_id),
            )
            conn.commit()
        return self.get_execution(execution_id) or {}

    def attach_execution_run(self, execution_id: str, run_id: str) -> dict[str, Any]:
        """Durably link an Automation execution to its real Agent Run.

        A scheduler callback creates its execution before the provider-backed
        reanalysis Run exists.  Persisting this link after the Run is queued
        keeps the event → execution → Run chain queryable after a restart,
        instead of leaving the Run identifier only inside a JSON decision.
        """

        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("Automation execution requires a non-empty run_id")
        with self._connect() as conn:
            row = conn.execute(
                "select run_id, payload_json from agent_automation_executions where execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown automation execution: {execution_id}")
            existing_run_id = str(row[0] or "").strip()
            if existing_run_id and existing_run_id != normalized_run_id:
                raise ValueError(
                    "Automation execution is already linked to a different run_id"
                )
            payload = _load(row[1])
            payload["reanalyzed_run_id"] = normalized_run_id
            conn.execute(
                "update agent_automation_executions set run_id=?, payload_json=? where execution_id=?",
                (normalized_run_id, _json(payload), execution_id),
            )
            conn.commit()
        return self.get_execution(execution_id) or {}

    def complete_execution(
        self,
        execution_id: str,
        *,
        status: str,
        output_payload: Mapping[str, Any] | None = None,
        meaningful_change: bool | None = None,
        stage: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            row = conn.execute(
                "select stage, input_json, payload_json from agent_automation_executions where execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown automation execution: {execution_id}")
            payload = _load(row[2])
            history = list(payload.get("stage_history") or [])
            history.append(
                {
                    "stage": stage or row[0],
                    "status": status,
                    "at": timestamp,
                    "output": dict(output_payload or {}),
                }
            )
            payload.update(
                {
                    "stage": stage or row[0],
                    "input": _load(row[1]),
                    "output": dict(output_payload or {}),
                    "stage_history": history,
                }
            )
            conn.execute(
                """update agent_automation_executions
                      set stage=?, status=?, output_json=?, meaningful_change=?,
                          decision_changed=?, completed_at=?, payload_json=?
                    where execution_id=?""",
                (
                    stage or row[0],
                    status,
                    _json(output_payload or {}),
                    None if meaningful_change is None else int(meaningful_change),
                    None if meaningful_change is None else int(meaningful_change),
                    timestamp,
                    _json(payload),
                    execution_id,
                ),
            )
            conn.commit()
        return self.get_execution(execution_id) or {}

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_automation_executions where execution_id=?",
                (execution_id,),
            ).fetchone()
        return _execution_row(row) if row else None

    def get_execution_by_source_event(
        self,
        automation_id: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """select * from agent_automation_executions
                     where automation_id=? and source_event_id=?
                     order by rowid desc limit 1""",
                (automation_id, source_event_id),
            ).fetchone()
        return _execution_row(row) if row else None

    def list_executions(self, automation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_automation_executions where automation_id=? order by rowid",
                (automation_id,),
            ).fetchall()
        result = []
        for row in rows:
            result.append(_execution_row(row))
        return result

    def save_submission(
        self,
        automation_id: str,
        *,
        backend: str,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        operation: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(now or utc_now())
        automation = self.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        with self._connect() as conn:
            conn.execute(
                """insert into agent_automation_submissions(
                       submission_id, automation_id, automation_version, backend,
                       operation, idempotency_key, status, request_json,
                       response_json, backend_reference, created_at, updated_at
                   ) values (?, ?, ?, ?, ?, ?, 'pending', ?, '{}', null, ?, ?)
                   on conflict(backend, idempotency_key) do update set
                       request_json=excluded.request_json, operation=excluded.operation,
                       updated_at=excluded.updated_at""",
                (
                    f"ASB-{uuid4().hex}",
                    automation_id,
                    int(automation.get("current_version") or 0),
                    backend,
                    operation,
                    idempotency_key,
                    _json(request_payload),
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
        return self.get_submission(backend=backend, idempotency_key=idempotency_key) or {}

    def complete_submission(
        self,
        submission_id: str,
        *,
        status: str,
        response_payload: Mapping[str, Any],
        backend_reference: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """update agent_automation_submissions
                      set status=?, response_json=?, backend_reference=?, updated_at=?
                    where submission_id=?""",
                (status, _json(response_payload), backend_reference, _iso(now or utc_now()), submission_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"unknown scheduler submission: {submission_id}")
            conn.commit()
        return self.get_submission(submission_id=submission_id) or {}

    def cancel_pending_submissions(
        self,
        automation_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        """Stop recovery from resubmitting an explicitly paused Automation."""

        timestamp = _iso(now or utc_now())
        response = _json({"submission_status": "cancelled", "reason": str(reason)})
        with self._connect() as conn:
            cursor = conn.execute(
                """update agent_automation_submissions
                      set status='cancelled', response_json=?, updated_at=?
                    where automation_id=? and status='pending'""",
                (response, timestamp, automation_id),
            )
            conn.commit()
        return int(cursor.rowcount)

    def set_submission_lifecycle(
        self,
        automation_id: str,
        *,
        lifecycle: str,
        now: datetime | None = None,
    ) -> int:
        """Mirror a confirmed Automation lifecycle transition in n8n receipts.

        A submission is an immutable deployment receipt, but its lifecycle
        projection must not keep saying ``active`` after the Host has
        successfully paused or archived the corresponding n8n workflow.  The
        original deployment response remains intact; this only records the
        current, externally-confirmed operating state.
        """

        allowed = {
            "active": ("paused",),
            "paused": ("active", "pending"),
            "archived": ("active", "paused", "pending"),
        }
        if lifecycle not in allowed:
            raise ValueError(f"unsupported submission lifecycle:{lifecycle}")
        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            cursor = conn.execute(
                """update agent_automation_submissions
                      set status=?, updated_at=?
                    where automation_id=? and backend='n8n'
                      and status in ({})""".format(
                    ", ".join("?" for _ in allowed[lifecycle])
                ),
                (lifecycle, timestamp, automation_id, *allowed[lifecycle]),
            )
            conn.commit()
        return int(cursor.rowcount)

    def reconcile_submission_lifecycles(self) -> int:
        """Repair legacy n8n receipt projections after an older app restart.

        This is a local metadata reconciliation only: it neither deploys nor
        changes any external workflow.  It closes the historical gap where a
        paused or archived Automation retained an ``active`` submission row.
        """

        timestamp = _iso(utc_now())
        changed = 0
        with self._connect() as conn:
            for lifecycle, source_states in (
                ("paused", ("active", "pending")),
                ("archived", ("active", "paused", "pending")),
            ):
                cursor = conn.execute(
                    """update agent_automation_submissions
                          set status=?, updated_at=?
                        where backend='n8n' and status in ({})
                          and automation_id in (
                              select automation_id from agent_automations where state=?
                          )""".format(
                        ", ".join("?" for _ in source_states)
                    ),
                    (lifecycle, timestamp, *source_states, lifecycle),
                )
                changed += int(cursor.rowcount)
            conn.commit()
        return changed

    def get_submission(
        self,
        *,
        submission_id: str | None = None,
        backend: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        if submission_id is None and (backend is None or idempotency_key is None):
            raise ValueError("submission_id or backend plus idempotency_key is required")
        query = "select * from agent_automation_submissions where submission_id=?"
        params: tuple[Any, ...] = (submission_id,)
        if submission_id is None:
            query = "select * from agent_automation_submissions where backend=? and idempotency_key=?"
            params = (backend, idempotency_key)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(query, params).fetchone()
        return _submission_row(row) if row else None

    def list_submissions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "select * from agent_automation_submissions"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " where status=?"
            params = (status,)
        query += " order by rowid"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_submission_row(row) for row in rows]

    def record_verified_callback(
        self,
        *,
        authentication: Mapping[str, Any],
        event_type: str,
        payload: Mapping[str, Any],
        outcomes: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist one immutable, secret-free receipt for an accepted n8n callback."""

        if authentication.get("source") != "n8n" or authentication.get("authenticated") is not True:
            raise ValueError("only authenticated n8n callbacks can be receipted")
        required_hashes = ("body_sha256", "nonce_sha256", "signature_sha256", "token_sha256")
        values = {key: str(authentication.get(key) or "").strip() for key in required_hashes}
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in values.values()):
            raise ValueError("callback authentication proof is incomplete")
        timestamp = int(authentication.get("callback_timestamp") or 0)
        verified_at = int(authentication.get("verified_at") or 0)
        if timestamp <= 0 or verified_at <= 0:
            raise ValueError("callback authentication timestamp is invalid")
        projected_payload = {
            key: payload[key]
            for key in (
                "automation_id", "automation_version", "submission_id", "source_event_id",
                "source", "compiler_digest", "compiled_stage_count", "market_calendar",
            )
            if key in payload
        }
        projected_outcomes = [
            {
                key: outcome[key]
                for key in (
                    "schedule_id", "automation_id", "status", "execution_id", "reanalyzed",
                    "meaningful_change", "notified",
                )
                if key in outcome
            }
            for outcome in outcomes
        ]
        receipt_input = {
            "schema_version": "open_stock_ai.n8n_callback_receipt.v1",
            "source": "n8n",
            "authenticated": True,
            "event_type": str(event_type),
            "callback_timestamp": timestamp,
            "verified_at": verified_at,
            **values,
            "payload": projected_payload,
            "outcomes": projected_outcomes,
        }
        receipt_sha256 = hashlib.sha256(_json(receipt_input).encode("utf-8")).hexdigest()
        created_at = _iso(utc_now())
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                "select * from agent_automation_callback_receipts where source=? and nonce_sha256=?",
                ("n8n", values["nonce_sha256"]),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return _callback_receipt_row(existing)
            receipt_id = f"ACR-{uuid4().hex}"
            conn.execute(
                """insert into agent_automation_callback_receipts(
                       receipt_id, source, authenticated, event_type, automation_id, automation_version,
                       submission_id, source_event_id, callback_timestamp, verified_at, body_sha256,
                       nonce_sha256, signature_sha256, token_sha256, payload_json, outcomes_json,
                       receipt_sha256, created_at
                   ) values (?, 'n8n', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt_id, str(event_type), projected_payload.get("automation_id"),
                    projected_payload.get("automation_version"), projected_payload.get("submission_id"),
                    projected_payload.get("source_event_id"), timestamp, verified_at, values["body_sha256"],
                    values["nonce_sha256"], values["signature_sha256"], values["token_sha256"],
                    _json(projected_payload), _json(projected_outcomes), receipt_sha256, created_at,
                ),
            )
            row = conn.execute(
                "select * from agent_automation_callback_receipts where receipt_id=?", (receipt_id,)
            ).fetchone()
            conn.commit()
        return _callback_receipt_row(row)

    def list_callback_receipts(
        self,
        *,
        automation_id: str | None = None,
        submission_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if automation_id:
            clauses.append("automation_id=?")
            params.append(automation_id)
        if submission_id:
            clauses.append("submission_id=?")
            params.append(submission_id)
        query = "select * from agent_automation_callback_receipts"
        if clauses:
            query += " where " + " and ".join(clauses)
        query += " order by rowid desc limit ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_callback_receipt_row(row) for row in rows]

    def register_schedule(
        self,
        automation_id: str,
        *,
        automation_version: int,
        definition: Mapping[str, Any],
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist an executable internal schedule and its registration receipt.

        A backend reference is valid only when this transaction commits both the
        schedule row and an accepted receipt.  Repeated submissions reuse the
        same schedule while still returning the durable receipt that proves the
        registry contains it.
        """

        timestamp = _iso(now or utc_now())
        automation = self.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        trigger = dict(definition.get("trigger") or {})
        if not trigger:
            raise ValueError("internal schedule requires a compiled trigger")
        next_due = _initial_due(trigger, now or utc_now(), self.market_calendar)
        misfire_policy = _misfire_policy(trigger)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                "select * from agent_automation_schedules where idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            conn.execute(
                """update agent_automation_schedules
                      set status='superseded', updated_at=?
                    where automation_id=? and idempotency_key<>? and status='active'""",
                (timestamp, automation_id, idempotency_key),
            )
            schedule_id = str(existing["schedule_id"]) if existing else f"AUS-{uuid4().hex}"
            receipt_id = f"AUR-{uuid4().hex}"
            payload = {
                "definition": dict(definition),
                "trigger": trigger,
                "registration_receipt_id": receipt_id,
                "next_due_at": _iso(next_due) if next_due else None,
                "misfire_policy": misfire_policy,
            }
            if existing:
                conn.execute(
                    """update agent_automation_schedules
                          set automation_id=?, automation_version=?, status='active',
                              trigger_json=?, definition_json=?, updated_at=?,
                              registration_receipt_id=?, last_error=null,
                              next_due_at=?, misfire_policy=?, payload_json=?
                        where schedule_id=?""",
                    (
                        automation_id,
                        automation_version,
                        _json(trigger),
                        _json(dict(definition)),
                        timestamp,
                        receipt_id,
                        _iso(next_due) if next_due else None,
                        misfire_policy,
                        _json(payload),
                        schedule_id,
                    ),
                )
            else:
                conn.execute(
                    """insert into agent_automation_schedules(
                           schedule_id, automation_id, automation_version, status,
                           trigger_json, definition_json, idempotency_key,
                           registration_receipt_id, created_at, updated_at,
                           last_triggered_at, last_error, next_due_at,
                           last_scheduled_for, misfire_policy, payload_json
                       ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, null, null, ?, null, ?, ?)""",
                    (
                        schedule_id,
                        automation_id,
                        automation_version,
                        _json(trigger),
                        _json(dict(definition)),
                        idempotency_key,
                        receipt_id,
                        timestamp,
                        timestamp,
                        _iso(next_due) if next_due else None,
                        misfire_policy,
                        _json(payload),
                    ),
                )
            response = {
                "accepted": True,
                "scheduled": True,
                "schedule_id": schedule_id,
                "receipt_id": receipt_id,
                "status": "active",
                "durability": "sqlite_schedule_registry",
            }
            conn.execute(
                """insert into agent_automation_schedule_receipts(
                       receipt_id, schedule_id, automation_id, receipt_type,
                       status, request_json, response_json, error,
                       created_at, completed_at
                   ) values (?, ?, ?, 'registration', 'accepted', ?, ?, null, ?, ?)""",
                (
                    receipt_id,
                    schedule_id,
                    automation_id,
                    _json({"idempotency_key": idempotency_key, "definition": dict(definition)}),
                    _json(response),
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
        return {
            "schedule": self.get_schedule(schedule_id) or {},
            "receipt": self.get_schedule_receipt(receipt_id) or {},
        }

    def list_due_schedules(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return active product-time schedules whose durable deadline has arrived."""

        timestamp = _iso(now or utc_now())
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """select * from agent_automation_schedules
                     where status='active' and next_due_at is not null and next_due_at<=?
                     order by next_due_at, rowid""",
                (timestamp,),
            ).fetchall()
        return [_schedule_row(row) for row in rows]

    def claim_due_schedule(
        self,
        schedule_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically claim one due occurrence and advance its durable clock.

        ``run_once`` coalesces all occurrences missed while the product was
        stopped. ``catch_up`` advances one occurrence at a time. ``skip``
        records the miss but intentionally performs no callback.
        """

        current = now or utc_now()
        timestamp = _iso(current)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            row = conn.execute(
                """select * from agent_automation_schedules
                     where schedule_id=? and status='active'""",
                (schedule_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return {"claimed": False, "reason": "schedule is not active"}
            due_text = str(row["next_due_at"] or "")
            if not due_text or _parse_iso(due_text) > _utc(current):
                conn.rollback()
                return {"claimed": False, "reason": "schedule is not due"}
            trigger = _load(row["trigger_json"])
            policy = str(row["misfire_policy"] or _misfire_policy(trigger))
            due = _parse_iso(due_text)
            if is_market_closed(_utc(current), trigger, self.market_calendar):
                next_due = _next_due(
                    trigger,
                    due,
                    _utc(current),
                    coalesce=True,
                    market_calendar=self.market_calendar,
                )
                conn.execute(
                    """update agent_automation_schedules
                          set next_due_at=?, updated_at=?, last_error=?
                        where schedule_id=?""",
                    (
                        _iso(next_due) if next_due else None,
                        timestamp,
                        "deferred_market_closed",
                        schedule_id,
                    ),
                )
                conn.commit()
                return {
                    "claimed": False,
                    "skipped": True,
                    "deferred": True,
                    "reason": "market_closed",
                    "next_due_at": _iso(next_due) if next_due else None,
                }
            missed = _missed_occurrences(trigger, due, _utc(current))
            occurrence_key = f"time:{schedule_id}:{due_text}"
            existing = conn.execute(
                """select * from agent_automation_schedule_receipts
                     where schedule_id=? and occurrence_key=?""",
                (schedule_id, occurrence_key),
            ).fetchone()
            if existing:
                conn.rollback()
                return {
                    "claimed": False,
                    "reason": "occurrence already claimed",
                    "receipt": _schedule_receipt_row(existing),
                }
            next_due = _next_due(
                trigger,
                due,
                _utc(current),
                coalesce=policy != "catch_up",
                market_calendar=self.market_calendar,
            )
            receipt_id = f"AUR-{uuid4().hex}"
            event = {
                "source_event_id": occurrence_key,
                "schedule_id": schedule_id,
                "scheduled_for": due_text,
                "triggered_at": timestamp,
                "missed_occurrences": missed,
                "misfire_policy": policy,
            }
            receipt_status = "skipped" if policy == "skip" and missed > 0 else "processing"
            conn.execute(
                """insert into agent_automation_schedule_receipts(
                       receipt_id, schedule_id, automation_id, receipt_type,
                       status, request_json, response_json, error,
                       created_at, completed_at, occurrence_key
                   ) values (?, ?, ?, 'time_trigger', ?, ?, '{}', null, ?, ?, ?)""",
                (
                    receipt_id,
                    schedule_id,
                    row["automation_id"],
                    receipt_status,
                    _json(event),
                    timestamp,
                    timestamp if receipt_status == "skipped" else None,
                    occurrence_key,
                ),
            )
            conn.execute(
                """update agent_automation_schedules
                      set next_due_at=?, last_scheduled_for=?, updated_at=?
                    where schedule_id=?""",
                (_iso(next_due) if next_due else None, due_text, timestamp, schedule_id),
            )
            conn.commit()
        return {
            "claimed": receipt_status == "processing",
            "skipped": receipt_status == "skipped",
            "event": event,
            "receipt": self.get_schedule_receipt(receipt_id) or {},
            "next_due_at": _iso(next_due) if next_due else None,
        }

    def list_incomplete_time_callbacks(self) -> list[dict[str, Any]]:
        """List time occurrences that were claimed but not completed before restart."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """select * from agent_automation_schedule_receipts
                     where receipt_type='time_trigger' and status='processing'
                     order by created_at, rowid"""
            ).fetchall()
        return [_schedule_receipt_row(row) for row in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_automation_schedules where schedule_id=?",
                (schedule_id,),
            ).fetchone()
        return _schedule_row(row) if row else None

    def list_schedules(self, automation_id: str | None = None) -> list[dict[str, Any]]:
        query = "select * from agent_automation_schedules"
        params: tuple[Any, ...] = ()
        if automation_id is not None:
            query += " where automation_id=?"
            params = (automation_id,)
        query += " order by rowid"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [_schedule_row(row) for row in rows]

    def begin_schedule_callback(
        self,
        schedule_id: str,
        event: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = _iso(now or utc_now())
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            raise KeyError(f"unknown automation schedule: {schedule_id}")
        if schedule["status"] != "active":
            raise ValueError("only active automation schedules accept callbacks")
        occurrence_key = str(event.get("source_event_id") or event.get("event_id") or "").strip() or None
        receipt_id = f"AUR-{uuid4().hex}"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            if occurrence_key:
                existing = conn.execute(
                    """select * from agent_automation_schedule_receipts
                         where schedule_id=? and occurrence_key=?""",
                    (schedule_id, occurrence_key),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return {**_schedule_receipt_row(existing), "claimed": False}
            conn.execute(
                """insert into agent_automation_schedule_receipts(
                       receipt_id, schedule_id, automation_id, receipt_type,
                       status, request_json, response_json, error,
                       created_at, completed_at, occurrence_key
                   ) values (?, ?, ?, 'trigger', 'processing', ?, '{}', null, ?, null, ?)""",
                (
                    receipt_id,
                    schedule_id,
                    schedule["automation_id"],
                    _json(dict(event)),
                    timestamp,
                    occurrence_key,
                ),
            )
            conn.commit()
        return {**(self.get_schedule_receipt(receipt_id) or {}), "claimed": True}

    def complete_schedule_callback(
        self,
        receipt_id: str,
        *,
        status: str,
        response: Mapping[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "filtered", "failed"}:
            raise ValueError("unsupported schedule callback receipt status")
        timestamp = _iso(now or utc_now())
        receipt = self.get_schedule_receipt(receipt_id)
        if not receipt:
            raise KeyError(f"unknown automation schedule receipt: {receipt_id}")
        schedule = self.get_schedule(str(receipt["schedule_id"])) or {}
        trigger_type = str(dict(schedule.get("trigger") or {}).get("type") or "").casefold()
        terminal = trigger_type in {"once", "one_shot"} and status != "failed"
        schedule_status = "failed" if status == "failed" else ("completed" if terminal else "active")
        with self._connect() as conn:
            conn.execute(
                """update agent_automation_schedule_receipts
                      set status=?, response_json=?, error=?, completed_at=?
                    where receipt_id=?""",
                (status, _json(dict(response or {})), error, timestamp, receipt_id),
            )
            conn.execute(
                """update agent_automation_schedules
                      set status=case when status in ('paused', 'archived', 'deleted') then status else ? end,
                          updated_at=?, last_triggered_at=?, last_error=?
                    where schedule_id=?""",
                (schedule_status, timestamp, timestamp, error, receipt["schedule_id"]),
            )
            conn.commit()
        return self.get_schedule_receipt(receipt_id) or {}

    def get_schedule_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_automation_schedule_receipts where receipt_id=?",
                (receipt_id,),
            ).fetchone()
        return _schedule_receipt_row(row) if row else None

    def list_schedule_receipts(self, schedule_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """select * from agent_automation_schedule_receipts
                     where schedule_id=? order by rowid""",
                (schedule_id,),
            ).fetchall()
        return [_schedule_receipt_row(row) for row in rows]

    def insert_delivery(self, payload: Mapping[str, Any]) -> None:
        body = {
            "user_id": payload["user_id"],
            "payload": dict(payload.get("payload") or {}),
            "provider_receipt": dict(payload.get("provider_receipt") or {}),
            "error": payload.get("error"),
            "delivered_at": payload.get("delivered_at"),
        }
        with self._connect() as conn:
            conn.execute(
                """insert into agent_notification_deliveries(
                       delivery_id, automation_id, execution_id, user_id, channel,
                       dedup_key, status, provider_receipt_json, error, created_at,
                       updated_at, delivered_at, acknowledged_at, snoozed_until,
                       expires_at, payload_json
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["delivery_id"],
                    payload["automation_id"],
                    payload.get("execution_id"),
                    payload["user_id"],
                    payload["channel"],
                    payload["dedup_key"],
                    payload["status"],
                    _json(payload.get("provider_receipt") or {}),
                    payload.get("error"),
                    payload["created_at"],
                    payload["updated_at"],
                    payload.get("delivered_at"),
                    payload.get("acknowledged_at"),
                    payload.get("snoozed_until"),
                    payload.get("expires_at"),
                    _json(body),
                ),
            )
            conn.commit()

    def update_delivery(self, delivery_id: str, **updates: Any) -> dict[str, Any]:
        direct = {key: value for key, value in updates.items() if key in {"status", "updated_at", "acknowledged_at", "snoozed_until", "expires_at"}}
        with self._connect() as conn:
            row = conn.execute("select payload_json from agent_notification_deliveries where delivery_id=?", (delivery_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown delivery: {delivery_id}")
            payload = _load(row[0])
            if "provider_receipt_json" in updates:
                payload["provider_receipt"] = updates["provider_receipt_json"]
            if "error" in updates:
                payload["error"] = updates["error"]
            if "delivered_at" in updates:
                payload["delivered_at"] = updates["delivered_at"]
            assignments = [*(f"{key}=?" for key in direct), "payload_json=?"]
            conn.execute(
                f"update agent_notification_deliveries set {', '.join(assignments)} where delivery_id=?",
                (*direct.values(), _json(payload), delivery_id),
            )
            conn.commit()
        return self.get_delivery(delivery_id) or {}

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_notification_deliveries where delivery_id=?", (delivery_id,)).fetchone()
        return _delivery_row(row) if row else None

    def latest_delivery(self, *, automation_id: str, dedup_key: str | None = None, channel: str | None = None) -> dict[str, Any] | None:
        clauses, params = ["automation_id=?"], [automation_id]
        if dedup_key is not None:
            clauses.append("dedup_key=?")
            params.append(dedup_key)
        if channel is not None:
            clauses.append("channel=?")
            params.append(channel)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"select * from agent_notification_deliveries where {' and '.join(clauses)} order by rowid desc limit 1",
                tuple(params),
            ).fetchone()
        return _delivery_row(row) if row else None

    def list_deliveries(self, automation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_notification_deliveries where automation_id=? order by rowid",
                (automation_id,),
            ).fetchall()
        return [_delivery_row(row) for row in rows]

    def _update_automation_payload(self, automation_id: str, updates: Mapping[str, Any], *, now: datetime | None) -> None:
        with self._connect() as conn:
            payload = self._automation_payload(conn, automation_id)
            payload.update(updates)
            conn.execute(
                "update agent_automations set payload_json=?, updated_at=? where automation_id=?",
                (_json(payload), _iso(now or utc_now()), automation_id),
            )
            conn.commit()

    @staticmethod
    def _automation_payload(conn: sqlite3.Connection, automation_id: str) -> dict[str, Any]:
        row = conn.execute("select payload_json from agent_automations where automation_id=?", (automation_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown automation: {automation_id}")
        return _load(row[0])

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists agent_automation_intents (
                    intent_id text primary key, session_id text, run_id text,
                    branch_id text, user_id text not null, goal text not null,
                    symbol text, kind text not null, status text not null,
                    semantic_fingerprint text not null, intent_json text not null,
                    created_at text not null, updated_at text, payload_json text not null default '{}'
                );
                create table if not exists agent_automations (
                    automation_id text primary key, intent_id text not null,
                    session_id text, user_id text not null, goal text not null,
                    symbol text, kind text not null, state text not null,
                    status text, backend text, backend_reference text,
                    semantic_fingerprint text not null, current_version integer not null default 0,
                    current_decision_json text, notification_state_json text not null default '{}',
                    created_at text not null, updated_at text not null, expires_at text,
                    payload_json text not null default '{}'
                );
                create index if not exists idx_agent_automations_active_fingerprint
                    on agent_automations(user_id, semantic_fingerprint, state);
                create table if not exists agent_automation_versions (
                    automation_version_id text, automation_id text not null,
                    version integer not null, status text not null default 'created',
                    intent_json text not null, compiled_json text not null,
                    artifact_json text not null, created_at text not null,
                    payload_json text not null default '{}', unique(automation_id, version)
                );
                create table if not exists agent_automation_executions (
                    execution_id text primary key, automation_id text not null,
                    automation_version integer, run_id text, stage text not null,
                    status text not null, input_json text not null default '{}',
                    output_json text not null default '{}', meaningful_change integer,
                    decision_changed integer, created_at text not null, started_at text,
                    completed_at text, payload_json text not null default '{}'
                    , source_event_id text
                );
                create table if not exists agent_notification_deliveries (
                    delivery_id text primary key, automation_id text, execution_id text,
                    user_id text not null, channel text not null, dedup_key text not null,
                    status text not null, provider_receipt_json text not null default '{}',
                    error text, created_at text not null, updated_at text not null,
                    delivered_at text, acknowledged_at text, snoozed_until text,
                    expires_at text, payload_json text not null default '{}'
                );
                create table if not exists agent_automation_reanalysis_budget (
                    execution_id text primary key, automation_id text not null,
                    budget_day text not null, reserved_at text not null
                );
                create index if not exists idx_agent_automation_reanalysis_budget_day
                    on agent_automation_reanalysis_budget(automation_id, budget_day);
                create index if not exists idx_agent_notification_dedup
                    on agent_notification_deliveries(automation_id, dedup_key, created_at);
                create table if not exists agent_automation_submissions (
                    submission_id text primary key, automation_id text not null,
                    automation_version integer not null, backend text not null,
                    operation text not null, idempotency_key text not null,
                    status text not null, request_json text not null,
                    response_json text not null default '{}', backend_reference text,
                    created_at text not null, updated_at text not null,
                    unique(backend, idempotency_key)
                );
                create index if not exists idx_agent_automation_submissions_pending
                    on agent_automation_submissions(status, backend, updated_at);
                create table if not exists agent_automation_schedules (
                    schedule_id text primary key, automation_id text not null,
                    automation_version integer not null, status text not null,
                    trigger_json text not null, definition_json text not null,
                    idempotency_key text not null unique,
                    registration_receipt_id text not null,
                    created_at text not null, updated_at text not null,
                    last_triggered_at text, last_error text,
                    next_due_at text, last_scheduled_for text,
                    misfire_policy text not null default 'run_once',
                    payload_json text not null default '{}'
                );
                create index if not exists idx_agent_automation_schedules_status
                    on agent_automation_schedules(status, updated_at);
                create table if not exists agent_automation_schedule_receipts (
                    receipt_id text primary key, schedule_id text not null,
                    automation_id text not null, receipt_type text not null,
                    status text not null, request_json text not null default '{}',
                    response_json text not null default '{}', error text,
                    created_at text not null, completed_at text,
                    occurrence_key text
                );
                create index if not exists idx_agent_automation_schedule_receipts
                    on agent_automation_schedule_receipts(schedule_id, created_at);
                create table if not exists agent_automation_callback_receipts (
                    receipt_id text primary key, source text not null,
                    authenticated integer not null check(authenticated in (0, 1)),
                    event_type text not null, automation_id text, automation_version integer,
                    submission_id text, source_event_id text, callback_timestamp integer not null,
                    verified_at integer not null, body_sha256 text not null, nonce_sha256 text not null,
                    signature_sha256 text not null, token_sha256 text not null,
                    payload_json text not null default '{}', outcomes_json text not null default '[]',
                    receipt_sha256 text not null unique, created_at text not null,
                    unique(source, nonce_sha256)
                );
                create index if not exists idx_agent_automation_callback_receipts_link
                    on agent_automation_callback_receipts(automation_id, submission_id, created_at);
                create trigger if not exists agent_automation_callback_receipts_immutable_update
                    before update on agent_automation_callback_receipts
                    begin select raise(abort, 'automation callback receipts are immutable'); end;
                create trigger if not exists agent_automation_callback_receipts_immutable_delete
                    before delete on agent_automation_callback_receipts
                    begin select raise(abort, 'automation callback receipts are immutable'); end;
                """
            )
            _ensure_column(conn, "agent_automation_schedules", "next_due_at", "text")
            _ensure_column(conn, "agent_automation_schedules", "last_scheduled_for", "text")
            _ensure_column(
                conn,
                "agent_automation_schedules",
                "misfire_policy",
                "text not null default 'run_once'",
            )
            _ensure_column(conn, "agent_automation_schedule_receipts", "occurrence_key", "text")
            _ensure_column(conn, "agent_automation_executions", "source_event_id", "text")
            conn.execute(
                """create index if not exists idx_agent_automation_schedules_due
                       on agent_automation_schedules(status, next_due_at)"""
            )
            conn.execute(
                """create unique index if not exists idx_agent_automation_execution_source_event
                       on agent_automation_executions(automation_id, source_event_id)
                     where source_event_id is not null"""
            )
            conn.execute(
                """create unique index if not exists idx_agent_automation_receipt_occurrence
                       on agent_automation_schedule_receipts(schedule_id, occurrence_key)
                     where occurrence_key is not null"""
            )
            # Upgrading an existing database must not reset today's spend.
            conn.execute(
                """insert or ignore into agent_automation_reanalysis_budget
                       (execution_id, automation_id, budget_day, reserved_at)
                     select execution_id, automation_id, substr(coalesce(started_at, created_at), 1, 10),
                            coalesce(started_at, created_at)
                       from agent_automation_executions
                      where stage in ('agent_reanalysis', 'decision_changed_gate')"""
            )
            conn.execute(
                """update agent_automation_schedules
                      set status=(select state from agent_automations
                                   where automation_id=agent_automation_schedules.automation_id)
                    where status='active' and automation_id in
                          (select automation_id from agent_automations
                            where state in ('paused', 'archived', 'expired', 'deleted'))"""
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _automation_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _load(row["payload_json"])
    return {
        **dict(row),
        **payload,
        "state": row["state"],
        "current_decision": _load(row["current_decision_json"]) or dict(payload.get("current_decision") or {}),
        "notification_state": _load(row["notification_state_json"]) or dict(payload.get("notification_state") or {}),
    }


def _delivery_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _load(row["payload_json"])
    return {
        **dict(row),
        "user_id": row["user_id"],
        "payload": dict(payload.get("payload") or {}),
        "provider_receipt": _load(row["provider_receipt_json"]) or dict(payload.get("provider_receipt") or {}),
        "error": row["error"] or payload.get("error"),
        "delivered_at": row["delivered_at"] or payload.get("delivered_at"),
    }


def _execution_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _load(row["payload_json"])
    return {
        **dict(row),
        **payload,
        "meaningful_change": None if row["meaningful_change"] is None else bool(row["meaningful_change"]),
        "decision_changed": None if row["decision_changed"] is None else bool(row["decision_changed"]),
    }


def _submission_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "request": _load(row["request_json"]),
        "response": _load(row["response_json"]),
    }


def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "trigger": _load(row["trigger_json"]),
        "definition": _load(row["definition_json"]),
        "payload": _load(row["payload_json"]),
    }


def _schedule_receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "request": _load(row["request_json"]),
        "response": _load(row["response_json"]),
    }


def _callback_receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "authenticated": bool(row["authenticated"]),
        "payload": _load(row["payload_json"]),
        "outcomes": _load_list(row["outcomes_json"]),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _load_list(value: str | None) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, json.JSONDecodeError):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)] if isinstance(parsed, list) else []


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row[1]) for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {declaration}")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _misfire_policy(trigger: Mapping[str, Any]) -> str:
    policy = str(trigger.get("misfire_policy") or "run_once").strip().casefold()
    if policy not in {"run_once", "catch_up", "skip"}:
        raise ValueError("misfire_policy must be run_once, catch_up, or skip")
    return policy


def _initial_due(
    trigger: Mapping[str, Any],
    now: datetime,
    market_calendar: Any | None = None,
) -> datetime | None:
    current = _utc(now)
    explicit = trigger.get("run_at") or trigger.get("start_at")
    if explicit:
        return align_market_due(_parse_iso(str(explicit)), trigger, market_calendar)
    trigger_type = str(trigger.get("type") or "").casefold()
    interval = int(trigger.get("interval_seconds") or 0)
    if interval > 0:
        return align_market_due(current + timedelta(seconds=interval), trigger, market_calendar)
    if trigger_type in {"once", "one_shot"}:
        return align_market_due(current, trigger, market_calendar)
    if trigger_type in {"schedule", "recurring", "interval"}:
        frequency = str(trigger.get("frequency") or "daily").casefold()
        if frequency == "hourly":
            return align_market_due(current + timedelta(hours=1), trigger, market_calendar)
        if frequency == "weekly":
            return align_market_due(current + timedelta(days=7), trigger, market_calendar)
        return align_market_due(current + timedelta(days=1), trigger, market_calendar)
    return None


def _period(trigger: Mapping[str, Any]) -> timedelta | None:
    interval = int(trigger.get("interval_seconds") or 0)
    if interval > 0:
        return timedelta(seconds=interval)
    frequency = str(trigger.get("frequency") or "").casefold()
    if frequency == "hourly":
        return timedelta(hours=1)
    if frequency == "daily":
        return timedelta(days=1)
    if frequency == "weekly":
        return timedelta(days=7)
    return None


def _next_due(
    trigger: Mapping[str, Any],
    due: datetime,
    now: datetime,
    *,
    coalesce: bool,
    market_calendar: Any | None = None,
) -> datetime | None:
    trigger_type = str(trigger.get("type") or "").casefold()
    if trigger_type in {"once", "one_shot"}:
        if market_calendar is not None and market_calendar_name(trigger):
            return align_market_due(max(due, now), trigger, market_calendar)
        return None
    period = _period(trigger)
    if period is None:
        return None
    candidate = due + period
    if coalesce:
        while candidate <= now:
            candidate += period
    return align_market_due(candidate, trigger, market_calendar)


def _missed_occurrences(trigger: Mapping[str, Any], due: datetime, now: datetime) -> int:
    period = _period(trigger)
    if period is None or now <= due:
        return 0
    return max(0, int((now - due).total_seconds() // period.total_seconds()))
