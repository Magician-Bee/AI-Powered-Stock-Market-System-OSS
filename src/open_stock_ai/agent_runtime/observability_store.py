from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import (
    ManagedSQLiteConnection,
    apply_migrations,
    configure_connection,
)

from .observability import (
    KPI_WINDOWS,
    ToolResultProvenance,
    classify_failure,
    resolve_kpi_window,
)
from .repair.contracts import canonical_hash


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class DurableRuntimeObservability:
    """Persist provenance, repair history, evidence and KPI facts from Host events."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def project(self, run_id: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        payload = dict(event.get("payload") or {})
        timestamp = str(event.get("timestamp") or _now())
        event_id = str(event.get("event_id") or _stable_id("EV", run_id, str(event.get("sequence") or 0)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            run = conn.execute(
                "select session_id, driver from agent_runs where run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                return
            session_id = str(run["session_id"] or "")
            model_provider = str(run["driver"] or "host")
            branch_id = str(event.get("branch_id") or "") or None
            step_id = str(
                event.get("node_id")
                or event.get("step_id")
                or payload.get("node_id")
                or payload.get("step_id")
                or ""
            ) or None
            if branch_id is None and step_id:
                branch = conn.execute(
                    """
                    select s.branch_id from agent_branch_steps s
                    join agent_branches b on b.branch_id=s.branch_id
                    join agent_task_forests f on f.forest_id=b.forest_id
                    where f.run_id=? and json_extract(s.payload_json, '$.source_node_id')=?
                    order by s.created_at desc limit 1
                    """,
                    (run_id, step_id),
                ).fetchone()
                branch_id = str(branch[0]) if branch else None

            if event_type in {"tool.completed", "tool.failed"}:
                provenance = self._tool_provenance(
                    conn,
                    run_id=run_id,
                    event=event,
                    model_provider=model_provider,
                    success=event_type == "tool.completed",
                )
                payload["provenance"] = provenance.to_dict()
                event["payload"] = payload
                event["provenance"] = provenance.to_dict()
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "tool_call",
                    1.0,
                    timestamp,
                    {"success": provenance.success, "tool": provenance.tool},
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "tool_latency_ms",
                    provenance.latency_ms,
                    timestamp,
                    {"tool": provenance.tool},
                )
                if not provenance.success:
                    self._kpi(conn, event_id, session_id, run_id, "tool_failure", 1.0, timestamp, {})

            if event_type == "model.provider.completed":
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "model_call",
                    1.0,
                    timestamp,
                    {"success": True, "provider": payload.get("provider")},
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "model_latency_ms",
                    max(0.0, float(payload.get("duration_ms") or 0.0)),
                    timestamp,
                    {"provider": payload.get("provider"), "model": payload.get("model")},
                )

            if event_type == "model.provider.failed":
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "model_call",
                    1.0,
                    timestamp,
                    {"success": False, "provider": payload.get("provider")},
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "model_failure",
                    1.0,
                    timestamp,
                    {"provider": payload.get("provider"), "model": payload.get("model")},
                )

            if event_type == "error.receipt.created":
                self._persist_failure(
                    conn,
                    session_id=session_id,
                    run_id=run_id,
                    branch_id=branch_id,
                    step_id=step_id,
                    payload=payload,
                    timestamp=timestamp,
                )
            elif event_type == "repair.attempted":
                self._persist_repair(conn, payload, timestamp)
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "repair_attempt",
                    1.0,
                    timestamp,
                    {"strategy": payload.get("strategy"), "identical_retry_blocked": payload.get("identical_retry_blocked")},
                )
                if payload.get("identical_retry_blocked"):
                    self._kpi(
                        conn,
                        event_id,
                        session_id,
                        run_id,
                        "identical_retry_blocked",
                        1.0,
                        timestamp,
                        {},
                    )

            if event_type == "interaction.requested":
                interaction_id = str(payload.get("interaction_id") or _stable_id("INT", event_id))
                waiting_state = str(payload.get("waiting_state") or "waiting_user_input")
                conn.execute(
                    """
                    insert or ignore into agent_decision_checkpoints(
                        interaction_id, session_id, run_id, branch_id, status,
                        interaction_type, created_at, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        interaction_id,
                        session_id,
                        run_id,
                        branch_id,
                        waiting_state,
                        waiting_state.removeprefix("waiting_"),
                        timestamp,
                        _json(payload),
                    ),
                )
                questions = payload.get("questions")
                question_count = (
                    len(questions)
                    if isinstance(questions, list)
                    else int(bool(str(payload.get("question") or "").strip()))
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "interaction_checkpoint",
                    1.0,
                    timestamp,
                    {"question_count": question_count, "waiting_state": waiting_state},
                )
                if question_count:
                    self._kpi(
                        conn,
                        event_id,
                        session_id,
                        run_id,
                        "interaction_question",
                        float(question_count),
                        timestamp,
                        {"waiting_state": waiting_state},
                    )
                if question_count > 3:
                    self._kpi(
                        conn,
                        event_id,
                        session_id,
                        run_id,
                        "question_budget_breach",
                        1.0,
                        timestamp,
                        {"question_count": question_count},
                    )

            # Validation hashes and generic tool receipts prove execution, but
            # they are not semantic research evidence.  Only an explicit
            # evidence event may enter the durable Evidence Graph.
            if (
                event_type == "research.evidence_added"
                or event_type.startswith("evidence.")
                or (event_type == "tool.completed" and isinstance(payload.get("evidence"), dict))
            ):
                self._persist_evidence(
                    conn,
                    session_id=session_id,
                    run_id=run_id,
                    branch_id=branch_id,
                    payload=payload,
                    event_type=event_type,
                    timestamp=timestamp,
                )

            if event_type in {"branch.completed", "branch.failed", "result.final", "run.completed", "run.failed"}:
                metric = event_type.replace(".", "_")
                self._kpi(conn, event_id, session_id, run_id, metric, 1.0, timestamp, payload)
            if event_type in {
                "tool.failed",
                "model.provider.failed",
                "error.receipt.created",
                "branch.failed",
                "run.failed",
            }:
                category = classify_failure(event_type, payload)
                failure_payload = {
                    "category": category,
                    "event_type": event_type,
                    "branch_id": branch_id,
                    "step_id": step_id,
                }
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "failure_event",
                    1.0,
                    timestamp,
                    failure_payload,
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    f"failure_category:{category}",
                    1.0,
                    timestamp,
                    failure_payload,
                )
            if event_type == "interaction.responded":
                self._kpi(conn, event_id, session_id, run_id, "user_intervention", 1.0, timestamp, payload)
            if event_type == "memory.retrieved":
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "context_retrieval_returned",
                    float(payload.get("returned_count") or 0),
                    timestamp,
                    payload,
                )
                self._kpi(
                    conn,
                    event_id,
                    session_id,
                    run_id,
                    "context_retrieval_relevant",
                    float(payload.get("relevant_count") or 0),
                    timestamp,
                    payload,
                )

            if event_type in {"tool.completed", "step.completed"} and (branch_id or step_id):
                repaired = conn.execute(
                    """
                    select error_id from agent_failure_ledger
                     where run_id=? and status='open'
                       and (branch_id=? or step_id=?)
                    """,
                    (run_id, branch_id, step_id),
                ).fetchall()
                for failure in repaired:
                    conn.execute(
                        "update agent_failure_ledger set status='resolved', resolved_at=? where error_id=?",
                        (timestamp, failure[0]),
                    )
                    conn.execute(
                        """
                        update agent_repair_attempts
                           set status='completed', completed_at=?
                         where error_id=? and status='attempted'
                        """,
                        (timestamp, failure[0]),
                    )
                    self._kpi(conn, f"{event_id}:{failure[0]}", session_id, run_id, "repair_success", 1.0, timestamp, {})
                    self._kpi(conn, f"{event_id}:{failure[0]}", session_id, run_id, "branch_recovered", 1.0, timestamp, {})

            tokens = self._tokens(payload)
            if tokens:
                self._consume_tokens(conn, session_id, run_id, branch_id, tokens, timestamp)
                self._kpi(conn, event_id, session_id, run_id, "token_usage", float(tokens), timestamp, {})
            conn.commit()

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_evidence where run_id=? order by observed_at, evidence_id",
                (run_id,),
            ).fetchall()
        return [{**dict(row), **_decode(row["payload_json"], {})} for row in rows]

    def kpis(
        self,
        *,
        run_id: str | None = None,
        window: str = "all",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        window_start, window_end = resolve_kpi_window(window, now=now)
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("run_id=?")
            parameters.append(run_id)
        if window_start is not None:
            clauses.extend(("julianday(created_at)>=julianday(?)", "julianday(created_at)<=julianday(?)"))
            parameters.extend((window_start.isoformat(), window_end.isoformat()))
        where = f" where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select run_id, metric, value, created_at, payload_json "
                f"from agent_kpi_events{where} order by julianday(created_at), kpi_event_id",
                tuple(parameters),
            ).fetchall()
        totals: dict[str, float] = {}
        metric_event_counts: dict[str, int] = {}
        failure_classification: dict[str, float] = {}
        terminal_runs: dict[str, str] = {}
        intervention_runs: set[str] = set()
        for row in rows:
            metric = str(row["metric"])
            value = float(row["value"])
            if metric.startswith("failure_category:"):
                category = metric.partition(":")[2] or "unknown"
                failure_classification[category] = failure_classification.get(category, 0.0) + value
            else:
                totals[metric] = totals.get(metric, 0.0) + value
            metric_event_counts[metric] = metric_event_counts.get(metric, 0) + 1
            row_run_id = str(row["run_id"] or "")
            if row_run_id and metric in {"result_final", "run_completed", "run_failed"}:
                terminal_runs[row_run_id] = "failed" if metric == "run_failed" else "completed"
            if row_run_id and metric == "user_intervention":
                intervention_runs.add(row_run_id)
        tool_calls = totals.get("tool_call", 0.0)
        model_latency_samples = float(metric_event_counts.get("model_latency_ms", 0))
        model_calls = max(
            totals.get("model_call", 0.0),
            model_latency_samples + totals.get("model_failure", 0.0),
        )
        repairs = totals.get("repair_attempt", 0.0)
        tasks = float(len(terminal_runs))
        tasks_completed = float(sum(status == "completed" for status in terminal_runs.values()))
        execution_attempts = tool_calls + model_calls
        execution_failures = totals.get("tool_failure", 0.0) + totals.get("model_failure", 0.0)
        ledger_clauses: list[str] = ["branch_id is not null"]
        ledger_parameters: list[Any] = []
        if run_id:
            ledger_clauses.append("run_id=?")
            ledger_parameters.append(run_id)
        if window_start is not None:
            ledger_clauses.extend(("julianday(created_at)>=julianday(?)", "julianday(created_at)<=julianday(?)"))
            ledger_parameters.extend((window_start.isoformat(), window_end.isoformat()))
        with self._connect() as conn:
            evidence_clauses: list[str] = []
            evidence_parameters: list[Any] = []
            if run_id:
                evidence_clauses.append("run_id=?")
                evidence_parameters.append(run_id)
            if window_start is not None:
                evidence_clauses.extend(("julianday(observed_at)>=julianday(?)", "julianday(observed_at)<=julianday(?)"))
                evidence_parameters.extend((window_start.isoformat(), window_end.isoformat()))
            evidence_where = f" where {' and '.join(evidence_clauses)}" if evidence_clauses else ""
            source_diversity = conn.execute(
                f"select count(distinct source_type) from agent_evidence{evidence_where}",
                tuple(evidence_parameters),
            ).fetchone()[0]
            ledger_row = conn.execute(
                "select count(*), sum(case when status='resolved' then 1 else 0 end) "
                f"from agent_failure_ledger where {' and '.join(ledger_clauses)}",
                tuple(ledger_parameters),
            ).fetchone()
        branch_recovery_samples = float(ledger_row[0] or 0)
        branch_recoveries = float(ledger_row[1] or 0)
        if not branch_recovery_samples:
            branch_recoveries = totals.get("branch_recovered", 0.0)
            branch_recovery_samples = branch_recoveries + totals.get("branch_recovery_failed", 0.0)
        notifications = totals.get("automation_notification", 0.0)
        automations = totals.get("automation_activation", 0.0)
        evidence_samples = totals.get("evidence_count", 0.0)
        corrections = totals.get("user_correction", 0.0)
        context_items = totals.get("context_retrieval_returned", 0.0)
        checkpoints = totals.get("interaction_checkpoint", 0.0)
        samples = {
            "tasks": tasks,
            "tool_calls": tool_calls,
            "model_calls": model_calls,
            "execution_attempts": execution_attempts,
            "repairs": repairs,
            "branch_recoveries": branch_recovery_samples,
            "notifications": notifications,
            "automations": automations,
            "evidence": evidence_samples,
            "corrections": corrections,
            "context_items": context_items,
            "interaction_checkpoints": checkpoints,
            "failure_events": totals.get("failure_event", 0.0),
        }
        metric_samples = {
            "task_completion_rate": tasks,
            "repair_success_rate": repairs,
            "repeated_failure_rate": repairs,
            "error_rate": execution_attempts,
            "tool_error_rate": tool_calls,
            "branch_recovery_rate": branch_recovery_samples,
            "user_intervention_rate": tasks,
            "false_automation_notification_rate": notifications,
            "average_tool_calls": tasks,
            "average_model_latency_ms": model_latency_samples,
            "average_token_cost": tasks,
            "evidence_freshness": evidence_samples,
            "user_correction_incorporation_rate": corrections,
            "automation_duplicate_rate": automations,
            "session_context_retrieval_precision": context_items,
            "average_questions_per_checkpoint": checkpoints,
            "question_budget_breach_rate": checkpoints,
        }
        rate = lambda numerator, denominator: numerator / denominator if denominator else 0.0
        return {
            "schema_version": "open_stock_ai.runtime_kpis.v1",
            **totals,
            "window": window,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window != "all" else None,
            "task_completion_rate": rate(tasks_completed, tasks),
            "repair_success_rate": rate(totals.get("repair_success", 0.0), repairs),
            "error_rate": rate(execution_failures, execution_attempts),
            "tool_error_rate": rate(totals.get("tool_failure", 0.0), tool_calls),
            "repeated_failure_rate": rate(totals.get("identical_retry_blocked", 0.0), repairs),
            "branch_recovery_rate": rate(branch_recoveries, branch_recovery_samples),
            "user_intervention_rate": rate(float(len(intervention_runs)), tasks),
            "false_automation_notification_rate": rate(
                totals.get("false_automation_notification", 0.0), notifications
            ),
            "average_tool_calls": rate(tool_calls, tasks),
            "average_tool_latency_ms": rate(totals.get("tool_latency_ms", 0.0), tool_calls),
            "average_model_latency_ms": rate(
                totals.get("model_latency_ms", 0.0), model_latency_samples
            ),
            "average_token_cost": rate(totals.get("token_cost", 0.0), tasks),
            "research_source_diversity": int(source_diversity),
            "evidence_freshness": rate(totals.get("evidence_freshness", 0.0), evidence_samples),
            "user_correction_incorporation_rate": rate(
                totals.get("user_correction_incorporated", 0.0), corrections
            ),
            "automation_duplicate_rate": rate(totals.get("automation_duplicate", 0.0), automations),
            "session_context_retrieval_precision": rate(
                totals.get("context_retrieval_relevant", 0.0), context_items
            ),
            "average_questions_per_checkpoint": rate(
                totals.get("interaction_question", 0.0), checkpoints
            ),
            "question_budget_breach_rate": rate(
                totals.get("question_budget_breach", 0.0), checkpoints
            ),
            "failure_classification": {
                key: int(value) if value.is_integer() else value
                for key, value in sorted(failure_classification.items())
            },
            "sample_counts": {
                key: int(value) if float(value).is_integer() else value
                for key, value in samples.items()
            },
            "metric_samples": {
                key: int(value) if float(value).is_integer() else value
                for key, value in metric_samples.items()
            },
            "metric_status": {
                key: "ok" if value > 0 else "no_data"
                for key, value in metric_samples.items()
            },
            "event_sample_counts": dict(sorted(metric_event_counts.items())),
        }

    def dashboard(self, *, now: datetime | str | None = None) -> dict[str, Any]:
        """Return the operator-facing, durable P70 summary without raw secrets."""

        windows = {name: self.kpis(window=name, now=now) for name in KPI_WINDOWS}
        metrics = windows["all"]
        active_run_statuses = (
            "queued", "planning", "running", "suspended", "waiting_user_input",
            "waiting_decision", "waiting_approval", "repairing", "replanning", "paused",
        )
        active_branch_statuses = ("ready", "running", "waiting", "blocked", "repairing", "paused")
        with self._connect() as conn:
            active_sessions = conn.execute(
                "select count(distinct session_id) from agent_runs where status in ({})".format(
                    ",".join("?" for _ in active_run_statuses)
                ),
                active_run_statuses,
            ).fetchone()[0]
            active_branches = conn.execute(
                """
                select count(*)
                  from agent_branches b
                  join agent_task_forests f on f.forest_id=b.forest_id
                  join agent_runs r on r.run_id=f.run_id
                 where b.status in ({branch_statuses})
                   and r.status in ({run_statuses})
                """.format(
                    branch_statuses=",".join("?" for _ in active_branch_statuses),
                    run_statuses=",".join("?" for _ in active_run_statuses),
                ),
                (*active_branch_statuses, *active_run_statuses),
            ).fetchone()[0]
            automation_count = conn.execute(
                "select count(*) from agent_automations where state='active'"
            ).fetchone()[0]
            notification_count = conn.execute(
                "select count(*) from agent_notification_deliveries where status='delivered'"
            ).fetchone()[0]
        return {
            "schema_version": "open_stock_ai.observability_dashboard.v1",
            "active_sessions": int(active_sessions or 0),
            "active_branches": int(active_branches or 0),
            "tool_latency_ms": float(metrics.get("average_tool_latency_ms") or 0.0),
            "model_latency_ms": float(metrics.get("average_model_latency_ms") or 0.0),
            "error_rate": float(metrics.get("error_rate") or 0.0),
            "repair_success_rate": float(metrics.get("repair_success_rate") or 0.0),
            "repeated_failure_rate": float(metrics.get("repeated_failure_rate") or 0.0),
            "token_usage": float(metrics.get("token_usage") or 0.0),
            "web_research_cost": float(metrics.get("web_research_cost") or 0.0),
            "automation_count": int(automation_count or 0),
            "notification_count": int(notification_count or 0),
            "false_automation_notification_rate": float(
                metrics.get("false_automation_notification_rate") or 0.0
            ),
            # Keep P105 release KPIs first-class in the operator projection.
            # The nested ``kpis`` object remains the complete durable record,
            # while these fields make the release-critical metrics available to
            # the Dock without UI-side knowledge of storage internals.
            "task_completion_rate": float(metrics.get("task_completion_rate") or 0.0),
            "branch_recovery_rate": float(metrics.get("branch_recovery_rate") or 0.0),
            "user_intervention_rate": float(metrics.get("user_intervention_rate") or 0.0),
            "average_tool_calls": float(metrics.get("average_tool_calls") or 0.0),
            "average_token_cost": float(metrics.get("average_token_cost") or 0.0),
            "research_source_diversity": int(metrics.get("research_source_diversity") or 0),
            "evidence_freshness": float(metrics.get("evidence_freshness") or 0.0),
            "user_correction_incorporation_rate": float(
                metrics.get("user_correction_incorporation_rate") or 0.0
            ),
            "automation_duplicate_rate": float(metrics.get("automation_duplicate_rate") or 0.0),
            "session_context_retrieval_precision": float(
                metrics.get("session_context_retrieval_precision") or 0.0
            ),
            "average_questions_per_checkpoint": float(
                metrics.get("average_questions_per_checkpoint") or 0.0
            ),
            "question_budget_breach_rate": float(
                metrics.get("question_budget_breach_rate") or 0.0
            ),
            "failure_classification": metrics.get("failure_classification") or {},
            "sample_counts": metrics.get("sample_counts") or {},
            "metric_samples": metrics.get("metric_samples") or {},
            "metric_status": metrics.get("metric_status") or {},
            "available_windows": list(KPI_WINDOWS),
            "default_window": "30d",
            "windows": windows,
            "kpis": metrics,
        }

    def record_metric(
        self,
        *,
        session_id: str,
        metric: str,
        value: float = 1.0,
        run_id: str | None = None,
        event_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        timestamp = _now()
        with self._connect() as conn:
            resolved_run_id = run_id
            if not resolved_run_id:
                row = conn.execute(
                    "select run_id from agent_runs where session_id=? order by created_at desc limit 1",
                    (session_id,),
                ).fetchone()
                resolved_run_id = str(row[0]) if row else None
            if not resolved_run_id:
                return
            self._kpi(
                conn,
                event_id or _stable_id("MET", session_id, metric, timestamp),
                session_id,
                resolved_run_id,
                metric,
                float(value),
                timestamp,
                dict(payload or {}),
            )
            conn.commit()

    def _tool_provenance(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        event: dict[str, Any],
        model_provider: str,
        success: bool,
    ) -> ToolResultProvenance:
        payload = dict(event.get("payload") or {})
        call_id = str(event.get("tool_call_id") or payload.get("call_id") or "")
        started = conn.execute(
            """
            select created_at from agent_events
             where run_id=? and event_type='tool.started'
               and (json_extract(payload_json, '$.tool_call_id')=?
                    or json_extract(payload_json, '$.payload.call_id')=?)
             order by sequence desc limit 1
            """,
            (run_id, call_id, call_id),
        ).fetchone()
        start_time = _parse_time(str(started[0])) if started else None
        end_time = _parse_time(str(event.get("timestamp") or ""))
        latency = max(0.0, (end_time - start_time).total_seconds() * 1000) if start_time and end_time else 0.0
        tool = str(payload.get("tool") or event.get("tool") or "unknown_tool")
        started_payload: dict[str, Any] = {}
        if started:
            started_event = conn.execute(
                """
                select payload_json from agent_events
                 where run_id=? and event_type='tool.started'
                   and (json_extract(payload_json, '$.tool_call_id')=?
                        or json_extract(payload_json, '$.payload.call_id')=?)
                 order by sequence desc limit 1
                """,
                (run_id, call_id, call_id),
            ).fetchone()
            started_payload = _decode(str(started_event[0]), {}) if started_event else {}
            if isinstance(started_payload.get("payload"), dict):
                started_payload = dict(started_payload["payload"])
        tool_provider = str(
            payload.get("tool_provider")
            or started_payload.get("tool_provider")
            or "host"
        )
        source_url = str(payload.get("source_url") or "") or None
        source = str(
            payload.get("source")
            or source_url
            or f"{tool_provider}:{tool}"
        )
        return ToolResultProvenance.create(
            tool=tool,
            provider=tool_provider,
            model_provider=model_provider,
            source=source,
            latency_ms=latency,
            success=success,
            freshness=str(payload.get("freshness") or "unknown"),
            request_id=call_id or str(event.get("event_id") or ""),
            timestamp=str(event.get("timestamp") or _now()),
            worker_id=str(payload.get("worker_id") or "") or None,
            source_url=source_url,
            published_at=str(payload.get("published_at") or "") or None,
            observed_at=str(payload.get("observed_at") or event.get("timestamp") or _now()),
        )

    def _persist_failure(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str,
        branch_id: str | None,
        step_id: str | None,
        payload: dict[str, Any],
        timestamp: str,
    ) -> None:
        receipt = dict(payload.get("error_receipt") or {})
        fingerprint = dict(payload.get("failure_fingerprint") or {})
        digest = str(fingerprint.get("digest") or canonical_hash(fingerprint))
        conn.execute(
            """
            insert into agent_failure_fingerprints(
                fingerprint, first_seen_at, last_seen_at, occurrence_count,
                identical_retry_count, payload_json
            ) values (?, ?, ?, 1, 0, ?)
            on conflict(fingerprint) do update set
                last_seen_at=excluded.last_seen_at,
                occurrence_count=agent_failure_fingerprints.occurrence_count+1,
                payload_json=excluded.payload_json
            """,
            (digest, timestamp, timestamp, _json(fingerprint)),
        )
        error_id = str(receipt.get("error_id") or _stable_id("ERR", run_id, digest, timestamp))
        conn.execute(
            """
            insert or ignore into agent_failure_ledger(
                error_id, session_id, run_id, branch_id, step_id, fingerprint,
                category, status, created_at, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                error_id,
                session_id,
                run_id,
                branch_id,
                step_id,
                digest,
                str(receipt.get("category") or "unknown"),
                timestamp,
                _json(receipt),
            ),
        )

    def _persist_repair(self, conn: sqlite3.Connection, payload: dict[str, Any], timestamp: str) -> None:
        error_id = str(payload.get("error_id") or "")
        if not error_id:
            return
        repair_id = _stable_id("RPA", error_id, str(payload.get("level") or 0), timestamp)
        conn.execute(
            """
            insert or ignore into agent_repair_attempts(
                repair_id, error_id, level, strategy, status, output_hash,
                arguments_hash, started_at, payload_json
            ) values (?, ?, ?, ?, 'attempted', ?, ?, ?, ?)
            """,
            (
                repair_id,
                error_id,
                int(payload.get("level") or 0),
                str(payload.get("strategy") or "unknown"),
                canonical_hash(payload.get("output")),
                canonical_hash(payload.get("arguments")),
                timestamp,
                _json(payload),
            ),
        )
        if payload.get("identical_retry_blocked"):
            conn.execute(
                "update agent_failure_fingerprints set identical_retry_count=identical_retry_count+1 where fingerprint=?",
                (str(payload.get("fingerprint") or ""),),
            )

    def _persist_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str,
        branch_id: str | None,
        payload: dict[str, Any],
        event_type: str,
        timestamp: str,
    ) -> None:
        explicit = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        evidence_ids = [str(item) for item in payload.get("evidence_ids") or [] if str(item)]
        if explicit:
            evidence_ids.insert(0, str(explicit.get("evidence_id") or _stable_id("EVD", run_id, _json(explicit))))
        for evidence_id in dict.fromkeys(evidence_ids):
            claim = str(
                explicit.get("claim")
                or payload.get("result_summary")
                or payload.get("summary")
                or event_type
            )
            observed_at = str(explicit.get("observed_at") or timestamp)
            conn.execute(
                """
                insert into agent_evidence(
                    evidence_id, session_id, run_id, branch_id, claim,
                    source_type, observed_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(evidence_id) do update set
                    claim=excluded.claim,
                    source_type=excluded.source_type,
                    observed_at=excluded.observed_at,
                    payload_json=excluded.payload_json
                """,
                (
                    evidence_id,
                    session_id,
                    run_id,
                    branch_id,
                    claim,
                    str(explicit.get("source_type") or payload.get("tool") or event_type),
                    observed_at,
                    _json({**explicit, "provenance": payload.get("provenance"), "event_type": event_type}),
                ),
            )
            metric_exists = conn.execute(
                "select 1 from agent_kpi_events where kpi_event_id=?",
                (_stable_id("KPI", evidence_id, "evidence_count"),),
            ).fetchone()
            if not metric_exists:
                provenance = explicit.get("provenance") or payload.get("provenance") or {}
                if not isinstance(provenance, dict):
                    provenance = {}
                published_at = str(
                    explicit.get("published_at")
                    or provenance.get("published_at")
                    or ""
                ) or None
                freshness_basis = published_at or observed_at
                observed = _parse_time(freshness_basis)
                recorded = _parse_time(timestamp)
                age_seconds = max(0.0, (recorded - observed).total_seconds()) if observed and recorded else 0.0
                freshness = (
                    1.0
                    if age_seconds <= 86_400
                    else 0.75
                    if age_seconds <= 604_800
                    else 0.5
                    if age_seconds <= 2_592_000
                    else 0.25
                )
                evidence_metric_payload = {
                    "evidence_id": evidence_id,
                    "source_type": str(explicit.get("source_type") or payload.get("tool") or event_type),
                    "freshness_basis": "published_at" if published_at else "observed_at",
                }
                self._kpi(
                    conn,
                    evidence_id,
                    session_id,
                    run_id,
                    "evidence_count",
                    1.0,
                    timestamp,
                    evidence_metric_payload,
                )
                self._kpi(
                    conn,
                    evidence_id,
                    session_id,
                    run_id,
                    "evidence_freshness",
                    freshness,
                    timestamp,
                    {
                        **evidence_metric_payload,
                        "observed_at": observed_at,
                        "published_at": published_at,
                        "age_seconds": age_seconds,
                    },
                )
            for relation, targets in (
                ("supports", explicit.get("supports") or []),
                ("contradicts", explicit.get("contradicts") or []),
            ):
                for target in targets:
                    target_id = str(target or "").strip()
                    if not target_id:
                        continue
                    conn.execute(
                        """
                        insert or ignore into agent_evidence_edges(
                            edge_id, from_evidence_id, to_evidence_id,
                            relation, claim_key, payload_json
                        ) values (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _stable_id("EGE", evidence_id, relation, target_id),
                            evidence_id,
                            target_id if conn.execute(
                                "select 1 from agent_evidence where evidence_id=?",
                                (target_id,),
                            ).fetchone() else None,
                            relation,
                            target_id,
                            _json({"target_id": target_id}),
                        ),
                    )

    @staticmethod
    def _tokens(payload: dict[str, Any]) -> int:
        usage = payload.get("usage") or payload.get("token_usage") or {}
        if not isinstance(usage, dict):
            return 0
        return max(0, int(usage.get("total_tokens") or usage.get("total") or 0))

    def _consume_tokens(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        run_id: str,
        branch_id: str | None,
        tokens: int,
        timestamp: str,
    ) -> None:
        for scope, identifier in (("session", session_id), ("run", run_id), ("branch", branch_id)):
            if not identifier:
                continue
            budget_id = _stable_id("BUD", scope, str(identifier))
            allocated = 64000 if scope == "session" else 16000
            conn.execute(
                """
                insert into agent_token_budgets(
                    budget_id, session_id, run_id, branch_id, scope,
                    allocated, consumed, updated_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, '{}')
                on conflict(budget_id) do update set
                    consumed=min(agent_token_budgets.allocated, agent_token_budgets.consumed+excluded.consumed),
                    updated_at=excluded.updated_at
                """,
                (
                    budget_id,
                    session_id,
                    run_id if scope != "session" else None,
                    branch_id if scope == "branch" else None,
                    scope,
                    allocated,
                    tokens,
                    timestamp,
                ),
            )

    @staticmethod
    def _kpi(
        conn: sqlite3.Connection,
        event_id: str,
        session_id: str,
        run_id: str,
        metric: str,
        value: float,
        timestamp: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            insert or ignore into agent_kpi_events(
                kpi_event_id, session_id, run_id, metric, value, created_at, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (_stable_id("KPI", event_id, metric), session_id, run_id, metric, value, timestamp, _json(payload)),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn
