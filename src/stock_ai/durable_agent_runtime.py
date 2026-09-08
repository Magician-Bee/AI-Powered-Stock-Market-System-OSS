from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from open_stock_ai.agent_runtime import AgentOrchestrator
from open_stock_ai.agent_runtime.approval_manager import ApprovalManager
from open_stock_ai.agent_runtime.artifact_store import ArtifactStore
from open_stock_ai.agent_runtime.checkpoint_manager import CheckpointManager
from open_stock_ai.agent_runtime.completion_contract import evaluate_objective_completion
from open_stock_ai.agent_runtime.contracts import AgentRunContext, build_runtime_event, redact_runtime_value
from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from open_stock_ai.agent_runtime.interaction import (
    ArbitrationDecision,
    ProposalArbitrator,
    UserInputKind,
)
from open_stock_ai.agent_runtime.memory import MemoryCandidate
from open_stock_ai.agent_runtime.plan_manager import PlanManager
from open_stock_ai.agent_runtime.plan_graph import PlanGraph
from open_stock_ai.agent_runtime.workflow_runtime import WorkflowRuntime
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.session_archival import (
    build_session_archive,
    materialize_session_archive,
    restore_session_archive as restore_verified_session_archive,
)
from open_stock_ai.agent_runtime.session.title_generator import (
    SessionTitleGenerator,
    SessionTitleHistory,
    fallback_title,
)
from open_stock_ai.agent_runtime.workflow_store import WorkflowStore
from open_stock_ai.agent_runtime.workers import WorkerSupervisor
from open_stock_ai.agent_runtime.scheduler import SchedulePlanner, condition_matches, event_digest
from open_stock_ai.agent_runtime.scheduler.market_schedule import is_market_closed
from open_stock_ai.agent_runtime.slo import DurableSLOStore, SLOObservation, SLORegistry

from .agent_run_store import AgentRunStore, TERMINAL_RUN_STATUSES
from .market_calendar import default_taiwan_market_calendar


# This is a resource budget (P62), not a proxy for whether recovery has
# exhausted its P40 strategies.  The latter is only asserted by a durable
# ``recovery_state`` written by the orchestrator after it has recorded the
# applicable local strategy attempts.
_AUTONOMOUS_RECOVERY_STEP_CEILING = 120


def _is_verified_explicit_local_paper_completion(result: dict[str, Any]) -> bool:
    """Recognize the bounded paper-order completion contract across recovery."""

    if str(result.get("autonomy") or "") not in {"paper_execute", "full_execute"}:
        return False
    if int(result.get("live_execution_count") or 0) != 0:
        return False
    trace = [item for item in result.get("tool_trace") or [] if isinstance(item, dict)]
    completion = evaluate_objective_completion(
        objective=str(result.get("objective") or ""),
        task_kind=str(result.get("task_kind") or ""),
        observations=trace,
        decision=None,
    )
    return bool(
        completion.get("passed") is True
        and completion.get("requirements", {}).get("paper_order_requested") is True
    )


def _filter_post_answer_proposals(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep a bounded, self-contained objective from reopening itself.

    A completed Run may expose optional next-step cards, but an objective that
    explicitly says it needs no further user input has already defined its
    terminal boundary. Letting a provider add a reflection follow-up after
    that point makes the UI look as though the same goal has paused again.
    The Host therefore drops every post-answer proposal for that Run. An
    explicit no-automation instruction independently drops only automation
    proposals, while preserving ordinary follow-ups when the user did not
    request a self-contained result.
    """

    objective = str(result.get("objective") or "").casefold()
    proposals = [
        dict(item)
        for item in result.get("interaction_proposals") or []
        if isinstance(item, dict)
    ]
    if not proposals:
        return [], []
    no_follow_up = any(
        phrase in objective
        for phrase in (
            "不得等待我提供額外條件",
            "不要等待我提供額外條件",
            "不必等待我提供額外條件",
            "不要等待額外條件",
            "無需等待額外條件",
            "do not wait for additional input",
            "do not wait for further input",
            "no further input required",
        )
    )
    no_automation = any(
        phrase in objective
        for phrase in (
            "不得建立自動化",
            "不要建立自動化",
            "不建立自動化",
            "不要自動化",
            "do not create automation",
            "no automation",
        )
    )
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for proposal in proposals:
        action = str(proposal.get("action") or "").strip().casefold()
        if no_follow_up or (no_automation and action == "draft_automation"):
            suppressed.append(proposal)
        else:
            kept.append(proposal)
    return kept, suppressed


class DurableAgentRuntime:
    """Own Agent tasks independently of any HTTP request or UI connection."""

    def __init__(
        self,
        *,
        service_provider: Callable[[], AgentOrchestrator],
        store: AgentRunStore,
        session_store: AgentSessionStore | None = None,
        plan_manager: PlanManager | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        approval_manager: ApprovalManager | None = None,
        artifact_store: ArtifactStore | None = None,
        workflow_store: WorkflowStore | None = None,
        worker_supervisor: WorkerSupervisor | None = None,
        final_runtime: FinalAgentRuntime | None = None,
        slo_store: DurableSLOStore | None = None,
        slo_registry: SLORegistry | None = None,
        namespace: str = "stock-ai",
    ) -> None:
        self._service_provider = service_provider
        self.store = store
        # A durable run always belongs to a durable session.  Keep this
        # invariant even for programmatic callers that only pass a run store;
        # otherwise Plan/Checkpoint foreign keys can be written without their
        # owning Session and recovery becomes impossible.
        self.session_store = session_store or AgentSessionStore(store.path)
        self.plan_manager = plan_manager
        self.checkpoint_manager = checkpoint_manager
        self.approval_manager = approval_manager
        self.artifact_store = artifact_store
        self.workflow_store = workflow_store
        self.worker_supervisor = worker_supervisor
        self.final_runtime = final_runtime or FinalAgentRuntime(store.path)
        # The Agent runtime is a real producer of SLO evidence, not merely a
        # dashboard consumer. Keep observations in the authoritative runtime
        # SQLite database so a desktop reconnect or process restart cannot
        # reset the measured latency/success history.
        self.slo_store = slo_store or DurableSLOStore(store.path)
        self.slo_registry = slo_registry or SLORegistry()
        # This Runtime owns the asynchronous provider callback used by durable
        # Automations.  Registration may therefore become active even though
        # the controller's legacy synchronous callback is intentionally absent.
        self.final_runtime.automations.enable_async_reanalysis()
        self.schedule_planner = SchedulePlanner(default_taiwan_market_calendar())
        self.namespace = namespace
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_reasons: dict[str, str] = {}
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = {}
        self._subscriber_lock = asyncio.Lock()
        self._started = False
        self._scheduler_task: asyncio.Task[None] | None = None
        self._automation_poller_task: asyncio.Task[Any] | None = None
        self._next_automation_submission_recovery_at: datetime | None = None
        self._next_runtime_event_compaction_at: datetime | None = None
        self._closing = False
        self._runtime_id = f"agent-runtime-{uuid4().hex}"
        self._pause_requested: set[str] = set()
        self._event_locks: dict[str, asyncio.Lock] = {}
        # Only these Runs were newly corrected during this Runtime boot.  Do
        # not reinterpret every historical partial result as permission to
        # replay old work whenever the desktop app is opened.
        self._startup_reclassified_run_ids: set[str] = set()

    def slo_dashboard(self) -> dict[str, Any]:
        """Return hash-verified SLO reports without manufacturing absent data."""

        # Reports are immutable evidence. Re-evaluating on every Dock poll
        # would create a new no-data report timestamp without a new runtime
        # observation, bloating the authoritative database and falsely
        # suggesting fresh measurement. Bootstrap each missing target exactly
        # once so the Dock can state ``no_data`` rather than silently hiding
        # an unwired service.
        reported = {str(item.get("service") or "") for item in self.slo_store.reports()}
        for service in self.slo_registry.services():
            if service not in reported:
                self.slo_store.evaluate_service_and_record(self.slo_registry, service)
        return self.slo_store.dashboard()

    def _record_agent_slo(
        self,
        *,
        run_id: str,
        started_monotonic: float,
        status: str,
        observation_id: str,
    ) -> None:
        """Persist one terminal observation for one durable execution attempt."""

        latency_ms = max(0.0, (time.monotonic() - started_monotonic) * 1000.0)
        self.record_slo_observation(
            "agent.run",
            latency_ms=latency_ms,
            success=status == "completed",
            observation_id=observation_id,
        )

    def record_slo_observation(
        self,
        service: str,
        *,
        latency_ms: float,
        success: bool,
        data_at: datetime | None = None,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one real runtime observation and its service-only report.

        This is deliberately a narrow Host boundary: callers must supply a
        measured duration and, for data freshness, the source timestamp.  It
        never fabricates a green result for another service.
        """

        normalized = str(service).strip()
        observed_at = datetime.now(timezone.utc)
        identity = self.slo_store.record_observation(
            normalized,
            SLOObservation(
                observed_at=observed_at,
                latency_ms=max(0.0, float(latency_ms)),
                success=bool(success),
                data_at=data_at,
            ),
            observation_id=observation_id,
        )
        report = self.slo_store.evaluate_service_and_record(
            self.slo_registry,
            normalized,
            evaluated_at=observed_at,
        )
        return {
            "observation_id": identity,
            "service": normalized,
            "status": report.status,
            "report_sha256": report.report_sha256,
        }

    def start(self) -> dict[str, Any]:
        interrupted = 0
        reclassified: list[dict[str, Any]] = []
        reconciled_paper_runs: list[dict[str, Any]] = []
        if not self._started:
            interrupted = self.store.recover_interrupted()
            reconciled_paper_runs = self.store.reconcile_historical_verified_paper_completions()
            reclassified = [
                *self.store.reclassify_false_completed_runs(),
                *self.store.reclassify_recoverable_failed_runs(),
            ]
            self._startup_reclassified_run_ids = {
                str(item["run_id"])
                for item in reclassified
                if str(item.get("run_id") or "")
            }
            self._started = True
        for corrected in reclassified:
            run_id = str(corrected["run_id"])
            snapshot = self.store.get_run(run_id)
            if snapshot is None:
                continue
            self.final_runtime.set_run_status(
                run_id,
                "partially_completed",
                result=dict(corrected["result"]),
            )
            event = build_runtime_event(
                "run.reclassified",
                sequence=int(snapshot.get("last_sequence") or 0) + 1,
                run_id=run_id,
                session_id=str(snapshot.get("session_id") or run_id),
                payload={
                    "status": "partially_completed",
                    "summary": "偵測到舊版假完成；已保留成功工作並重新排入局部恢復。",
                    "unresolved_failed_nodes": corrected["unresolved_failed_nodes"],
                    "preserve_completed_work": True,
                },
            )
            self.final_runtime.project_runtime_event(run_id, event)
            self.store.append_event(run_id, event)
        for reconciled in reconciled_paper_runs:
            run_id = str(reconciled["run_id"])
            self.final_runtime.set_run_status(
                run_id,
                "completed",
                result=dict(reconciled["result"]),
            )
        return {
            "schema_version": "open_stock_ai.durable_agent_runtime.v1",
            "started": True,
            "recovered_as_interrupted": interrupted,
            "reclassified_false_completions": len(reclassified),
            "reconciled_verified_paper_completions": len(reconciled_paper_runs),
            "active_run_count": len(self._tasks),
            "storage": str(self.store.path),
        }

    async def start_background(self) -> None:
        self.start()
        self._closing = False
        # Desktop maintenance must not wake providers through recovery or a
        # due schedule. Preserve durable schedules and checkpoints for restart.
        if os.environ.get("STOCK_AI_AGENT_BACKGROUND_PAUSED") == "1":
            return
        if self.worker_supervisor is not None:
            await self.worker_supervisor.start()
        self.final_runtime.automations.recover_pending_submissions()
        for run in self.store.recoverable_runs():
            # A crash/safe shutdown may resume from its durable checkpoint, but
            # an explicit user pause is itself durable state.  Treating every
            # ``suspended`` run as interrupted made a desktop restart silently
            # override the user's control decision.
            error = run.get("error") if isinstance(run.get("error"), dict) else {}
            if run["status"] == "suspended" and error.get("type") != "UserPaused":
                await self.resume(run["run_id"])
        # Incomplete recovery checkpoints are not ordinary terminal results.
        # Restore their bounded self-repair loop after a desktop restart before
        # exposing an L8 interaction.  A user-retained partial result is an
        # explicit control decision and must remain untouched.
        for run in self.store.list_runs(limit=200):
            run_id = str(run.get("run_id") or "")
            if not run_id or run_id not in self._startup_reclassified_run_ids:
                continue
            if self._should_auto_continue_recovery(run_id):
                # The recovery pass is deliberate work, not a background
                # housekeeping detail.  Persist its root Session selection
                # before the task is scheduled so a freshly opened desktop
                # Dock restores this Task Forest rather than an old empty
                # Session from local UI storage.
                self._preserve_root_session_focus(run)
                self._schedule_recovery_followup(
                    run_id,
                    self._continue_recovery_automatically(run_id),
                    name=f"agent-startup-recovery:{run_id}",
                )
            elif self._should_publish_nonblocking_recovery_boundary(run_id):
                self._preserve_root_session_focus(run)
                self._schedule_recovery_followup(
                    run_id,
                    self._publish_nonblocking_recovery_boundary(run_id),
                    name=f"agent-startup-recovery-boundary:{run_id}",
                )
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="agent-scheduler")

    def create_schedule(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("autonomy") or "advisory") != "advisory":
            raise ValueError("Scheduled Agent runs are advisory-only")
        schedule_spec = self.schedule_planner.prepare(payload)
        trigger_type = str(schedule_spec["trigger_type"])
        dedup_key = str(payload.get("dedup_key") or "").strip()
        if dedup_key:
            for schedule in self.list_schedules(limit=200):
                if schedule["enabled"] and str(schedule["payload"].get("dedup_key") or "") == dedup_key:
                    return schedule
        schedule_id = f"AS-{uuid4().hex}"
        service = self._service_provider()
        driver_id = str(payload.get("driver_id") or getattr(service, "default_driver", "codex"))
        drivers = getattr(service, "drivers", None)
        if isinstance(drivers, dict) and driver_id not in drivers:
            raise ValueError(f"Unknown Agent driver: {driver_id}")
        return self.store.create_schedule(
            schedule_id,
            {
                **payload,
                "driver_id": driver_id,
                "autonomy": "advisory",
                "trigger_type": trigger_type,
                "next_run_at": schedule_spec.get("next_run_at"),
                "misfire_policy": schedule_spec["misfire_policy"],
                "enabled": True,
            },
        )

    def list_schedules(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_schedules(limit=limit)

    def disable_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        if self.store.get_schedule(schedule_id) is None:
            return None
        self.store.disable_schedule(schedule_id)
        return self.store.get_schedule(schedule_id)

    def resume_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            return None
        payload = dict(schedule["payload"])
        if payload.get("trigger_type") == "cron":
            payload["next_run_at"] = self.schedule_planner.next_cron(
                str(payload["cron_expression"]),
                datetime.now(timezone.utc),
            ).isoformat()
            payload["enabled"] = True
            return self.store.update_schedule(schedule_id, payload)
        self.store.enable_schedule(schedule_id)
        return self.store.get_schedule(schedule_id)

    def update_schedule(self, schedule_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.store.get_schedule(schedule_id)
        if existing is None:
            return None
        merged = {**existing["payload"], **payload}
        merged["autonomy"] = "advisory"
        merged["driver_id"] = str(
            merged.get("driver_id")
            or getattr(self._service_provider(), "default_driver", "codex")
        )
        drivers = getattr(self._service_provider(), "drivers", None)
        if isinstance(drivers, dict) and merged["driver_id"] not in drivers:
            raise ValueError(f"Unknown Agent driver: {merged['driver_id']}")
        trigger_type = str(merged.get("trigger_type") or "one_shot")
        if trigger_type == "cron" and merged.get("cron_expression"):
            merged["next_run_at"] = self.schedule_planner.next_cron(
                str(merged["cron_expression"]),
                datetime.now(timezone.utc),
            ).isoformat()
        elif merged.get("next_run_at"):
            merged["next_run_at"] = _utc_time(str(merged["next_run_at"]))
        return self.store.update_schedule(schedule_id, merged)

    async def trigger_schedule_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        fired = []
        for schedule in self.list_schedules(limit=200):
            spec = schedule["payload"]
            if not schedule["enabled"] or schedule["trigger_type"] not in {"event", "condition"}:
                continue
            if schedule["trigger_type"] == "event" and spec.get("event_type") != event_type:
                continue
            if schedule["trigger_type"] == "condition" and not condition_matches(
                payload,
                spec.get("condition") or {},
            ):
                continue
            now = datetime.now(timezone.utc)
            if not self.store.claim_event_schedule(
                schedule["schedule_id"],
                owner=self._runtime_id,
                at=now.isoformat(),
                lease_expires_at=(now + timedelta(seconds=30)).isoformat(),
            ):
                continue
            try:
                if spec.get("workflow_id"):
                    run = await self.run_workflow(
                        str(spec["workflow_id"]),
                        objective=str(spec.get("objective") or "") or None,
                        driver_id=str(spec.get("driver_id") or "") or None,
                        autonomy="advisory",
                        session_id=spec.get("session_id"),
                        max_steps=int(spec.get("max_steps") or 6),
                        idempotency_key=f"schedule:{schedule['schedule_id']}:{event_type}:{event_digest(payload)}",
                        schedule_id=schedule["schedule_id"],
                    )
                else:
                    run = await self.create_run(
                        objective=str(spec["objective"]),
                        symbols=spec.get("symbols") or [],
                        driver_id=str(spec.get("driver_id") or "") or None,
                        autonomy="advisory",
                        max_steps=int(spec.get("max_steps") or 6),
                        session_id=spec.get("session_id"),
                        idempotency_key=f"schedule:{schedule['schedule_id']}:{event_type}:{event_digest(payload)}",
                        schedule_id=schedule["schedule_id"],
                    )
                self.store.schedule_event_fired(schedule["schedule_id"], run_id=run["run_id"])
                fired.append(run)
            except BaseException:
                self.store.release_schedule_claim(
                    schedule["schedule_id"],
                    owner=self._runtime_id,
                )
                raise
        # Product Automations use a separate durable schedule registry from
        # generic Agent schedules.  Route the same external/event-engine input
        # through their filter → reanalysis → decision-change pipeline too.
        # The method intentionally returns the legacy Run list so existing API
        # clients remain compatible; automation receipts are persisted and are
        # available from the Automation resource.
        await self.trigger_automation_event(event_type, payload)
        return fired

    async def trigger_automation_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Dispatch one external/runtime event to matching active Automations.

        Reanalysis happens as a real same-Session durable Run.  It is not a
        scheduler-side heuristic: completion, evidence and notification state
        are all persisted by the ordinary Agent Runtime and Automation Store.
        """

        safe_payload = redact_runtime_value(payload)
        callback_payload = (
            _n8n_wake_payload(safe_payload)
            if event_type == "automation.n8n.trigger"
            else dict(safe_payload)
        )
        event = {"event_type": event_type, **callback_payload}
        targeted_automation_id = str(callback_payload.get("automation_id") or "").strip()
        targeted_n8n_callback = (
            event_type == "automation.n8n.trigger"
            and str(callback_payload.get("source") or "") == "n8n"
            and bool(targeted_automation_id)
        )
        if targeted_n8n_callback and str(callback_payload.get("market_calendar") or "").strip():
            if is_market_closed(
                datetime.now(timezone.utc),
                {"market_calendar": callback_payload.get("market_calendar")},
                self.schedule_planner.market_calendar,
            ):
                return [
                    {
                        "schedule_id": callback_payload.get("submission_id"),
                        "automation_id": targeted_automation_id,
                        "status": "deferred_market_closed",
                    }
                ]
        outcomes: list[dict[str, Any]] = []
        for schedule in self.final_runtime.automation_store.list_schedules():
            if str(schedule.get("status") or "") != "active":
                continue
            schedule_automation_id = str(schedule.get("automation_id") or "")
            if targeted_n8n_callback and schedule_automation_id != targeted_automation_id:
                continue
            if not targeted_n8n_callback and not _automation_schedule_matches(
                dict(schedule.get("trigger") or {}), event_type, callback_payload
            ):
                continue
            try:
                outcome = await self.final_runtime.automations.handle_scheduler_callback_async(
                    str(schedule["schedule_id"]),
                    event,
                    reanalyze=self._automation_reanalyze,
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "schedule_id": schedule.get("schedule_id"),
                        "automation_id": schedule.get("automation_id"),
                        "status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
            else:
                outcomes.append(
                    {
                        "schedule_id": schedule.get("schedule_id"),
                        "automation_id": outcome.automation_id,
                        "status": "completed" if outcome.condition_candidate else "filtered",
                        "execution_id": outcome.execution_id,
                        "reanalyzed": outcome.reanalyzed,
                        "meaningful_change": outcome.meaningful_change,
                        "notified": outcome.notified,
                    }
                )
        if targeted_n8n_callback and not outcomes:
            submission_id = str(callback_payload.get("submission_id") or "").strip()
            submission = (
                self.final_runtime.automation_store.get_submission(
                    submission_id=submission_id
                )
                if submission_id
                else None
            )
            compiled = dict(((submission or {}).get("request") or {}).get("compiled") or {})
            expected_digest = str(compiled.get("digest") or "")
            callback_digest = str(callback_payload.get("compiler_digest") or "")
            valid_submission = bool(
                submission
                and str(submission.get("automation_id") or "") == targeted_automation_id
                and str(submission.get("status") or "") == "active"
                and str(compiled.get("backend") or "") == "n8n"
                and expected_digest
                and callback_digest == expected_digest
            )
            if not valid_submission:
                return []
            try:
                outcome = await self.final_runtime.automations.handle_trigger_async(
                    targeted_automation_id,
                    event,
                    reanalyze=self._automation_reanalyze,
                    # The n8n graph is only the durable clock/executor. The
                    # Host owns observation and decision logic, so the wake-up
                    # envelope intentionally has no market-price field.
                    cheap_filter=lambda _event, _logic: True,
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "schedule_id": submission_id,
                        "automation_id": targeted_automation_id,
                        "status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
            else:
                outcomes.append(
                    {
                        "schedule_id": submission_id,
                        "automation_id": outcome.automation_id,
                        "status": "completed" if outcome.condition_candidate else "filtered",
                        "execution_id": outcome.execution_id,
                        "reanalyzed": outcome.reanalyzed,
                        "meaningful_change": outcome.meaningful_change,
                        "notified": outcome.notified,
                    }
                )
        return outcomes

    def record_verified_automation_callback(
        self,
        *,
        authentication: Mapping[str, Any],
        event_type: str,
        payload: Mapping[str, Any],
        outcomes: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist the Host-side receipt after an authenticated n8n dispatch."""

        return self.final_runtime.automation_store.record_verified_callback(
            authentication=authentication,
            event_type=event_type,
            payload=payload,
            outcomes=outcomes,
        )

    async def _automation_reanalyze(
        self,
        intent: Any,
        event: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Wake the selected provider in the Automation's original Session."""

        if os.environ.get("STOCK_AI_AGENT_BACKGROUND_PAUSED") == "1":
            raise RuntimeError("agent_background_paused_for_maintenance")
        session_id = str(getattr(intent, "session_id", "") or "").strip()
        if not session_id:
            raise ValueError("Automation reanalysis requires an owning session_id")
        goal = str(getattr(intent, "goal", "")).strip()
        symbol = str(getattr(intent, "symbol", "") or "").strip()
        bounded_event = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)[:4000]
        previous_text = (
            json.dumps(previous, ensure_ascii=False, sort_keys=True, default=str)[:2000]
            if previous else "無先前決策。"
        )
        child = await self.create_run(
            objective=(
                f"Automation reanalysis for: {goal}\n"
                f"Trigger event: {bounded_event}\n"
                f"Previous decision: {previous_text}\n"
                "Collect only the evidence needed to determine whether the strategy has materially changed. "
                "Return an advisory decision; do not trade or send external messages."
            ),
            symbols=[symbol] if symbol else [],
            autonomy="advisory",
            max_steps=6,
            session_id=session_id,
            run_metadata={
                "automation_trigger_id": str(event.get("event_id") or event.get("source_event_id") or ""),
                "source": "automation_reanalysis",
            },
        )
        result = await self.wait(str(child["run_id"]))
        decision = result.get("decision")
        return {
            "schema_version": "open_stock_ai.automation_reanalysis.v1",
            "conclusion": str(result.get("summary") or "Automation reanalysis completed."),
            "recommendation": decision if isinstance(decision, str) else None,
            "confidence": _automation_confidence(result),
            "evidence_ids": [
                str(item.get("evidence_id") or item.get("event_id") or "")
                for item in (self.snapshot(str(child["run_id"])) or {}).get("evidence", [])
                if str(item.get("evidence_id") or item.get("event_id") or "")
            ],
            "reanalyzed_run_id": str(child["run_id"]),
        }

    async def create_run(
        self,
        *,
        objective: str,
        symbols: list[str] | tuple[str, ...] | None = None,
        driver_id: str | None = None,
        autonomy: str = "advisory",
        max_steps: int = 6,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        idempotency_key: str | None = None,
        initial_plan: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        schedule_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.start()
        service = self._service_provider()
        selected_driver = driver_id or getattr(service, "default_driver", "codex")
        drivers = getattr(service, "drivers", None)
        if isinstance(drivers, dict) and selected_driver not in drivers:
            raise ValueError(f"Unknown Agent driver: {selected_driver}")
        run_id = f"AR-{uuid4().hex}"
        active_session_id = session_id or f"AS-{uuid4().hex}"
        if self.session_store is not None:
            if session_id:
                self.session_store.ensure(
                    active_session_id,
                    namespace=self.namespace,
                )
            else:
                self.session_store.create(
                    session_id=active_session_id,
                    namespace=self.namespace,
                    title=objective.strip()[:120] or "Stock AI Agent",
                    metadata={"created_by": "agent_run"},
                )
        request = {
            "objective": objective.strip(),
            "symbols": list(symbols or []),
            "driver_id": selected_driver,
            "autonomy": autonomy,
            "max_steps": int(max_steps),
            "session_id": active_session_id,
            "parent_run_id": parent_run_id,
            "idempotency_key": idempotency_key,
            "initial_plan": initial_plan,
            "workflow_id": workflow_id,
            "schedule_id": schedule_id,
            "metadata": dict(run_metadata or {}),
        }
        created = self.store.create_run(run_id, request)
        if created.get("run_id") != run_id:
            return created
        # Persist an instantiated workflow PlanGraph before scheduling work so
        # a crash between queueing and the first provider turn is recoverable.
        if initial_plan is not None and self.plan_manager is not None:
            self.plan_manager.create(
                session_id=active_session_id,
                run_id=run_id,
                objective=objective.strip(),
                plan=PlanGraph.from_dict(initial_plan),
            )
        if self.session_store is not None:
            message = self.session_store.add_message(
                session_id=active_session_id,
                run_id=run_id,
                role="user",
                content={"objective": objective.strip(), "symbols": list(symbols or [])},
                source={"type": "stock_ai_ui"},
                set_active_run=parent_run_id is None,
            )
        else:
            message = None
        final_domain = self.final_runtime.create_forest(
            session_id=active_session_id,
            run_id=run_id,
            objective=objective.strip(),
            message_id=(message or {}).get("message_id"),
        )
        source_branch_id = str((run_metadata or {}).get("source_branch_id") or "").strip()
        if source_branch_id:
            # Link before scheduling the background Task.  This makes the
            # Branch/Run relationship crash-safe and prevents a fast child
            # from being moved back to ready after it has already started.
            self.final_runtime.link_branch_run(source_branch_id, run_id)
        intent_title = str(
            ((run_metadata or {}).get("intent") or {}).get("title") or ""
        ).strip()
        current_session = self.session_store.get(active_session_id) if self.session_store else None
        current_title = str((current_session or {}).get("title") or "").strip()
        generated_title_revision = None
        if current_title in {"", "新對話", "Stock AI Agent 對話"}:
            title_history = SessionTitleHistory(
                active_session_id,
                temporary_title=current_title or "新對話",
            )
            generated_title_revision = await SessionTitleGenerator().generate(
                title_history,
                understood_intent=objective.strip(),
                previous_intent=str((current_session or {}).get("metadata", {}).get("last_intent") or ""),
            )
        semantic_title = intent_title or (
            generated_title_revision.title if generated_title_revision else fallback_title(objective)
        )
        await self._append_host_event(
            run_id,
            "run.queued",
            {
                "summary": "Agent run queued.",
                "objective": objective.strip(),
                "provider": selected_driver,
                "autonomy": autonomy,
            },
        )
        if message is not None:
            await self._append_host_event(
                run_id,
                "message.created",
                {
                    "summary": objective.strip(),
                    "message": message,
                },
            )
        if (
            message is not None
            and ProposalArbitrator().classify(objective) is UserInputKind.PREFERENCE
            and self._queue_explicit_preference_memory(
                service,
                session_id=active_session_id,
                run_id=run_id,
                message_id=str(message["message_id"]),
                content=objective,
            )
        ):
            await self._append_host_event(
                run_id,
                "memory.candidate.created",
                {
                    "summary": "Initial explicit user preference queued for governed memory consolidation.",
                    "message_id": message["message_id"],
                    "memory_layer": "user_preference",
                    "hard_rule": False,
                },
            )
        if final_domain.get("objective_event"):
            await self._append_host_event(
                run_id,
                str(final_domain["objective_event"]),
                {"summary": "Session objective version persisted.", **final_domain["objective"]},
            )
        await self._append_host_event(
            run_id,
            "forest.created",
            {
                "summary": "Recursive Task Forest created.",
                "forest_id": final_domain["forest_id"],
                "branch_id": final_domain["branch_id"],
                "objective_id": final_domain["objective"]["objective_id"],
            },
        )
        if semantic_title and self.session_store is not None:
            asyncio.create_task(
                self._update_session_title(
                    run_id=run_id,
                    session_id=active_session_id,
                    title=semantic_title,
                    reason=("first_intent_understood" if final_domain["objective"]["revision"] == 1 else "topic_shift"),
                    source={
                        "type": (
                            "model_intent_classification"
                            if intent_title
                            else "session_title_generator"
                        ),
                        "title_revision_id": getattr(generated_title_revision, "revision_id", None),
                        "title_source": getattr(generated_title_revision, "source", None),
                    },
                ),
                name=f"agent-title:{active_session_id}",
            )
        task = asyncio.create_task(self._execute(run_id, request), name=f"agent-run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished, key=run_id: self._task_finished(key, finished))
        return self.store.get_run(run_id) or {"run_id": run_id, "status": "queued"}

    async def _update_session_title(
        self,
        *,
        run_id: str,
        session_id: str,
        title: str,
        reason: str,
        source: dict[str, Any],
    ) -> None:
        """Persist a semantic title off-path, then project it into the live Dock."""
        if self.session_store is None:
            return
        updated = await asyncio.to_thread(
            self.session_store.update_title,
            session_id,
            title=title,
            reason=reason,
            source=source,
        )
        await self._append_host_event(
            run_id,
            "session.title.updated",
            {"session": updated, "title": updated.get("title") or title},
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.start()
        return self.store.get_run(run_id)

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        self.start()
        run = self.store.get_run(run_id)
        if run is None:
            return None
        plan = self.get_plan(run_id)
        steps = self.store.steps(run_id)
        if not steps and plan:
            steps = [
                {
                    **node,
                    "type": node.get("node_type"),
                    "parent_node_id": node.get("parent_id"),
                    "dependency_ids": node.get("dependencies") or [],
                }
                for node in plan.get("nodes") or []
            ]
        approvals = (
            self.approval_manager.list(run_id)
            if self.approval_manager is not None
            else []
        )
        artifacts = []
        artifact_versions: dict[str, list[dict[str, Any]]] = {}
        for raw_artifact in self.artifacts(run_id):
            artifact = dict(raw_artifact)
            initial_version = self.final_runtime.ensure_artifact_version(
                str(artifact["artifact_id"]),
                _artifact_version_content(artifact),
                changed_by="host_artifact_projection",
            )
            artifact["version"] = int(initial_version["version"])
            artifact["artifact_version"] = int(initial_version["version"])
            artifacts.append(artifact)
            artifact_versions[str(artifact["artifact_id"])] = self.final_runtime.artifact_history(
                str(artifact["artifact_id"])
            )
        session_id = str(run.get("session_id") or "")
        environment_snapshot = self.store.environment_snapshot(run_id)
        if environment_snapshot is None and self._service_provider() is not None:
            environment_snapshot = self.environment(run_id)
        forest = self.final_runtime.forest(session_id, run_id=run_id) if session_id else None
        if forest is not None and plan is not None:
            forest = _filter_obsolete_forest_steps(forest, plan)
        return {
            "schema_version": "open_stock_ai.agent_run_snapshot.v2",
            "run": run,
            "current_plan": plan,
            "plan_revisions": self.plan_revisions(run_id),
            "steps": steps,
            "tool_calls": self.store.tool_calls(run_id),
            "approvals": approvals,
            "artifacts": artifacts,
            "artifact_versions": artifact_versions,
            "evidence": self.final_runtime.evidence(run_id),
            "kpis": self.final_runtime.kpis(run_id=run_id),
            "forest": forest,
            "interactions": self.final_runtime.interactions(session_id) if session_id else [],
            # The Dock snapshot is a Session projection, never an account-wide
            # Automation inventory.  Mixing Sessions here lets an unrelated
            # historical Automation look as if the current request created it.
            "automations": self.final_runtime.list_automations(
                session_id=session_id or None,
                limit=100,
            ),
            "events": self.events(run_id, after_sequence=0),
            "environment_snapshot": environment_snapshot,
            "last_sequence": int(run.get("last_sequence") or 0),
            "final_result": run.get("result"),
        }

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.start()
        return self.store.list_runs(limit=limit)

    async def wait(self, run_id: str) -> dict[str, Any]:
        # A recoverable partial result can schedule the next bounded repair
        # pass from the completion callback.  Follow that successor instead of
        # reporting a momentary terminal state to a caller that is explicitly
        # waiting for the Agent to finish its own recovery loop.
        while True:
            task = self._tasks.get(run_id)
            if task is not None:
                await asyncio.shield(task)
            recovery_task = self._recovery_tasks.get(run_id)
            if recovery_task is not None:
                await asyncio.shield(recovery_task)
            await asyncio.sleep(0)
            snapshot = self.store.get_run(run_id)
            if snapshot is None:
                raise KeyError(run_id)
            successor = self._tasks.get(run_id)
            recovery_successor = self._recovery_tasks.get(run_id)
            if recovery_successor is not None:
                continue
            if snapshot["status"] in {"queued", "running"} and successor is not None:
                continue
            break
        if snapshot["status"] == "failed":
            error = snapshot.get("error") or {}
            raise RuntimeError(str(error.get("message") or "Agent run failed"))
        if snapshot["status"] in {
            "waiting_user_input", "waiting_decision", "waiting_approval", "suspended",
        }:
            return {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "session_id": snapshot.get("session_id"),
                "status": snapshot["status"],
                "summary": {
                    "waiting_user_input": "等待使用者補充必要資訊。",
                    "waiting_decision": "等待使用者選擇決策方向。",
                    "waiting_approval": "等待使用者批准後繼續。",
                    "suspended": "任務已安全暫停，可從 checkpoint 繼續。",
                }[snapshot["status"]],
                "pending_approvals": self.list_approvals(run_id),
                "plan": self.get_plan(run_id),
            }
        if snapshot.get("result") is None:
            raise RuntimeError(f"Agent run ended without a result: {snapshot['status']}")
        return snapshot["result"]

    async def cancel(
        self,
        run_id: str,
        *,
        reason: str = "Run cancelled by the user.",
    ) -> dict[str, Any] | None:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return None
        self._preserve_root_session_focus(snapshot)
        if snapshot["status"] in TERMINAL_RUN_STATUSES:
            return snapshot
        self._cancel_reasons[run_id] = reason
        self.store.request_cancel(run_id)
        task = self._tasks.get(run_id)
        recovery_task = self._recovery_tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        if recovery_task is not None and not recovery_task.done():
            recovery_task.cancel()
        if self.worker_supervisor is not None:
            await self.worker_supervisor.cancel_run(run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if recovery_task is not None:
            await asyncio.gather(recovery_task, return_exceptions=True)
        latest = self.store.get_run(run_id)
        if latest is not None and latest["status"] not in TERMINAL_RUN_STATUSES:
            # Waiting approval/user-input is represented by a completed
            # background task.  It still needs a concrete terminal transition;
            # merely setting ``cancelling`` leaves the old Run recoverable on
            # the next desktop startup and blocks a new Session.
            self.store.cancel_run(run_id)
            self.final_runtime.set_run_status(run_id, "cancelled")
            self._sync_source_branch(
                run_id,
                "cancelled",
                error={"type": "CancelledError", "message": reason},
            )
            await self._append_host_event(
                run_id,
                "run.cancelled",
                {"summary": reason},
            )
        self.final_runtime.cancel_interactions_for_run(
            run_id,
            reason=reason,
        )
        await self._wake(run_id)
        return self.store.get_run(run_id)

    async def pause(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return None
        self._preserve_root_session_focus(snapshot)
        if snapshot["status"] in TERMINAL_RUN_STATUSES:
            return snapshot
        self._pause_requested.add(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            self.store.pause_run(
                run_id,
                status="suspended",
                error={"type": "UserPaused", "message": "Run paused by the user."},
            )
            await self._append_host_event(
                run_id,
                "run.paused",
                {"summary": "Run paused by the user."},
            )
        if self.worker_supervisor is not None:
            await self.worker_supervisor.cancel_run(run_id)
        await self._wake(run_id)
        return self.store.get_run(run_id)

    async def resume(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return None
        self._preserve_root_session_focus(snapshot)
        if snapshot["status"] not in {
            "suspended", "waiting_user_input", "waiting_decision", "waiting_approval", "failed",
        }:
            return snapshot
        if run_id in self._tasks and not self._tasks[run_id].done():
            return snapshot
        checkpoint = self._restore_checkpoint_for_run(run_id, snapshot)
        self.store.mark_resuming(run_id)
        request = dict(snapshot.get("request") or {})
        resume_state = {
            "checkpoint": checkpoint,
            "last_sequence": snapshot.get("last_sequence") or 0,
            "current_step": snapshot.get("current_step") or 0,
            # A suspended in-progress turn may be replayed once; a completed
            # continuation supplies its own next_step below so it cannot walk
            # the whole historical plan again.
            "next_step": max(1, int(snapshot.get("current_step") or 1)),
        }
        task = asyncio.create_task(
            self._execute(run_id, request, resume_state=resume_state),
            name=f"agent-resume:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished, key=run_id: self._task_finished(key, finished))
        await self._wake(run_id)
        return self.store.get_run(run_id)

    def _preserve_root_session_focus(self, snapshot: dict[str, Any]) -> None:
        if self.session_store is None or snapshot.get("parent_run_id"):
            return
        session_id = str(snapshot.get("session_id") or "").strip()
        run_id = str(snapshot.get("run_id") or "").strip()
        if session_id and run_id:
            self.session_store.set_active_run(session_id, run_id)

    async def continue_after_limit(
        self,
        run_id: str,
        *,
        additional_steps: int = 6,
        max_steps: int | None = None,
        recovery_interaction: bool = False,
        driver_id: str | None = None,
    ) -> dict[str, Any] | None:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return None
        # Autonomous recovery is still user-visible work.  Keep the root
        # Session focused on this Run so the Dock opens its Task Forest and
        # Host events instead of leaving the user on an unrelated empty state.
        self._preserve_root_session_focus(snapshot)
        continuable_statuses = {"max_steps_reached", "partially_completed"}
        if recovery_interaction:
            continuable_statuses.update({"waiting_user_input", "waiting_decision"})
        if snapshot["status"] not in continuable_statuses:
            raise ValueError("Only an incomplete run can continue")
        if run_id in self._tasks and not self._tasks[run_id].done():
            return snapshot
        current_limit = int(snapshot.get("max_steps") or 6)
        requested_limit = (
            int(max_steps)
            if max_steps is not None
            else current_limit + max(1, int(additional_steps))
        )
        # Keep a bounded global ceiling (P62), without treating a small number
        # of continuations as proof that the P40 recovery ladder is exhausted.
        # A recoverable local branch may need several distinct L4–L7 actions;
        # it must not be escalated merely because an earlier implementation
        # happened to use two continuation passes.
        next_limit = max(
            current_limit + 1,
            min(requested_limit, _AUTONOMOUS_RECOVERY_STEP_CEILING),
        )
        current_driver = str(
            (snapshot.get("request") or {}).get("driver_id")
            or snapshot.get("driver")
            or ""
        ).strip()
        selected_driver = str(driver_id or current_driver).strip()
        service = self._service_provider()
        drivers = getattr(service, "drivers", None)
        if isinstance(drivers, dict) and selected_driver not in drivers:
            raise ValueError(f"Unknown Agent driver: {selected_driver}")
        if recovery_interaction and driver_id and selected_driver == current_driver:
            raise ValueError("L8 final-synthesis recovery requires a different configured model")
        checkpoint = self._restore_checkpoint_for_run(run_id, snapshot)
        latest_plan = self.get_plan(run_id)
        if checkpoint is not None:
            checkpoint = {**checkpoint, "payload": dict(checkpoint.get("payload") or {})}
            if latest_plan is not None:
                continuation_plan = {
                    **latest_plan,
                    "nodes": [
                        {
                            **node,
                            "status": (
                                "ready"
                                if node.get("status") == "blocked"
                                and (node.get("metadata") or {}).get("blocked_reason")
                                == "max_steps_reached"
                                else node.get("status")
                            ),
                            "error_summary": (
                                None
                                if node.get("status") == "blocked"
                                and (node.get("metadata") or {}).get("blocked_reason")
                                == "max_steps_reached"
                                else node.get("error_summary")
                            ),
                        }
                        for node in latest_plan.get("nodes") or []
                    ],
                }
                checkpoint["payload"]["plan"] = continuation_plan
            context_state = dict(checkpoint["payload"].get("context_state") or {})
            context_state["current_step"] = min(
                next_limit,
                int(snapshot.get("current_step") or current_limit) + 1,
            )
            checkpoint["payload"]["context_state"] = context_state
        reopened = self.store.prepare_continuation(
            run_id,
            max_steps=next_limit,
            allow_waiting_recovery=recovery_interaction,
            driver_id=selected_driver or None,
        )
        request = dict(reopened.get("request") or {})
        request["max_steps"] = next_limit
        prior_result = (
            dict(snapshot.get("result") or {})
            if isinstance(snapshot.get("result"), dict)
            else {}
        )
        resume_state = {
            "checkpoint": checkpoint,
            # ``prepare_continuation`` clears result_json by design. Retain a
            # recovery-only copy so a provider cannot erase an unresolved
            # failure by returning an empty ``completed`` result.
            "recovery_context": {
                "tool_trace": list(prior_result.get("tool_trace") or []),
                "pending_recovery_node_ids": list(
                    prior_result.get("pending_recovery_node_ids") or []
                ),
                # Historical rows created before the durable trace contract
                # are migrated by AgentRunStore. Preserve that one-time
                # compatibility path; new Runs remain subject to the strict
                # no-green-without-recovery gate below.
                "historical_reclassification": bool(
                    prior_result.get("historical_reclassification")
                ),
                "explicit_replan_requested": any(
                    isinstance(item, dict)
                    and isinstance(item.get("recovery"), dict)
                    and item["recovery"].get("requires_model_replan") is True
                    for item in (prior_result.get("tool_trace") or [])
                ),
            },
            "last_sequence": snapshot.get("last_sequence") or 0,
            "current_step": snapshot.get("current_step") or 0,
            "next_step": min(
                next_limit,
                int(snapshot.get("current_step") or current_limit) + 1,
            ),
        }
        continuation_reason = (
            "User-selected L8 recovery increased the step budget"
            if recovery_interaction
            else
            "Step limit increased"
            if snapshot["status"] == "max_steps_reached"
            else "Recovery continuation increased the step budget"
        )
        await self._append_host_event(
            run_id,
            "run.continuation_requested",
            {
                "summary": f"{continuation_reason} from {current_limit} to {next_limit}.",
                "previous_max_steps": current_limit,
                "max_steps": next_limit,
                "resume_from_status": snapshot["status"],
                "previous_driver": current_driver or None,
                "driver": selected_driver or None,
            },
        )
        task = asyncio.create_task(
            self._execute(run_id, request, resume_state=resume_state),
            name=f"agent-continue:{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda finished, key=run_id: self._task_finished(key, finished))
        await self._wake(run_id)
        return self.store.get_run(run_id)

    async def retry(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return None
        if snapshot["status"] not in {"failed", "suspended", "interrupted"}:
            raise ValueError("Only failed or suspended runs can retry")
        self.store.add_control_message(
            run_id,
            control_type="retry",
            payload={"instruction": "Retry the failed step from the latest safe checkpoint."},
        )
        return await self.resume(run_id)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        challenge: str,
    ) -> dict[str, Any]:
        if self.approval_manager is None:
            raise RuntimeError("Approval manager is unavailable")
        approval = self.approval_manager.resolve(
            approval_id,
            approved=approved,
            decided_by=decided_by,
            challenge=challenge,
        )
        active = self._tasks.get(approval["run_id"])
        if active is not None and not active.done():
            await asyncio.shield(active)
        if approved and approval["status"] == "approved":
            await self.resume(approval["run_id"])
        else:
            # Denial is evidence for replanning, not a terminal runtime error.
            # Resume from the checkpoint so the provider can choose a safer
            # tool or modify the PlanGraph without repeating completed steps.
            self.store.add_control_message(
                approval["run_id"],
                control_type="approval_denied",
                payload={
                    "approval_id": approval_id,
                    "tool_name": approval.get("tool_name"),
                    "risk_class": approval.get("risk_class"),
                    "message": "The user denied this action; revise the plan and do not repeat these arguments.",
                },
            )
            await self.resume(approval["run_id"])
        await self._wake(approval["run_id"])
        return approval

    def list_approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return self.approval_manager.pending(run_id) if self.approval_manager else []

    def create_session(self, *, title: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.session_store is None:
            raise RuntimeError("Session store is unavailable")
        return self.session_store.create(title=title, namespace=self.namespace, metadata=metadata)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.session_store.get(session_id) if self.session_store else None

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.session_store.list(namespace=self.namespace, limit=limit) if self.session_store else []

    def session_messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.session_store.messages(session_id, limit=limit) if self.session_store else []

    def export_session_archive(self, session_id: str) -> dict[str, Any] | None:
        """Export the Session and every durable Run snapshot as one bundle."""

        if self.session_store is None:
            return None
        session = self.session_store.get(session_id)
        if session is None:
            return None
        run_rows = self.store.list_runs_for_session(session_id, limit=1000)
        snapshots = [self.snapshot(str(run["run_id"])) for run in run_rows]
        return build_session_archive(
            session=session,
            title_history=self.session_store.title_history(session_id),
            messages=self.session_store.messages(session_id, limit=1000),
            runs=[snapshot for snapshot in snapshots if snapshot is not None],
        )

    def restore_session_archive(
        self,
        archive: dict[str, Any],
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a cold archive without overwriting authoritative live data."""

        return restore_verified_session_archive(
            archive,
            expected_session_id=expected_session_id,
        )

    def materialize_session_archive(
        self,
        archive: dict[str, Any],
        target_db: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Rehydrate a verified archive into a fresh authoritative SQLite DB."""

        return materialize_session_archive(
            archive,
            target_db,
            expected_session_id=expected_session_id,
        )

    def _queue_explicit_preference_memory(
        self,
        service: Any,
        *,
        session_id: str,
        run_id: str | None,
        message_id: str,
        content: str,
    ) -> bool:
        """Schedule governed preference consolidation without delaying a Run.

        Initial Composer input and follow-up Session messages are both user
        statements.  They must enter the same advisory-only memory path so a
        later Session can retrieve stable project preferences without storing
        live market observations or turning a preference into an execution
        rule.
        """

        memory_manager = getattr(service, "memory_manager", None)
        background_work = getattr(service, "background_work", None)
        if memory_manager is None or background_work is None:
            return False
        candidate = MemoryCandidate(
            content=content.strip(),
            kind="user_preference",
            importance=0.7,
            future_relevance=0.85,
            confidence=1.0,
            source={"type": "user_explicit", "message_id": message_id},
            durability="long_term",
            semantic_key=_preference_semantic_key(content),
            run_id=run_id,
            session_id=session_id,
            host_verified=True,
        )
        background_work.submit(memory_manager.consider, candidate)
        return True

    async def add_session_message(
        self,
        session_id: str,
        *,
        content: str,
        artifact_context_selection: dict[str, Any] | None = None,
        intent: str | None = None,
        target_branch_id: str | None = None,
        affected_branch_ids: tuple[str, ...] = (),
        replacement_objective: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        active_run_id = str(session.get("active_run_id") or "") or None
        # A Session retains its latest Run ID for durable history, including a
        # run that has already reached a terminal state.  That historical link
        # must not cause the next natural-language request to inherit an old
        # execution lane (for example ``paper_execute``) or be steered back
        # into an exhausted plan.  Treat terminal runs as history and let the
        # new message create a fresh, advisory Run unless it independently
        # requests the bounded paper lane through the normal API path.
        if active_run_id:
            active_run = self.get_run(active_run_id)
            if active_run is None or active_run.get("status") in TERMINAL_RUN_STATUSES:
                active_run_id = None
        message = self.session_store.add_message(
            session_id=session_id,
            run_id=active_run_id,
            role="user",
            content={"text": content.strip(), "artifact_context_selection": artifact_context_selection},
            source={"type": "stock_ai_ui", "routing": "session_message"},
        )
        context_selection = _message_selection_context(
            artifact_context_selection,
            message_id=str(message["message_id"]),
        )
        # A graph node is a first-class conversation target even when it is
        # not backed by a mutable artifact.  Preserve the graph coordinates in
        # the durable message and use its Branch as the steering boundary.
        # This prevents a comment on one evidence node from silently changing
        # the root task forest.
        effective_target_branch_id = target_branch_id or (
            context_selection.get("branch_id") if context_selection else None
        )
        proposal_record = None
        arbitration = ProposalArbitrator()
        # A non-overridable live-trade request must be handled before generic
        # language classification.  Otherwise wording such as "不需要我批准"
        # is incorrectly treated as an ordinary approval and can reach the
        # provider-facing proposal path.
        prohibited = arbitration.is_prohibited_trade_proposal(content)
        input_kind = arbitration.classify(content)
        if prohibited:
            input_kind = UserInputKind.PROPOSAL
        if input_kind is UserInputKind.PREFERENCE:
            service = self._service_provider()
            queued = self._queue_explicit_preference_memory(
                service,
                session_id=session_id,
                run_id=active_run_id,
                message_id=str(message["message_id"]),
                content=content,
            )
            if queued and active_run_id and self.get_run(active_run_id):
                await self._append_host_event(
                    active_run_id,
                    "memory.candidate.created",
                    {
                        "summary": "Explicit user preference queued for governed memory consolidation.",
                        "message_id": message["message_id"],
                        "memory_layer": "user_preference",
                        "hard_rule": False,
                    },
                )
        if input_kind in {UserInputKind.PROPOSAL, UserInputKind.HYPOTHESIS}:
            lowered = content.casefold()
            if not prohibited:
                # The Host may perform a deterministic safety precheck, but
                # it must not decide that a proposal is evidence-compatible
                # from keywords. Send the proposal through a real, bounded
                # Agent evaluation Run that can inspect the current evidence
                # and return ACCEPT/MODIFY/REJECT/ASK with a receipt.
                proposal_record = self.final_runtime.record_user_proposal(
                    session_id=session_id,
                    message_id=message["message_id"],
                    branch_id=effective_target_branch_id,
                    proposal_type=input_kind.value,
                    proposal={
                        "content": content.strip(),
                        "intent": intent,
                        "evaluation_protocol": "model_evidence_branch_v1",
                    },
                    evaluation={
                        "decision": "pending",
                        "input_kind": input_kind.value,
                        "evidence_compatible": None,
                        "risk_level": "pending_model_evaluation",
                        "impact_level": "pending_model_evaluation",
                        "requires_user_decision": True,
                    },
                )
                current_run = self.get_run(active_run_id) if active_run_id else None
                proposal_objective = (
                    "[MODEL_TASK_KIND:market_information] 評估使用者提案與目前 Host 證據的相容性。"
                    "\n[MODEL_OUTPUT_SCHEMA:proposal_evaluation]\n"
                    "請先讀取可用的市場／研究證據，再判斷 ACCEPT、MODIFY、REJECT 或 ASK；"
                    "不要執行交易、不要建立自動化，也不要把提案偏好直接當成規則。"
                    f"\n使用者提案：{content.strip()}"
                    "\n請在 structured_result 回傳 schema_version=open_stock_ai.proposal_evaluation.v1，"
                    "包含 decision、evidence_compatible、risk_level、impact_level、recommendation、"
                    "rationale、safe_alternative、evidence_ids。"
                )
                evaluation_run = await self.create_run(
                    objective=proposal_objective,
                    symbols=list((current_run or {}).get("symbols") or []),
                    driver_id=str((current_run or {}).get("driver") or "") or None,
                    autonomy="advisory",
                    max_steps=8,
                    session_id=session_id,
                    parent_run_id=active_run_id,
                    run_metadata={
                        "source": "proposal_evaluation",
                        "proposal_id": proposal_record["proposal_id"],
                        "source_branch_id": effective_target_branch_id,
                    },
                )
                return {
                    "schema_version": "open_stock_ai.session_message_result.v1",
                    "message": message,
                    "proposal": proposal_record,
                    "proposal_evaluation_run": evaluation_run,
                    "steering": {
                        "intent": "proposal_evaluation_pending",
                        "created_branch_ids": [],
                        "affected_branch_ids": [],
                        "preserved_branch_ids": [
                            item["branch_id"]
                            for item in (self.final_runtime.forest(session_id) or {}).get("branches", [])
                        ],
                        "forest": self.final_runtime.forest(session_id),
                    },
                    "selection": context_selection,
                    "interaction": None,
                }
            global_impact = (intent or "") in {"hard_steer", "new_goal"}
            evidence_compatible = not any(
                token in lowered
                for token in ("不要看證據", "忽略證據", "ignore evidence")
            )
            if prohibited:
                safe_alternative = (
                    "此系統不會操作台新或任何真實帳戶，也不會建立、送出或排程任何實盤訂單；"
                    "此要求已被拒絕。可改為分析標的，或明確要求本機紙上模擬交易。"
                )
            elif not evidence_compatible:
                safe_alternative = (
                    "此要求不能略過證據或風險邊界，且不會建立任何訂單；此要求已被拒絕。"
                )
            else:
                safe_alternative = None
            result = arbitration.arbitrate(
                content,
                evidence_compatible=evidence_compatible,
                risk_level="prohibited" if prohibited else "medium",
                impact_level="global" if global_impact else "local",
                safe_alternative=safe_alternative,
                input_kind=input_kind,
            )
            evaluation = {
                "input_kind": result.input_kind.value,
                "decision": result.decision.value,
                "evidence_compatible": result.evidence_compatible,
                "risk_level": result.risk_level,
                "impact_level": result.impact_level,
                "recommendation": result.recommendation,
                "rationale": list(result.rationale),
                "requires_user_decision": result.requires_user_decision,
            }
            proposal_record = self.final_runtime.record_user_proposal(
                session_id=session_id,
                message_id=message["message_id"],
                branch_id=target_branch_id,
                proposal_type=input_kind.value,
                proposal={"content": content.strip(), "intent": intent},
                evaluation=evaluation,
            )
            if result.decision is ArbitrationDecision.ASK:
                interaction = self.final_runtime.create_interaction(
                    session_id=session_id,
                    run_id=active_run_id,
                    branch_id=target_branch_id,
                    waiting_state="waiting_decision",
                    payload={
                        "prompt": "這個提案會影響全局計畫，要採用 Agent 建議的安全方案嗎？",
                        "agent_view": result.recommendation,
                        "preferred_option": "safe_alternative",
                        "options": [
                            {
                                "option_id": "safe_alternative",
                                "label": "採用安全方案",
                                "reason": result.recommendation,
                                "recommended": True,
                            },
                            {
                                "option_id": "keep_current",
                                "label": "保留現狀",
                                "reason": "不在證據或影響尚未釐清時改變全局計畫。",
                                "recommended": False,
                            },
                        ],
                        "proposal_id": proposal_record["proposal_id"],
                    },
                )
                if active_run_id and self.get_run(active_run_id):
                    await self._append_host_event(
                        active_run_id,
                        "proposal.requires_decision",
                        {
                            "summary": result.recommendation,
                            "proposal": proposal_record,
                            "interaction": interaction,
                        },
                    )
                return {
                    "schema_version": "open_stock_ai.session_message_result.v1",
                    "message": message,
                    "steering": {
                        "intent": "proposal_pending_decision",
                        "created_branch_ids": [],
                        "affected_branch_ids": [],
                        "preserved_branch_ids": [
                            item["branch_id"]
                            for item in (self.final_runtime.forest(session_id) or {}).get("branches", [])
                        ],
                        "forest": self.final_runtime.forest(session_id),
                    },
                    "selection": None,
                    "proposal": proposal_record,
                    "interaction": interaction,
                }
            if result.decision is ArbitrationDecision.REJECT:
                safety_message = self.session_store.add_message(
                    session_id=session_id,
                    run_id=active_run_id,
                    role="assistant",
                    content={
                        "text": result.recommendation,
                        "kind": "safety_rejection",
                        "proposal_id": proposal_record["proposal_id"],
                    },
                    source={"type": "host_policy", "decision": "proposal_rejected"},
                )
                return {
                    "schema_version": "open_stock_ai.session_message_result.v1",
                    "message": message,
                    "messages": [message, safety_message],
                    "result": {
                        "status": "proposal_rejected",
                        "summary": result.recommendation,
                    },
                    "steering": {
                        "intent": "proposal_rejected",
                        "created_branch_ids": [],
                        "affected_branch_ids": [],
                        "preserved_branch_ids": [
                            item["branch_id"]
                            for item in (self.final_runtime.forest(session_id) or {}).get("branches", [])
                        ],
                        "forest": self.final_runtime.forest(session_id),
                    },
                    "selection": None,
                    "proposal": proposal_record,
                }
            if result.decision is ArbitrationDecision.MODIFY:
                content = result.recommendation
        selection = context_selection
        if artifact_context_selection and artifact_context_selection.get("artifact_id"):
            selection = self.final_runtime.select_artifact(
                session_id=session_id,
                artifact_id=str(artifact_context_selection["artifact_id"]),
                artifact_version=int(artifact_context_selection.get("artifact_version") or 1),
                target_type=str(artifact_context_selection.get("target_type") or "artifact"),
                path=str(artifact_context_selection.get("path") or "artifact"),
                branch_id=artifact_context_selection.get("branch_id"),
                node_id=artifact_context_selection.get("node_id"),
                evidence_id=artifact_context_selection.get("evidence_id"),
            )
            intent = intent or "modify_artifact"
        steering_content = content.strip()
        if selection and intent == "modify_artifact":
            steering_content = (
                f"{steering_content}\n[Selected artifact: {selection.get('artifact_id')}; "
                f"version: {selection.get('artifact_version')}; path: {selection.get('path')}; "
                f"node: {selection.get('node_id')}]"
            )
        # A new Session message is itself the Master Agent objective. Do not
        # run an OpportunityDetector before the Agent has researched it; the
        # Agent may return a post-answer interaction_proposal when it finds a
        # worthwhile future automation.
        if not active_run_id and not effective_target_branch_id:
            created = await self.create_run(
                objective=steering_content,
                session_id=session_id,
                run_metadata={"source": "session_message", "message_id": message["message_id"]},
            )
            return {
                "schema_version": "open_stock_ai.session_message_result.v1",
                "message": message,
                "steering": {
                    "intent": "new_goal",
                    "created_branch_ids": [],
                    "affected_branch_ids": [],
                    "preserved_branch_ids": [],
                    "forest": self.final_runtime.forest(session_id, run_id=str(created["run_id"])),
                },
                "selection": selection,
                "proposal": proposal_record,
                "follow_run_id": created["run_id"],
                "follow_run_ids": [created["run_id"]],
                "runs": [created],
            }
        outcome = self.final_runtime.steer(
            session_id=session_id,
            message_id=message["message_id"],
            content=steering_content,
            target_branch_id=effective_target_branch_id,
            intent=intent,
            affected_branch_ids=affected_branch_ids,
            replacement_objective=replacement_objective,
        )
        steering_run_id = str((outcome.get("forest") or {}).get("run_id") or active_run_id or "") or None
        root_branch_id = str((outcome.get("forest") or {}).get("root_branch_id") or "")
        replaces_active_root_run = (
            outcome.get("intent") in {"hard_steer", "correct_fact"}
            and active_run_id is not None
            and self.get_run(active_run_id) is not None
            and self.get_run(active_run_id).get("status") not in TERMINAL_RUN_STATUSES
            and root_branch_id in set(outcome.get("affected_branch_ids") or ())
        )
        if replaces_active_root_run:
            # A correction to the root objective is not a cosmetic Forest
            # revision.  The provider-facing parent Run may already be
            # executing the obsolete task, so it must be cancelled and
            # replaced.  The old Forest remains durable evidence; the new
            # Run gets a separate, auditable Forest rooted in the user's
            # revised outcome.
            previous_run = self.get_run(active_run_id) or {}
            await self.cancel(
                active_run_id,
                reason="Host replaced the obsolete root Run after the user revised the objective.",
            )
            revised_run = await self.create_run(
                objective=replacement_objective or content.strip(),
                symbols=list(previous_run.get("symbols") or []),
                driver_id=str(previous_run.get("driver") or "") or None,
                autonomy=str(previous_run.get("autonomy") or "advisory"),
                max_steps=int(previous_run.get("max_steps") or 12),
                session_id=session_id,
                parent_run_id=active_run_id,
                run_metadata={
                    "source": "root_hard_steer",
                    "message_id": message["message_id"],
                    "replaces_run_id": active_run_id,
                    "replaces_forest_id": (outcome.get("forest") or {}).get("forest_id"),
                },
            )
            revised_run_id = str(revised_run["run_id"])
            await self._append_host_event(
                revised_run_id,
                "objective.revised",
                {
                    "summary": "Host cancelled the obsolete root Run and started the revised objective.",
                    "message_id": message["message_id"],
                    "replaces_run_id": active_run_id,
                    "steering": outcome,
                },
            )
            outcome.update({
                "cancelled_root_run_id": active_run_id,
                "replacement_run_id": revised_run_id,
                "cancelled_follow_run_ids": [active_run_id],
                "forest": self.final_runtime.forest(session_id, run_id=revised_run_id),
            })
            return {
                "schema_version": "open_stock_ai.session_message_result.v1",
                "message": message,
                "steering": outcome,
                "selection": selection,
                "proposal": proposal_record,
                "follow_run_id": revised_run_id,
                "follow_run_ids": [revised_run_id],
                "runs": [revised_run],
            }
        cancelled_follow_run_ids: list[str] = []
        for branch_id in outcome.get("cancelled_branch_ids") or []:
            cancelled_branch = self.final_runtime.branch(str(branch_id)) or {}
            follow_run_id = str(cancelled_branch.get("follow_run_id") or "")
            if not follow_run_id:
                continue
            follow_run = self.get_run(follow_run_id)
            if follow_run is None or follow_run.get("status") in TERMINAL_RUN_STATUSES:
                continue
            await self.cancel(follow_run_id)
            cancelled_follow_run_ids.append(follow_run_id)
        if cancelled_follow_run_ids:
            outcome["cancelled_follow_run_ids"] = cancelled_follow_run_ids
        if outcome.get("intent") == "correct_fact":
            correction_event_id = f"correction:{message['message_id']}"
            self.final_runtime.record_metric(
                session_id=session_id,
                run_id=steering_run_id,
                metric="user_correction",
                event_id=correction_event_id,
                payload={"message_id": message["message_id"]},
            )
            self.final_runtime.record_metric(
                session_id=session_id,
                run_id=steering_run_id,
                metric="user_correction_incorporated",
                event_id=correction_event_id,
                payload={
                    "message_id": message["message_id"],
                    "affected_branch_ids": outcome.get("affected_branch_ids") or [],
                },
            )
        follow_runs = await self._start_steering_runs(
            session_id=session_id,
            parent_run_id=steering_run_id,
            branch_ids=tuple(outcome.get("created_branch_ids") or ()),
        )
        if follow_runs:
            outcome["follow_run_ids"] = [item["run_id"] for item in follow_runs]
            if steering_run_id:
                outcome["forest"] = self.final_runtime.forest(
                    session_id,
                    run_id=steering_run_id,
                )
        if steering_run_id and self.get_run(steering_run_id):
            await self._append_host_event(
                steering_run_id,
                "message.created",
                {"summary": content.strip(), "message": message, "steering": outcome},
            )
            event_type = {
                "soft_steer": "forest.revised",
                "append_requirement": "objective.revised",
                "hard_steer": "forest.revised",
                "correct_fact": "forest.revised",
                "fork_branch": "branch.created",
                "new_goal": "branch.created",
                "pause_branch": "branch.paused",
                "resume_branch": "branch.resumed",
                "cancel_branch": "branch.cancelled",
                "modify_artifact": "artifact.selected",
            }.get(outcome["intent"], "proposal.received")
            if selection and selection.get("evidence_id") and not selection.get("artifact_id"):
                event_type = "research.evidence_selected"
            await self._append_host_event(
                steering_run_id,
                event_type,
                {"summary": outcome["intent"], "selection": selection, **outcome},
            )
        return {
            "schema_version": "open_stock_ai.session_message_result.v1",
            "message": message,
            "steering": outcome,
            "selection": selection,
            "proposal": proposal_record,
            "follow_run_id": follow_runs[0]["run_id"] if follow_runs else None,
            "follow_run_ids": [item["run_id"] for item in follow_runs],
            "runs": follow_runs,
        }

    async def submit_session_run(
        self,
        session_id: str,
        *,
        objective: str,
        symbols: list[str] | tuple[str, ...] | None = None,
        driver_id: str | None = None,
        autonomy: str = "advisory",
        max_steps: int = 6,
        parent_run_id: str | None = None,
        idempotency_key: str | None = None,
        initial_plan: dict[str, Any] | None = None,
        workflow_id: str | None = None,
        schedule_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new Session Run after Host-level safety preflight.

        The initial composer previously created a Run directly, bypassing the
        ProposalArbitrator path that protects in-run messages.  Trade-bypass
        proposals now become durable, user-visible rejections before a
        provider, tool, approval, or Automation can be invoked.
        """

        if self.get_session(session_id) is None:
            raise KeyError(session_id)
        arbitration = ProposalArbitrator()
        if arbitration.is_prohibited_trade_proposal(objective):
            return await self.add_session_message(
                session_id,
                content=objective,
                intent="new_goal",
            )
        return await self.create_run(
            objective=objective,
            symbols=symbols,
            driver_id=driver_id,
            autonomy=autonomy,
            max_steps=max_steps,
            session_id=session_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            initial_plan=initial_plan,
            workflow_id=workflow_id,
            schedule_id=schedule_id,
            run_metadata=run_metadata,
        )

    async def respond_interaction(
        self,
        interaction_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        pending_interaction = self.final_runtime.interaction(interaction_id)
        if pending_interaction is None:
            raise KeyError(interaction_id)
        pending_payload = dict(pending_interaction)
        pending_selection = str(response.get("option_id") or "").strip().casefold()
        pending_option = next(
            (
                dict(option)
                for option in pending_payload.get("options") or []
                if isinstance(option, dict)
                and str(option.get("option_id") or "")
                == str(response.get("option_id") or "")
            ),
            None,
        )
        pending_run = self.get_run(str(pending_payload.get("run_id") or "")) or {}
        if (
            pending_payload.get("interaction_purpose") == "recovery_escalation"
            and pending_selection == "continue_alternative_recovery"
            and str((pending_run.get("result") or {}).get("recovery_state") or "")
            == "completion_output_repair_exhausted"
            and not str((pending_option or {}).get("recovery_driver_id") or "").strip()
        ):
            raise ValueError(
                "This recovery choice has no configured alternate model; "
                "keep the verified partial result or configure a different provider first"
            )
        resolved = self.final_runtime.respond_interaction(interaction_id, response)
        interaction_payload = dict(resolved.get("interaction_payload") or {})
        proposal_follow_run = None
        proposal_interaction_handled = False
        if interaction_payload.get("interaction_purpose") == "automation_confirmation":
            activation_run_id = str(pending_payload.get("run_id") or "")
            selected = str(response.get("option_id") or "").strip().casefold()
            if selected == "confirm":
                intent = dict(interaction_payload.get("automation_intent") or {})
                if not intent:
                    raise ValueError("Automation confirmation is missing its semantic intent")
                resolved["automation_activation"] = self.final_runtime.activate_automation(
                    intent,
                    confirmed=True,
                )
                activation = dict(resolved["automation_activation"] or {})
                automation = dict(activation.get("automation") or {})
                automation_state = str(automation.get("state") or "pending")
                automation["status"] = "active" if automation_state == "active" else automation_state
                automation["artifact"] = dict(activation.get("artifact") or {})
                automation["host_status"] = str(activation.get("host_status") or "")
                automation["technical"] = {
                    "backend": str(automation.get("backend") or "internal_scheduler"),
                    "execution_status": str(activation.get("host_status") or automation_state),
                    "workflow_configured": bool(automation.get("backend_reference")),
                }
                if activation_run_id:
                    await self._append_host_event(
                        activation_run_id,
                        "automation.activated",
                        {
                            "summary": str(activation.get("host_status") or "Automation 已由 Host 驗證並交給執行層。"),
                            "automation": automation,
                            "activation": {
                                "version": activation.get("version"),
                                "backend": automation.get("backend"),
                                "host_status": activation.get("host_status"),
                                "accepted": automation_state == "active",
                            },
                        },
                    )
            else:
                resolved["automation_activation"] = {
                    "status": "not_activated",
                    "reason": "User did not confirm the automation proposal.",
                }
        if interaction_payload.get("interaction_purpose") == "proposal_evaluation":
            proposal_interaction_handled = True
            selected = str(response.get("option_id") or "").strip().casefold()
            evaluation = dict(interaction_payload.get("evaluation") or {})
            if selected == "apply_recommendation":
                recommendation = str(evaluation.get("recommendation") or "").strip()
                if not recommendation:
                    raise ValueError("Proposal evaluation has no recommendation to apply")
                proposal_follow_run = await self.create_run(
                    objective=(
                        "依使用者確認，執行提案評估後的建議方案；保留原證據並重新驗證下游影響。\n"
                        + recommendation
                    ),
                    symbols=list((pending_run or {}).get("symbols") or []),
                    driver_id=str((pending_run or {}).get("driver") or "") or None,
                    autonomy="advisory",
                    max_steps=8,
                    session_id=str(resolved.get("session_id") or ""),
                    parent_run_id=str(resolved.get("run_id") or "") or None,
                    run_metadata={
                        "source": "proposal_evaluation_apply",
                        "proposal_id": interaction_payload.get("proposal_id"),
                        "source_branch_id": resolved.get("branch_id"),
                    },
                )
                resolved["proposal_applied"] = True
                resolved["follow_run_id"] = proposal_follow_run.get("run_id")
            else:
                partial = dict(pending_run.get("result") or {})
                partial.update({"status": "partially_completed", "recovery_state": "user_retained_partial"})
                self.store.complete_run(str(resolved.get("run_id") or ""), partial)
                self.final_runtime.set_run_status(str(resolved.get("run_id") or ""), "partially_completed", result=partial)
                resolved["proposal_applied"] = False
        if interaction_payload.get("interaction_purpose") == "post_answer_proposal":
            # This decision belongs to a completed answer, not a paused model
            # coroutine.  Resolve it without reopening the parent Run.  Only
            # the affirmative option creates a new same-Session child Run.
            proposal_interaction_handled = True
            selected = str(response.get("option_id") or "").strip().casefold()
            free_text = str(response.get("free_text") or "").strip()
            if selected == "start_follow_up" or free_text:
                arguments = dict(interaction_payload.get("proposal_arguments") or {})
                objective = free_text or next(
                    (
                        str(arguments.get(key) or "").strip()
                        for key in ("objective", "prompt", "instruction", "query", "task")
                        if str(arguments.get(key) or "").strip()
                    ),
                    "",
                )
                title = str(interaction_payload.get("prompt") or "").strip()
                reason = str(interaction_payload.get("agent_view") or "").strip()
                if not objective:
                    objective = "\n".join(item for item in (title, reason) if item)
                if not objective:
                    raise ValueError("Post-answer proposal has no follow-up objective")
                action = str(interaction_payload.get("proposal_action") or "follow_up")
                if action == "create_artifact":
                    objective = (
                        "建立可定位、可版本化的 Structured Artifact，並用 Host 工具驗證其 schema。\n"
                        + objective
                    )
                proposal_follow_run = await self.create_run(
                    objective=objective,
                    symbols=list((pending_run or {}).get("symbols") or []),
                    driver_id=str((pending_run or {}).get("driver") or "") or None,
                    autonomy="advisory",
                    max_steps=8,
                    session_id=str(resolved.get("session_id") or ""),
                    parent_run_id=str(resolved.get("run_id") or "") or None,
                    run_metadata={
                        "source": "post_answer_interaction_proposal",
                        "proposal_id": interaction_payload.get("proposal_id"),
                        "proposal_action": action,
                        "source_branch_id": resolved.get("branch_id"),
                    },
                )
                resolved["proposal_accepted"] = True
                resolved["follow_run_id"] = proposal_follow_run.get("run_id")
                resolved["follow_run"] = proposal_follow_run
            else:
                resolved["proposal_accepted"] = False
        run_id = str(resolved.get("run_id") or "")
        session_id = str(resolved.get("session_id") or "")
        recovery_escalation = interaction_payload.get("interaction_purpose") == "recovery_escalation"
        recovery_selection = str(response.get("option_id") or "").strip().casefold()
        selected_option = next(
            (
                dict(option)
                for option in interaction_payload.get("options") or []
                if isinstance(option, dict)
                and str(option.get("option_id") or "")
                == str(response.get("option_id") or "")
            ),
            None,
        )
        if self.session_store is not None and session_id:
            self.session_store.add_message(
                session_id=session_id,
                run_id=run_id or None,
                role="user",
                content={
                    "interaction_id": interaction_id,
                    "response": response,
                    "selected_option": selected_option,
                    "interaction_prompt": interaction_payload.get("prompt"),
                    "agent_view": interaction_payload.get("agent_view"),
                },
                source={"type": "interaction_response"},
                set_active_run=not bool(
                    (self.get_run(run_id) or {}).get("parent_run_id")
                ),
            )
        if run_id and self.get_run(run_id) is not None:
            await self._append_host_event(
                run_id,
                "interaction.responded",
                {
                    "summary": "User answered the pending Agent interaction.",
                    **resolved,
                },
            )
            run = self.get_run(run_id) or {}
            if recovery_escalation and recovery_selection == "keep_partial_result":
                partial_result = dict(run.get("result") or {})
                partial_result["status"] = "partially_completed"
                partial_result["recovery_state"] = "user_retained_partial"
                partial_result.setdefault(
                    "summary",
                    "已依使用者決定保留部分結果；未取得替代證據的分支仍明確標示為未完成。",
                )
                self.store.complete_run(run_id, partial_result)
                self.final_runtime.set_run_status(run_id, "partially_completed", result=partial_result)
                self._sync_source_branch(run_id, "partially_completed", result=partial_result)
                await self._append_host_event(
                    run_id,
                    "recovery.escalation_resolved",
                    {
                        "summary": "User chose to retain the explicitly partial result.",
                        "interaction_id": interaction_id,
                        "preserve_completed_work": True,
                    },
                )
                resolved["partial_result_retained"] = True
            elif recovery_escalation and recovery_selection in {
                "continue_alternative_recovery",
                "continue_goal_recovery",
            }:
                # L8 is an explicit request to keep repairing the *local*
                # failed branch.  A plain resume would reuse an already
                # exhausted budget and can immediately re-escalate without a
                # chance to execute the selected independent alternative.
                selected_recovery_driver = (
                    str(selected_option.get("recovery_driver_id") or "").strip()
                    if selected_option
                    else ""
                )
                resolved["resumed_run"] = await self.continue_after_limit(
                    run_id,
                    additional_steps=6,
                    recovery_interaction=recovery_selection == "continue_alternative_recovery",
                    driver_id=selected_recovery_driver or None,
                )
                resolved["follow_run_id"] = run_id
            elif not proposal_interaction_handled and run.get("status") in {"waiting_user_input", "waiting_decision"}:
                resolved["resumed_run"] = await self.resume(run_id)
                resolved["follow_run_id"] = run_id
        return resolved

    async def control_branch(
        self,
        branch_id: str,
        *,
        action: str,
        reason: str = "",
    ) -> dict[str, Any]:
        branch = self.final_runtime.branch(branch_id)
        if branch is None:
            raise KeyError(branch_id)
        follow_run_id = str(branch.get("follow_run_id") or "")
        if action == "pause":
            if follow_run_id:
                await self.pause(follow_run_id)
            else:
                self.final_runtime.set_branch_status(branch_id, status="paused", reason=reason)
        elif action == "resume":
            if follow_run_id:
                await self.resume(follow_run_id)
            else:
                self.final_runtime.set_branch_status(branch_id, status="ready", reason=reason)
        elif action == "cancel":
            if follow_run_id:
                await self.cancel(follow_run_id)
            else:
                self.final_runtime.set_branch_status(branch_id, status="cancelled", reason=reason)
        else:
            raise ValueError(f"Unsupported Branch control action: {action}")
        current = self.final_runtime.branch(branch_id) or {}
        return {
            **current,
            "control_action": action,
            "follow_run_id": follow_run_id or None,
            "follow_run": self.get_run(follow_run_id) if follow_run_id else None,
        }

    def forest(self, session_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        return self.final_runtime.forest(session_id, run_id=run_id)

    def branch(self, branch_id: str) -> dict[str, Any] | None:
        return self.final_runtime.branch(branch_id)

    def archive_session(
        self,
        session_id: str,
        *,
        archived: bool = True,
    ) -> dict[str, Any] | None:
        return (
            self.session_store.archive(session_id, archived=archived)
            if self.session_store
            else None
        )

    def get_plan(self, run_id: str) -> dict[str, Any] | None:
        plan = self.plan_manager.for_run(run_id) if self.plan_manager else None
        return plan.to_dict() if plan else None

    def plan_revisions(self, run_id: str) -> list[dict[str, Any]]:
        return self.plan_manager.revisions(run_id) if self.plan_manager else []

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        if self.checkpoint_manager is None:
            return []
        return self.checkpoint_manager.store.list(run_id)

    def _restore_checkpoint_for_run(
        self,
        run_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Prefer the narrowest scoped checkpoint, then retain old DB support."""

        branch_id: str | None = None
        step_id: str | None = None
        decision_id: str | None = None
        automation_id: str | None = None
        for event in reversed(self.store.events_after(run_id, 0)):
            raw_payload = event.get("payload")
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            branch_id = branch_id or str(event.get("branch_id") or payload.get("branch_id") or "") or None
            step_id = step_id or str(
                event.get("durable_step_id")
                or event.get("step_id")
                or payload.get("durable_step_id")
                or payload.get("step_id")
                or ""
            ) or None
            decision_id = decision_id or str(payload.get("interaction_id") or "") or None
            automation_id = automation_id or str(payload.get("automation_id") or "") or None
            if branch_id and step_id:
                break
        scoped = self.final_runtime.restore_scoped_runtime_checkpoint(
            session_id=str(snapshot.get("session_id") or ""),
            run_id=run_id,
            branch_id=branch_id,
            step_id=step_id,
            decision_id=decision_id,
            automation_id=automation_id,
        )
        if scoped is not None:
            return scoped
        return self.checkpoint_manager.restore(run_id) if self.checkpoint_manager else None

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self.artifact_store.list(run_id) if self.artifact_store else []

    def artifact(self, run_id: str, artifact_id: str) -> dict[str, Any] | None:
        return self.artifact_store.get(run_id, artifact_id) if self.artifact_store else None

    async def revise_artifact(
        self,
        *,
        artifact_id: str,
        expected_version: int,
        content: Any,
        changed_by: str,
        reason: str,
        message_id: str | None = None,
        affected_node_ids: tuple[str, ...] = (),
        emit_event: bool = True,
    ) -> dict[str, Any]:
        if self.artifact_store is None:
            raise RuntimeError("Artifact store is unavailable")
        artifact = self.artifact_store.get_by_id(artifact_id)
        if artifact is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        self._validate_artifact_projection(artifact, content)
        version = self.final_runtime.revise_artifact(
            artifact_id=artifact_id,
            expected_version=expected_version,
            content=content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            affected_node_ids=affected_node_ids,
        )
        updated = self._project_artifact_version(artifact_id, version["content"])
        event = None
        if emit_event:
            event = await self._append_host_event(
                str(updated["run_id"]),
                "artifact.updated",
                {
                    "artifact_id": artifact_id,
                    "artifact": {
                        **updated,
                        "version": int(version["version"]),
                        "artifact_version": int(version["version"]),
                    },
                    "artifact_version": version,
                    "reason": reason,
                    "affected_node_ids": list(affected_node_ids),
                },
            )
        return {**version, "artifact": updated, "events": [event] if event else []}

    async def restore_artifact(
        self,
        *,
        artifact_id: str,
        source_version: int,
        expected_version: int,
        changed_by: str,
    ) -> dict[str, Any]:
        if self.artifact_store is None:
            raise RuntimeError("Artifact store is unavailable")
        artifact = self.artifact_store.get_by_id(artifact_id)
        if artifact is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        source = self.final_runtime.artifact_versions.store.get_version(artifact_id, source_version)
        if source is None:
            raise KeyError(f"Unknown artifact version: {artifact_id} v{source_version}")
        self._validate_artifact_projection(artifact, source.content)
        version = self.final_runtime.restore_artifact(
            artifact_id=artifact_id,
            source_version=source_version,
            expected_version=expected_version,
            changed_by=changed_by,
        )
        updated = self._project_artifact_version(artifact_id, version["content"])
        event = await self._append_host_event(
            str(updated["run_id"]),
            "artifact.updated",
            {
                "artifact_id": artifact_id,
                "artifact": {
                    **updated,
                    "version": int(version["version"]),
                    "artifact_version": int(version["version"]),
                },
                "artifact_version": version,
                "reason": f"Restore artifact version {source_version}",
            },
        )
        return {**version, "artifact": updated, "events": [event] if event else []}

    def _project_artifact_version(self, artifact_id: str, content: Any) -> dict[str, Any]:
        if self.artifact_store is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("Artifact store is unavailable")
        artifact = self.artifact_store.get_by_id(artifact_id)
        if artifact is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        self._validate_artifact_projection(artifact, content)
        media_type = str(artifact.get("media_type") or "")
        if media_type == ArtifactStore.STRUCTURED_MEDIA_TYPE:
            if not isinstance(content, dict):
                raise ValueError("Structured artifact revisions require an object payload")
            required = {"renderer", "document", "schema_version"}
            if not required <= set(content):
                raise ValueError("Structured artifact revisions require renderer, document, and schema_version")
            return self.artifact_store.replace_structured(
                artifact_id=artifact_id,
                renderer=str(content["renderer"]),
                document=content["document"],
                schema_version=str(content["schema_version"]),
            )
        if media_type.startswith("text/"):
            text = content.get("content") if isinstance(content, dict) else content
            if not isinstance(text, str):
                raise ValueError("Text artifact revisions require string content")
            return self.artifact_store.replace_text(artifact_id=artifact_id, content=text)
        return artifact

    @staticmethod
    def _validate_artifact_projection(artifact: dict[str, Any], content: Any) -> None:
        media_type = str(artifact.get("media_type") or "")
        if media_type == ArtifactStore.STRUCTURED_MEDIA_TYPE:
            if not isinstance(content, dict):
                raise ValueError("Structured artifact revisions require an object payload")
            required = {"renderer", "document", "schema_version"}
            if not required <= set(content):
                raise ValueError("Structured artifact revisions require renderer, document, and schema_version")
            renderer = str(content.get("renderer") or "").strip().casefold()
            if renderer not in ArtifactStore.STRUCTURED_RENDERERS:
                raise ValueError(f"Unsupported structured artifact renderer: {renderer}")
            if not str(content.get("schema_version") or "").strip():
                raise ValueError("schema_version is required for structured artifacts")
            if not isinstance(content.get("document"), (dict, list)):
                raise ValueError("Structured artifact document must be an object or array")
        elif media_type.startswith("text/"):
            text = content.get("content") if isinstance(content, dict) else content
            if not isinstance(text, str):
                raise ValueError("Text artifact revisions require string content")

    def events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        return self.store.events_after(run_id, after_sequence)

    def environment(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id:
            persisted = self.store.environment_snapshot(run_id)
            if persisted is not None:
                return persisted
        service = self._service_provider()
        builder = service.snapshot_builder
        if builder is None:
            return {
                "schema_version": "open_stock_ai.environment_snapshot.v1",
                "available": False,
            }
        snapshot = self.store.get_run(run_id) if run_id else None
        request = dict((snapshot or {}).get("request") or {})
        active_run_id = str(run_id or "environment-preview")
        session_id = str(
            request.get("session_id")
            or (snapshot or {}).get("session_id")
            or "environment-preview"
        )
        autonomy = str(request.get("autonomy") or "advisory")
        context = AgentRunContext(
            run_id=active_run_id,
            session_id=session_id,
            parent_run_id=request.get("parent_run_id"),
            autonomy=autonomy,
            symbols=tuple(request.get("symbols") or ()),
            allow_paper_orders=autonomy in {"paper_execute", "full_execute"},
            allow_project_actions=autonomy in {"project_execute", "full_execute"},
            allow_external_actions=autonomy in {"external_execute", "full_execute"},
        )
        plan = self.get_plan(active_run_id)
        memories = (
            service.memory_manager.retrieve(
                str(request.get("objective") or ""),
                session_id=session_id,
            )
            if service.memory_manager is not None
            else []
        )
        return builder.build(
            context,
            plan=plan,
            pending_approvals=self.list_approvals(run_id) if run_id else [],
            memories=memories,
        ).to_dict()

    async def request_replan(self, run_id: str, *, instruction: str) -> dict[str, Any]:
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        if snapshot["terminal"] and snapshot["status"] != "max_steps_reached":
            raise ValueError("A terminal run cannot be replanned; create a new run in the same session")
        control = self.store.add_control_message(
            run_id,
            control_type="replan",
            payload={"instruction": instruction.strip()},
        )
        if snapshot["status"] in {"max_steps_reached", "partially_completed"}:
            continued = await self.continue_after_limit(run_id, additional_steps=6)
            return continued or control
        if snapshot["status"] == "suspended":
            await self.resume(run_id)
        await self._wake(run_id)
        return control

    def consume_controls(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.consume_control_messages(run_id)

    def save_workflow(
        self,
        *,
        name: str,
        plan: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.workflow_store is None:
            raise RuntimeError("Workflow store is unavailable")
        from open_stock_ai.agent_runtime.plan_graph import PlanGraph

        return self.workflow_store.save(
            namespace=self.namespace,
            name=name,
            plan=PlanGraph.from_dict(plan),
            metadata=metadata,
        )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        workflow = self.workflow_store.get(workflow_id) if self.workflow_store else None
        if workflow is not None and workflow.get("namespace") != self.namespace:
            return None
        return workflow

    async def run_workflow(
        self,
        workflow_id: str,
        *,
        objective: str | None = None,
        patch: dict[str, Any] | None = None,
        driver_id: str | None = None,
        autonomy: str = "advisory",
        session_id: str | None = None,
        parent_run_id: str | None = None,
        max_steps: int = 6,
        idempotency_key: str | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise KeyError(workflow_id)
        service = self._service_provider()
        runtime = WorkflowRuntime(service.tools.manifest())
        plan, compiled = runtime.instantiate(
            workflow,
            objective=objective,
            patch=patch,
            autonomy=autonomy,
        )
        if not compiled.get("valid"):
            raise ValueError(f"Workflow plan is invalid: {compiled.get('errors')}")
        return await self.create_run(
            objective=plan.objective,
            symbols=[],
            driver_id=driver_id,
            autonomy=autonomy,
            max_steps=max_steps,
            session_id=session_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
            initial_plan=plan.to_dict(),
            workflow_id=workflow_id,
            schedule_id=schedule_id,
        )

    def list_workflows(self) -> list[dict[str, Any]]:
        return self.workflow_store.list(self.namespace) if self.workflow_store else []

    async def stream(self, run_id: str, *, after_sequence: int = 0) -> AsyncIterator[dict[str, Any]]:
        if self.store.get_run(run_id) is None:
            raise KeyError(run_id)
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        async with self._subscriber_lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
        sequence = max(0, int(after_sequence))
        try:
            while True:
                for event in self.store.events_after(run_id, sequence):
                    event_sequence = int(event.get("sequence") or 0)
                    if event_sequence <= sequence:
                        continue
                    sequence = event_sequence
                    yield {"type": "activity", "event": event}

                snapshot = self.store.get_run(run_id)
                if snapshot is None:
                    raise KeyError(run_id)
                if snapshot["terminal"]:
                    # A recoverable partial/max-step checkpoint is only a
                    # durable boundary between two autonomous repair passes.
                    # Keep the SSE connection alive until the callback has
                    # reopened the same Run; otherwise the native Dock sees a
                    # false terminal result and stops displaying the repair.
                    recovery_task = self._recovery_tasks.get(run_id)
                    if (
                        (recovery_task is not None and not recovery_task.done())
                        or self._should_auto_continue_recovery(run_id)
                    ):
                        try:
                            await asyncio.wait_for(queue.get(), timeout=15.0)
                        except TimeoutError:
                            yield {
                                "type": "heartbeat",
                                "run_id": run_id,
                                "status": "repairing",
                                "last_sequence": sequence,
                            }
                        continue
                    yield _terminal_message(snapshot)
                    return
                if snapshot["status"] in {
                    "waiting_user_input",
                    "waiting_decision",
                    "waiting_approval",
                    "suspended",
                }:
                    yield {
                        "type": snapshot["status"],
                        "run_id": run_id,
                        "status": snapshot["status"],
                        "pending_approvals": self.list_approvals(run_id),
                        "plan": self.get_plan(run_id),
                    }
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {
                        "type": "heartbeat",
                        "run_id": run_id,
                        "status": snapshot["status"],
                        "last_sequence": sequence,
                    }
        finally:
            async with self._subscriber_lock:
                subscribers = self._subscribers.get(run_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(run_id, None)

    async def close(self) -> None:
        self._closing = True
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        self._scheduler_task = None
        if self._automation_poller_task is not None and not self._automation_poller_task.done():
            self._automation_poller_task.cancel()
            await asyncio.gather(self._automation_poller_task, return_exceptions=True)
        self._automation_poller_task = None
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        recovery_tasks = [task for task in self._recovery_tasks.values() if not task.done()]
        for task in recovery_tasks:
            task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        self._recovery_tasks.clear()
        if self.worker_supervisor is not None:
            await self.worker_supervisor.close()

    async def _start_steering_runs(
        self,
        *,
        session_id: str,
        parent_run_id: str | None,
        branch_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Execute steering-created Branches as durable same-Session Runs.

        Independent Branches are scheduled together; each Run owns its real
        provider lifecycle and Task Forest while the originating Branch keeps
        a durable link for local pause/resume/cancel controls.
        """

        if not branch_ids:
            return []
        parent = self.get_run(parent_run_id) if parent_run_id else None

        async def start_branch(branch_id: str) -> dict[str, Any]:
            branch = self.final_runtime.branch(branch_id)
            if branch is None:
                raise KeyError(branch_id)
            existing_run_id = str(branch.get("follow_run_id") or "")
            if existing_run_id:
                existing = self.get_run(existing_run_id)
                if existing is not None:
                    return existing
            objective = str(branch.get("objective") or "").strip()
            run = await self.create_run(
                objective=objective,
                symbols=list((parent or {}).get("symbols") or []),
                driver_id=str((parent or {}).get("driver") or "") or None,
                autonomy=str((parent or {}).get("autonomy") or "advisory"),
                max_steps=int((parent or {}).get("max_steps") or 12),
                session_id=session_id,
                parent_run_id=parent_run_id,
                idempotency_key=f"steering-branch:{branch_id}",
                run_metadata={
                    "source_branch_id": branch_id,
                    "source": "mid_run_steering",
                    "objective_version_id": branch.get("objective_id"),
                },
            )
            self.final_runtime.link_branch_run(branch_id, str(run["run_id"]))
            return run

        return list(await asyncio.gather(*(start_branch(branch_id) for branch_id in branch_ids)))

    def _sync_source_branch(
        self,
        run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        snapshot = self.store.get_run(run_id)
        metadata = dict(((snapshot or {}).get("request") or {}).get("metadata") or {})
        branch_id = str(metadata.get("source_branch_id") or "")
        if not branch_id:
            return
        self.final_runtime.set_linked_branch_run_status(
            branch_id,
            run_id=run_id,
            status=status,
            result=result,
            error=error,
        )

    async def _scheduler_loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            await self._drain_runtime_events(now)
            if (
                self._next_runtime_event_compaction_at is None
                or now >= self._next_runtime_event_compaction_at
            ):
                await asyncio.to_thread(
                    self.store.compact_processed_runtime_events,
                    keep_recent=2_000,
                )
                self._next_runtime_event_compaction_at = now + timedelta(hours=1)
            if (
                self._next_automation_submission_recovery_at is None
                or now >= self._next_automation_submission_recovery_at
            ):
                # The first startup probe can run before Uvicorn begins
                # accepting the loopback callback. Keep approved, durable n8n
                # submissions pending and retry them after the app becomes
                # reachable instead of requiring another desktop restart.
                await asyncio.to_thread(
                    self.final_runtime.automations.recover_pending_submissions,
                    now=now,
                )
                self._next_automation_submission_recovery_at = now + timedelta(seconds=15)
            if self._automation_poller_task is not None and self._automation_poller_task.done():
                # Consume an unexpected task exception before starting the
                # next durable polling cycle. Per-schedule failures are
                # already receipts and normally do not escape the poller.
                if not self._automation_poller_task.cancelled():
                    self._automation_poller_task.exception()
            if self._automation_poller_task is None or self._automation_poller_task.done():
                self._automation_poller_task = asyncio.create_task(
                    self.final_runtime.automations.poll_due_schedules_async(
                        reanalyze=self._automation_reanalyze,
                        now=now,
                    ),
                    name="agent-product-automation-poller",
                )
            schedules = self.store.claim_due_schedules(
                now.isoformat(),
                owner=self._runtime_id,
                lease_expires_at=(now + timedelta(seconds=30)).isoformat(),
            )
            for schedule in schedules:
                payload = schedule["payload"]
                try:
                    if is_market_closed(now, payload, self.schedule_planner.market_calendar):
                        next_run_value = self.schedule_planner.next_occurrence(payload, after=now)
                        self.store.schedule_deferred(
                            schedule["schedule_id"],
                            next_run_at=next_run_value.isoformat() if next_run_value else None,
                        )
                        continue
                    expires_at = payload.get("expires_at")
                    if expires_at and _parse_time(str(expires_at)) <= now:
                        self.store.disable_schedule(schedule["schedule_id"])
                        continue
                    fire_key = str(schedule.get("next_run_at") or now.isoformat())
                    if payload.get("workflow_id"):
                        run = await self.run_workflow(
                            str(payload["workflow_id"]),
                            objective=str(payload.get("objective") or "") or None,
                            driver_id=str(payload.get("driver_id") or "") or None,
                            autonomy="advisory",
                            session_id=payload.get("session_id"),
                            max_steps=int(payload.get("max_steps") or 6),
                            idempotency_key=f"schedule:{schedule['schedule_id']}:{fire_key}",
                            schedule_id=schedule["schedule_id"],
                        )
                    else:
                        run = await self.create_run(
                            objective=str(payload["objective"]),
                            symbols=payload.get("symbols") or [],
                            driver_id=str(payload.get("driver_id") or "") or None,
                            autonomy="advisory",
                            max_steps=int(payload.get("max_steps") or 6),
                            idempotency_key=f"schedule:{schedule['schedule_id']}:{fire_key}",
                            schedule_id=schedule["schedule_id"],
                        )
                    trigger_type = str(payload.get("trigger_type") or "one_shot")
                    interval = int(payload.get("interval_seconds") or 0)
                    if trigger_type == "interval" and interval >= 60:
                        next_run_value = self.schedule_planner.next_occurrence(payload, after=now)
                        next_run = next_run_value.isoformat() if next_run_value else None
                    elif trigger_type == "cron":
                        next_run_value = self.schedule_planner.next_occurrence(payload, after=now)
                        next_run = next_run_value.isoformat() if next_run_value else None
                    else:
                        next_run = None
                    self.store.schedule_fired(
                        schedule["schedule_id"],
                        run_id=run["run_id"],
                        next_run_at=next_run,
                    )
                except asyncio.CancelledError:
                    self.store.release_schedule_claim(
                        schedule["schedule_id"],
                        owner=self._runtime_id,
                    )
                    raise
                except Exception:
                    self.store.release_schedule_claim(
                        schedule["schedule_id"],
                        owner=self._runtime_id,
                    )
            await asyncio.sleep(1.0)

    async def _drain_runtime_events(self, now: datetime) -> int:
        events = self.store.claim_runtime_events(
            owner=self._runtime_id,
            at=now.isoformat(),
            lease_expires_at=(now + timedelta(seconds=30)).isoformat(),
        )
        processed = 0
        for event in events:
            try:
                await self.trigger_schedule_event(
                    str(event["event_type"]),
                    dict(event.get("payload") or {}),
                )
            except asyncio.CancelledError:
                self.store.release_runtime_event(
                    event["event_id"],
                    owner=self._runtime_id,
                    error={"type": "CancelledError"},
                )
                raise
            except Exception as exc:
                self.store.release_runtime_event(
                    event["event_id"],
                    owner=self._runtime_id,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            else:
                self.store.complete_runtime_event(
                    event["event_id"],
                    owner=self._runtime_id,
                )
                processed += 1
        return processed

    @staticmethod
    def _enforce_host_completion_gate(
        result: dict[str, Any],
        *,
        resume_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Prevent a continuation from erasing an unresolved failed branch.

        Providers are allowed to propose a final answer, but only the Host can
        decide that a Run is green.  Continuation preparation clears the
        previous result row by design; retain only the failure ledger needed to
        validate the new result and merge it back into the public receipt when
        the provider omitted it.
        """

        if (
            resume_state is None
            or not isinstance(result, dict)
            or str(result.get("status") or "") != "completed"
        ):
            return result
        if _is_verified_explicit_local_paper_completion(result):
            # An explicit paper-training order is a narrow, Host-validated
            # local operation.  A stale earlier research failure must not
            # reopen it after the inner orchestrator has verified preview,
            # durable order persistence and execution.  The normal gate below
            # remains strict for every other continuation.
            return result
        # Minimal test doubles and legacy providers may return only a prose
        # status. The production Orchestrator always supplies its structured
        # trace/validation contract; apply the strict cross-continuation gate
        # only to that contract so old durable migration fixtures remain
        # readable while real Runs cannot erase a failure ledger.
        if not any(
            key in result for key in ("tool_trace", "completion_validation", "execution_mode")
        ):
            return result
        context = dict((resume_state or {}).get("recovery_context") or {})
        if context.get("historical_reclassification") or context.get("explicit_replan_requested"):
            return result
        prior_trace = [
            dict(item)
            for item in context.get("tool_trace") or []
            if isinstance(item, dict)
        ]
        current_trace = [
            dict(item)
            for item in result.get("tool_trace") or []
            if isinstance(item, dict)
        ]
        # Preserve the earlier failure receipt unless the current provider
        # returned the same durable record already.
        seen = {
            str(item.get("error_id") or item.get("call_id") or item.get("node_id") or "")
            for item in current_trace
        }
        merged_trace = list(current_trace)
        for item in prior_trace:
            identity = str(item.get("error_id") or item.get("call_id") or item.get("node_id") or "")
            if item.get("ok") is False and identity and identity not in seen:
                merged_trace.append(item)
        # A bounded provider/Host transport retry is different from an
        # unresolved market or mutation branch.  When the continuation itself
        # reaches a Host-validated final result, that result is durable proof
        # that the earlier runtime boundary recovered.  Keep the failure
        # receipt for audit, but link it to this successful continuation so a
        # completed P89-style analysis cannot expand its step budget forever.
        # Do not infer this for tool failures: they still require their own
        # ``recovery_for`` evidence and remain subject to the strict gate.
        completion_validation = result.get("completion_validation")
        continuation_is_validated = (
            isinstance(completion_validation, dict)
            and completion_validation.get("passed") is True
            and bool(current_trace)
        )
        if continuation_is_validated:
            for item in prior_trace:
                if item.get("ok") is not False:
                    continue
                failed_node_id = str(item.get("node_id") or "").strip()
                recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
                if (
                    not failed_node_id
                    or str(item.get("tool") or "") not in {"host.runtime", "model.provider"}
                    or str(recovery.get("action") or "")
                    not in {"retry_from_durable_checkpoint", "retry_after_provider_reachable"}
                ):
                    continue
                merged_trace.append(
                    {
                        "node_id": f"host-recovery-{failed_node_id}",
                        "tool": "host.runtime_recovery",
                        "ok": True,
                        "result": {
                            "status": "recovered",
                            "evidence": "validated_continuation_completion",
                        },
                        "recovery_for": [
                            {
                                "failed_node_id": failed_node_id,
                                "reason": "validated_continuation_completion",
                            }
                        ],
                    }
                )
        recovered = {
            str(link.get("failed_node_id") or "")
            for item in merged_trace
            if item.get("ok") is True
            for link in (item.get("recovery_for") or [])
            if isinstance(link, dict)
        }
        unresolved = sorted({
            str(item.get("node_id") or item.get("call_id") or "")
            for item in merged_trace
            if item.get("ok") is False
            and str(item.get("node_id") or item.get("call_id") or "").strip()
            and str(item.get("node_id") or item.get("call_id") or "") not in recovered
        })
        unresolved.extend(
            str(item)
            for item in context.get("pending_recovery_node_ids") or []
            if str(item).strip() and str(item) not in unresolved and str(item) not in recovered
        )
        if not unresolved:
            # Persist merged historical receipts and the recovery link.  Returning
            # the original ``result`` here would make this call look green now,
            # but discard the link before the next durable restart and reopen the
            # same host-runtime failure again.
            return {**result, "tool_trace": merged_trace}
        return {
            **result,
            "status": "partially_completed",
            "summary": (
                "Host 保留已完成證據，但拒絕在受影響分支尚未修復時標示完成；"
                "已帶回 Error Receipt 並繼續局部恢復。"
            ),
            "decision": None,
            "structured_result": None,
            "interaction_proposals": [],
            "tool_trace": merged_trace,
            "recovery_pending": True,
            "pending_recovery_node_ids": unresolved,
            "recovery_state": "host_completion_gate_blocked",
            "host_completion_gate": {
                "accepted": False,
                "reason": "unresolved_recovery",
                "unresolved_node_ids": unresolved,
                "preserve_completed_work": True,
            },
        }
    def _execution_forest_identity(self, run_id: str, *, session_id: str) -> dict[str, str]:
        """Load the Run's durable Task Forest identity before provider work.

        The SQLite Forest is created before the background provider task is
        scheduled.  Supplying its IDs to the in-memory executor prevents a
        second `TF-*`/root `BR-*` authority from being invented for the same
        Run, including after a checkpoint recovery.
        """

        forest = self.final_runtime.forest(session_id, run_id=run_id)
        if forest is None:
            raise RuntimeError(f"Missing durable Task Forest for Agent Run {run_id}")
        forest_id = str(forest.get("forest_id") or "").strip()
        root_branch_id = str(forest.get("root_branch_id") or "").strip()
        objective_id = str(forest.get("objective_id") or "").strip()
        if not forest_id or not root_branch_id or not objective_id:
            raise RuntimeError(f"Incomplete durable Task Forest identity for Agent Run {run_id}")
        return {
            "forest_id": forest_id,
            "root_branch_id": root_branch_id,
            "objective_id": objective_id,
        }

    async def _execute(
        self,
        run_id: str,
        request: dict[str, Any],
        *,
        resume_state: dict[str, Any] | None = None,
    ) -> None:
        execution_started_monotonic = time.monotonic()
        self.store.mark_running(run_id)
        execution_snapshot = self.store.get_run(run_id) or {}
        # A continuation reuses its durable Run ID but is a distinct provider
        # execution.  SLO observations are immutable, so binding all attempts
        # to ``agent-run:{run_id}`` made the second attempt conflict with the
        # first measured latency/status and silently lose observability.
        execution_slo_observation_id = (
            f"agent-run:{run_id}:resume-{int(execution_snapshot.get('resume_count') or 0)}:"
            f"started-{execution_snapshot.get('updated_at') or 'unknown'}"
        )
        self.final_runtime.set_run_status(run_id, "running")
        self._sync_source_branch(run_id, "running")
        # Persist the host-owned running state before any provider lifecycle
        # call. A provider may need time to create its remote session, and the
        # Dock must not keep presenting an accepted run as merely queued.
        await self._append_host_event(
            run_id,
            "run.started",
            {
                "summary": "Host accepted the run and is preparing the selected model.",
                "driver": request.get("driver_id"),
                "phase": "provider_preparation",
            },
        )
        event_sequence = int(
            (self.store.get_run(run_id) or {}).get("last_sequence") or 0
        )

        async def event_sink(event: dict[str, Any]) -> None:
            nonlocal event_sequence

            async def persist(item: dict[str, Any]) -> bool:
                if not self.store.append_event(run_id, item):
                    return False
                await self._wake(run_id)
                if not request.get("schedule_id") and not (
                    dict(request.get("metadata") or {}).get("automation_trigger_id")
                ):
                    await self.trigger_schedule_event(
                        str(item.get("type") or "agent.event"),
                        item,
                    )
                return True

            lock = self._event_locks.setdefault(run_id, asyncio.Lock())
            async with lock:
                normalized = dict(event)
                incoming = int(normalized.get("sequence") or 0)
                persisted = int(
                    (self.store.get_run(run_id) or {}).get("last_sequence") or 0
                )
                event_sequence = max(event_sequence + 1, incoming, persisted + 1)
                normalized["sequence"] = event_sequence
                normalized["run_id"] = run_id
                projection = self.final_runtime.project_runtime_event(run_id, normalized)
                if (
                    str(normalized.get("type") or "") == "checkpoint.created"
                    and self.checkpoint_manager is not None
                ):
                    legacy_checkpoint = self.checkpoint_manager.restore(run_id)
                    if legacy_checkpoint is not None:
                        self.final_runtime.persist_runtime_checkpoint(
                            session_id=str(request.get("session_id") or run_id),
                            run_id=run_id,
                            checkpoint=legacy_checkpoint,
                            sequence=event_sequence,
                            branch_id=projection.branch_id,
                            step_id=projection.step_id,
                        )
                if projection.forest_id:
                    normalized["forest_id"] = projection.forest_id
                if projection.branch_id:
                    normalized["branch_id"] = projection.branch_id
                if projection.step_id:
                    payload = dict(normalized.get("payload") or {})
                    payload["durable_step_id"] = projection.step_id
                    normalized["payload"] = payload
                    normalized["durable_step_id"] = projection.step_id
                if not await persist(normalized):
                    return

                for projected in projection.events:
                    event_sequence += 1
                    projected_payload = dict(projected.get("payload") or {})
                    if projected.get("node_id"):
                        projected_payload.setdefault("node_id", projected["node_id"])
                    projected_event = build_runtime_event(
                        str(projected.get("type") or "branch.updated"),
                        sequence=event_sequence,
                        run_id=run_id,
                        session_id=str(request.get("session_id") or run_id),
                        payload=projected_payload,
                        plan_id=normalized.get("plan_id"),
                        plan_revision=normalized.get("plan_revision"),
                        previous_event_id=str(normalized.get("event_id") or "") or None,
                        forest_id=projection.forest_id,
                        branch_id=(
                            str(projected.get("branch_id"))
                            if projected.get("branch_id")
                            else projection.branch_id
                        ),
                    )
                    self.final_runtime.project_runtime_event(run_id, projected_event)
                    await persist(projected_event)

        try:
            session_history = []
            if self.session_store is not None and request.get("session_id"):
                session_history = [
                    {
                        "role": message["role"],
                        "content": message["content"],
                        "created_at": message["created_at"],
                        "source": message["source"],
                    }
                    for message in self.session_store.messages(
                        str(request["session_id"]),
                        limit=40,
                    )
                    if (
                        message.get("run_id") != run_id
                        or (message.get("source") or {}).get("type")
                        == "interaction_response"
                    )
                ]
            execution_forest = self._execution_forest_identity(
                run_id,
                session_id=str(request.get("session_id") or run_id),
            )
            provider_resume_state = dict(resume_state or {})
            provider_resume_state["execution_forest"] = execution_forest
            result = await self._service_provider().run(
                objective=request["objective"],
                symbols=request["symbols"],
                driver_id=request["driver_id"],
                autonomy=request["autonomy"],
                max_steps=request["max_steps"],
                run_id=run_id,
                session_id=request.get("session_id"),
                parent_run_id=request.get("parent_run_id"),
                initial_plan=request.get("initial_plan") if isinstance(request.get("initial_plan"), dict) else None,
                resume_state=provider_resume_state,
                session_history=session_history,
                event_sink=event_sink,
                initial_sequence=int(
                    (self.store.get_run(run_id) or {}).get("last_sequence") or 0
                ),
            )
            result = self._enforce_host_completion_gate(result, resume_state=resume_state)
        except asyncio.CancelledError:
            if run_id in self._pause_requested:
                self._pause_requested.discard(run_id)
                self.store.pause_run(
                    run_id,
                    status="suspended",
                    error={"type": "UserPaused", "message": "Run paused by the user."},
                )
                self.final_runtime.set_run_status(run_id, "suspended")
                self._sync_source_branch(
                    run_id,
                    "suspended",
                    error={"type": "UserPaused", "message": "Run paused by the user."},
                )
                await self._append_host_event(
                    run_id,
                    "run.paused",
                    {"summary": "Run paused by the user."},
                )
            elif self._closing and not self.store.get_run(run_id).get("cancel_requested"):
                self.store.pause_run(
                    run_id,
                    status="suspended",
                    error={
                        "type": "SafeShutdown",
                        "message": "The application shut down after saving a checkpoint.",
                    },
                )
                self._sync_source_branch(
                    run_id,
                    "suspended",
                    error={
                        "type": "SafeShutdown",
                        "message": "The application shut down after saving a checkpoint.",
                    },
                )
            else:
                cancel_reason = self._cancel_reasons.pop(
                    run_id,
                    "Run cancelled by the user.",
                )
                self.store.cancel_run(run_id)
                self.final_runtime.set_run_status(run_id, "cancelled")
                self._sync_source_branch(
                    run_id,
                    "cancelled",
                    error={"type": "CancelledError", "message": cancel_reason},
                )
                await self._append_host_event(
                    run_id,
                    "run.cancelled",
                    {"summary": cancel_reason},
                )
            await self._wake(run_id)
            raise
        except Exception as exc:
            # P1/P40: Host and persistence failures are evidence about a
            # local branch, never evidence that the user's whole objective is
            # impossible.  Persist a public Error Receipt and leave a bounded
            # checkpoint recovery path; _task_finished will either continue
            # it or create the L8 decision card after its safe budget.
            snapshot = self.store.get_run(run_id) or {}
            previous_result = (
                dict(snapshot.get("result") or {})
                if isinstance(snapshot.get("result"), dict)
                else {}
            )
            checkpoint_payload = dict(
                dict((resume_state or {}).get("checkpoint") or {}).get("payload") or {}
            )
            checkpoint_trace = checkpoint_payload.get("trace", [])
            trace = list(previous_result.get("tool_trace") or checkpoint_trace or [])
            provider_transport_failure = self._is_terminal_model_transport_failure(
                run_id,
                exc,
            )
            receipt_id = f"ERR-host-{uuid4().hex}"
            failure_node_id = (
                f"model-provider-connectivity-{int(snapshot.get('current_step') or 0) + 1}"
                if provider_transport_failure
                else f"host-runtime-{int(snapshot.get('current_step') or 0) + 1}"
            )
            error = {"type": type(exc).__name__, "message": str(exc)}
            trace.append(
                {
                    "node_id": failure_node_id,
                    "tool": "model.provider" if provider_transport_failure else "host.runtime",
                    "ok": False,
                    "error_id": receipt_id,
                    "error": error,
                    "recovery": {
                        "action": (
                            "retry_after_provider_reachable"
                            if provider_transport_failure
                            else "retry_from_durable_checkpoint"
                        ),
                        "reason": (
                            "The configured model provider could not be reached after its bounded transport retry."
                            if provider_transport_failure
                            else "The Host detected a recoverable runtime/persistence error."
                        ),
                        "preserve_completed_work": True,
                    },
                }
            )
            recovery_result = {
                **previous_result,
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "status": "partially_completed",
                "summary": (
                    "Agent 無法連線到目前設定的模型服務；已保留完成工作與 checkpoint，"
                    "等待模型恢復或使用者指定下一步。"
                    if provider_transport_failure
                    else "Agent 偵測到 Host 執行錯誤，已保留完成工作與 checkpoint，正改用修復流程。"
                ),
                "tool_trace": trace,
                "recovery_state": (
                    "autonomous_strategies_exhausted"
                    if provider_transport_failure
                    else "host_exception_pending"
                ),
                "error_receipt": {
                    "error_id": receipt_id,
                    "category": "model_provider_connectivity" if provider_transport_failure else "host_runtime",
                    "error": error,
                    "failed_node_id": failure_node_id,
                    "next_action": (
                        "retry_after_provider_reachable"
                        if provider_transport_failure
                        else "retry_from_durable_checkpoint"
                    ),
                    "preserve_completed_work": True,
                },
            }
            self.store.complete_run(run_id, recovery_result)
            self.final_runtime.set_run_status(
                run_id,
                "partially_completed",
                result=recovery_result,
            )
            self._sync_source_branch(
                run_id,
                "partially_completed",
                result=recovery_result,
                error=error,
            )
            await self._append_host_event(
                run_id,
                "error.receipt.created",
                {
                    "summary": (
                        "Configured model provider is unreachable after a bounded retry; completed work is preserved for a user-visible recovery choice."
                        if provider_transport_failure
                        else "Host error recorded; completed work is preserved for bounded recovery."
                    ),
                    "error_receipt": recovery_result["error_receipt"],
                },
            )
            await self._append_host_event(
                run_id,
                "run.partially_completed",
                {
                    "summary": recovery_result["summary"],
                    "status": "partially_completed",
                    "preserve_completed_work": True,
                },
            )
            await self._wake(run_id)
        else:
            if result.get("status") in {
                "waiting_user_input",
                "waiting_decision",
                "waiting_approval",
            }:
                waiting_status = str(result["status"])
                self.store.pause_run(
                    run_id,
                    status=waiting_status,
                    error={
                        "type": (
                            "ApprovalRequired"
                            if waiting_status == "waiting_approval"
                            else "UserInteractionRequired"
                        ),
                        "approval": result.get("pending_approval"),
                        "interaction": result.get("pending_interaction"),
                    },
                )
                self.final_runtime.set_run_status(run_id, waiting_status)
                self._sync_source_branch(run_id, waiting_status, result=result)
            else:
                if result.get("host_completion_gate", {}).get("accepted") is False:
                    await self._append_host_event(
                        run_id,
                        "completion.rejected",
                        {
                            "summary": "Host rejected a green completion because a local recovery branch is unresolved.",
                            **dict(result.get("host_completion_gate") or {}),
                        },
                    )
                proposal_context = {
                    **result,
                    # Provider adapters normally return the objective, but
                    # durable test/custom adapters may only return the final
                    # answer. The persisted request remains authoritative for
                    # the user's terminal boundary in either case.
                    "objective": result.get("objective") or request.get("objective") or "",
                }
                allowed_proposals, suppressed_proposals = _filter_post_answer_proposals(proposal_context)
                if suppressed_proposals:
                    result = {**result, "interaction_proposals": allowed_proposals}
                    await self._append_host_event(
                        run_id,
                        "interaction.proposals.suppressed",
                        {
                            "summary": "Run 已依使用者明確邊界完成；Host 未建立額外下一步或 Automation 提案。",
                            "suppressed_actions": [
                                str(item.get("action") or "") for item in suppressed_proposals
                            ],
                            "reason": "objective_disallows_post_answer_interaction",
                        },
                    )
                model_automation_proposals = await self._materialize_model_automation_proposals(
                    run_id,
                    result,
                )
                model_follow_up_proposals = await self._materialize_model_follow_up_proposals(
                    run_id,
                    result,
                )
                if model_automation_proposals:
                    result = {
                        **result,
                        "automation_proposals": model_automation_proposals,
                    }
                if model_follow_up_proposals:
                    result = {
                        **result,
                        "post_answer_interactions": model_follow_up_proposals,
                    }
                self.store.complete_run(run_id, result)
                self.final_runtime.set_run_status(
                    run_id,
                    str(result.get("status") or "completed"),
                    result=result,
                )
                self._sync_source_branch(
                    run_id,
                    str(result.get("status") or "completed"),
                    result=result,
                )
                await self._append_host_event(
                    run_id,
                    "result.final",
                    {
                        "summary": str(result.get("summary") or "Agent run completed."),
                        "status": str(result.get("status") or "completed"),
                        "completion_validation": result.get("completion_validation"),
                        "recovery_pending": bool(result.get("recovery_pending")),
                        "pending_recovery_node_ids": list(
                            result.get("pending_recovery_node_ids") or []
                        ),
                    },
                )
                if self.session_store is not None:
                    self.session_store.add_message(
                        session_id=str(request.get("session_id") or run_id),
                        run_id=run_id,
                        role="assistant",
                        content={
                            "summary": result.get("summary"),
                            "decision": result.get("decision"),
                            "status": result.get("status"),
                            "interaction_proposals": list(result.get("interaction_proposals") or []),
                        },
                        source={
                            "type": "validated_agent_run",
                            "completion_validation": result.get("completion_validation"),
                        },
                        set_active_run=not bool(request.get("parent_run_id")),
                    )
            await self._wake(run_id)
        finally:
            # Observability must record terminal outcomes, including failed,
            # cancelled, paused and partially-completed runs. A telemetry
            # persistence failure is deliberately made visible as a Host
            # event.  That preserves an already-durable Agent result while
            # ensuring an absent SLO measurement is never mistaken for a
            # successful observation.
            terminal_snapshot = self.store.get_run(run_id) or {}
            terminal_status = str(terminal_snapshot.get("status") or "failed")
            try:
                self._record_agent_slo(
                    run_id=run_id,
                    started_monotonic=execution_started_monotonic,
                    status=terminal_status,
                    observation_id=execution_slo_observation_id,
                )
            except Exception as exc:
                await self._append_host_event(
                    run_id,
                    "slo.record_failed",
                    {
                        "summary": "SLO evidence could not be persisted after the Agent Run completed.",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )

    async def _append_host_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        lock = self._event_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            snapshot = self.store.get_run(run_id)
            if snapshot is None:
                return None
            if event_type in {"artifact.created", "artifact.updated"}:
                artifact_id = str(payload.get("artifact_id") or (payload.get("artifact") or {}).get("artifact_id") or "")
                artifact = self.artifact(run_id, artifact_id) if artifact_id else None
                if artifact is not None:
                    if event_type == "artifact.created":
                        version = self.final_runtime.ensure_artifact_version(
                            artifact_id,
                            _artifact_version_content(artifact),
                            changed_by="host_artifact_projection",
                        )
                    else:
                        history = self.final_runtime.artifact_history(artifact_id)
                        version = history[-1] if history else self.final_runtime.ensure_artifact_version(
                            artifact_id,
                            _artifact_version_content(artifact),
                            changed_by="host_artifact_projection",
                        )
                    payload = {
                        **payload,
                        "artifact_id": artifact_id,
                        "artifact": {
                            **artifact,
                            "version": int(version["version"]),
                            "artifact_version": int(version["version"]),
                        },
                    }
            plan = self.get_plan(run_id)
            event = build_runtime_event(
                event_type,
                sequence=int(snapshot.get("last_sequence") or 0) + 1,
                run_id=run_id,
                session_id=str(snapshot.get("session_id") or run_id),
                payload=payload,
                plan_id=(plan or {}).get("plan_id"),
                plan_revision=(plan or {}).get("revision_number"),
            )
            self.final_runtime.project_runtime_event(run_id, event)
            self.store.append_event(run_id, event)
            await self._wake(run_id)
            return event

    async def _materialize_model_automation_proposals(
        self,
        run_id: str,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Turn a completed Agent proposal into a semantic confirmation card.

        OpportunityDetector is intentionally not called here. The Agent has
        already researched the objective and returned a structured future
        intent; Host validates that contract, renders it, and waits for an
        explicit confirmation before any compiler/backend operation.
        """

        snapshot = self.store.get_run(run_id) or {}
        request = dict(snapshot.get("request") or {})
        session_id = str(snapshot.get("session_id") or request.get("session_id") or "")
        if not session_id:
            return []
        branch_id = str((request.get("metadata") or {}).get("source_branch_id") or "") or None
        output: list[dict[str, Any]] = []
        for proposal in result.get("interaction_proposals") or []:
            if not isinstance(proposal, dict):
                continue
            if str(proposal.get("action") or "").strip().casefold() != "draft_automation":
                continue
            arguments = dict(proposal.get("arguments") or {})
            intent_payload = arguments.get("intent") or arguments.get("automation_intent")
            if intent_payload is None and isinstance(arguments.get("intent_json"), str):
                try:
                    decoded_intent = json.loads(str(arguments["intent_json"]))
                except json.JSONDecodeError:
                    decoded_intent = None
                intent_payload = decoded_intent
            if intent_payload is None:
                # Compatibility for the pre-v1 provider contract used by
                # deterministic tests and already-persisted proposals.
                intent_payload = arguments
            if not isinstance(intent_payload, dict):
                continue
            intent_payload = {
                **intent_payload,
                "goal": str(intent_payload.get("goal") or proposal.get("title") or result.get("objective") or "").strip(),
                "user_id": str(intent_payload.get("user_id") or "stock_ai_local_user"),
                "session_id": str(intent_payload.get("session_id") or session_id),
            }
            try:
                automation_proposal, automation_intent = self.final_runtime.automations.propose_semantic_intent(
                    intent_payload,
                    reason=str(proposal.get("reason") or ""),
                    title=str(proposal.get("title") or ""),
                )
            except (TypeError, ValueError) as exc:
                await self._append_host_event(
                    run_id,
                    "automation.proposal.rejected",
                    {
                        "summary": "Agent 的語意 Automation Intent 未通過 Host schema 驗證。",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                        "proposal": proposal,
                    },
                )
                continue
            proposal_record = self.final_runtime.record_user_proposal(
                session_id=session_id,
                message_id=str((request.get("metadata") or {}).get("message_id") or run_id),
                branch_id=branch_id,
                proposal_type="automation",
                proposal={
                    "content": result.get("objective") or automation_intent.goal,
                    "source": "agent_post_answer_interaction_proposal",
                    "opportunity": asdict(automation_proposal.opportunity),
                    "natural_language": automation_proposal.natural_language,
                    "host_status": automation_proposal.host_status,
                    "artifact_preview": dict(automation_proposal.artifact_preview),
                    "intent": automation_intent.to_semantic_dict(),
                },
                evaluation={
                    "decision": "ask",
                    "risk_level": "future",
                    "impact_level": "future",
                    "requires_user_decision": True,
                },
            )
            interaction = self.final_runtime.create_interaction(
                session_id=session_id,
                run_id=run_id,
                branch_id=branch_id,
                waiting_state="waiting_decision",
                payload={
                    "kind": "proposal",
                    "prompt": "Agent 已完成目前回答；要建立這個語意 Automation Plan 嗎？確認後才會 compile、validate、dry-run。",
                    "agent_view": automation_proposal.natural_language,
                    "preferred_option": "confirm",
                    "options": [
                        {"option_id": "confirm", "label": "確認建立", "reason": "先驗證與試跑後才啟用", "recommended": True},
                        {"option_id": "skip", "label": "先不建立", "reason": "保留目前答案，不建立未來喚醒", "recommended": False},
                    ],
                    "proposal_id": proposal_record["proposal_id"],
                    "interaction_purpose": "automation_confirmation",
                    "automation_intent": automation_intent.to_semantic_dict(),
                    "artifact_preview": dict(automation_proposal.artifact_preview),
                },
            )
            artifact = None
            if self.artifact_store is not None:
                preview = dict(automation_proposal.artifact_preview)
                artifact = self.artifact_store.create_structured(
                    session_id=session_id,
                    run_id=run_id,
                    name=f"automation-plan-{proposal_record['proposal_id']}",
                    renderer=str(preview.get("renderer") or "workflow"),
                    document=preview,
                    schema_version=str(preview.get("schema_version") or "open_stock_ai.automation_artifact.v1"),
                    branch_id=branch_id,
                    metadata={"source": "agent_post_answer_interaction_proposal"},
                )
                await self._append_host_event(
                    run_id,
                    "artifact.created",
                    {"artifact_id": artifact.get("artifact_id"), "artifact": artifact},
                )
            payload = {
                "summary": automation_proposal.natural_language,
                "proposal": proposal_record,
                "interaction": interaction,
                "artifact": artifact or dict(automation_proposal.artifact_preview),
                "host_status": automation_proposal.host_status,
                "source": "agent_post_answer_interaction_proposal",
            }
            await self._append_host_event(run_id, "automation.proposed", payload)
            await self._append_host_event(
                run_id,
                "interaction.proposed",
                {
                    "interaction": interaction,
                    "proposal": proposal_record,
                    "requires_user_confirmation": True,
                },
            )
            output.append({
                "proposal": proposal_record,
                "interaction": interaction,
                "artifact": artifact or dict(automation_proposal.artifact_preview),
            })
        return output

    async def _materialize_model_follow_up_proposals(
        self,
        run_id: str,
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Persist every non-Automation post-answer proposal as a real card.

        A provider proposal is intentionally non-blocking: the current answer
        remains completed.  It still needs a durable Interaction ID and real
        choices, however, otherwise the frontend can only render a decorative
        card whose response endpoint does not exist.  Accepting one starts a
        new same-Session Run; declining it has no side effect.
        """

        snapshot = self.store.get_run(run_id) or {}
        request = dict(snapshot.get("request") or {})
        session_id = str(snapshot.get("session_id") or request.get("session_id") or "")
        if not session_id:
            return []
        branch_id = str((request.get("metadata") or {}).get("source_branch_id") or "") or None
        output: list[dict[str, Any]] = []
        for proposal in result.get("interaction_proposals") or []:
            if not isinstance(proposal, dict):
                continue
            action = str(proposal.get("action") or "ask").strip().casefold()
            if action == "draft_automation" or action not in {"ask", "follow_up", "create_artifact"}:
                continue
            title = str(proposal.get("title") or "").strip()
            reason = str(proposal.get("reason") or "").strip()
            if not title or not reason:
                continue
            arguments = dict(proposal.get("arguments") or {})
            interaction = self.final_runtime.create_interaction(
                session_id=session_id,
                run_id=run_id,
                branch_id=branch_id,
                waiting_state="waiting_decision",
                payload={
                    "kind": "proposal",
                    "interaction_purpose": "post_answer_proposal",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                    "proposal_action": action,
                    "proposal_arguments": arguments,
                    "prompt": title,
                    "agent_view": reason,
                    "preferred_option": "start_follow_up",
                    "options": [
                        {
                            "option_id": "start_follow_up",
                            "label": "開始下一步",
                            "reason": reason,
                            "recommended": True,
                        },
                        {
                            "option_id": "skip",
                            "label": "先略過",
                            "reason": "保留目前已完成的回答，不建立新的 Run。",
                            "recommended": False,
                        },
                    ],
                    "unknowns": [],
                    "important_risks": [
                        "只有確認後才會建立新的同 Session Run；目前答案不受影響。",
                    ],
                },
            )
            await self._append_host_event(
                run_id,
                "interaction.proposed",
                {
                    "interaction": interaction,
                    "proposal": {
                        "proposal_id": str(proposal.get("proposal_id") or ""),
                        "title": title,
                        "reason": reason,
                        "action": action,
                        "arguments": arguments,
                    },
                    "requires_user_confirmation": True,
                },
            )
            output.append({"proposal": dict(proposal), "interaction": interaction})
        return output
    async def _wake(self, run_id: str) -> None:
        async with self._subscriber_lock:
            subscribers = tuple(self._subscribers.get(run_id, ()))
        for queue in subscribers:
            if queue.empty():
                queue.put_nowait(None)

    def _task_finished(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                # ``_execute`` normally converts provider/Host errors into a
                # durable partial checkpoint.  A failure can still escape
                # from the outer task (for example an event projection,
                # persistence, or callback error).  Never silently return in
                # that case: a completed asyncio Task is otherwise invisible
                # to the recovery scheduler and the Dock appears to stop.
                self._schedule_recovery_followup(
                    run_id,
                    self._recover_from_task_exception(run_id, exception),
                    name=f"agent-task-exception-recovery:{run_id}",
                )
                return
            snapshot = self.store.get_run(run_id)
            metadata = dict(((snapshot or {}).get("request") or {}).get("metadata") or {})
            proposal_id = str(metadata.get("proposal_id") or "").strip()
            if proposal_id:
                self._schedule_recovery_followup(
                    run_id,
                    self._finalize_proposal_evaluation(run_id, proposal_id),
                    name=f"agent-proposal-evaluation:{run_id}",
                )
            if self._should_auto_continue_recovery(run_id):
                self._schedule_recovery_followup(
                    run_id,
                    self._continue_recovery_automatically(run_id),
                    name=f"agent-auto-recovery:{run_id}",
                )
            elif self._should_publish_nonblocking_recovery_boundary(run_id):
                self._schedule_recovery_followup(
                    run_id,
                    self._publish_nonblocking_recovery_boundary(run_id),
                    name=f"agent-recovery-boundary:{run_id}",
                )

    async def _recover_from_task_exception(
        self,
        run_id: str,
        exception: BaseException,
    ) -> None:
        """Turn an escaped runtime task exception into the normal recovery loop.

        The outer task boundary is the final safety net.  It must preserve the
        latest checkpoint and successful evidence, create the same structured
        failure receipt used by tool recovery, and then hand control back to
        ``continue_after_limit``.  This keeps unexpected Host failures
        visible and repairable instead of leaving a green/idle UI with no
        successor task.
        """

        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return
        if snapshot.get("cancel_requested") or snapshot.get("status") in {
            "cancelled",
            "waiting_user_input",
            "waiting_decision",
            "waiting_approval",
            "suspended",
        }:
            return
        result = dict(snapshot.get("result") or {}) if isinstance(snapshot.get("result"), dict) else {}
        trace = list(result.get("tool_trace") or [])
        failed_node_id = f"host-task-{int(snapshot.get('current_step') or 0) + 1}"
        error_id = f"ERR-task-{uuid4().hex}"
        error = {
            "type": type(exception).__name__,
            "message": str(exception) or "Agent execution task exited unexpectedly.",
        }
        trace.append(
            {
                "node_id": failed_node_id,
                "tool": "host.runtime.task_boundary",
                "call_id": error_id,
                "ok": False,
                "error_id": error_id,
                "error": error,
                "recovery": {
                    "action": "retry_from_durable_checkpoint",
                    "requires_model_replan": True,
                    "reason": "The outer Host task boundary caught an escaped exception.",
                    "preserve_completed_work": True,
                },
            }
        )
        recovery_result = {
            **result,
            "schema_version": "open_stock_ai.agent_run.v2",
            "run_id": run_id,
            "status": "partially_completed",
            "summary": "Agent 偵測到未預期的 Host 執行錯誤，已保留 checkpoint，正在分析原因並重新執行受影響分支。",
            "tool_trace": trace,
            "recovery_pending": True,
            "recovery_state": "task_exception_pending",
            "pending_recovery_node_ids": [failed_node_id],
            "error_receipt": {
                "error_id": error_id,
                "category": "host_task_boundary",
                "component": "durable_agent_runtime",
                "failed_node_id": failed_node_id,
                "error": error,
                "next_action": "retry_from_durable_checkpoint",
                "preserve_completed_work": True,
            },
        }
        self.store.complete_run(run_id, recovery_result)
        self.final_runtime.set_run_status(run_id, "partially_completed", result=recovery_result)
        self._sync_source_branch(
            run_id,
            "partially_completed",
            result=recovery_result,
            error=error,
        )
        await self._append_host_event(
            run_id,
            "error.receipt.created",
            {
                "summary": "Host task boundary failure captured; completed work is preserved.",
                "error_receipt": recovery_result["error_receipt"],
            },
        )
        await self._append_host_event(
            run_id,
            "repair.started",
            {
                "summary": "Host 正在分析未預期錯誤，並從最近 checkpoint 重新執行受影響分支。",
                "strategy": "retry_from_durable_checkpoint",
                "failed_node_id": failed_node_id,
                "preserve_completed_work": True,
            },
        )
        await self._append_host_event(
            run_id,
            "run.partially_completed",
            {
                "summary": recovery_result["summary"],
                "status": "partially_completed",
                "recovery_pending": True,
            },
        )
        await self._wake(run_id)
        if self._should_auto_continue_recovery(run_id):
            await self._continue_recovery_automatically(run_id)
        elif self._should_publish_nonblocking_recovery_boundary(run_id):
            await self._publish_nonblocking_recovery_boundary(run_id)

    async def _finalize_proposal_evaluation(self, run_id: str, proposal_id: str) -> None:
        """Turn a completed evidence branch into a public proposal decision."""

        await asyncio.sleep(0)
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        structured = result.get("structured_result") if isinstance(result.get("structured_result"), dict) else {}
        trace = result.get("tool_trace") if isinstance(result.get("tool_trace"), list) else []
        known_evidence = {
            str(value)
            for item in trace
            if isinstance(item, dict) and item.get("ok") is True
            for value in (item.get("call_id"), item.get("node_id"), (item.get("validation") or {}).get("evidence_hash"))
            if value
        }
        allowed = {"accept", "modify", "reject", "ask"}
        decision = str(structured.get("decision") or "ask").strip().casefold()
        if decision not in allowed:
            decision = "ask"
        evidence_ids = [
            str(item)
            for item in structured.get("evidence_ids") or []
            if str(item) in known_evidence
        ]
        if not structured or not evidence_ids:
            decision = "ask"
        evaluation = {
            "decision": decision,
            "evidence_compatible": structured.get("evidence_compatible") if isinstance(structured.get("evidence_compatible"), bool) else None,
            "risk_level": str(structured.get("risk_level") or "unknown"),
            "impact_level": str(structured.get("impact_level") or "unknown"),
            "recommendation": str(structured.get("recommendation") or result.get("summary") or "需補充證據後再決定。"),
            "rationale": [str(item) for item in structured.get("rationale") or [] if str(item).strip()],
            "safe_alternative": str(structured.get("safe_alternative") or "保留現行方案並補充證據。"),
            "evidence_ids": evidence_ids,
            "evaluation_run_id": run_id,
        }
        persisted = self.final_runtime.update_user_proposal_evaluation(
            proposal_id=proposal_id,
            evaluation=evaluation,
        )
        if persisted is None:
            return
        session_id = str(snapshot.get("session_id") or "")
        source_branch_id = str(((snapshot.get("request") or {}).get("metadata") or {}).get("source_branch_id") or "") or None
        preferred_option = "keep_current" if decision == "reject" else "apply_recommendation"
        apply_label = {
            "accept": "採用經證據支持的提案",
            "modify": "採用 Agent 修正版",
            "reject": "採用安全替代方案",
            "ask": "依 Agent 建議補證據",
        }[decision]
        interaction = self.final_runtime.create_interaction(
            session_id=session_id,
            run_id=run_id,
            branch_id=source_branch_id,
            waiting_state="waiting_decision",
            payload={
                "interaction_purpose": "proposal_evaluation",
                "proposal_id": proposal_id,
                "evaluation": evaluation,
                "prompt": "Agent 已根據目前證據評估這個提案；你要採用建議方案，還是保留現行方案？",
                "agent_view": evaluation["recommendation"],
                "preferred_option": preferred_option,
                "options": [
                    {
                        "option_id": "apply_recommendation",
                        "label": apply_label,
                        "reason": evaluation["recommendation"],
                        "recommended": preferred_option == "apply_recommendation",
                    },
                    {
                        "option_id": "keep_current",
                        "label": "保留現行方案",
                        "reason": evaluation["safe_alternative"],
                        "recommended": preferred_option == "keep_current",
                    },
                ],
                "main_evidence": [f"Host Evidence {item}" for item in evaluation["evidence_ids"]],
                "evidence_ids": list(evaluation["evidence_ids"]),
                "unknowns": ["目前證據是否已涵蓋提案改變後的所有下游影響。"],
                "important_risks": [
                    *evaluation["rationale"][:3],
                    "提案未經使用者確認前不會改變正式計畫。",
                ],
            },
        )
        self.store.pause_run(
            run_id,
            status="waiting_decision",
            error={
                "type": "ProposalEvaluationDecisionRequired",
                "proposal_id": proposal_id,
                "interaction": interaction,
            },
        )
        self.final_runtime.set_run_status(run_id, "waiting_decision", result={**result, "proposal_evaluation": evaluation})
        await self._append_host_event(
            run_id,
            "proposal.evaluated",
            {"proposal": persisted, "evaluation": evaluation, "interaction": interaction},
        )
        await self._append_host_event(run_id, "reflection.created", {"interaction": interaction, "public_summary_only": True})
        await self._append_host_event(run_id, "interaction.requested", interaction)
        await self._wake(run_id)
    def _schedule_recovery_followup(
        self,
        run_id: str,
        coroutine: Any,
        *,
        name: str,
    ) -> None:
        """Track the callback task so waiters never observe a false terminal gap."""

        existing = self._recovery_tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(coroutine, name=name)
        self._recovery_tasks[run_id] = task

        def complete_recovery_task(finished: asyncio.Task[None], *, key: str = run_id) -> None:
            self._recovery_tasks.pop(key, None)
            if not finished.cancelled():
                # Consume an unexpected callback failure so it is observable to
                # the event loop rather than becoming an unhandled Task warning.
                finished.exception()

        task.add_done_callback(complete_recovery_task)

    def _is_terminal_model_transport_failure(
        self,
        run_id: str,
        exception: BaseException,
    ) -> bool:
        """Return whether the current provider call exhausted its transport retry.

        The provider emits a durable ``model.provider.failed`` event only after
        its own bounded retry completes. Treating that exact event as a Host
        fault caused a second, unbounded recovery loop that repeatedly called
        an unreachable remote model. Other Host, tool, and data-source errors
        retain the normal autonomous recovery path.
        """

        message = str(exception)
        provider_admission_failure = (
            isinstance(exception, RuntimeError)
            and message.startswith("external transport rate limit blocked provider:")
        )
        if not provider_admission_failure and type(exception).__name__ not in {
            "ConnectError",
            "ConnectTimeout",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "TransportError",
            "WriteError",
            "WriteTimeout",
        }:
            return False
        events = self.events(run_id)
        latest_provider_failure = max(
            (
                int(event.get("sequence") or 0)
                for event in events
                if event.get("type") == "model.provider.failed"
            ),
            default=0,
        )
        latest_provider_success = max(
            (
                int(event.get("sequence") or 0)
                for event in events
                if event.get("type") == "model.provider.completed"
            ),
            default=0,
        )
        return latest_provider_failure > latest_provider_success

    def _should_auto_continue_recovery(self, run_id: str) -> bool:
        """Continue P40 repair while a durable local recovery remains viable.

        The Orchestrator has already recorded the Error Receipt, the selected
        ladder strategy and the safe checkpoint.  This Runtime layer only
        grants the same Run a bounded extra turn budget when a recoverable
        local branch remains unresolved; it never replays completed work.
        """

        snapshot = self.store.get_run(run_id)
        if snapshot is None or snapshot.get("status") not in {
            "partially_completed", "max_steps_reached",
        }:
            return False
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        if result.get("recovery_state") in {
            "user_retained_partial",
            "autonomous_strategies_exhausted",
        }:
            return False
        # A Host resource budget/guard is an intentional hard boundary, not a
        # failed data source. Increasing only the step budget cannot replenish
        # a cost or token budget, and it cannot clear a guard. Treating any of
        # them as a normal goal gap merely replays the same remote-model path
        # and produces the visible start -> pause -> start loop.
        exhausted_gaps = {
            str(item)
            for item in result.get("goal_completion_gaps") or []
            if str(item).strip()
        }
        completion_validation = result.get("completion_validation")
        if isinstance(completion_validation, dict):
            exhausted_gaps.update(
                str(item)
                for item in completion_validation.get("remaining_gaps") or []
                if str(item).strip()
            )
        if exhausted_gaps & {
            "cost_budget_exhausted",
            "token_budget_exhausted",
            "execution_guard_blocked",
        }:
            return False
        goal_gaps = self._goal_completion_repair_items(snapshot)
        if goal_gaps:
            return int(snapshot.get("max_steps") or 0) < _AUTONOMOUS_RECOVERY_STEP_CEILING
        # P78 failure can be deterministic rather than a new tool failure:
        # the Host may already hold validated evidence while a restored Plan
        # node was left pending by an earlier projection.  That is an L6 local
        # completion repair, not a reason to expose a misleading partial result
        # or ask the user to click Continue.  It gets one bounded follow-up
        # pass even if the normal failed-branch retry count was already used.
        if self._completion_repair_items(snapshot):
            return int(snapshot.get("max_steps") or 0) < _AUTONOMOUS_RECOVERY_STEP_CEILING
        return (
            bool(self._unresolved_recovery_items(snapshot))
            and int(snapshot.get("max_steps") or 0) < _AUTONOMOUS_RECOVERY_STEP_CEILING
        )

    @staticmethod
    def _unresolved_recovery_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return failed local branches still awaiting their recorded recovery."""

        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        trace = result.get("tool_trace") if isinstance(result, dict) else []
        if not isinstance(trace, list):
            return []
        recovered_nodes = {
            str(link.get("failed_node_id") or "")
            for item in trace
            if isinstance(item, dict) and item.get("ok") is True
            for link in (item.get("recovery_for") or [])
            if isinstance(link, dict)
        }
        return [
            dict(item)
            for item in trace
            if isinstance(item, dict)
            and item.get("ok") is False
            and isinstance(item.get("recovery"), dict)
            and str(item.get("node_id") or "") not in recovered_nodes
        ]

    @staticmethod
    def _completion_repair_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Find pending tool nodes whose evidence was already durably verified.

        This intentionally does not infer success from a model sentence.  It
        only accepts a concrete trace record with ``ok=True`` for the same
        PlanGraph node.  Such a mismatch is safe to repair locally by giving
        the same Run one more bounded turn to project the completed node and
        validate its final answer; no completed branch is replayed.
        """

        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        validation = result.get("completion_validation") if isinstance(result, dict) else None
        if isinstance(validation, dict) and validation.get("passed") is True:
            return []
        plan = result.get("plan") if isinstance(result, dict) else None
        trace = result.get("tool_trace") if isinstance(result, dict) else None
        if not isinstance(plan, dict) or not isinstance(trace, list):
            return []
        verified_node_ids = {
            str(item.get("node_id") or "")
            for item in trace
            if isinstance(item, dict)
            and item.get("ok") is True
            and str(item.get("node_id") or "").strip()
        }
        return [
            dict(node)
            for node in plan.get("nodes") or []
            if isinstance(node, dict)
            and str(node.get("node_type") or "") == "tool"
            and str(node.get("status") or "") in {"pending", "ready", "running", "blocked"}
            and str(node.get("node_id") or "") in verified_node_ids
        ]

    @staticmethod
    def _goal_completion_repair_items(snapshot: dict[str, Any]) -> list[str]:
        """Return objective obligations that remain unproven after a Run.

        A syntactic ``recovery_for`` link is not enough: an unrelated web
        receipt can be valid while the requested candidate, decision, or order
        is still absent. Keep this check durable so a process restart resumes
        the same objective instead of accepting the old false-complete result.
        """

        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        gaps = [str(item) for item in result.get("goal_completion_gaps") or [] if str(item).strip()]
        if gaps:
            return list(dict.fromkeys(gaps))
        objective = str(snapshot.get("objective") or result.get("objective") or "")
        task_kind = str(result.get("task_kind") or "") or None
        trace = result.get("tool_trace") if isinstance(result.get("tool_trace"), list) else []
        check = evaluate_objective_completion(
            objective=objective,
            task_kind=task_kind,
            observations=(item for item in trace if isinstance(item, dict)),
            decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        )
        return [
            str(item)
            for item in check.get("missing_requirements") or []
            if str(item).strip()
        ]

    def _should_publish_nonblocking_recovery_boundary(self, run_id: str) -> bool:
        """Expose a recovery boundary without asking users to run the system.

        A normal user expresses the desired outcome, not a recovery strategy.
        Once the Host has reached a bounded local recovery limit, retain the
        durable partial result and show its evidence instead of opening a
        ``waiting_user_input`` card that repeatedly asks how the Agent should
        proceed.  Dedicated approvals for external actions remain separate.
        """

        snapshot = self.store.get_run(run_id)
        if snapshot is None or snapshot.get("status") not in {
            "partially_completed", "max_steps_reached",
        }:
            return False
        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
        if result.get("recovery_state") in {
            "user_retained_partial",
            "autonomous_recovery_exhausted_nonblocking",
        }:
            return False
        completion_output_exhausted = (
            result.get("recovery_state") == "completion_output_repair_exhausted"
        )
        goal_gaps = self._goal_completion_repair_items(snapshot)
        if (
            not completion_output_exhausted
            and not goal_gaps
            and not self._unresolved_recovery_items(snapshot)
        ):
            return False
        if completion_output_exhausted:
            # The model already received bounded, precise final-answer repair
            # prompts and repeated the same invalid output fingerprint.  P39
            # forbids spending another full Run on an identical retry; this is
            # the explicit P40 L8 boundary.
            return True
        if result.get("recovery_state") == "autonomous_strategies_exhausted":
            # The Orchestrator already exposed only safe L4/L5 alternatives
            # and the provider failed to execute either one.  Do not turn that
            # explicit local dead-end into two more full-Run retries; present
            # the public L8 decision immediately with the checkpoint intact.
            return True
        if goal_gaps:
            return (
                int(snapshot.get("max_steps") or 0) >= _AUTONOMOUS_RECOVERY_STEP_CEILING
            )
        return False

    async def _continue_recovery_automatically(self, run_id: str) -> None:
        """Schedule the next repair pass after the task completion callback."""

        await asyncio.sleep(0)
        if not self._should_auto_continue_recovery(run_id):
            return
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return
        current_limit = int(snapshot.get("max_steps") or 6)
        completion_repairs = self._completion_repair_items(snapshot)
        next_limit = min(_AUTONOMOUS_RECOVERY_STEP_CEILING, current_limit + 6)
        await self._append_host_event(
            run_id,
            "recovery.continuation_scheduled",
            {
                "summary": (
                    "Host 已找到已驗證但尚未投影完成的局部節點；正在從安全 checkpoint "
                    "進行完成驗證修復。"
                    if completion_repairs
                    else "失敗分支仍可恢復；Host 正在從安全 checkpoint 繼續下一層修復。"
                ),
                "current_max_steps": current_limit,
                "next_max_steps": next_limit,
                "preserve_completed_work": True,
                "repair_kind": "completion_validation_projection"
                if completion_repairs
                else "failed_branch_recovery",
                "completion_repair_node_ids": [
                    str(item.get("node_id") or "") for item in completion_repairs
                ],
            },
        )
        try:
            await self.continue_after_limit(run_id, max_steps=next_limit)
        except (KeyError, ValueError):
            # A user may pause/cancel or manually continue in the brief gap
            # after the terminal event. Their explicit control always wins.
            return

    def _configured_alternative_recovery_models(
        self, snapshot: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Return real configured providers other than the failed model.

        L8 only offers a model switch that the next durable execution can
        actually make; it never invents a provider name from model output.
        """

        request = (
            snapshot.get("request")
            if isinstance(snapshot.get("request"), dict)
            else {}
        )
        current_driver = str(
            request.get("driver_id") or snapshot.get("driver") or ""
        ).strip()
        drivers = getattr(self._service_provider(), "drivers", None)
        if not isinstance(drivers, dict):
            return []
        alternatives: list[dict[str, str]] = []
        for driver_id, driver in drivers.items():
            identifier = str(driver_id).strip()
            if not identifier or identifier == current_driver:
                continue
            describe = getattr(driver, "describe", None)
            details = describe() if callable(describe) else {}
            if not isinstance(details, dict) or not details.get("configured"):
                continue
            model = str(details.get("model") or identifier).strip()
            alternatives.append(
                {
                    "driver_id": identifier,
                    "label": f"{model}（{identifier}）",
                }
            )
        return alternatives

    async def _publish_nonblocking_recovery_boundary(self, run_id: str) -> None:
        """Persist an exhausted recovery receipt without manufacturing a question."""

        await asyncio.sleep(0)
        if not self._should_publish_nonblocking_recovery_boundary(run_id):
            return
        snapshot = self.store.get_run(run_id)
        if snapshot is None:
            return
        result = dict(snapshot.get("result") or {})
        status = str(snapshot.get("status") or result.get("status") or "partially_completed")
        if status not in {"partially_completed", "max_steps_reached"}:
            return
        unresolved = self._unresolved_recovery_items(snapshot)
        goal_gaps = self._goal_completion_repair_items(snapshot)
        boundary = {
            "reason": "autonomous_recovery_limit_reached",
            "goal_completion_gaps": goal_gaps,
            "failed_node_ids": [str(item.get("node_id") or "未命名分支") for item in unresolved],
            "failed_tools": sorted({str(item.get("tool") or "未知工具") for item in unresolved}),
            "preserve_completed_work": True,
            "requires_user_instruction": False,
        }
        updated = {
            **result,
            "status": status,
            "recovery_state": "autonomous_recovery_exhausted_nonblocking",
            "recovery_boundary": boundary,
        }
        self.store.complete_run(run_id, updated)
        self.final_runtime.set_run_status(run_id, status, result=updated)
        self._sync_source_branch(run_id, status, result=updated)
        await self._append_host_event(
            run_id,
            "recovery.exhausted_nonblocking",
            {
                "summary": (
                    "Host 已保留可驗證的部分結果與 checkpoint；此為系統內部修復邊界，"
                    "不需要使用者提供如何處理的指示。"
                ),
                **boundary,
            },
        )
        await self._wake(run_id)

def _filter_obsolete_forest_steps(
    forest: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Hide legacy provider turn IDs from the durable PlanGraph presentation.

    Older projectors could materialize a numeric provider turn ID as a root
    Forest step.  The event remains in the audit trail, but it is not an
    executable plan node and must not appear in Task Forest, DAG, or Fishbone.
    """

    current_node_ids = {
        str(node.get("node_id") or "")
        for node in plan.get("nodes") or []
        if isinstance(node, dict) and str(node.get("node_id") or "")
    }
    filtered_branches: list[dict[str, Any]] = []
    for raw_branch in forest.get("branches") or []:
        branch = dict(raw_branch)
        branch["steps"] = [
            dict(step)
            for step in raw_branch.get("steps") or []
            if not (
                str(step.get("source_node_id") or "").isdigit()
                and str(step.get("source_node_id") or "") not in current_node_ids
            )
        ]
        filtered_branches.append(branch)
    return {**forest, "branches": filtered_branches}


def _artifact_version_content(artifact: dict[str, Any]) -> dict[str, Any]:
    """Project a file Artifact into its immutable v1 payload without leaking paths."""

    text: str | None = None
    media_type = str(artifact.get("media_type") or "")
    path_value = str(artifact.get("path") or "")
    if media_type == ArtifactStore.STRUCTURED_MEDIA_TYPE:
        return {
            "renderer": artifact.get("renderer") or artifact.get("metadata", {}).get("renderer"),
            "schema_version": artifact.get("schema_version") or artifact.get("metadata", {}).get("schema_version"),
            "document": artifact.get("document"),
        }
    if media_type.startswith("text/") and path_value:
        try:
            path = Path(path_value)
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
        except OSError:
            text = None
    return {
        "name": artifact.get("name"),
        "kind": artifact.get("kind"),
        "media_type": media_type or None,
        "sha256": artifact.get("sha256"),
        "content": text,
        "metadata": dict(artifact.get("metadata") or {}),
    }


def _terminal_message(snapshot: dict[str, Any]) -> dict[str, Any]:
    status = snapshot["status"]
    if snapshot.get("result") is not None:
        return {
            "type": "result",
            "run_id": snapshot["run_id"],
            "status": status,
            "result": snapshot["result"],
        }
    if status == "cancelled":
        return {
            "type": "cancelled",
            "run_id": snapshot["run_id"],
            "status": status,
            "error": snapshot.get("error"),
        }
    return {
        "type": "error",
        "run_id": snapshot["run_id"],
        "status": status,
        "error": snapshot.get("error") or {"type": "AgentRunError", "message": status},
    }


def _automation_schedule_matches(
    trigger: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    """Route only compatible external events into an Automation callback."""

    kind = str(trigger.get("type") or "").casefold()
    expected = str(
        trigger.get("event_type") or trigger.get("event") or trigger.get("name") or ""
    ).strip()
    if kind in {"event", "market_event", "agent_wakeup"}:
        return not expected or expected == event_type
    if kind in {"condition", "price_crossing"}:
        # Price/condition watches should not create a durable callback for
        # every internal tool event.  Their cheap filter receives explicit
        # market/event-engine payloads only.
        return any(key in payload for key in ("price", "value", "market_event", "condition"))
    return False


def _n8n_wake_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep an n8n callback as a clock signal, never an action envelope.

    n8n is an untrusted external execution boundary.  Its callback may name
    an already stored automation and its immutable compiler product, but it
    cannot introduce symbols, prices, actions, permissions, instructions or
    any other input that could alter the Host-owned research decision.
    """

    fields = (
        "automation_id",
        "automation_version",
        "submission_id",
        "source_event_id",
        "source",
        "compiler_digest",
        "compiled_stage_count",
        "market_calendar",
    )
    normalized: dict[str, Any] = {}
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        if field in {"automation_version", "compiled_stage_count"}:
            try:
                normalized[field] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            text = str(value).strip()
            if text:
                normalized[field] = text[:256]
    return normalized


def _automation_confidence(result: dict[str, Any]) -> float:
    decision = result.get("decision")
    raw = decision.get("confidence") if isinstance(decision, dict) else result.get("confidence")
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _message_selection_context(
    selection: dict[str, Any] | None,
    *,
    message_id: str,
) -> dict[str, Any] | None:
    """Normalize a UI graph selection into a replayable message context.

    Artifact selections gain a durable database row through FinalAgentRuntime.
    Evidence, branch, decision, and automation selections are intentionally
    immutable conversation coordinates, so their durable source of truth is
    the user message that references them.  Keeping one normalized shape lets
    the Agent use either kind without treating evidence as a fake artifact.
    """

    if not selection:
        return None
    target_type = str(selection.get("target_type") or "artifact").strip()
    if not target_type:
        return None
    artifact_id = str(selection.get("artifact_id") or "").strip() or None
    artifact_version = selection.get("artifact_version")
    normalized: dict[str, Any] = {
        "selection_id": str(selection.get("selection_id") or f"AMSEL-{message_id}"),
        "target_type": target_type,
        "artifact_id": artifact_id,
        "artifact_version": int(artifact_version) if artifact_id and artifact_version else None,
        "branch_id": str(selection.get("branch_id") or "").strip() or None,
        "node_id": str(selection.get("node_id") or "").strip() or None,
        "evidence_id": str(selection.get("evidence_id") or "").strip() or None,
        "automation_id": str(selection.get("automation_id") or "").strip() or None,
        "path": str(selection.get("path") or selection.get("label") or target_type).strip(),
        "message_id": message_id,
    }
    if artifact_id and normalized["artifact_version"] is None:
        raise ValueError("Artifact context selection requires artifact_version")
    if not any(
        normalized[key]
        for key in ("artifact_id", "branch_id", "node_id", "evidence_id", "automation_id")
    ):
        raise ValueError("Selection context requires an artifact, branch, node, evidence, or automation id")
    return normalized


def _utc_time(value: str) -> str:
    return _parse_time(value).isoformat()


def _preference_semantic_key(content: str) -> str:
    lowered = content.casefold()
    categories = {
        "sources": ("來源", "出處", "source", "citation"),
        "explanation_depth": ("詳細", "簡短", "解釋", "detail", "concise"),
        "visualization": ("圖表", "視覺", "chart", "visual"),
        "risk_style": ("風險", "保守", "積極", "risk", "conservative"),
        "question_timing": ("反問", "先問", "再問", "clarify", "question"),
    }
    for category, tokens in categories.items():
        if any(token in lowered for token in tokens):
            return f"user-preference:{category}"
    normalized = " ".join(lowered.split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"user-preference:{digest}"
