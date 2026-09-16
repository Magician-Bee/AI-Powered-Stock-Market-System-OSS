from __future__ import annotations

import json
import inspect
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .backends import BackendResult, HeadlessN8nAdapter, InternalSchedulerBackend
from .dedup import DedupDecision, DedupResult, SemanticDeduplicator, semantic_fingerprint
from .intent import AutomationIntent, AutomationKind, NotificationPolicy
from .lifecycle import AutomationState, Lifecycle
from .notifications import NotificationManager, NotificationReceipt
from .opportunity_detector import AutomationOpportunity, OpportunityDetector
from .policy_engine import AutomationPolicyDecision, AutomationPolicyEngine
from .store import AutomationStore
from .workflow_compiler import CompiledWorkflow, WorkflowCompiler


Reanalyze = Callable[
    [AutomationIntent, Mapping[str, Any], Mapping[str, Any] | None],
    Mapping[str, Any],
]
AsyncReanalyze = Callable[
    [AutomationIntent, Mapping[str, Any], Mapping[str, Any] | None],
    Awaitable[Mapping[str, Any]] | Mapping[str, Any],
]


@dataclass(frozen=True, slots=True)
class AutomationProposal:
    opportunity: AutomationOpportunity
    natural_language: str
    host_status: str
    artifact_preview: Mapping[str, Any]
    requires_user_confirmation: bool = True


@dataclass(frozen=True, slots=True)
class ActivationOutcome:
    automation: Mapping[str, Any]
    intent_id: str | None
    version: int
    dedup: DedupResult
    policy: AutomationPolicyDecision
    validation_errors: tuple[str, ...]
    dry_run: BackendResult | None
    activation: BackendResult | None
    natural_language: str
    host_status: str
    artifact: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TriggerOutcome:
    automation_id: str
    condition_candidate: bool
    reanalyzed: bool
    meaningful_change: bool
    notified: bool
    decision: Mapping[str, Any] | None
    receipt: NotificationReceipt | None
    reason: str
    execution_id: str | None = None


class AutomationController:
    def __init__(
        self,
        store: AutomationStore,
        *,
        detector: OpportunityDetector | None = None,
        policy_engine: AutomationPolicyEngine | None = None,
        compiler: WorkflowCompiler | None = None,
        deduplicator: SemanticDeduplicator | None = None,
        internal_backend: InternalSchedulerBackend | None = None,
        n8n_backend: HeadlessN8nAdapter | None = None,
        notifications: NotificationManager | None = None,
        reanalyze: Reanalyze | None = None,
        trigger_filter: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.store = store
        self.detector = detector or OpportunityDetector()
        self.policy_engine = policy_engine or AutomationPolicyEngine()
        self.compiler = compiler or WorkflowCompiler()
        self.deduplicator = deduplicator or SemanticDeduplicator()
        self.reanalyze = reanalyze
        self._async_reanalysis_ready = False
        self.trigger_filter = trigger_filter
        scheduler_backend = internal_backend or InternalSchedulerBackend(self._submit_internal_schedule)
        scheduler_backend.bind_trigger_callback(self.handle_scheduler_callback)
        self.backends = {
            "internal_scheduler": scheduler_backend,
            "n8n": n8n_backend or HeadlessN8nAdapter(),
        }
        self.notifications = notifications or NotificationManager(store)
        self.lifecycle = Lifecycle()

    def enable_async_reanalysis(self) -> None:
        """Allow durable registration when an owning Runtime awaits providers."""

        self._async_reanalysis_ready = True

    def backend_status(self) -> dict[str, dict[str, Any]]:
        """Expose only secret-free readiness receipts to the Host/UI."""

        statuses: dict[str, dict[str, Any]] = {}
        for backend_id, backend in self.backends.items():
            describe = getattr(backend, "status", None)
            if callable(describe):
                statuses[backend_id] = dict(describe())
            else:
                statuses[backend_id] = {
                    "backend": backend_id,
                    "configured": False,
                    "activation_ready": False,
                }
        return statuses

    def propose(self, goal: str, context: Mapping[str, Any] | None = None) -> AutomationProposal:
        opportunity = self.detector.detect(goal, context)
        if not opportunity.worthwhile:
            return AutomationProposal(
                opportunity,
                "這次需求不需要建立自動化，我會直接完成目前任務。",
                "不建立自動化",
                {"schema_version": "open_stock_ai.automation_artifact.v1", "title": goal, "steps": []},
                False,
            )
        return AutomationProposal(
            opportunity,
            "我建議把後續條件監控自動化；確認後會先驗證並試跑，再正式啟用。",
            "正在準備自動化提案",
            {
                "schema_version": "open_stock_ai.automation_artifact.v1",
                "title": goal,
                "steps": ["等待未來條件", "重新取得證據", "重新分析", "只有重大改變才通知"],
            },
            True,
        )

    def draft_intent(
        self,
        goal: str,
        context: Mapping[str, Any] | None = None,
    ) -> AutomationIntent:
        """Create a conservative semantic draft for a confirmed UI proposal.

        This is deliberately a product-level draft rather than an executable
        workflow.  It contains no credentials, transport details, or order
        action; the compiler remains the only path to a backend submission.
        """

        opportunity = self.detector.detect(goal, context)
        if not opportunity.worthwhile:
            raise ValueError("The request does not describe an automation opportunity")
        details = dict(context or {})
        # The Agent may propose a semantic intent after observing evidence.
        # Host validation/compiler remain authoritative, but the old fixed
        # 90-day/in-app draft is no longer the only path for model-generated
        # automation.  Technical backend fields are still rejected by
        # AutomationIntent.from_dict and WorkflowCompiler.
        model_intent = details.get("agent_automation_intent")
        if isinstance(model_intent, Mapping):
            payload = dict(model_intent)
            payload.setdefault("goal", goal.strip())
            payload.setdefault("user_id", str(details.get("user_id") or "stock_ai_local_user"))
            payload.setdefault("session_id", str(details.get("session_id") or "") or None)
            return AutomationIntent.from_dict(payload)
        symbol = _explicit_symbol(goal) or _explicit_symbol(str(details.get("symbol") or ""))
        kind = opportunity.kind
        trigger = _draft_trigger(kind, goal)
        decision_logic = _draft_condition(goal) if kind is AutomationKind.CONDITION_WATCH else {}
        observation_type = "market_price" if kind is AutomationKind.CONDITION_WATCH else "market_event"
        return AutomationIntent(
            goal=goal.strip(),
            user_id=str(details.get("user_id") or "stock_ai_local_user").strip(),
            session_id=str(details.get("session_id") or "").strip() or None,
            symbol=symbol,
            kind=kind,
            trigger=trigger,
            observations=({"type": observation_type, "label": "取得最新可驗證資料"},),
            analysis=({"type": "strategy_reanalysis", "label": "重新分析策略是否改變"},),
            decision_logic=decision_logic,
            actions=({"type": "notify", "message": "監控條件或策略出現重大變化"},),
            notification_policy=NotificationPolicy(channels=("in_app",), meaningful_only=True),
            lifecycle={"expires_after_days": 90},
        )

    def propose_semantic_intent(
        self,
        intent_payload: Mapping[str, Any],
        *,
        reason: str = "",
        title: str = "",
    ) -> tuple[AutomationProposal, AutomationIntent]:
        """Validate an Agent-produced semantic intent for user confirmation.

        The model owns the future goal, trigger, evidence observations,
        decision logic, actions and lifecycle. Host code only validates the
        semantic contract and creates a renderer-ready preview; it does not
        infer an automation opportunity from the user's wording.
        """

        intent = AutomationIntent.from_dict(intent_payload)
        opportunity = AutomationOpportunity(
            kind=intent.kind,
            worthwhile=True,
            confidence=1.0,
            reason=reason.strip() or "Agent identified a future decision worth revisiting.",
        )
        artifact = {
            "schema_version": "open_stock_ai.automation_artifact.v1",
            "title": title.strip() or intent.goal,
            "renderer": "workflow",
            "intent": intent.to_semantic_dict(),
            "steps": [
                {"type": "trigger", "label": "依語意觸發條件喚醒"},
                *[
                    {"type": "observation", **dict(item)}
                    for item in intent.observations
                ],
                *[
                    {"type": "analysis", **dict(item)}
                    for item in intent.analysis
                ],
                *[
                    {"type": "action", **dict(item)}
                    for item in intent.actions
                ],
            ],
            "host_status": "等待使用者確認；確認後才會 compile、validate、dry-run。",
        }
        proposal = AutomationProposal(
            opportunity=opportunity,
            natural_language=(
                f"Agent 建議：{intent.goal}。已形成語意 Automation Plan，"
                "需經你確認後才會驗證與試跑。"
            ),
            host_status="等待使用者確認；尚未提交 n8n 或 Scheduler。",
            artifact_preview=artifact,
            requires_user_confirmation=True,
        )
        return proposal, intent

    def confirm_and_activate(
        self,
        intent: AutomationIntent,
        *,
        user_confirmed: bool,
        external_permission: bool = False,
        credential_refs: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> ActivationOutcome:
        if not user_confirmed:
            raise PermissionError("Automation proposal requires user confirmation")
        current = _utc(now)
        policy = self.policy_engine.evaluate(intent, external_permission=external_permission)
        if not policy.allowed:
            raise PermissionError(policy.reason)
        fingerprint = semantic_fingerprint(intent)
        dedup = self.deduplicator.resolve(intent, self.store.semantic_candidates(intent))
        if dedup.decision == DedupDecision.REUSE and dedup.automation_id:
            automation = self.store.get_automation(dedup.automation_id) or {}
            version = int(automation.get("current_version") or 0)
            artifact = (self.store.latest_version(dedup.automation_id) or {}).get("artifact") or {}
            return ActivationOutcome(
                automation,
                None,
                version,
                dedup,
                policy,
                (),
                None,
                None,
                "已找到相同監控，沿用既有自動化以避免重複通知。",
                "已沿用自動化",
                artifact,
            )
        effective_intent = intent
        existing_id = dedup.automation_id if dedup.decision in {DedupDecision.MERGE, DedupDecision.UPDATE} else None
        if dedup.decision == DedupDecision.MERGE and existing_id:
            existing_version = self.store.latest_version(existing_id)
            if existing_version:
                effective_intent = _merge_intents(AutomationIntent.from_dict(existing_version["intent"]), intent)
        policy = self.policy_engine.evaluate(effective_intent, external_permission=external_permission)
        if not policy.allowed:
            raise PermissionError(policy.reason)
        fingerprint = semantic_fingerprint(effective_intent)
        compiled = self.compiler.compile(effective_intent, credential_refs=credential_refs)
        validation = self.compiler.validate(compiled)
        # n8n needs an opaque reference before this intent becomes durable.
        # Calling the adapter later would otherwise leave a brand-new
        # Automation in ``testing`` after its precondition error; semantic
        # deduplication then returns that stranded record forever instead of
        # allowing the caller to retry the same user goal once the Host has
        # supplied its already-configured control-plane reference.
        if compiled.backend == "n8n" and not compiled.credential_refs:
            raise ValueError("headless n8n activation requires credential references")
        if not validation.valid and existing_id:
            automation = self.store.get_automation(existing_id) or {}
            previous_version = self.store.latest_version(existing_id) or {}
            return ActivationOutcome(
                automation,
                None,
                int(automation.get("current_version") or 0),
                dedup,
                policy,
                validation.errors,
                None,
                None,
                "自動化更新驗證失敗，既有版本保持不變。",
                "自動化更新驗證失敗",
                previous_version.get("artifact") or {},
            )
        intent_id = self.store.save_intent(effective_intent, fingerprint, now=current)
        operation = dedup.decision.value
        if existing_id:
            automation_id = existing_id
            self.store.retarget_automation(
                automation_id,
                intent_id=intent_id,
                intent=effective_intent,
                fingerprint=fingerprint,
                now=current,
            )
        else:
            automation = self.store.create_automation(
                intent_id=intent_id,
                intent=effective_intent,
                fingerprint=fingerprint,
                now=current,
            )
            automation_id = str(automation["automation_id"])
        version = self.store.save_version(
            automation_id,
            intent=effective_intent,
            compiled=_compiled_payload(compiled),
            artifact=compiled.user_artifact,
            backend=compiled.backend,
            now=current,
        )
        if not validation.valid:
            if not existing_id:
                self._transition(automation_id, AutomationState.FAILED, now=current)
            return ActivationOutcome(
                self.store.get_automation(automation_id) or {},
                intent_id,
                version,
                dedup,
                policy,
                validation.errors,
                None,
                None,
                "自動化驗證失敗，尚未啟用。",
                "自動化驗證失敗",
                compiled.user_artifact,
            )
        if not existing_id:
            self._transition(automation_id, AutomationState.VALIDATED, now=current)
        if not policy.auto_activatable:
            if existing_id and (self.store.get_automation(automation_id) or {}).get("state") == AutomationState.ACTIVE.value:
                self._transition(automation_id, AutomationState.PAUSED, now=current)
            return ActivationOutcome(
                self.store.get_automation(automation_id) or {},
                intent_id,
                version,
                dedup,
                policy,
                (),
                None,
                None,
                "監控已驗證；取得外部訊息權限後才能試跑與啟用。",
                "等待外部訊息權限",
                compiled.user_artifact,
            )
        if not existing_id:
            self._transition(automation_id, AutomationState.TESTING, now=current)
        backend = self.backends[compiled.backend]
        existing_reference = (self.store.get_automation(automation_id) or {}).get("backend_reference")
        # Persist the sealed compiler product before probing an external
        # backend.  A healthy-but-unconfigured n8n instance is a recoverable
        # deployment boundary, not evidence that the user's approved
        # Automation is invalid.  Startup recovery can resubmit this exact
        # idempotent contract after owner/API-key setup without asking the
        # model to regenerate intent or replaying prior work.
        submission = self.store.save_submission(
            automation_id,
            backend=compiled.backend,
            idempotency_key=f"{automation_id}:{compiled.digest}",
            request_payload={
                "compiled": _compiled_payload(compiled),
                "operation": operation,
                "target_backend_reference": existing_reference,
            },
            operation=operation,
            now=current,
        )
        dry_run = backend.dry_run(compiled)
        dry_run_pending = (
            not dry_run.accepted
            and bool(dry_run.detail.get("durable_submission_required"))
        )
        self.store.save_execution(
            automation_id,
            stage="dry_run",
            status="completed" if dry_run.accepted else "pending" if dry_run_pending else "failed",
            output_payload={**asdict(dry_run), "submission_id": submission["submission_id"]},
            now=current,
        )
        if not dry_run.accepted:
            self.store.complete_submission(
                str(submission["submission_id"]),
                status="pending" if dry_run_pending else "failed",
                response_payload=asdict(dry_run),
                backend_reference=existing_reference,
                now=current,
            )
            if not dry_run_pending:
                self._transition(automation_id, AutomationState.FAILED, now=current)
            return ActivationOutcome(
                self.store.get_automation(automation_id) or {},
                intent_id,
                version,
                dedup,
                policy,
                (),
                dry_run,
                None,
                (
                    "自動化已保存 durable submission，等待 n8n owner、API key 與 callback "
                    "readiness 通過後自動續送。"
                    if dry_run_pending
                    else "自動化試跑失敗，尚未啟用。"
                ),
                "等待 n8n 執行層就緒" if dry_run_pending else "自動化試跑失敗",
                compiled.user_artifact,
            )
        activation = backend.activate(
            compiled,
            context={
                "automation_id": automation_id,
                "automation_version": version,
                "submission_id": submission["submission_id"],
                "idempotency_key": f"{automation_id}:{compiled.digest}",
                "target_backend_reference": existing_reference,
                "now": current,
            },
        )
        activation_status = "completed" if activation.accepted else (
            "pending" if activation.detail.get("durable_submission_required") else "failed"
        )
        self.store.complete_submission(
            str(submission["submission_id"]),
            status="active" if activation.accepted else activation_status,
            response_payload=asdict(activation),
            backend_reference=activation.backend_reference,
            now=current,
        )
        self.store.save_execution(
            automation_id,
            stage="activate",
            status=activation_status,
            output_payload={**asdict(activation), "submission_id": submission["submission_id"]},
            now=current,
        )
        if not activation.accepted:
            if activation_status == "pending":
                if existing_id and self.store.get_automation(automation_id)["state"] == AutomationState.ACTIVE.value:
                    self._transition(automation_id, AutomationState.PAUSED, now=current)
            elif not existing_id or self.store.get_automation(automation_id)["state"] == AutomationState.ACTIVE.value:
                self._transition(automation_id, AutomationState.FAILED, now=current)
        else:
            if activation.backend_reference:
                self.store.set_backend_reference(automation_id, activation.backend_reference, now=current)
            state = str((self.store.get_automation(automation_id) or {}).get("state"))
            if state != AutomationState.ACTIVE.value:
                self._transition(automation_id, AutomationState.ACTIVE, now=current)
        pending = activation_status == "pending"
        return ActivationOutcome(
            self.store.get_automation(automation_id) or {},
            intent_id,
            version,
            dedup,
            policy,
            (),
            dry_run,
            activation,
            "自動化已保存 durable scheduler submission，等待排程器接管。" if pending else "自動化已完成驗證、試跑並啟用。",
            "等待排程器接管" if pending else "自動化已啟用",
            compiled.user_artifact,
        )

    def recover_pending_submissions(self, *, now: datetime | None = None) -> list[BackendResult]:
        """Resubmit durable scheduler contracts after process restart."""
        current = _utc(now)
        recovered: list[BackendResult] = []
        for submission in self.store.list_submissions(status="pending"):
            request = dict(submission.get("request") or {})
            compiled_payload = dict(request.get("compiled") or {})
            try:
                workflow = _compiled_from_payload(compiled_payload)
                backend = self.backends.get(workflow.backend)
                if backend is None:
                    raise ValueError(f"unsupported recovered backend: {workflow.backend}")
                result = backend.activate(
                    workflow,
                    context={
                        "automation_id": submission["automation_id"],
                        "automation_version": submission["automation_version"],
                        "submission_id": submission["submission_id"],
                        "idempotency_key": submission["idempotency_key"],
                        "target_backend_reference": request.get("target_backend_reference"),
                        "now": current,
                    },
                )
            except Exception as exc:
                result = BackendResult(
                    str(compiled_payload.get("backend") or submission.get("backend") or "unknown"),
                    "activate",
                    False,
                    None,
                    {
                        "submission_status": "failed",
                        "recoverable": False,
                        "reason": f"invalid durable submission: {type(exc).__name__}",
                    },
                )
            status = "active" if result.accepted else "pending"
            if not result.accepted and not result.detail.get("durable_submission_required"):
                status = "failed"
            self.store.complete_submission(
                str(submission["submission_id"]),
                status=status,
                response_payload=asdict(result),
                backend_reference=result.backend_reference,
                now=current,
            )
            self.store.save_execution(
                str(submission["automation_id"]),
                stage="activate_recovery",
                status="completed" if result.accepted else status,
                output_payload={**asdict(result), "submission_id": submission["submission_id"]},
                now=current,
            )
            if result.accepted:
                automation_id = str(submission["automation_id"])
                if result.backend_reference:
                    self.store.set_backend_reference(automation_id, result.backend_reference, now=current)
                state = str((self.store.get_automation(automation_id) or {}).get("state"))
                if state != AutomationState.ACTIVE.value:
                    self._transition(automation_id, AutomationState.ACTIVE, now=current)
            elif status == "failed":
                automation_id = str(submission["automation_id"])
                state = str((self.store.get_automation(automation_id) or {}).get("state"))
                if state in {AutomationState.DRAFT.value, AutomationState.TESTING.value}:
                    self._transition(automation_id, AutomationState.FAILED, now=current)
            recovered.append(result)
        return recovered

    def handle_scheduler_callback(
        self,
        schedule_id: str,
        event: Mapping[str, Any],
        *,
        now: datetime | None = None,
        receipt_id: str | None = None,
    ) -> TriggerOutcome:
        """Run one durable scheduler callback through reanalysis and notification."""

        current = _utc(now)
        callback_receipt = (
            self.store.get_schedule_receipt(receipt_id)
            if receipt_id
            else self.store.begin_schedule_callback(schedule_id, event, now=current)
        )
        if not callback_receipt:
            raise KeyError(f"unknown automation schedule receipt: {receipt_id}")
        callback_receipt_id = str(callback_receipt["receipt_id"])
        if not receipt_id and callback_receipt.get("claimed") is False:
            return _duplicate_callback_outcome(callback_receipt)
        if str(callback_receipt.get("status")) != "processing":
            return _duplicate_callback_outcome(callback_receipt)
        if self.reanalyze is None:
            reason = "automation reanalysis callback is not configured"
            self.store.complete_schedule_callback(
                callback_receipt_id,
                status="failed",
                error=reason,
                now=current,
            )
            raise RuntimeError(reason)
        try:
            schedule = self.store.get_schedule(schedule_id) or {}
            outcome = self.handle_trigger(
                str(schedule["automation_id"]),
                event,
                reanalyze=self.reanalyze,
                cheap_filter=self.trigger_filter,
                now=current,
            )
        except Exception as exc:
            self.store.complete_schedule_callback(
                callback_receipt_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                now=current,
            )
            raise
        if outcome.reason in {"automation_triggered", "occurrence_already_reserved"}:
            return outcome
        self.store.complete_schedule_callback(
            callback_receipt_id,
            status="completed" if outcome.condition_candidate else "filtered",
            response={
                "execution_id": outcome.execution_id,
                "reanalyzed": outcome.reanalyzed,
                "reason": outcome.reason,
                "meaningful_change": outcome.meaningful_change,
                "notified": outcome.notified,
                "notification_receipt_id": (
                    outcome.receipt.delivery_id if outcome.receipt else None
                ),
            },
            now=current,
        )
        self._expire_one_shot(schedule_id, now=current)
        return outcome

    async def handle_scheduler_callback_async(
        self,
        schedule_id: str,
        event: Mapping[str, Any],
        *,
        reanalyze: AsyncReanalyze,
        now: datetime | None = None,
        receipt_id: str | None = None,
    ) -> TriggerOutcome:
        """Durably process a scheduler callback whose reanalysis is async.

        The original callback remains synchronous for embedders that execute a
        deterministic reanalysis function.  Durable Agent Runs are naturally
        asynchronous, so this companion path prevents a scheduler thread from
        faking a decision or blocking an event loop while a real provider run
        gathers fresh evidence.
        """

        current = _utc(now)
        callback_receipt = (
            self.store.get_schedule_receipt(receipt_id)
            if receipt_id
            else self.store.begin_schedule_callback(schedule_id, event, now=current)
        )
        if not callback_receipt:
            raise KeyError(f"unknown automation schedule receipt: {receipt_id}")
        callback_receipt_id = str(callback_receipt["receipt_id"])
        if not receipt_id and callback_receipt.get("claimed") is False:
            return _duplicate_callback_outcome(callback_receipt)
        if str(callback_receipt.get("status")) != "processing":
            return _duplicate_callback_outcome(callback_receipt)
        try:
            schedule = self.store.get_schedule(schedule_id) or {}
            outcome = await self.handle_trigger_async(
                str(schedule["automation_id"]),
                event,
                reanalyze=reanalyze,
                cheap_filter=self.trigger_filter,
                now=current,
            )
        except Exception as exc:
            self.store.complete_schedule_callback(
                callback_receipt_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                now=current,
            )
            raise
        if outcome.reason in {"automation_triggered", "occurrence_already_reserved"}:
            return outcome
        self.store.complete_schedule_callback(
            callback_receipt_id,
            status="completed" if outcome.condition_candidate else "filtered",
            response={
                "execution_id": outcome.execution_id,
                "reanalyzed": outcome.reanalyzed,
                "reason": outcome.reason,
                "meaningful_change": outcome.meaningful_change,
                "notified": outcome.notified,
                "notification_receipt_id": (
                    outcome.receipt.delivery_id if outcome.receipt else None
                ),
            },
            now=current,
        )
        self._expire_one_shot(schedule_id, now=current)
        return outcome

    def _prepare_reanalysis(
        self, automation_id: str, event: Mapping[str, Any], *,
        cheap_filter: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None,
        now: datetime,
    ) -> tuple[str, AutomationIntent, Mapping[str, Any] | None] | TriggerOutcome:
        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        version = self.store.latest_version(automation_id)
        if not version:
            raise RuntimeError("automation has no compiled version")
        intent = AutomationIntent.from_dict(version["intent"])
        source_event_id = str(event.get("source_event_id") or event.get("event_id") or "").strip()
        execution_id = self.store.begin_execution(
            automation_id, stage="trigger_pipeline", input_payload=event,
            source_event_id=source_event_id, now=now,
        )
        execution = self.store.get_execution(execution_id) or {}
        if execution.get("completed_at"):
            return _existing_execution_outcome(automation_id, execution)
        if automation["state"] != AutomationState.ACTIVE.value:
            return self._skip_reanalysis(
                automation_id, execution_id, f"automation_{automation['state']}", now=now,
                # Never overwrite a callback already running in another worker.
                persist=automation["state"] != AutomationState.TRIGGERED.value,
            )
        if not (cheap_filter or _cheap_filter)(event, intent.decision_logic):
            return self._skip_reanalysis(
                automation_id, execution_id, "cheap deterministic filter rejected the event",
                now=now, stage="cheap_filter",
            )
        reservation = self.store.reserve_reanalysis(
            automation_id, execution_id,
            max_per_day=intent.cost_policy.max_reanalysis_per_day, now=now,
        )
        if not reservation["admitted"]:
            return self._skip_reanalysis(
                automation_id, execution_id, str(reservation["reason"]), now=now,
                details=reservation,
                persist=reservation["reason"] != "occurrence_already_reserved",
            )
        self.store.checkpoint_execution(
            execution_id, stage="cheap_filter", status="candidate",
            output_payload={"condition_candidate": True}, now=now,
        )
        self.store.checkpoint_execution(
            execution_id, stage="reanalysis_budget", status="reserved",
            output_payload=reservation, now=now,
        )
        return execution_id, intent, automation.get("current_decision") or None

    def _skip_reanalysis(
        self, automation_id: str, execution_id: str, reason: str, *, now: datetime,
        stage: str = "reanalysis_budget", details: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> TriggerOutcome:
        if persist:
            self.store.complete_execution(
                execution_id, stage=stage, status="filtered", meaningful_change=False,
                output_payload={"condition_candidate": False, "reason": reason, **dict(details or {})},
                now=now,
            )
        return TriggerOutcome(
            automation_id, False, False, False, False, None, None, reason, execution_id,
        )

    def handle_trigger(
        self,
        automation_id: str,
        event: Mapping[str, Any],
        *,
        reanalyze: Callable[[AutomationIntent, Mapping[str, Any], Mapping[str, Any] | None], Mapping[str, Any]],
        cheap_filter: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
        now: datetime | None = None,
    ) -> TriggerOutcome:
        current = _utc(now)
        prepared = self._prepare_reanalysis(automation_id, event, cheap_filter=cheap_filter, now=current)
        if isinstance(prepared, TriggerOutcome):
            return prepared
        execution_id, intent, previous = prepared
        try:
            decision = dict(reanalyze(intent, event, previous))
        except Exception as exc:
            self.store.complete_execution(
                execution_id,
                stage="agent_reanalysis",
                status="failed",
                output_payload={"error": f"{type(exc).__name__}: {exc}"},
                now=current,
            )
            self.store.finish_reanalysis_state(automation_id, "failed", now=current)
            raise
        if not decision:
            self.store.complete_execution(
                execution_id,
                stage="agent_reanalysis",
                status="failed",
                output_payload={"error": "empty decision"},
                now=current,
            )
            self.store.finish_reanalysis_state(automation_id, "failed", now=current)
            raise ValueError("Agent reanalysis returned an empty decision")
        reanalyzed_run_id = str(
            decision.get("reanalyzed_run_id") or decision.get("run_id") or ""
        ).strip()
        if reanalyzed_run_id:
            self.store.attach_execution_run(execution_id, reanalyzed_run_id)
        self.store.checkpoint_execution(
            execution_id,
            stage="agent_reanalysis",
            status="completed",
            output_payload={"decision": decision},
            now=current,
        )
        meaningful = _decision_changed(previous, decision)
        self.store.update_decision(automation_id, decision, now=current)
        receipt: NotificationReceipt | None = None
        if meaningful or not intent.notification_policy.meaningful_only:
            receipt = self.notifications.deliver(
                automation_id=automation_id,
                user_id=intent.user_id,
                decision=decision,
                payload={"goal": intent.goal, "symbol": intent.symbol, "decision": decision},
                policy=intent.notification_policy,
                execution_id=execution_id,
                now=current,
            )
        self.store.complete_execution(
            execution_id,
            stage="decision_changed_gate",
            status="notified" if receipt and receipt.status.value == "delivered" else "logged",
            output_payload={"decision": decision, "receipt_id": receipt.delivery_id if receipt else None},
            meaningful_change=meaningful,
            now=current,
        )
        self.store.finish_reanalysis_state(automation_id, "active", now=current)
        notified = bool(receipt and receipt.status.value == "delivered")
        return TriggerOutcome(
            automation_id,
            True,
            True,
            meaningful,
            notified,
            decision,
            receipt,
            "meaningful decision change notified" if notified else "decision unchanged or notification suppressed",
            execution_id,
        )

    async def handle_trigger_async(
        self,
        automation_id: str,
        event: Mapping[str, Any],
        *,
        reanalyze: AsyncReanalyze,
        cheap_filter: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
        now: datetime | None = None,
    ) -> TriggerOutcome:
        """Async equivalent of :meth:`handle_trigger` for real Agent Runs."""

        current = _utc(now)
        prepared = self._prepare_reanalysis(automation_id, event, cheap_filter=cheap_filter, now=current)
        if isinstance(prepared, TriggerOutcome):
            return prepared
        execution_id, intent, previous = prepared
        try:
            decision_value = reanalyze(intent, event, previous)
            decision = dict(await decision_value) if inspect.isawaitable(decision_value) else dict(decision_value)
        except Exception as exc:
            self.store.complete_execution(
                execution_id,
                stage="agent_reanalysis",
                status="failed",
                output_payload={"error": f"{type(exc).__name__}: {exc}"},
                now=current,
            )
            self.store.finish_reanalysis_state(automation_id, "failed", now=current)
            raise
        if not decision:
            self.store.complete_execution(
                execution_id,
                stage="agent_reanalysis",
                status="failed",
                output_payload={"error": "empty decision"},
                now=current,
            )
            self.store.finish_reanalysis_state(automation_id, "failed", now=current)
            raise ValueError("Agent reanalysis returned an empty decision")
        reanalyzed_run_id = str(
            decision.get("reanalyzed_run_id") or decision.get("run_id") or ""
        ).strip()
        if reanalyzed_run_id:
            self.store.attach_execution_run(execution_id, reanalyzed_run_id)
        self.store.checkpoint_execution(
            execution_id,
            stage="agent_reanalysis",
            status="completed",
            output_payload={"decision": decision},
            now=current,
        )
        meaningful = _decision_changed(previous, decision)
        self.store.update_decision(automation_id, decision, now=current)
        receipt: NotificationReceipt | None = None
        if meaningful or not intent.notification_policy.meaningful_only:
            receipt = self.notifications.deliver(
                automation_id=automation_id,
                user_id=intent.user_id,
                decision=decision,
                payload={"goal": intent.goal, "symbol": intent.symbol, "decision": decision},
                policy=intent.notification_policy,
                execution_id=execution_id,
                now=current,
            )
        self.store.complete_execution(
            execution_id,
            stage="decision_changed_gate",
            status="notified" if receipt and receipt.status.value == "delivered" else "logged",
            output_payload={"decision": decision, "receipt_id": receipt.delivery_id if receipt else None},
            meaningful_change=meaningful,
            now=current,
        )
        self.store.finish_reanalysis_state(automation_id, "active", now=current)
        notified = bool(receipt and receipt.status.value == "delivered")
        return TriggerOutcome(
            automation_id, True, True, meaningful, notified, decision, receipt,
            "meaningful decision change notified" if notified else "decision unchanged or notification suppressed",
            execution_id,
        )

    def pause(self, automation_id: str, *, now: datetime | None = None) -> Mapping[str, Any]:
        current = _utc(now)
        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        if (
            str(automation.get("state")) in {AutomationState.TESTING.value, AutomationState.PAUSED.value}
            and not str(automation.get("backend_reference") or "").strip()
        ):
            cancelled = self.store.cancel_pending_submissions(
                automation_id,
                reason="user paused the Automation before n8n deployment completed",
                now=current,
            )
            if cancelled:
                self.store.save_execution(
                    automation_id,
                    stage="n8n_pause_pending_submission",
                    status="completed",
                    output_payload={"cancelled_submission_count": cancelled},
                    now=current,
                )
            return self._transition_if_needed(automation_id, AutomationState.PAUSED, now=current)
        self._sync_external_lifecycle(automation_id, "pause")
        self.store.set_submission_lifecycle(automation_id, lifecycle="paused", now=current)
        return self._transition_if_needed(automation_id, AutomationState.PAUSED, now=current)

    def resume(self, automation_id: str, *, now: datetime | None = None) -> Mapping[str, Any]:
        current = _utc(now)
        self._sync_external_lifecycle(automation_id, "resume")
        self.store.set_submission_lifecycle(automation_id, lifecycle="active", now=current)
        return self._transition_if_needed(automation_id, AutomationState.ACTIVE, now=current)

    def archive(self, automation_id: str, *, now: datetime | None = None) -> Mapping[str, Any]:
        current = _utc(now)
        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        version = self.store.latest_version(automation_id) or {}
        backend = _automation_backend(automation, version)
        requires_external_archive = backend != "n8n" or bool(
            str(automation.get("backend_reference") or "").strip()
        )
        if requires_external_archive:
            self._sync_external_lifecycle(automation_id, "archive")
        else:
            # A historical failed/pending deployment can have no n8n workflow
            # reference at all.  It must still be possible to clear it from
            # the live workspace while retaining every durable receipt.
            cancelled = self.store.cancel_pending_submissions(
                automation_id,
                reason="obsolete Automation archived before external deployment",
                now=current,
            )
            self.store.save_execution(
                automation_id,
                stage="archive_without_external_workflow",
                status="completed",
                output_payload={"cancelled_submission_count": cancelled},
                now=current,
            )
        self.store.set_submission_lifecycle(automation_id, lifecycle="archived", now=current)
        return self._transition_if_needed(automation_id, AutomationState.ARCHIVED, now=current)

    def _sync_external_lifecycle(self, automation_id: str, action: str) -> None:
        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        version = self.store.latest_version(automation_id) or {}
        if _automation_backend(automation, version) != "n8n":
            return
        reference = str(automation.get("backend_reference") or "").strip()
        if not reference:
            raise RuntimeError("n8n lifecycle change requires a deployed workflow reference")
        operation = getattr(self.backends["n8n"], action)
        result = operation(reference)
        self.store.save_execution(
            automation_id,
            stage=f"n8n_{action}",
            status="completed" if result.accepted else "failed",
            output_payload=asdict(result),
        )
        if not result.accepted:
            raise RuntimeError(str(result.detail.get("reason") or f"n8n {action} failed"))

    def poll_due_schedules(self, *, now: datetime | None = None):
        """Run one product-time polling cycle over durable internal schedules."""

        from .time_poller import ProductTimePoller

        return ProductTimePoller(self.store, self.handle_scheduler_callback).poll(now=now)

    async def poll_due_schedules_async(
        self,
        *,
        reanalyze: AsyncReanalyze,
        now: datetime | None = None,
    ):
        """Poll durable time schedules using real async Agent reanalysis."""

        from .time_poller import ProductTimePoller

        async def dispatch(
            schedule_id: str,
            event: Mapping[str, Any],
            *,
            now: datetime,
            receipt_id: str,
        ) -> TriggerOutcome:
            return await self.handle_scheduler_callback_async(
                schedule_id,
                event,
                reanalyze=reanalyze,
                now=now,
                receipt_id=receipt_id,
            )

        return await ProductTimePoller(self.store, dispatch).poll_async(now=now)

    def _transition(self, automation_id: str, target: AutomationState, *, now: datetime) -> Mapping[str, Any]:
        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        self.lifecycle.require(str(automation["state"]), target)
        return self.store.set_state(automation_id, target.value, now=now)

    def _transition_if_needed(
        self,
        automation_id: str,
        target: AutomationState,
        *,
        now: datetime,
    ) -> Mapping[str, Any]:
        """Reconcile the backend even when the durable state already agrees.

        A prior crash or callback-secret rotation can leave n8n published while
        Stock AI already records the Automation as paused.  Repeating Pause
        must repair that external drift instead of failing with ``paused ->
        paused`` and leaving the old workflow to keep calling back.
        """

        automation = self.store.get_automation(automation_id)
        if not automation:
            raise KeyError(f"unknown automation: {automation_id}")
        if str(automation["state"]) == target.value:
            return automation
        return self._transition(automation_id, target, now=now)

    def _submit_internal_schedule(
        self,
        definition: Mapping[str, Any],
        dry_run: bool,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if dry_run:
            return {
                "accepted": True,
                "would_schedule": True,
                "callback_ready": self.reanalyze is not None or self._async_reanalysis_ready,
                "durability": "sqlite_schedule_registry",
            }
        if self.reanalyze is None and not self._async_reanalysis_ready:
            return {
                "accepted": False,
                "submission_status": "pending",
                "durable_submission_required": True,
                "reason": "automation reanalysis callback is not configured",
            }
        automation_id = str(context.get("automation_id") or "")
        if not automation_id:
            return {
                "accepted": False,
                "submission_status": "failed",
                "reason": "scheduler registration context has no automation_id",
            }
        registered = self.store.register_schedule(
            automation_id,
            automation_version=int(context.get("automation_version") or 0),
            definition=definition,
            idempotency_key=str(context.get("idempotency_key") or ""),
            now=context.get("now") if isinstance(context.get("now"), datetime) else None,
        )
        schedule = dict(registered["schedule"])
        receipt = dict(registered["receipt"])
        return {
            "accepted": receipt.get("status") == "accepted",
            "scheduled": schedule.get("status") == "active",
            "schedule_id": schedule.get("schedule_id"),
            "receipt_id": receipt.get("receipt_id"),
            "durability": "sqlite_schedule_registry",
            "callback_ready": True,
        }

    def _expire_one_shot(self, schedule_id: str, *, now: datetime) -> None:
        schedule = self.store.get_schedule(schedule_id) or {}
        trigger_type = str(dict(schedule.get("trigger") or {}).get("type") or "").casefold()
        if trigger_type not in {"once", "one_shot"}:
            return
        automation_id = str(schedule.get("automation_id") or "")
        automation = self.store.get_automation(automation_id) or {}
        if automation.get("state") == AutomationState.ACTIVE.value:
            self._transition(automation_id, AutomationState.EXPIRED, now=now)


def _compiled_payload(workflow: CompiledWorkflow) -> dict[str, Any]:
    return {
        "schema_version": workflow.schema_version,
        "backend": workflow.backend,
        "definition": dict(workflow.definition),
        "user_artifact": dict(workflow.user_artifact),
        "credential_refs": list(workflow.credential_refs),
        "model_routes": {key: dict(value) for key, value in workflow.model_routes.items()},
        "digest": workflow.digest,
    }


def _compiled_from_payload(payload: Mapping[str, Any]) -> CompiledWorkflow:
    return CompiledWorkflow(
        backend=str(payload["backend"]),
        definition=dict(payload.get("definition") or {}),
        user_artifact=dict(payload.get("user_artifact") or payload.get("artifact") or {}),
        credential_refs=tuple(str(item) for item in payload.get("credential_refs") or ()),
        model_routes={str(key): dict(value) for key, value in dict(payload.get("model_routes") or {}).items()},
        digest=str(payload["digest"]),
        schema_version=str(payload.get("schema_version") or "open_stock_ai.compiled_automation.v1"),
    )


def _merge_intents(existing: AutomationIntent, incoming: AutomationIntent) -> AutomationIntent:
    def unique(items: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
        seen: set[str] = set()
        merged: list[Mapping[str, Any]] = []
        for item in items:
            key = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                merged.append(dict(item))
        return tuple(merged)

    conditions = [dict(existing.decision_logic), dict(incoming.decision_logic)]
    decision_logic = conditions[0] if conditions[0] == conditions[1] else {"any": conditions}
    channels = tuple(dict.fromkeys((*existing.notification_policy.channels, *incoming.notification_policy.channels)))
    payload = incoming.to_semantic_dict()
    payload.update(
        {
            "observations": unique((*existing.observations, *incoming.observations)),
            "analysis": unique((*existing.analysis, *incoming.analysis)),
            "decision_logic": decision_logic,
            "actions": unique((*existing.actions, *incoming.actions)),
            "notification_policy": {
                "channels": channels,
                "cooldown_seconds": min(
                    existing.notification_policy.cooldown_seconds,
                    incoming.notification_policy.cooldown_seconds,
                ),
                "meaningful_only": existing.notification_policy.meaningful_only and incoming.notification_policy.meaningful_only,
                "expires_after_seconds": incoming.notification_policy.expires_after_seconds,
                "allow_fallback": existing.notification_policy.allow_fallback or incoming.notification_policy.allow_fallback,
            },
        }
    )
    return AutomationIntent.from_dict(payload)


def _explicit_symbol(text: str) -> str | None:
    match = re.search(r"(?<![A-Za-z0-9])(\d{4,6})\.(TW|TWO)(?![A-Za-z0-9])", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}.{match.group(2).upper()}"
    return None


def _draft_trigger(kind: AutomationKind, goal: str) -> dict[str, Any]:
    if kind is AutomationKind.CONDITION_WATCH:
        return {"type": "price_crossing", "field": "price"}
    if kind is AutomationKind.EVENT_WATCH:
        return {"type": "market_event", "event_type": "market.event"}
    if kind is AutomationKind.RECURRING:
        return {"type": "schedule", "frequency": "daily"}
    if kind is AutomationKind.ONE_SHOT:
        return {"type": "once"}
    if kind is AutomationKind.CROSS_SYSTEM:
        return {"type": "event", "event_type": "market.event"}
    raise ValueError(f"Unsupported automation draft kind: {kind.value}")


def _draft_condition(goal: str) -> dict[str, Any]:
    """Keep a threshold only when it is explicitly tied to a price phrase."""

    normalized = goal.casefold()
    match = re.search(
        r"(?:股價|price)\s*(?:突破|達到|高於|低於|>=|<=|>|<|above|below|cross(?:es|ing)?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        normalized,
    )
    if not match:
        return {}
    value = float(match.group(1).replace(",", ""))
    relation = match.group(0)
    operator = "gte"
    if any(token in relation for token in ("低於", "<=", "<", "below")):
        operator = "lte"
    return {"field": "price", "operator": operator, "value": value}


def _cheap_filter(event: Mapping[str, Any], logic: Mapping[str, Any]) -> bool:
    # A sealed n8n Schedule Trigger is already the deterministic candidate
    # event.  Its payload intentionally does not contain live market fields;
    # the same-Session Agent reanalysis fetches those observations after the
    # wake-up.  Applying a price predicate to the schedule envelope would
    # filter every real n8n callback before the Agent ever runs.
    if (
        str(event.get("event_type") or "") == "automation.n8n.trigger"
        and str(event.get("source") or "") == "n8n"
        and str(event.get("automation_id") or "").strip()
    ):
        return True
    if not logic:
        return True
    conditions = logic.get("all") or logic.get("conditions")
    if isinstance(conditions, list):
        return all(_match(event, condition) for condition in conditions)
    if isinstance(logic.get("any"), list):
        return any(_match(event, condition) for condition in logic["any"])
    return _match(event, logic)


def _match(event: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    field = str(condition.get("field") or "value")
    operator = str(condition.get("operator") or condition.get("op") or "eq").casefold()
    expected = condition.get("value")
    actual: Any = event
    for part in field.split("."):
        if not isinstance(actual, Mapping) or part not in actual:
            return False
        actual = actual[part]
    operations = {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "gt": lambda: float(actual) > float(expected),
        "gte": lambda: float(actual) >= float(expected),
        "lt": lambda: float(actual) < float(expected),
        "lte": lambda: float(actual) <= float(expected),
        "contains": lambda: expected in actual,
    }
    if operator not in operations:
        raise ValueError(f"unsupported deterministic condition operator: {operator}")
    try:
        return bool(operations[operator]())
    except (TypeError, ValueError):
        return False


def _decision_changed(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> bool:
    if not previous:
        return True
    keys = ("action", "symbol", "rating", "risk_level", "recommendation")
    before = {key: previous.get(key) for key in keys if previous.get(key) is not None}
    after = {key: current.get(key) for key in keys if current.get(key) is not None}
    # With a structured decision, wording alone is not a strategy change.
    # Narrative-only integrations retain their existing conclusion comparison.
    if not any(previous.get(key) or current.get(key) for key in keys if key != "symbol"):
        before["conclusion"] = previous.get("conclusion")
        after["conclusion"] = current.get("conclusion")
    if before != after:
        return True
    def confidence(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(numeric):
            return 0.0
        return max(0.0, min(1.0, numeric / 100.0 if numeric > 1.0 else numeric))
    return abs(confidence(current.get("confidence")) - confidence(previous.get("confidence"))) >= 0.15 - 1e-12


def _duplicate_callback_outcome(receipt: Mapping[str, Any]) -> TriggerOutcome:
    response = dict(receipt.get("response") or {})
    return TriggerOutcome(
        automation_id=str(receipt.get("automation_id") or ""),
        condition_candidate=str(receipt.get("status")) != "filtered",
        reanalyzed=False,
        meaningful_change=False,
        notified=False,
        decision=None,
        receipt=None,
        reason="duplicate scheduler occurrence already claimed",
        execution_id=str(response.get("execution_id") or "") or None,
    )


def _existing_execution_outcome(
    automation_id: str,
    execution: Mapping[str, Any],
) -> TriggerOutcome:
    output = dict(execution.get("output") or {})
    decision_value = output.get("decision")
    decision = dict(decision_value) if isinstance(decision_value, Mapping) else None
    return TriggerOutcome(
        automation_id=automation_id,
        condition_candidate=str(execution.get("status")) != "filtered",
        reanalyzed=False,
        meaningful_change=bool(execution.get("meaningful_change")),
        notified=False,
        decision=decision,
        receipt=None,
        reason="durable trigger execution already completed",
        execution_id=str(execution.get("execution_id") or "") or None,
    )


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _automation_backend(
    automation: Mapping[str, Any],
    version: Mapping[str, Any],
) -> str:
    """Read the persisted compiler backend without relying on a flattened row.

    ``agent_automation_versions`` stores the backend inside ``compiled_json``;
    it is not a top-level version column.  The Automation row retains the
    same backend as a durable projection.  Reading both keeps lifecycle calls
    bound to the real n8n workflow instead of silently treating it as an
    internal scheduler.
    """

    compiled = version.get("compiled")
    return str(
        (compiled.get("backend") if isinstance(compiled, Mapping) else None)
        or automation.get("backend")
        or ""
    ).strip()
