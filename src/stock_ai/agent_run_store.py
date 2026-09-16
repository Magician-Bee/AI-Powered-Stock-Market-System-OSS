from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from open_stock_ai.agent_runtime.contracts import redact_runtime_value
from open_stock_ai.agent_runtime.completion_contract import evaluate_objective_completion
from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


TERMINAL_RUN_STATUSES = {
    "completed", "partially_completed", "max_steps_reached", "failed", "cancelled", "interrupted",
}


class AgentRunStore:
    """SQLite source of truth for durable Agent runs and their auditable events."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as conn:
            apply_migrations(conn)

    def create_run(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            idempotency_key = str(request.get("idempotency_key") or "").strip() or None
            if idempotency_key:
                existing = conn.execute(
                    "select run_id from agent_runs where idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return self.get(str(existing[0])) or {}
            conn.execute(
                """
                insert into agent_runs(
                    run_id, created_at, updated_at, status, objective, driver, autonomy,
                    symbols_json, max_steps, request_json, session_id, parent_run_id,
                    idempotency_key
                ) values (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    now,
                    str(request.get("objective") or ""),
                    request.get("driver_id"),
                    str(request.get("autonomy") or "advisory"),
                    _json(request.get("symbols") or []),
                    int(request.get("max_steps") or 6),
                    _json(request),
                    request.get("session_id"),
                    request.get("parent_run_id"),
                    idempotency_key,
                ),
            )
            conn.commit()
        return self.get_run(run_id) or {}

    def mark_running(self, run_id: str) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_runs
                   set status = 'running', started_at = coalesce(started_at, ?), updated_at = ?
                 where run_id = ? and status in ('queued', 'running')
                """,
                (now, now, run_id),
            )
            conn.commit()

    def append_event(self, run_id: str, event: dict[str, Any]) -> bool:
        event = redact_runtime_value(event)
        sequence = int(event.get("sequence") or 0)
        if sequence < 1:
            raise ValueError("Durable Agent events require a positive sequence")
        event_type = str(event.get("type") or "unknown")
        created_at = str(event.get("timestamp") or _now())
        payload_json = _json(event)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                insert or ignore into agent_events(
                    run_id, sequence, created_at, event_type, payload_json
                ) values (?, ?, ?, ?, ?)
                """,
                (run_id, sequence, created_at, event_type, payload_json),
            )
            if cursor.rowcount:
                self._project_event(conn, run_id, event, created_at)
                conn.execute(
                    "update agent_runs set updated_at = ? where run_id = ?",
                    (_now(), run_id),
                )
            conn.commit()
            return bool(cursor.rowcount)

    def complete_run(self, run_id: str, result: dict[str, Any]) -> None:
        now = _now()
        status = str(result.get("status") or "completed")
        if status not in {"completed", "partially_completed", "max_steps_reached"}:
            raise ValueError(f"Unsupported successful terminal status: {status}")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_runs
                   set status = ?, updated_at = ?, completed_at = ?, result_json = ?, error_json = null
                 where run_id = ?
                """,
                (status, now, now, _json(result), run_id),
            )
            if status == "completed":
                conn.execute(
                    """
                    update agent_steps
                       set status = 'completed', completed_at = coalesce(completed_at, ?)
                     where run_id = ? and status = 'running'
                    """,
                    (now, run_id),
                )
                conn.execute(
                    """
                    update agent_plan_steps
                       set status = 'completed', completed_at = coalesce(completed_at, ?)
                     where run_id = ? and status = 'running'
                    """,
                    (now, run_id),
                )
            else:
                reason = (
                    "Some independent branches completed while one or more local branches remain unresolved."
                    if status == "partially_completed"
                    else "Step budget exhausted before completion criteria were satisfied."
                )
                conn.execute(
                    """
                    update agent_steps
                       set status = 'blocked', completed_at = null
                     where run_id = ? and status = 'running'
                    """,
                    (run_id,),
                )
                conn.execute(
                    """
                    update agent_plan_steps
                       set status = 'blocked', completed_at = null,
                           error_summary = coalesce(error_summary, ?)
                     where run_id = ? and status = 'running'
                    """,
                    (reason, run_id),
                )
            conn.commit()

    def prepare_continuation(
        self,
        run_id: str,
        *,
        max_steps: int,
        allow_waiting_recovery: bool = False,
        driver_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-open an incomplete run without erasing its durable audit trail.

        A ``partially_completed`` Run has preserved valid work plus an
        unresolved local failure. It is a recovery checkpoint, not a terminal
        answer; otherwise the Dock's “繼續修復” action falsely offers an
        operation that the Host rejects.
        """

        # Keep the durable request ceiling aligned with the Host runtime.  A
        # continuation must not silently lose its final recovery turns after
        # the runtime has granted them.
        bounded = max(2, min(int(max_steps), 120))
        now = _now()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select status, request_json from agent_runs where run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current_status = str(row["status"])
            continuable_statuses = {"max_steps_reached", "partially_completed"}
            if allow_waiting_recovery:
                # This narrow opt-in is used only after the user selected the
                # public L8 recovery choice.  It is not a general bypass for
                # normal interaction/approval waits.
                continuable_statuses.update({"waiting_user_input", "waiting_decision"})
            if current_status not in continuable_statuses:
                raise ValueError("Only an incomplete run can continue with more steps")
            request = _decode(row["request_json"], {})
            request["max_steps"] = bounded
            selected_driver = str(driver_id or request.get("driver_id") or "").strip()
            if selected_driver:
                request["driver_id"] = selected_driver
            status_placeholders = ", ".join("?" for _ in continuable_statuses)
            conn.execute(
                f"""
                update agent_runs
                   set status='queued', updated_at=?, completed_at=null,
                       result_json=null, error_json=null, max_steps=?,
                       request_json=?, driver=?, resume_count=resume_count+1
                 where run_id=? and status in ({status_placeholders})
                """,
                (
                    now,
                    bounded,
                    _json(request),
                    selected_driver or None,
                    run_id,
                    *sorted(continuable_statuses),
                ),
            )
            conn.execute(
                """
                update agent_steps
                   set status='queued', completed_at=null
                 where run_id=? and status='blocked'
                """,
                (run_id,),
            )
            conn.execute(
                """
                update agent_plan_steps
                   set status='ready', completed_at=null, error_summary=null
                 where run_id=? and status='blocked'
                   and error_summary like 'Step budget exhausted%'
                """,
                (run_id,),
            )
            conn.commit()
        return self.get_run(run_id) or {}

    def fail_run(self, run_id: str, error: dict[str, Any]) -> None:
        self._finish_without_result(run_id, "failed", error)

    def cancel_run(self, run_id: str) -> None:
        self._finish_without_result(
            run_id,
            "cancelled",
            {"type": "CancelledError", "message": "Agent run was cancelled by request."},
        )

    def request_cancel(self, run_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_runs
                   set cancel_requested = 1, status = 'cancelling', updated_at = ?
                 where run_id = ? and status not in ('completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled', 'interrupted')
                """,
                (_now(), run_id),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def recover_interrupted(self) -> int:
        now = _now()
        error = _json(
            {
                "type": "ProcessRestarted",
                "message": "The host process stopped before this Agent run reached a terminal state; resume is available.",
            }
        )
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_runs
                   set status = 'suspended', updated_at = ?, completed_at = null, error_json = ?
                 where status in (
                     'queued', 'planning', 'running', 'waiting_dependency',
                     'repairing', 'replanning', 'cancelling'
                 )
                """,
                (now, error),
            )
            conn.commit()
            return int(cursor.rowcount)

    def reclassify_false_completed_runs(self) -> list[dict[str, Any]]:
        """Correct legacy false-complete records without discarding their audit trail.

        Older Runtime versions could mark a Run ``completed`` after an
        unrelated tool succeeded, even though its Failure Ledger still had an
        open, unrecovered local branch or the requested outcome contract was
        never satisfied. Both checks are Host-owned and idempotent.
        """

        corrected: list[dict[str, Any]] = []
        now = _now()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select run_id, session_id, result_json
                  from agent_runs
                 where status='completed'
                """
            ).fetchall()
            for row in rows:
                result = _decode(row["result_json"], {})
                if not isinstance(result, dict):
                    continue
                reconciliation = result.get("historical_reconciliation")
                if (
                    isinstance(reconciliation, dict)
                    and reconciliation.get("reason") == "verified_explicit_local_paper_order"
                    and isinstance(result.get("completion_validation"), dict)
                    and result["completion_validation"].get("passed") is True
                ):
                    # The narrow reconciliation below has already required a
                    # Host-finalized paper receipt plus a passing validator
                    # from the immutable event ledger.  Do not let the older
                    # open-failure heuristic resurrect the synthetic recovery
                    # node that reconciliation intentionally retired.
                    continue
                trace = result.get("tool_trace")
                if not isinstance(trace, list):
                    continue
                failures = conn.execute(
                    """
                    select error_id, step_id, category
                      from agent_failure_ledger
                     where run_id=? and status='open'
                    """,
                    (row["run_id"],),
                ).fetchall()
                recovered_nodes = {
                    str(link.get("failed_node_id") or "")
                    for item in trace
                    if isinstance(item, dict) and item.get("ok") is True
                    for link in (item.get("recovery_for") or [])
                    if isinstance(link, dict)
                }
                unresolved = [
                    dict(item)
                    for item in failures
                    if str(item["step_id"] or "")
                    and str(item["step_id"] or "") not in recovered_nodes
                    and any(
                        isinstance(trace_item, dict)
                        and trace_item.get("ok") is False
                        and str(trace_item.get("node_id") or "") == str(item["step_id"] or "")
                        for trace_item in trace
                    )
                ]
                objective_contract = evaluate_objective_completion(
                    objective=str(result.get("objective") or ""),
                    task_kind=str(result.get("task_kind") or "") or None,
                    observations=(item for item in trace if isinstance(item, dict)),
                    decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
                )
                goal_gaps = [
                    str(item)
                    for item in objective_contract.get("missing_requirements") or []
                    if str(item).strip()
                ]
                if not unresolved and not goal_gaps:
                    continue
                unresolved_node_ids = [str(item["step_id"]) for item in unresolved]
                legacy_summary = str(result.get("summary") or "").strip()
                corrected_summary = (
                    "系統偵測到此舊 Run 被錯誤標示為完成：Host 完成閘門仍有未滿足的目標條件。"
                    "已保留原有成功工作並改為部分完成；恢復將從安全 checkpoint 繼續。"
                )
                completion = result.get("completion_validation")
                prior_checks = list(
                    (completion if isinstance(completion, dict) else {}).get("checks") or []
                )
                contract_check = {
                    **objective_contract,
                    "passed": False,
                }
                prior_checks = [
                    item
                    for item in prior_checks
                    if not (
                        isinstance(item, dict)
                        and item.get("name") == "objective_completion_contract"
                    )
                ]
                result = {
                    **result,
                    "status": "partially_completed",
                    "summary": corrected_summary,
                    "legacy_summary": legacy_summary or None,
                    "recovery_pending": True,
                    "recovery_state": "objective_completion_gap_pending"
                    if goal_gaps
                    else "failed_branch_recovery_pending",
                    "goal_completion_gaps": goal_gaps,
                    "pending_recovery_node_ids": unresolved_node_ids,
                    "completion_validation": {
                        **(completion if isinstance(completion, dict) else {}),
                        "passed": False,
                        "historical_false_completion_corrected": True,
                        "unresolved_failed_nodes": unresolved_node_ids,
                        "checks": [*prior_checks, contract_check],
                    },
                    "historical_reclassification": {
                        "reason": (
                            "objective_completion_gap"
                            if goal_gaps
                            else "open_failure_without_explicit_recovery_link"
                        ),
                        "unresolved_failed_nodes": unresolved_node_ids,
                        "goal_completion_gaps": goal_gaps,
                        "corrected_at": now,
                    },
                }
                error = {
                    "type": "HistoricalFalseCompletion",
                    "message": "A legacy completed status conflicted with the Host objective completion contract.",
                    "unresolved_failed_nodes": unresolved_node_ids,
                    "goal_completion_gaps": goal_gaps,
                    "preserve_completed_work": True,
                }
                conn.execute(
                    """
                    update agent_runs
                       set status='partially_completed', updated_at=?, completed_at=null, result_json=?, error_json=?
                     where run_id=? and status='completed'
                    """,
                    (now, _json(result), _json(error), row["run_id"]),
                )
                # Remove only the known historical projection artifact: a
                # numeric provider turn was never a PlanGraph node. Keep every
                # real branch step and every audit event intact.
                conn.execute(
                    """
                    delete from agent_branch_steps
                     where step_id in (
                         select s.step_id
                           from agent_branch_steps s
                           join agent_branches b on b.branch_id=s.branch_id
                           join agent_task_forests f on f.forest_id=b.forest_id
                          where f.run_id=?
                            and s.position=9999
                            and json_extract(s.payload_json, '$.source_node_id') GLOB '[0-9]*'
                            and not exists (
                                select 1 from agent_plan_steps p
                                 where p.run_id=?
                                   and p.node_id=json_extract(s.payload_json, '$.source_node_id')
                            )
                     )
                    """,
                    (row["run_id"], row["run_id"]),
                )
                corrected.append(
                    {
                        "run_id": str(row["run_id"]),
                        "session_id": str(row["session_id"] or ""),
                        "unresolved_failed_nodes": unresolved_node_ids,
                        "result": result,
                    }
                )
            conn.commit()
        return corrected

    def reconcile_historical_verified_paper_completions(self) -> list[dict[str, Any]]:
        """Close only legacy paper Runs whose durable evidence already proves completion.

        A short-lived older completion path could write a valid paper-order
        receipt, Host finalisation and a passing completion validator, then
        immediately overwrite that green result with a synthetic
        ``host_completion_gate_blocked`` recovery checkpoint.  Replaying such
        a Run at every desktop launch is both incorrect and can submit work
        repeatedly.  This is deliberately narrower than general recovery:
        every required proof must already be present in the immutable event
        ledger and the execution must be local paper-only.
        """

        reconciled: list[dict[str, Any]] = []
        now = _now()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select run_id, session_id, result_json
                  from agent_runs
                 where status='partially_completed'
                """
            ).fetchall()
            for row in rows:
                result = _decode(row["result_json"], {})
                if not isinstance(result, dict):
                    continue
                if str(result.get("recovery_state") or "") != "host_completion_gate_blocked":
                    continue
                if int(result.get("paper_execution_count") or 0) < 1:
                    continue
                if int(result.get("live_execution_count") or 0) != 0:
                    continue
                completion = result.get("completion_validation")
                if not isinstance(completion, dict) or completion.get("passed") is not True:
                    continue

                events = conn.execute(
                    """
                    select event_type, payload_json
                      from agent_events
                     where run_id=?
                     order by sequence
                    """,
                    (row["run_id"],),
                ).fetchall()
                host_finalized = False
                validation_passed = False
                completed_summary: str | None = None
                for event in events:
                    payload = _decode(event["payload_json"], {})
                    payload = payload if isinstance(payload, dict) else {}
                    detail = payload.get("payload")
                    detail = detail if isinstance(detail, dict) else {}
                    event_type = str(event["event_type"] or "")
                    if (
                        event_type == "completion.host_finalized"
                        and str(detail.get("reason") or "")
                        == "verified_explicit_local_paper_order"
                    ):
                        host_finalized = True
                    elif event_type == "validation.passed":
                        validation = detail.get("validation")
                        if isinstance(validation, dict) and validation.get("passed") is True:
                            validation_passed = True
                    elif event_type == "run.completed":
                        if (
                            str(payload.get("status") or "") == "completed"
                            and int(detail.get("paper_execution_count") or 0) >= 1
                            and int(detail.get("live_execution_count") or 0) == 0
                        ):
                            completed_summary = str(
                                detail.get("summary") or payload.get("summary") or ""
                            ).strip() or None

                if not (host_finalized and validation_passed and completed_summary):
                    continue

                reconciled_completion = {
                    **completion,
                    "passed": True,
                    "historical_verified_paper_reconciliation": True,
                }
                reconciliation = result.get("historical_reconciliation")
                reconciliation = dict(reconciliation) if isinstance(reconciliation, dict) else {}
                reconciliation.update(
                    {
                        "reason": "verified_explicit_local_paper_order",
                        "reconciled_at": now,
                        "required_events": [
                            "completion.host_finalized",
                            "validation.passed",
                            "run.completed",
                        ],
                        "prior_recovery_state": "host_completion_gate_blocked",
                    }
                )
                corrected = {
                    **result,
                    "status": "completed",
                    "summary": completed_summary,
                    "recovery_pending": False,
                    "recovery_state": None,
                    "pending_recovery_node_ids": [],
                    "goal_completion_gaps": [],
                    "completion_validation": reconciled_completion,
                    "historical_reconciliation": reconciliation,
                }
                conn.execute(
                    """
                    update agent_runs
                       set status='completed', updated_at=?, completed_at=?, result_json=?, error_json=null
                     where run_id=? and status='partially_completed'
                    """,
                    (now, now, _json(corrected), row["run_id"]),
                )
                conn.execute(
                    """
                    update agent_steps
                       set status='completed', completed_at=coalesce(completed_at, ?)
                     where run_id=? and status='running'
                    """,
                    (now, row["run_id"]),
                )
                conn.execute(
                    """
                    update agent_plan_steps
                       set status='completed', completed_at=coalesce(completed_at, ?)
                     where run_id=? and status='running'
                    """,
                    (now, row["run_id"]),
                )
                reconciled.append(
                    {
                        "run_id": str(row["run_id"]),
                        "session_id": str(row["session_id"] or ""),
                        "result": corrected,
                    }
                )
            conn.commit()
        return reconciled

    def reclassify_recoverable_failed_runs(self) -> list[dict[str, Any]]:
        """Migrate legacy Host write conflicts back into the recovery ladder.

        Versions before the durable recovery boundary treated a stale PlanGraph
        revision as a terminal Run failure.  The failure is now fixed by the
        PlanManager rebase path, so those exact historical records must resume
        from their saved checkpoint rather than remain permanently red.
        """

        corrected: list[dict[str, Any]] = []
        now = _now()
        conflict_marker = "agent_plan_revisions.plan_id, agent_plan_revisions.revision"
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select run_id, session_id, current_step, result_json, error_json
                  from agent_runs
                 where status='failed'
                """
            ).fetchall()
            for row in rows:
                error = _decode(row["error_json"], {})
                message = str((error if isinstance(error, dict) else {}).get("message") or "")
                if conflict_marker not in message:
                    continue
                previous = _decode(row["result_json"], {})
                previous = previous if isinstance(previous, dict) else {}
                failure_node_id = f"host-runtime-{int(row['current_step'] or 0) + 1}"
                result = {
                    **previous,
                    "schema_version": "open_stock_ai.agent_run.v2",
                    "run_id": str(row["run_id"]),
                    "status": "partially_completed",
                    "summary": "系統已修正 PlanGraph 版本衝突；保留既有 checkpoint 並重新執行受影響的修復分支。",
                    "tool_trace": [
                        *(previous.get("tool_trace") or []),
                        {
                            "node_id": failure_node_id,
                            "tool": "host.runtime",
                            "ok": False,
                            "error": error,
                            "recovery": {
                                "action": "retry_from_durable_checkpoint",
                                "reason": "Plan revision conflict now has a durable rebase repair.",
                                "preserve_completed_work": True,
                            },
                        },
                    ],
                    "recovery_state": "legacy_plan_revision_rebase_pending",
                    "historical_reclassification": {
                        "reason": "legacy_plan_revision_integrity_conflict",
                        "corrected_at": now,
                        "failed_node_id": failure_node_id,
                    },
                }
                correction_error = {
                    "type": "HistoricalPlanRevisionConflict",
                    "message": "A legacy PlanGraph revision write conflict was converted to durable recovery.",
                    "preserve_completed_work": True,
                }
                conn.execute(
                    """
                    update agent_runs
                       set status='partially_completed', updated_at=?, completed_at=null,
                           result_json=?, error_json=?, resume_count=0
                     where run_id=? and status='failed'
                    """,
                    (now, _json(result), _json(correction_error), row["run_id"]),
                )
                corrected.append(
                    {
                        "run_id": str(row["run_id"]),
                        "session_id": str(row["session_id"] or ""),
                        "unresolved_failed_nodes": [failure_node_id],
                        "result": result,
                    }
                )
            conn.commit()
        return corrected

    def recoverable_runs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select r.*,
                       (select count(*) from agent_events e where e.run_id = r.run_id) as event_count,
                       (select coalesce(max(sequence), 0) from agent_events e where e.run_id = r.run_id) as last_sequence
                  from agent_runs r where status in (
                      'suspended', 'waiting_user_input', 'waiting_decision', 'waiting_approval'
                  )
                 order by updated_at
                """
            ).fetchall()
        return [_run_row(row) for row in rows]

    def mark_resuming(self, run_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_runs
                   set status='queued', updated_at=?, completed_at=null,
                       resume_count=resume_count+1, error_json=null
                 where run_id=? and status in (
                     'suspended', 'waiting_user_input', 'waiting_decision', 'waiting_approval', 'failed'
                 )
                """,
                (_now(), run_id),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def pause_run(self, run_id: str, *, status: str, error: dict[str, Any] | None = None) -> None:
        if status not in {
            "waiting_user_input", "waiting_decision", "waiting_approval", "suspended",
        }:
            raise ValueError(f"Unsupported resumable status: {status}")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_runs
                   set status=?, updated_at=?, completed_at=null, error_json=?
                 where run_id=?
                """,
                (status, _now(), _json(error) if error else None, run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select r.*,
                       (select count(*) from agent_events e where e.run_id = r.run_id) as event_count,
                       (select coalesce(max(sequence), 0) from agent_events e where e.run_id = r.run_id) as last_sequence
                  from agent_runs r where r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return _run_row(row) if row is not None else None

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select r.*,
                       (select count(*) from agent_events e where e.run_id = r.run_id) as event_count,
                       (select coalesce(max(sequence), 0) from agent_events e where e.run_id = r.run_id) as last_sequence
                  from agent_runs r order by created_at desc limit ?
                """,
                (bounded,),
            ).fetchall()
        return [_run_row(row) for row in rows]

    def list_runs_for_session(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return every Run belonging to a Session in stable lineage order."""

        bounded = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select r.*,
                       (select count(*) from agent_events e where e.run_id = r.run_id) as event_count,
                       (select coalesce(max(sequence), 0) from agent_events e where e.run_id = r.run_id) as last_sequence
                  from agent_runs r
                 where r.session_id=?
                 order by r.created_at asc, r.run_id asc limit ?
                """,
                (session_id, bounded),
            ).fetchall()
        return [_run_row(row) for row in rows]

    def events_after(self, run_id: str, sequence: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                select payload_json from agent_events
                 where run_id = ? and sequence > ? order by sequence asc
                """,
                (run_id, max(0, int(sequence))),
            ).fetchall()
        return [_decode(row[0], {}) for row in rows]

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            plan_row = conn.execute(
                """
                select plan_json from agent_plans
                 where run_id=? order by updated_at desc limit 1
                """,
                (run_id,),
            ).fetchone()
            current_plan = _decode(plan_row["plan_json"], {}) if plan_row else {}
            current_node_ids = {
                str(node.get("node_id") or "")
                for node in current_plan.get("nodes") or []
                if isinstance(node, dict) and str(node.get("node_id") or "")
            }
            rows = conn.execute(
                """
                select * from agent_plan_steps
                 where run_id=? order by order_index, node_id
                """,
                (run_id,),
            ).fetchall()
        if plan_row is not None:
            rows = [row for row in rows if str(row["node_id"]) in current_node_ids]
        return [
            {
                "run_id": row["run_id"],
                "node_id": row["node_id"],
                "plan_id": row["plan_id"],
                "parent_node_id": row["parent_node_id"],
                "type": row["node_type"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "order_index": int(row["order_index"]),
                "dependency_ids": _decode(row["dependency_ids_json"], []),
                "assigned_agent": row["assigned_agent"],
                "capability": row["capability"],
                "skill_ids": _decode(row["skill_ids_json"], []),
                "tool_call_ids": _decode(row["tool_call_ids_json"], []),
                "reason_summary": row["reason_summary"],
                "result_summary": row["result_summary"],
                "error_summary": row["error_summary"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]

    def tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_tool_calls
                 where run_id=? order by started_at, call_id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "tool_call_id": row["call_id"],
                "run_id": row["run_id"],
                "step_id": row["node_id"] or str(row["step"]),
                "tool_name": row["tool_name"],
                "status": row["status"],
                "arguments_redacted": _decode(row["arguments_json"], {}),
                "result_summary": _decode(row["result_json"], None),
                "error": _decode(row["error_json"], None),
                "validation": _decode(row["validation_json"], None),
                "retry_count": max(0, int(row["attempt"] or 1) - 1),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "risk_class": row["risk_class"],
                "worker_id": row["worker_id"],
            }
            for row in rows
        ]

    def environment_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                select payload_json from agent_environment_snapshots
                 where run_id=? order by created_at desc limit 1
                """,
                (run_id,),
            ).fetchone()
        return _decode(row[0], None) if row else None

    def add_control_message(
        self,
        run_id: str,
        *,
        control_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        control_id = f"ACTL-{uuid4().hex}"
        created_at = _now()
        with self._lock, self._connect() as conn:
            if conn.execute("select 1 from agent_runs where run_id=?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            conn.execute(
                """
                insert into agent_control_messages(
                    control_id, run_id, control_type, status, created_at, payload_json
                ) values (?, ?, ?, 'pending', ?, ?)
                """,
                (control_id, run_id, control_type, created_at, _json(payload)),
            )
            conn.commit()
        return {
            "control_id": control_id,
            "run_id": run_id,
            "control_type": control_type,
            "status": "pending",
            "created_at": created_at,
            "payload": payload,
        }

    def consume_control_messages(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_control_messages
                 where run_id=? and status='pending' order by created_at
                """,
                (run_id,),
            ).fetchall()
            if rows:
                conn.executemany(
                    """
                    update agent_control_messages set status='consumed', consumed_at=?
                     where control_id=? and status='pending'
                    """,
                    [(_now(), row["control_id"]) for row in rows],
                )
                conn.commit()
        return [
            {
                "control_id": row["control_id"],
                "control_type": row["control_type"],
                "created_at": row["created_at"],
                "payload": _decode(row["payload_json"], {}),
            }
            for row in rows
        ]

    def publish_runtime_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedup_key: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        encoded = _json(payload)
        payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        event_id = f"AEVT-{uuid4().hex}"
        created_at = _now()
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                insert or ignore into agent_runtime_events(
                    event_id, event_type, status, payload_json, payload_hash,
                    dedup_key, occurred_at, created_at
                ) values (?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(event_type),
                    encoded,
                    payload_hash,
                    dedup_key,
                    occurred_at or created_at,
                    created_at,
                ),
            )
            if not cursor.rowcount and dedup_key:
                row = conn.execute(
                    "select * from agent_runtime_events where dedup_key=?",
                    (dedup_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "select * from agent_runtime_events where event_id=?",
                    (event_id,),
                ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Runtime event could not be persisted")
        return _runtime_event_row(row)

    def claim_runtime_events(
        self,
        *,
        owner: str,
        at: str,
        lease_expires_at: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            rows = conn.execute(
                """
                select * from agent_runtime_events
                 where processed_at is null
                   and (
                       status='pending'
                       or (status='processing' and claim_expires_at <= ?)
                   )
                 order by created_at
                 limit ?
                """,
                (at, max(1, min(int(limit), 500))),
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            if event_ids:
                conn.executemany(
                    """
                    update agent_runtime_events
                       set status='processing', claim_owner=?, claim_expires_at=?,
                           error_json=null
                     where event_id=?
                    """,
                    [(owner, lease_expires_at, event_id) for event_id in event_ids],
                )
            conn.commit()
        return [_runtime_event_row(row) for row in rows]

    def complete_runtime_event(self, event_id: str, *, owner: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_runtime_events
                   set status='processed', processed_at=?, claim_owner=null,
                       claim_expires_at=null, error_json=null
                 where event_id=? and claim_owner=? and status='processing'
                """,
                (_now(), event_id, owner),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def release_runtime_event(
        self,
        event_id: str,
        *,
        owner: str,
        error: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_runtime_events
                   set status='pending', claim_owner=null, claim_expires_at=null,
                       error_json=?
                 where event_id=? and claim_owner=? and status='processing'
                """,
                (_json(error) if error else None, event_id, owner),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def compact_processed_runtime_events(self, *, keep_recent: int = 2_000) -> dict[str, int]:
        """Bound the durable inbox without weakening event idempotency.

        Processed events without a deduplication key are disposable queue
        records. Deduplicated events remain as small tombstones so replayed
        producer messages still resolve to the original event id, but their
        potentially large payloads no longer accumulate forever.
        """

        keep = max(0, int(keep_recent))
        empty_payload = _json({})
        empty_hash = hashlib.sha256(empty_payload.encode("utf-8")).hexdigest()
        recent_sql = """
            select event_id from agent_runtime_events
             where processed_at is not null
             order by processed_at desc, created_at desc, event_id desc
             limit ?
        """
        with self._lock, self._connect() as conn:
            conn.execute("begin immediate")
            compacted = conn.execute(
                f"""
                update agent_runtime_events
                   set payload_json=?, payload_hash=?, error_json=null
                 where processed_at is not null
                   and dedup_key is not null
                   and payload_json<>?
                   and event_id not in ({recent_sql})
                """,
                (empty_payload, empty_hash, empty_payload, keep),
            ).rowcount
            deleted = conn.execute(
                f"""
                delete from agent_runtime_events
                 where processed_at is not null
                   and dedup_key is null
                   and event_id not in ({recent_sql})
                """,
                (keep,),
            ).rowcount
            conn.commit()
        return {"compacted": int(compacted), "deleted": int(deleted), "kept_recent": keep}

    def create_schedule(self, schedule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into agent_schedules(
                    schedule_id, name, cron_expression, next_run_at, enabled, created_at,
                    updated_at, payload_json, session_id, workflow_id, trigger_type,
                    misfire_policy
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    payload["name"],
                    payload.get("cron_expression"),
                    payload.get("next_run_at"),
                    int(payload.get("enabled", True)),
                    now,
                    now,
                    _json(payload),
                    payload.get("session_id"),
                    payload.get("workflow_id"),
                    payload.get("trigger_type", "one_shot"),
                    payload.get("misfire_policy", "run_once"),
                ),
            )
            conn.commit()
        return self.get_schedule(schedule_id) or {}

    def update_schedule(self, schedule_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.get_schedule(schedule_id)
        if existing is None:
            return None
        merged = {**existing["payload"], **payload}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_schedules
                   set name=?, cron_expression=?, next_run_at=?, enabled=?, updated_at=?,
                       payload_json=?, session_id=?, workflow_id=?, trigger_type=?, misfire_policy=?
                 where schedule_id=?
                """,
                (
                    merged["name"],
                    merged.get("cron_expression"),
                    merged.get("next_run_at"),
                    int(merged.get("enabled", True)),
                    _now(),
                    _json(merged),
                    merged.get("session_id"),
                    merged.get("workflow_id"),
                    merged.get("trigger_type", "one_shot"),
                    merged.get("misfire_policy", "run_once"),
                    schedule_id,
                ),
            )
            conn.commit()
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_schedules where schedule_id = ?", (schedule_id,)).fetchone()
        return _schedule_row(row) if row else None

    def list_schedules(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_schedules order by created_at desc limit ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [_schedule_row(row) for row in rows]

    def due_schedules(self, at: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_schedules
                 where enabled = 1 and next_run_at is not null and next_run_at <= ?
                   and trigger_type in ('one_shot', 'interval', 'cron')
                 order by next_run_at
                """,
                (at,),
            ).fetchall()
        return [_schedule_row(row) for row in rows]

    def claim_due_schedules(
        self,
        at: str,
        *,
        owner: str,
        lease_expires_at: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Atomically lease due schedules so multiple API processes cannot fire them twice."""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            rows = conn.execute(
                """
                select * from agent_schedules
                 where enabled = 1 and next_run_at is not null and next_run_at <= ?
                   and trigger_type in ('one_shot', 'interval', 'cron')
                   and (claim_expires_at is null or claim_expires_at <= ?)
                 order by next_run_at
                 limit ?
                """,
                (at, at, max(1, min(int(limit), 200))),
            ).fetchall()
            schedule_ids = [str(row["schedule_id"]) for row in rows]
            if schedule_ids:
                conn.executemany(
                    """
                    update agent_schedules
                       set claim_owner=?, claim_expires_at=?, updated_at=?
                     where schedule_id=?
                    """,
                    [(owner, lease_expires_at, _now(), schedule_id) for schedule_id in schedule_ids],
                )
            conn.commit()
        return [_schedule_row(row) for row in rows]

    def claim_event_schedule(
        self,
        schedule_id: str,
        *,
        owner: str,
        at: str,
        lease_expires_at: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("begin immediate")
            cursor = conn.execute(
                """
                update agent_schedules
                   set claim_owner=?, claim_expires_at=?, updated_at=?
                 where schedule_id=? and enabled=1
                   and (claim_expires_at is null or claim_expires_at <= ?)
                """,
                (owner, lease_expires_at, _now(), schedule_id, at),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def release_schedule_claim(self, schedule_id: str, *, owner: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_schedules
                   set claim_owner=null, claim_expires_at=null, updated_at=?
                 where schedule_id=? and claim_owner=?
                """,
                (_now(), schedule_id, owner),
            )
            conn.commit()

    def schedule_fired(self, schedule_id: str, *, run_id: str, next_run_at: str | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_schedules
                   set run_id=?, next_run_at=?, enabled=?, updated_at=?, last_fired_at=?,
                       claim_owner=null, claim_expires_at=null
                 where schedule_id=?
                """,
                (run_id, next_run_at, 1 if next_run_at else 0, _now(), _now(), schedule_id),
            )
            conn.commit()

    def schedule_deferred(self, schedule_id: str, *, next_run_at: str | None) -> None:
        """Advance a claimed market-calendar occurrence without creating a Run."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_schedules
                   set next_run_at=?, enabled=?, updated_at=?,
                       claim_owner=null, claim_expires_at=null
                 where schedule_id=?
                """,
                (next_run_at, 1 if next_run_at else 0, _now(), schedule_id),
            )
            conn.commit()

    def schedule_event_fired(self, schedule_id: str, *, run_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_schedules
                   set run_id=?, updated_at=?, last_fired_at=?,
                       claim_owner=null, claim_expires_at=null
                 where schedule_id=?
                """,
                (run_id, _now(), _now(), schedule_id),
            )
            conn.commit()

    def disable_schedule(self, schedule_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_schedules
                   set enabled=0, updated_at=?, claim_owner=null, claim_expires_at=null
                 where schedule_id=? and enabled=1
                """,
                (_now(), schedule_id),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def enable_schedule(self, schedule_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "update agent_schedules set enabled=1, updated_at=? where schedule_id=? and enabled=0",
                (_now(), schedule_id),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def _finish_without_result(self, run_id: str, status: str, error: dict[str, Any]) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update agent_runs
                   set status = ?, updated_at = ?, completed_at = ?, error_json = ?
                 where run_id = ?
                """,
                (status, now, now, _json(error), run_id),
            )
            conn.execute(
                """
                update agent_steps set status = ?, completed_at = coalesce(completed_at, ?)
                 where run_id = ? and status = 'running'
                """,
                (status, now, run_id),
            )
            conn.commit()

    def _project_event(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event: dict[str, Any],
        created_at: str,
    ) -> None:
        event_type = str(event.get("type") or "")
        step = int(event.get("step") or 0)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        runtime_state = {
            "plan.proposed": "planning",
            "plan.revised": "replanning",
            "plan.replan.requested": "replanning",
            "repair.attempted": "repairing",
            "branch.waiting_dependency": "waiting_dependency",
            "model.turn.started": "running",
            "tool.started": "running",
        }.get(event_type)
        if runtime_state:
            conn.execute(
                "update agent_runs set status=?, updated_at=? where run_id=?",
                (runtime_state, created_at, run_id),
            )
        plan_snapshot = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
        if plan_snapshot and event_type in {
            "plan.proposed",
            "plan.revised",
            "plan.current",
            "plan.compiled",
        }:
            self._project_plan_steps(conn, run_id, plan_snapshot)
        if step > 0:
            conn.execute(
                "update agent_runs set current_step = max(current_step, ?) where run_id = ?",
                (step, run_id),
            )
        if event_type == "context.snapshot.created":
            conn.execute(
                "update agent_runs set environment_hash=? where run_id=?",
                (event.get("snapshot_hash"), run_id),
            )
            if event.get("snapshot_id") and isinstance(event.get("snapshot"), dict):
                conn.execute(
                    """
                    insert or ignore into agent_environment_snapshots(
                        snapshot_id, run_id, snapshot_hash, created_at, payload_json
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("snapshot_id"),
                        run_id,
                        event.get("snapshot_hash") or "",
                        created_at,
                        _json(event.get("snapshot")),
                    ),
                )
        elif event_type == "context.snapshot.expired" and event.get("snapshot_id"):
            conn.execute(
                """
                update agent_environment_snapshots set expired_at=?
                 where snapshot_id=? and run_id=?
                """,
                (created_at, event.get("snapshot_id"), run_id),
            )
        if step > 0 and event_type in {
            "agent.status",
            "agent.thinking",
            "model.turn.started",
            "model.sdk.turn.started",
        }:
            conn.execute(
                """
                insert into agent_steps(run_id, step, status, started_at, summary, payload_json)
                values (?, ?, 'running', ?, ?, ?)
                on conflict(run_id, step) do update set
                    status = 'running', summary = excluded.summary, payload_json = excluded.payload_json
                """,
                (run_id, step, created_at, event.get("label") or event.get("summary"), _json(event)),
            )
        elif step > 0 and event_type in {"agent.plan", "plan.current", "plan.revised"}:
            conn.execute(
                """
                insert into agent_steps(run_id, step, status, started_at, summary, payload_json)
                values (?, ?, 'running', ?, ?, ?)
                on conflict(run_id, step) do update set
                    summary = excluded.summary, payload_json = excluded.payload_json
                """,
                (run_id, step, created_at, event.get("summary"), _json(event)),
            )

        node_id = str(event.get("step_id") or event.get("node_id") or "")
        if node_id and event_type.startswith("step."):
            statuses = {
                "step.proposed": "proposed",
                "step.ready": "ready",
                "step.started": "running",
                "step.waiting_approval": "waiting_approval",
                "step.completed": "completed",
                "step.failed": "failed",
                "step.blocked": "blocked",
                "step.skipped": "skipped",
                "step.cancelled": "cancelled",
            }
            status = statuses.get(event_type, "pending")
            conn.execute(
                """
                insert into agent_plan_steps(
                    run_id, node_id, plan_id, parent_node_id, node_type, title,
                    status, order_index, dependency_ids_json, skill_ids_json,
                    tool_call_ids_json, assigned_agent, capability, reason_summary,
                    result_summary, error_summary, started_at, completed_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id, node_id) do update set
                    status=excluded.status,
                    parent_node_id=coalesce(excluded.parent_node_id, agent_plan_steps.parent_node_id),
                    assigned_agent=coalesce(excluded.assigned_agent, agent_plan_steps.assigned_agent),
                    result_summary=coalesce(excluded.result_summary, agent_plan_steps.result_summary),
                    error_summary=coalesce(excluded.error_summary, agent_plan_steps.error_summary),
                    started_at=coalesce(agent_plan_steps.started_at, excluded.started_at),
                    completed_at=coalesce(excluded.completed_at, agent_plan_steps.completed_at),
                    payload_json=excluded.payload_json
                """,
                (
                    run_id,
                    node_id,
                    event.get("plan_id"),
                    event.get("parent_step_id"),
                    event.get("node_type") or "reasoning",
                    event.get("title") or node_id,
                    status,
                    int(event.get("order_index") or step),
                    _json(event.get("dependency_ids") or []),
                    _json(event.get("skill_ids") or []),
                    _json(
                        [event.get("tool_call_id")]
                        if event.get("tool_call_id")
                        else []
                    ),
                    event.get("assigned_agent"),
                    event.get("capability"),
                    _summary_text(event.get("reason_summary")),
                    _summary_text(event.get("result_summary")),
                    _summary_text(event.get("error_summary")),
                    created_at if status == "running" else None,
                    created_at
                    if status in {"completed", "failed", "cancelled", "skipped"}
                    else None,
                    _json(event),
                ),
            )

        call_id = str(event.get("tool_call_id") or event.get("call_id") or "")
        if event_type in {"tool.queued", "tool.started"} and call_id:
            status = "running" if event_type == "tool.started" else "queued"
            conn.execute(
                """
                insert into agent_tool_calls(
                    run_id, step, call_id, tool_name, status, started_at, arguments_json,
                    argument_digest, risk_class, node_id
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id, call_id) do update set
                    status = excluded.status, arguments_json = excluded.arguments_json,
                    argument_digest=excluded.argument_digest, risk_class=excluded.risk_class,
                    node_id=coalesce(excluded.node_id, agent_tool_calls.node_id)
                """,
                (
                    run_id,
                    step,
                    call_id,
                    str(event.get("tool") or "unknown"),
                    status,
                    created_at,
                    _json(event.get("arguments") or {}),
                    hashlib.sha256(
                        _json(
                            {
                                "tool": event.get("tool"),
                                "arguments": event.get("arguments") or {},
                            }
                        ).encode("utf-8")
                    ).hexdigest(),
                    event.get("risk_class"),
                    event.get("step_id") or event.get("node_id"),
                ),
            )
        elif event_type in {"validation.started", "validation.passed", "validation.failed"} and call_id:
            conn.execute(
                """
                update agent_tool_calls set validation_json=?
                 where run_id=? and call_id=?
                """,
                (_json(event.get("validation") or {}), run_id, call_id),
            )
        elif event_type in {"tool.completed", "tool.failed", "tool.cancelled", "tool.retrying"} and call_id:
            status = {
                "tool.completed": "completed",
                "tool.failed": "failed",
                "tool.cancelled": "cancelled",
                "tool.retrying": "retrying",
            }[event_type]
            conn.execute(
                """
                update agent_tool_calls
                   set status = ?, completed_at = ?, result_json = ?, error_json = ?,
                       worker_id=coalesce(?, worker_id), validation_json=coalesce(?, validation_json)
                 where run_id = ? and call_id = ?
                """,
                (
                    status,
                    created_at if status in {"completed", "failed", "cancelled"} else None,
                    _json(event) if status == "completed" else None,
                    _json(event.get("error")) if status == "failed" else None,
                    event.get("worker_id"),
                    _json(event.get("validation")) if event.get("validation") else None,
                    run_id,
                    call_id,
                ),
            )

    def _project_plan_steps(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        plan: dict[str, Any],
    ) -> None:
        nodes = [
            node
            for node in (plan.get("nodes") or [])
            if isinstance(node, dict) and str(node.get("node_id") or "")
        ]
        active_node_ids = [str(node["node_id"]) for node in nodes]
        if active_node_ids:
            placeholders = ",".join("?" for _ in active_node_ids)
            conn.execute(
                f"""
                delete from agent_plan_steps
                 where run_id = ?
                   and node_id not in ({placeholders})
                """,
                (run_id, *active_node_ids),
            )
        else:
            conn.execute("delete from agent_plan_steps where run_id = ?", (run_id,))
        for index, raw_node in enumerate(nodes):
            node_id = str(raw_node.get("node_id") or "")
            conn.execute(
                """
                insert into agent_plan_steps(
                    run_id, node_id, plan_id, parent_node_id, node_type, title,
                    description, status, order_index, dependency_ids_json,
                    assigned_agent, capability, skill_ids_json, tool_call_ids_json,
                    reason_summary, result_summary, error_summary, started_at,
                    completed_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id, node_id) do update set
                    plan_id=excluded.plan_id,
                    parent_node_id=excluded.parent_node_id,
                    node_type=excluded.node_type,
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    order_index=excluded.order_index,
                    dependency_ids_json=excluded.dependency_ids_json,
                    assigned_agent=excluded.assigned_agent,
                    capability=excluded.capability,
                    skill_ids_json=excluded.skill_ids_json,
                    tool_call_ids_json=excluded.tool_call_ids_json,
                    reason_summary=excluded.reason_summary,
                    result_summary=coalesce(excluded.result_summary, agent_plan_steps.result_summary),
                    error_summary=coalesce(excluded.error_summary, agent_plan_steps.error_summary),
                    started_at=coalesce(agent_plan_steps.started_at, excluded.started_at),
                    completed_at=coalesce(excluded.completed_at, agent_plan_steps.completed_at),
                    payload_json=excluded.payload_json
                """,
                (
                    run_id,
                    node_id,
                    plan.get("plan_id"),
                    raw_node.get("parent_id") or raw_node.get("parent_node_id"),
                    raw_node.get("node_type") or raw_node.get("type") or "reasoning",
                    raw_node.get("title") or node_id,
                    raw_node.get("description"),
                    raw_node.get("status") or "pending",
                    int(raw_node.get("order_index") or index),
                    _json(raw_node.get("dependencies") or raw_node.get("dependency_ids") or []),
                    raw_node.get("assigned_agent"),
                    raw_node.get("capability") or raw_node.get("tool_name"),
                    _json(raw_node.get("skill_ids") or []),
                    _json(raw_node.get("tool_call_ids") or []),
                    raw_node.get("reason_summary"),
                    raw_node.get("result_summary"),
                    raw_node.get("error_summary"),
                    raw_node.get("started_at"),
                    raw_node.get("completed_at"),
                    _json(raw_node),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = _decode(row["result_json"], None)
    error = _decode(row["error_json"], None)
    return {
        "schema_version": "open_stock_ai.agent_run_state.v1",
        "run_id": row["run_id"],
        "session_id": row["session_id"] if "session_id" in row.keys() else None,
        "plan_id": row["plan_id"] if "plan_id" in row.keys() else None,
        "checkpoint_id": row["checkpoint_id"] if "checkpoint_id" in row.keys() else None,
        "parent_run_id": row["parent_run_id"] if "parent_run_id" in row.keys() else None,
        "resume_count": int(row["resume_count"]) if "resume_count" in row.keys() else 0,
        "environment_hash": row["environment_hash"] if "environment_hash" in row.keys() else None,
        "status": row["status"],
        "objective": row["objective"],
        "driver": row["driver"],
        "autonomy": row["autonomy"],
        "symbols": _decode(row["symbols_json"], []),
        "max_steps": row["max_steps"],
        "current_step": row["current_step"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "event_count": row["event_count"],
        "last_sequence": row["last_sequence"],
        "request": _decode(row["request_json"], {}),
        "result": result,
        "error": error,
        "terminal": row["status"] in TERMINAL_RUN_STATUSES,
    }


def _schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": "open_stock_ai.agent_schedule.v1",
        "schedule_id": row["schedule_id"],
        "run_id": row["run_id"],
        "session_id": row["session_id"] if "session_id" in row.keys() else None,
        "workflow_id": row["workflow_id"] if "workflow_id" in row.keys() else None,
        "name": row["name"],
        "trigger_type": row["trigger_type"] if "trigger_type" in row.keys() else "one_shot",
        "cron_expression": row["cron_expression"],
        "next_run_at": row["next_run_at"],
        "misfire_policy": row["misfire_policy"] if "misfire_policy" in row.keys() else "run_once",
        "last_fired_at": row["last_fired_at"] if "last_fired_at" in row.keys() else None,
        "claim_owner": row["claim_owner"] if "claim_owner" in row.keys() else None,
        "claim_expires_at": row["claim_expires_at"] if "claim_expires_at" in row.keys() else None,
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "payload": _decode(row["payload_json"], {}),
    }


def _runtime_event_row(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if not isinstance(row, sqlite3.Row):
        raise TypeError("Runtime event rows require sqlite3.Row")
    return {
        "schema_version": "open_stock_ai.runtime_event.v1",
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "status": row["status"],
        "payload": _decode(row["payload_json"], {}),
        "payload_hash": row["payload_hash"],
        "dedup_key": row["dedup_key"],
        "occurred_at": row["occurred_at"],
        "created_at": row["created_at"],
        "claim_owner": row["claim_owner"],
        "claim_expires_at": row["claim_expires_at"],
        "processed_at": row["processed_at"],
        "error": _decode(row["error_json"], None),
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _summary_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else _json(value)


def _decode(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
