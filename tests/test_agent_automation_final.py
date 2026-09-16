from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import ssl
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.automation import (
    AutomationController,
    AutomationIntent,
    AutomationKind,
    AutomationPolicyEngine,
    AutomationState,
    AutomationStore,
    CostPolicy,
    HeadlessN8nAdapter,
    InternalSchedulerBackend,
    Lifecycle,
    ModelRoleRouter,
    NotificationManager,
    NotificationPolicy,
    OpportunityDetector,
    ProductTimePoller,
    WorkflowCompiler,
)
from open_stock_ai.agent_runtime.automation.dedup import DedupDecision, DedupResult, semantic_fingerprint
from open_stock_ai.agent_runtime.automation.notifications import DeliveryStatus
from stock_ai.n8n_automation import (
    N8nGatewayConfig,
    N8nGatewayExecutor,
    _verify_peer_certificate_pin,
    build_n8n_gateway_adapter,
)
from stock_ai.market_calendar import default_taiwan_market_calendar


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def _intent(
    *,
    goal: str = "監控緯創下一個分批停利時機",
    trigger: dict | None = None,
    decision_logic: dict | None = None,
    actions: tuple[dict, ...] = ({"type": "notify", "message": "策略改變"},),
    channels: tuple[str, ...] = ("in_app",),
    kind: AutomationKind = AutomationKind.CONDITION_WATCH,
) -> AutomationIntent:
    return AutomationIntent(
        goal=goal,
        user_id="user-1",
        symbol="3231.TW",
        kind=kind,
        trigger=trigger or {"type": "price_crossing", "field": "price"},
        observations=(
            {"type": "market_price", "label": "取得最新行情"},
            {"type": "institutional_flow", "label": "取得籌碼變化"},
        ),
        analysis=({"type": "strategy_reanalysis"},),
        decision_logic=decision_logic or {"field": "price", "operator": "gte", "value": 120},
        actions=actions,
        notification_policy=NotificationPolicy(
            channels=channels,
            cooldown_seconds=300,
            meaningful_only=True,
            expires_after_seconds=600,
            allow_fallback=True,
        ),
        lifecycle={"expires_after_days": 90},
        cost_policy=CostPolicy(
            budget_class="balanced",
            role_models={"repair": "small-json-model", "critic": "strong-review-model"},
        ),
    )


def _controller(tmp_path, *, senders=None, n8n=None, internal=None):
    store = AutomationStore(tmp_path / "automation.sqlite")
    notifications = NotificationManager(store, senders or {"in_app": lambda payload: {"accepted": True, "id": "local-1"}})
    return store, AutomationController(
        store,
        notifications=notifications,
        n8n_backend=n8n,
        internal_backend=internal,
        reanalyze=lambda intent, event, previous: {
            "conclusion": "重新分析完成",
            "event": dict(event),
        },
    )


def test_opportunity_detector_supports_all_product_choices():
    detector = OpportunityDetector()
    assert detector.detect("直接解釋這段資料").kind == AutomationKind.NO_AUTOMATION
    assert detector.detect("明天提醒我一次").kind == AutomationKind.ONE_SHOT
    assert detector.detect("每天產生報告").kind == AutomationKind.RECURRING
    assert detector.detect("監控價格突破 120").kind == AutomationKind.CONDITION_WATCH
    assert detector.detect("公告事件發生時重跑分析").kind == AutomationKind.EVENT_WATCH
    assert detector.detect("分析後寄 Email").kind == AutomationKind.CROSS_SYSTEM


def test_opportunity_detector_honors_explicit_no_automation_constraint():
    detector = OpportunityDetector(
        classifier=lambda *_: {"kind": AutomationKind.CONDITION_WATCH.value, "confidence": 0.99}
    )

    opportunity = detector.detect(
        "請討論 n8n 自主監控架構的設計原則；不要建立自動化、不要查市場、不要交易。"
    )

    assert opportunity.kind is AutomationKind.NO_AUTOMATION
    assert opportunity.worthwhile is False
    assert opportunity.confidence == 1.0
    assert "explicitly" in opportunity.reason

    mixed_language = detector.detect(
        "請分析 2330.TW，但不要交易、不要建立 Automation。"
    )
    assert mixed_language.kind is AutomationKind.NO_AUTOMATION
    assert mixed_language.worthwhile is False

    coordinated = detector.detect(
        "請分析 2330.TW，只做分析，不要下單或建立自動化。"
    )
    assert coordinated.kind is AutomationKind.NO_AUTOMATION
    assert coordinated.worthwhile is False


def test_opportunity_detector_uses_structured_future_value_without_goal_keywords():
    detector = OpportunityDetector()

    opportunity = detector.detect(
        "請替我處理這件事",
        {
            "automation_assessment": {
                "future_trigger": True,
                "repeatable": True,
                "cadence": "business_day",
                "decision_impact": "high",
                "expected_value": 0.9,
                "manual_cost": 0.8,
            }
        },
    )
    rejected = detector.detect(
        "每天與監控只是這份架構文件中的例子",
        {"automation_assessment": {"worthwhile": False, "reason": "discussion only"}},
    )

    assert opportunity.kind is AutomationKind.RECURRING
    assert opportunity.worthwhile and opportunity.confidence >= 0.8
    assert rejected.kind is AutomationKind.NO_AUTOMATION
    assert rejected.reason == "discussion only"


def test_intent_is_semantic_and_rejects_execution_technology():
    payload = _intent().to_semantic_dict()
    assert "n8n" not in json.dumps(payload, ensure_ascii=False).casefold()
    payload["actions"] = [{"type": "notify", "credential": "plain-secret"}]
    with pytest.raises(ValueError, match="technical execution detail"):
        AutomationIntent.from_dict(payload)


def test_policy_separates_monitoring_external_message_and_trade_approval():
    policy = AutomationPolicyEngine()
    monitoring = policy.evaluate(_intent())
    assert monitoring.allowed and monitoring.auto_activatable
    external = policy.evaluate(_intent(channels=("telegram",)))
    assert external.allowed and not external.auto_activatable
    assert external.external_permission_required and not external.trade_approval_required
    permitted = policy.evaluate(_intent(channels=("telegram",)), external_permission=True)
    assert permitted.auto_activatable
    trading = policy.evaluate(_intent(actions=({"type": "live_trade"},)))
    assert not trading.allowed and trading.trade_approval_required


def test_compiler_routes_internal_and_headless_n8n_without_artifact_leaks():
    compiler = WorkflowCompiler()
    internal = compiler.compile(_intent())
    assert internal.backend == "internal_scheduler"
    assert compiler.validate(internal).valid
    assert internal.model_routes["repair"]["tier"] == "economy"
    assert internal.model_routes["critic"]["model"] == "strong-review-model"

    recurring = _intent(
        goal="每日整理緯創資料並寄送報告",
        trigger={"type": "schedule", "frequency": "daily", "at": "08:00"},
        channels=("email",),
        kind=AutomationKind.CROSS_SYSTEM,
        actions=({"type": "email", "content": "daily report"},),
    )
    compiled = compiler.compile(recurring, credential_refs=("secret://notifications/email-primary",))
    assert compiled.backend == "n8n"
    visible = json.dumps(compiled.user_artifact, ensure_ascii=False).casefold()
    assert all(token not in visible for token in ("credential", "webhook", "http request", "n8n node"))
    adapter = HeadlessN8nAdapter(lambda request: {"accepted": True, "workflow_id": "wf-1"})
    activated = adapter.activate(compiled)
    assert activated.backend_reference == "wf-1"
    request = adapter.published["wf-1"]
    assert request["credential_refs"] == ["secret://notifications/email-primary"]
    with pytest.raises((TypeError, AttributeError)):
        InternalSchedulerBackend().activate({"backend": "internal_scheduler"})  # type: ignore[arg-type]


def test_model_role_router_uses_cheap_and_strong_models_by_role():
    router = ModelRoleRouter()
    policy = CostPolicy(role_models={"repair": "tiny", "high_risk_review": "largest"})
    assert router.route("repair", policy).tier == "economy"
    assert router.route("repair", policy).model == "tiny"
    review = router.route("critic", policy, high_risk=True)
    assert review.tier == "strong" and review.model == "largest"


def test_full_proposal_compile_validate_dry_run_activate_is_durable(tmp_path):
    store, controller = _controller(tmp_path)
    proposal = controller.propose("監控緯創突破 120 後重新評估")
    assert proposal.requires_user_confirmation
    assert proposal.host_status == "正在準備自動化提案"
    assert proposal.artifact_preview["steps"]

    outcome = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = outcome.automation["automation_id"]
    assert outcome.automation["state"] == AutomationState.ACTIVE.value
    assert outcome.dedup.decision == DedupDecision.CREATE
    assert outcome.dry_run and outcome.dry_run.accepted
    assert outcome.activation and outcome.activation.accepted
    assert outcome.activation.detail["receipt_id"]
    assert outcome.host_status == "自動化已啟用"
    assert store.latest_version(automation_id)["version"] == 1
    assert [item["stage"] for item in store.list_executions(automation_id)] == ["dry_run", "activate"]

    with sqlite3.connect(store.path) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {
        "agent_automation_intents",
        "agent_automations",
        "agent_automation_versions",
        "agent_automation_executions",
        "agent_notification_deliveries",
        "agent_automation_submissions",
        "agent_automation_schedules",
        "agent_automation_schedule_receipts",
    }.issubset(tables)
    schedule = store.get_schedule(outcome.activation.backend_reference)
    receipts = store.list_schedule_receipts(outcome.activation.backend_reference)
    assert schedule and schedule["status"] == "active"
    assert receipts[0]["receipt_type"] == "registration"
    assert receipts[0]["status"] == "accepted"


def test_internal_scheduler_without_submit_is_pending_then_recovered_after_process_reopen(tmp_path):
    store, controller = _controller(tmp_path, internal=InternalSchedulerBackend())
    pending = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = pending.automation["automation_id"]

    assert pending.automation["state"] == AutomationState.TESTING.value
    assert pending.activation and not pending.activation.accepted
    assert pending.host_status == "等待排程器接管"
    durable = store.list_submissions(status="pending")
    assert len(durable) == 1
    assert durable[0]["automation_id"] == automation_id
    assert durable[0]["request"]["operation"] == "create"

    reopened_store = AutomationStore(store.path)
    recovered_controller = AutomationController(
        reopened_store,
        reanalyze=lambda intent, event, previous: {"conclusion": "recovered"},
    )
    recovered = recovered_controller.recover_pending_submissions(now=NOW + timedelta(minutes=1))

    assert len(recovered) == 1 and recovered[0].accepted
    assert reopened_store.get_automation(automation_id)["state"] == AutomationState.ACTIVE.value
    schedule_id = reopened_store.get_automation(automation_id)["backend_reference"]
    assert reopened_store.get_schedule(schedule_id)["status"] == "active"
    assert recovered[0].detail["receipt_id"]
    assert reopened_store.list_submissions(status="pending") == []
    assert reopened_store.list_submissions(status="active")[0]["backend_reference"] == schedule_id


def test_durable_scheduler_callback_runs_reanalysis_and_notification(tmp_path):
    reanalysis_calls = []
    notifications = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(
        store,
        notifications=NotificationManager(
            store,
            {
                "in_app": lambda payload: notifications.append(dict(payload))
                or {"accepted": True, "message_id": "notice-1"}
            },
        ),
        reanalyze=lambda intent, event, previous: reanalysis_calls.append(
            (intent.goal, dict(event), previous)
        )
        or {
            "conclusion": "提高停利保護",
            "confidence": 0.93,
            "evidence_ids": ["EV-price-1"],
        },
    )
    activated = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    schedule_id = str(activated.activation.backend_reference)

    outcome = controller.backends["internal_scheduler"].dispatch(
        schedule_id,
        {"price": 125, "source_event_id": "market-1"},
    )

    assert outcome.reanalyzed and outcome.meaningful_change and outcome.notified
    assert reanalysis_calls[0][1]["source_event_id"] == "market-1"
    assert notifications[0]["decision"]["evidence_ids"] == ["EV-price-1"]
    receipts = store.list_schedule_receipts(schedule_id)
    assert [item["receipt_type"] for item in receipts] == ["registration", "trigger"]
    assert receipts[-1]["status"] == "completed"
    assert receipts[-1]["response"]["execution_id"] == outcome.execution_id
    assert receipts[-1]["response"]["notification_receipt_id"] == outcome.receipt.delivery_id
    assert store.get_schedule(schedule_id)["status"] == "active"
    assert store.get_automation(activated.automation["automation_id"])["state"] == AutomationState.ACTIVE.value


def test_product_time_poller_coalesces_missed_intervals_and_is_restart_idempotent(tmp_path):
    calls = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(
        store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "重新分析", "evidence_ids": [event["source_event_id"]]},
    )
    intent = replace(
        _intent(),
        kind=AutomationKind.RECURRING,
        trigger={"type": "interval", "interval_seconds": 60, "misfire_policy": "run_once"},
        decision_logic={},
    )
    activated = controller.confirm_and_activate(intent, user_confirmed=True, now=NOW)
    schedule_id = str(activated.activation.backend_reference)

    first = ProductTimePoller(store, controller.handle_scheduler_callback).poll(
        now=NOW + timedelta(minutes=5)
    )
    reopened_store = AutomationStore(store.path)
    reopened_controller = AutomationController(
        reopened_store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "不應重複"},
    )
    second = ProductTimePoller(
        reopened_store,
        reopened_controller.handle_scheduler_callback,
    ).poll(now=NOW + timedelta(minutes=5))

    assert first.claimed == first.completed == 1
    assert first.failed == first.skipped == 0
    assert calls[0]["missed_occurrences"] == 4
    assert calls[0]["misfire_policy"] == "run_once"
    assert second.claimed == second.recovered == second.completed == 0
    time_receipts = [
        item for item in reopened_store.list_schedule_receipts(schedule_id)
        if item["receipt_type"] == "time_trigger"
    ]
    assert len(time_receipts) == 1
    assert time_receipts[0]["status"] == "completed"
    assert reopened_store.get_schedule(schedule_id)["next_due_at"] > (
        NOW + timedelta(minutes=5)
    ).isoformat()


def test_product_time_poller_defers_taiwan_holiday_without_creating_run(tmp_path):
    calls = []
    store = AutomationStore(
        tmp_path / "automation.sqlite",
        market_calendar=default_taiwan_market_calendar(),
    )
    controller = AutomationController(
        store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "不應在休市日執行"},
    )
    intent = replace(
        _intent(),
        kind=AutomationKind.RECURRING,
        trigger={
            "type": "interval",
            "interval_seconds": 60,
            "market_calendar": "taiwan",
            "misfire_policy": "run_once",
        },
        decision_logic={},
    )
    activated = controller.confirm_and_activate(
        intent,
        user_confirmed=True,
        now=datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    )
    schedule_id = str(activated.activation.backend_reference)
    result = ProductTimePoller(store, controller.handle_scheduler_callback).poll(
        now=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc),
    )

    assert result.claimed == result.completed == 0
    assert result.skipped == 1
    assert calls == []
    schedule = store.get_schedule(schedule_id)
    assert schedule["last_error"] == "deferred_market_closed"
    assert schedule["next_due_at"].startswith("2026-09-29T")


def test_product_time_poller_recovers_claimed_callback_after_process_reopen(tmp_path):
    calls = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(
        store,
        reanalyze=lambda intent, event, previous: {"conclusion": "initial"},
    )
    intent = replace(
        _intent(),
        kind=AutomationKind.RECURRING,
        trigger={"type": "interval", "interval_seconds": 60},
        decision_logic={},
    )
    activated = controller.confirm_and_activate(intent, user_confirmed=True, now=NOW)
    schedule_id = str(activated.activation.backend_reference)
    claim = store.claim_due_schedule(schedule_id, now=NOW + timedelta(minutes=2))
    assert claim["claimed"] and claim["receipt"]["status"] == "processing"

    reopened_store = AutomationStore(store.path)
    restarted = AutomationController(
        reopened_store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "recovered after restart"},
    )
    result = ProductTimePoller(
        reopened_store,
        restarted.handle_scheduler_callback,
    ).poll(now=NOW + timedelta(minutes=2))

    assert result.recovered == result.completed == 1
    assert result.failed == 0
    assert calls[0]["restart_recovery"] is True
    assert calls[0]["source_event_id"] == claim["event"]["source_event_id"]
    receipt = reopened_store.get_schedule_receipt(claim["receipt"]["receipt_id"])
    assert receipt["status"] == "completed"
    assert reopened_store.list_incomplete_time_callbacks() == []


def test_restart_recovery_reuses_completed_execution_if_receipt_was_not_finished(tmp_path):
    calls = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(store, reanalyze=lambda *_: {"conclusion": "ready"})
    intent = replace(
        _intent(),
        kind=AutomationKind.RECURRING,
        trigger={"type": "interval", "interval_seconds": 60},
        decision_logic={},
    )
    activated = controller.confirm_and_activate(intent, user_confirmed=True, now=NOW)
    automation_id = activated.automation["automation_id"]
    schedule_id = str(activated.activation.backend_reference)
    claim = store.claim_due_schedule(schedule_id, now=NOW + timedelta(minutes=2))

    completed_before_crash = controller.handle_trigger(
        automation_id,
        claim["event"],
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "already completed"},
        now=NOW + timedelta(minutes=2),
    )
    assert completed_before_crash.reanalyzed and len(calls) == 1
    assert store.get_schedule_receipt(claim["receipt"]["receipt_id"])["status"] == "processing"

    reopened_store = AutomationStore(store.path)
    restarted = AutomationController(
        reopened_store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "must not run"},
    )
    recovered = ProductTimePoller(
        reopened_store,
        restarted.handle_scheduler_callback,
    ).poll(now=NOW + timedelta(minutes=2))

    assert recovered.recovered == recovered.completed == 1
    assert len(calls) == 1
    assert reopened_store.get_schedule_receipt(claim["receipt"]["receipt_id"])["status"] == "completed"
    matching = reopened_store.get_execution_by_source_event(
        automation_id,
        claim["event"]["source_event_id"],
    )
    assert matching["execution_id"] == completed_before_crash.execution_id


def test_product_time_poller_supports_async_durable_reanalysis(tmp_path):
    calls = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(store)
    controller.enable_async_reanalysis()
    intent = replace(
        _intent(),
        kind=AutomationKind.RECURRING,
        trigger={"type": "interval", "interval_seconds": 60},
        decision_logic={},
    )
    activated = controller.confirm_and_activate(intent, user_confirmed=True, now=NOW)

    async def reanalyze(intent, event, previous):
        calls.append(dict(event))
        return {"conclusion": "async durable run completed"}

    result = asyncio.run(
        controller.poll_due_schedules_async(
            reanalyze=reanalyze,
            now=NOW + timedelta(minutes=1),
        )
    )

    assert result.claimed == result.completed == 1
    assert len(calls) == 1
    schedule = store.get_schedule(str(activated.activation.backend_reference))
    assert schedule["status"] == "active"


def test_duplicate_scheduler_event_is_deduplicated_before_reanalysis(tmp_path):
    calls = []
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(
        store,
        reanalyze=lambda intent, event, previous: calls.append(dict(event))
        or {"conclusion": "第一次分析"},
    )
    activated = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    schedule_id = str(activated.activation.backend_reference)
    event = {"price": 125, "source_event_id": "market-event-stable-1"}

    first = controller.handle_scheduler_callback(schedule_id, event, now=NOW + timedelta(minutes=1))
    duplicate = controller.handle_scheduler_callback(schedule_id, event, now=NOW + timedelta(minutes=2))

    assert first.reanalyzed and len(calls) == 1
    assert not duplicate.reanalyzed and not duplicate.notified
    assert "duplicate" in duplicate.reason
    trigger_receipts = [
        item for item in store.list_schedule_receipts(schedule_id)
        if item["receipt_type"] == "trigger"
    ]
    assert len(trigger_receipts) == 1


def test_internal_backend_rejects_reference_without_durable_receipt(tmp_path):
    backend = InternalSchedulerBackend(
        lambda definition, dry_run: (
            {"accepted": True, "would_schedule": True}
            if dry_run
            else {"accepted": True, "scheduled": True, "schedule_id": "hash-only"}
        )
    )
    store, controller = _controller(tmp_path, internal=backend)

    outcome = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)

    assert outcome.activation and not outcome.activation.accepted
    assert outcome.activation.detail["durable_registration_required"] is True
    assert outcome.automation["state"] == AutomationState.FAILED.value
    assert store.list_schedules() == []


def test_scheduler_callback_failure_is_durable_and_does_not_stay_active(tmp_path):
    store = AutomationStore(tmp_path / "automation.sqlite")
    controller = AutomationController(
        store,
        reanalyze=lambda intent, event, previous: (_ for _ in ()).throw(
            RuntimeError("provider disconnected")
        ),
    )
    activated = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    schedule_id = str(activated.activation.backend_reference)

    with pytest.raises(RuntimeError, match="provider disconnected"):
        controller.backends["internal_scheduler"].dispatch(schedule_id, {"price": 125})

    schedule = store.get_schedule(schedule_id)
    receipts = store.list_schedule_receipts(schedule_id)
    assert schedule["status"] == "failed"
    assert "provider disconnected" in schedule["last_error"]
    assert receipts[-1]["receipt_type"] == "trigger"
    assert receipts[-1]["status"] == "failed"
    assert store.get_automation(activated.automation["automation_id"])["state"] == AutomationState.FAILED.value


def test_confirmation_permission_and_semantic_reuse_guards(tmp_path):
    store, controller = _controller(tmp_path)
    with pytest.raises(PermissionError, match="user confirmation"):
        controller.confirm_and_activate(_intent(), user_confirmed=False)
    external = _intent(channels=("telegram",))
    pending = controller.confirm_and_activate(external, user_confirmed=True, external_permission=False, now=NOW)
    assert pending.automation["state"] == AutomationState.VALIDATED.value
    assert pending.activation is None

    first = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    duplicate = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW + timedelta(seconds=1))
    assert duplicate.dedup.decision == DedupDecision.REUSE
    assert duplicate.automation["automation_id"] == first.automation["automation_id"]
    assert store.latest_version(first.automation["automation_id"])["version"] == 1


def test_dedup_update_and_merge_retarget_existing_automation_instead_of_creating(tmp_path):
    store, controller = _controller(tmp_path)
    first = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = first.automation["automation_id"]

    class ForcedDeduplicator:
        def __init__(self, decision):
            self.decision = decision

        def resolve(self, intent, candidates):
            return DedupResult(
                self.decision,
                automation_id,
                semantic_fingerprint(intent),
                0.95 if self.decision == DedupDecision.UPDATE else 0.8,
                "forced durable dedup path",
            )

    controller.deduplicator = ForcedDeduplicator(DedupDecision.UPDATE)
    updated_intent = replace(_intent(), decision_logic={"field": "price", "operator": "gte", "value": 130})
    updated = controller.confirm_and_activate(updated_intent, user_confirmed=True, now=NOW + timedelta(minutes=1))
    assert updated.automation["automation_id"] == automation_id
    assert updated.version == 2
    assert store.latest_version(automation_id)["intent"]["decision_logic"]["value"] == 130

    controller.deduplicator = ForcedDeduplicator(DedupDecision.MERGE)
    merged_intent = replace(
        _intent(),
        goal="監控緯創籌碼反轉並重新評估",
        decision_logic={"field": "institutional_flow", "operator": "gt", "value": 0},
        actions=({"type": "notify", "message": "籌碼反轉"},),
    )
    merged = controller.confirm_and_activate(merged_intent, user_confirmed=True, now=NOW + timedelta(minutes=2))
    latest = store.latest_version(automation_id)
    assert merged.automation["automation_id"] == automation_id
    assert merged.version == 3
    assert "any" in latest["intent"]["decision_logic"]
    assert len(latest["intent"]["actions"]) == 2
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("select count(*) from agent_automations").fetchone()[0] == 1


def test_headless_n8n_pipeline_requires_permission_and_credential_reference(tmp_path):
    requests = []
    n8n = HeadlessN8nAdapter(lambda request: requests.append(request) or {"accepted": True, "workflow_id": "n8n-42"})
    store, controller = _controller(tmp_path, senders={"email": lambda payload: True}, n8n=n8n)
    intent = _intent(
        goal="每日整理緯創資料並寄送 Email",
        trigger={"type": "schedule", "frequency": "daily", "at": "08:00"},
        channels=("email",),
        kind=AutomationKind.CROSS_SYSTEM,
        actions=({"type": "email", "content": "daily report"},),
    )
    with pytest.raises(ValueError, match="credential references"):
        controller.confirm_and_activate(intent, user_confirmed=True, external_permission=True, now=NOW)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("select count(*) from agent_automations").fetchone()[0] == 0
    outcome = controller.confirm_and_activate(
        intent,
        user_confirmed=True,
        external_permission=True,
        credential_refs=("credential-ref://email/primary",),
        now=NOW,
    )
    assert outcome.automation["state"] == "active"
    assert outcome.automation["backend_reference"] == "n8n-42"
    assert requests[0]["credential_refs"] == ["credential-ref://email/primary"]


def test_automation_backend_status_never_leaks_credentials_or_claims_unconfigured_n8n_is_active(tmp_path):
    _, controller = _controller(tmp_path)
    status = controller.backend_status()

    assert status["n8n"] == {
        "backend": "n8n",
        "configured": False,
        "activation_ready": False,
        "healthy": False,
        "mode": "headless_execution_only",
        "user_message": "外部工作流執行層尚未設定；自動化不會假裝已啟用。",
    }


def test_n8n_health_without_owner_or_api_key_is_explicitly_not_deployment_ready():
    calls = []
    setup = {"required": True}

    def request(method, url, headers, body, timeout):
        calls.append((method, url))
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/rest/settings"):
            return {
                "data": {
                    "userManagement": {
                        "authenticationMethod": "email",
                        "showSetupOnFirstLoad": setup["required"],
                    }
                }
            }
        raise AssertionError(f"deployment API must not be called before owner setup: {method} {url}")

    executor = N8nGatewayExecutor(
        N8nGatewayConfig(gateway_url="http://127.0.0.1:5678"),
        request=request,
    )
    adapter = HeadlessN8nAdapter(executor)
    compiled = WorkflowCompiler().compile(
        _intent(
            trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
            channels=("email",),
            kind=AutomationKind.CROSS_SYSTEM,
            actions=({"type": "email", "content": "report"},),
        ),
        credential_refs=("credential-ref://email/primary",),
    )

    status = adapter.status()
    dry_run = adapter.dry_run(compiled)
    activation = adapter.activate(compiled, context={"automation_id": "not-ready"})

    assert status["healthy"] is True
    assert status["setup_required"] is True
    assert status["owner_setup_complete"] is False
    assert status["api_key_configured"] is False
    assert status["api_authenticated"] is False
    assert status["activation_ready"] is False
    assert status["readiness_code"] == "owner_setup_required"
    assert dry_run.accepted is False and activation.accepted is False
    assert dry_run.detail["submission_status"] == "pending"
    assert activation.detail["durable_submission_required"] is True
    assert activation.detail["healthy"] is True
    assert activation.detail["setup_required"] is True
    assert not any("/api/v1/" in url for _, url in calls)

    setup["required"] = False
    owner_complete_but_key_missing = adapter.status()
    assert owner_complete_but_key_missing["healthy"] is True
    assert owner_complete_but_key_missing["owner_setup_complete"] is True
    assert owner_complete_but_key_missing["setup_required"] is False
    assert owner_complete_but_key_missing["activation_ready"] is False
    assert owner_complete_but_key_missing["readiness_code"] == "api_key_required"


def test_controller_preserves_approved_n8n_compiler_product_as_pending_until_readiness(tmp_path):
    def request(method, url, headers, body, timeout):
        del method, headers, body, timeout
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/rest/settings"):
            return {"data": {"userManagement": {"showSetupOnFirstLoad": True}}}
        raise AssertionError(f"n8n deployment must wait for owner setup: {url}")

    adapter = HeadlessN8nAdapter(
        N8nGatewayExecutor(
            N8nGatewayConfig(gateway_url="http://127.0.0.1:5678"),
            request=request,
        )
    )
    store, controller = _controller(tmp_path, n8n=adapter)
    outcome = controller.confirm_and_activate(
        _intent(
            goal="每日整理緯創資料並寄送 Email",
            trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
            channels=("email",),
            kind=AutomationKind.CROSS_SYSTEM,
            actions=({"type": "email", "content": "daily report"},),
        ),
        user_confirmed=True,
        external_permission=True,
        credential_refs=("credential-ref://email/primary",),
        now=NOW,
    )

    assert outcome.dry_run is not None and outcome.dry_run.accepted is False
    assert outcome.activation is None
    assert outcome.automation["state"] == AutomationState.TESTING.value
    pending = store.list_submissions(status="pending")
    assert len(pending) == 1
    assert pending[0]["automation_id"] == outcome.automation["automation_id"]
    assert pending[0]["response"]["detail"]["readiness_code"] == "owner_setup_required"
    assert outcome.host_status == "等待 n8n 執行層就緒"


def test_n8n_api_executor_health_checks_deploys_compiler_graph_and_keeps_secrets_out_of_workflow():
    calls = []

    def request(method, url, headers, body, timeout):
        payload = json.loads(body) if body else None
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": payload, "timeout": timeout})
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/rest/settings"):
            return {"data": {"userManagement": {"showSetupOnFirstLoad": False}}}
        if url == "http://127.0.0.1:8000/health":
            return {"status": "ok", "system_id": "stock-ai-system"}
        if method == "GET" and "/workflows?name=" in url:
            return {"data": []}
        if method == "GET" and "/workflows?limit=1" in url:
            return {"data": []}
        if method == "POST" and url.endswith("/api/v1/workflows"):
            return {"id": "n8n-api-42", "active": False}
        if method == "POST" and url.endswith("/workflows/n8n-api-42/publish"):
            return {"id": "n8n-api-42", "active": True}
        raise AssertionError(f"unexpected n8n request: {method} {url}")

    config = N8nGatewayConfig(
        gateway_url="https://n8n.example.test",
        gateway_token="local-secret",
        callback_secret="callback-secret-v1",
        timeout_seconds=9,
        deployment_mode="remote",
        remote_host_allowlist=("n8n.example.test",),
        remote_tls_certificate_sha256="a" * 64,
    )
    executor = N8nGatewayExecutor(config, request=request)
    compiler = WorkflowCompiler()
    compiled = compiler.compile(
        _intent(
            goal="每日整理緯創資料並寄送 Email",
            trigger={
                "type": "schedule",
                "frequency": "daily",
                "at": "09:15",
                "market_calendar": "taiwan",
            },
            channels=("email",),
            kind=AutomationKind.CROSS_SYSTEM,
            actions=({"type": "email", "content": "daily report"},),
        ),
        credential_refs=("credential-ref://email/primary",),
    )
    adapter = HeadlessN8nAdapter(executor)

    status = adapter.status()
    dry_run = adapter.dry_run(compiled)
    deployed = adapter.activate(
        compiled,
        context={
            "automation_id": "automation-42",
            "automation_version": 3,
            "submission_id": "submission-42",
            "idempotency_key": "automation-42:version-3",
        },
    )

    assert status["healthy"] and status["api_authenticated"] and status["activation_ready"]
    assert dry_run.accepted and dry_run.detail["compiler_product_verified"]
    assert deployed.accepted and deployed.backend_reference == "n8n-api-42"
    create = next(item for item in calls if item["method"] == "POST" and item["url"].endswith("/api/v1/workflows"))
    assert create["headers"]["X-N8N-API-KEY"] == "local-secret"
    assert create["body"]["name"] == "Stock AI Automation automation-42"
    assert "description" not in create["body"]
    assert create["body"]["settings"] == {
        "executionOrder": "v1",
        "timezone": "Asia/Taipei",
    }
    assert create["body"]["nodes"][0]["type"] == "n8n-nodes-base.scheduleTrigger"
    assert create["body"]["nodes"][0]["parameters"]["rule"]["interval"] == [
        {
            "field": "days",
            "daysInterval": 1,
            "triggerAtHour": 9,
            "triggerAtMinute": 15,
        }
    ]
    assert len(create["body"]["nodes"]) == 3
    envelope = create["body"]["nodes"][1]
    callback = create["body"]["nodes"][2]
    assert envelope["type"] == "n8n-nodes-base.code"
    assert '"automation_id":"automation-42"' in envelope["parameters"]["jsCode"]
    assert '"submission_id":"submission-42"' in envelope["parameters"]["jsCode"]
    assert '"market_calendar":"taiwan"' in envelope["parameters"]["jsCode"]
    assert "source_event_id" in envelope["parameters"]["jsCode"]
    assert "$execution.id" in envelope["parameters"]["jsCode"]
    assert "require('crypto')" in envelope["parameters"]["jsCode"]
    assert "createHmac('sha256'" in envelope["parameters"]["jsCode"]
    assert "__stock_ai_callback" in envelope["parameters"]["jsCode"]
    assert callback["type"] == "n8n-nodes-base.httpRequest"
    assert callback["parameters"]["method"] == "POST"
    assert callback["parameters"]["url"] == "http://127.0.0.1:8000/api/agents/automations/events"
    callback_headers = {
        item["name"]: item["value"]
        for item in callback["parameters"]["headerParameters"]["parameters"]
    }
    assert callback_headers["X-Stock-AI-Automation-Source"] == "n8n"
    assert callback_headers["X-Stock-AI-Automation-Token"]
    assert callback_headers["X-Stock-AI-Automation-Token"] != "local-secret"
    assert callback_headers["X-Stock-AI-Automation-Timestamp"] == "={{ $json.__stock_ai_callback.timestamp }}"
    assert callback_headers["X-Stock-AI-Automation-Nonce"] == "={{ $json.__stock_ai_callback.nonce }}"
    assert callback_headers["X-Stock-AI-Automation-Signature"] == "={{ $json.__stock_ai_callback.signature }}"
    assert "Content-Type" not in callback_headers
    assert callback["parameters"]["contentType"] == "raw"
    assert callback["parameters"]["rawContentType"] == "application/json"
    assert callback["parameters"]["body"] == "={{ $json.__stock_ai_callback.body }}"
    assert "signature, body" in envelope["parameters"]["jsCode"]
    assert callback["retryOnFail"] is True and callback["maxTries"] == 5
    serialized_workflow = json.dumps(create["body"], ensure_ascii=False)
    assert "local-secret" not in serialized_workflow
    assert "credential-ref://email/primary" not in serialized_workflow
    assert "user_artifact" not in serialized_workflow
    semantic_artifact = json.dumps(compiled.user_artifact, ensure_ascii=False).casefold()
    assert all(token not in semantic_artifact for token in ("n8n", "http", "127.0.0.1", "node"))
    assert build_n8n_gateway_adapter(N8nGatewayConfig()).status()["configured"] is False


def test_n8n_local_control_plane_allows_a_bounded_sequential_deployment_probe(monkeypatch):
    """A healthy local deployment must not mistake its own API probes for a burst."""

    calls = []

    def request_json(method, url, headers, body, timeout, **kwargs):
        del headers, body, timeout, kwargs
        calls.append((method, url))
        return {"data": []}

    monkeypatch.setattr("stock_ai.n8n_automation._request_json", request_json)
    executor = N8nGatewayExecutor(
        N8nGatewayConfig(
            gateway_url="http://127.0.0.1:5678",
            gateway_token="api-key-v1",
            callback_secret="callback-secret-v1",
        )
    )

    executor._secure_request(
        "GET", "http://127.0.0.1:5678/api/v1/workflows?limit=1", {}, None, 5
    )
    executor._secure_request(
        "GET", "http://127.0.0.1:5678/api/v1/workflows?name=Stock%20AI", {}, None, 5
    )
    executor._secure_request("GET", "http://127.0.0.1:8000/health", {}, None, 5)

    assert len(calls) == 3


def test_n8n_requires_an_independent_callback_secret_before_activation():
    def request(method, url, headers, body, timeout):
        del method, headers, body, timeout
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/rest/settings"):
            return {"data": {"userManagement": {"showSetupOnFirstLoad": False}}}
        if "/api/v1/workflows?limit=1" in url:
            return {"data": []}
        raise AssertionError(f"unexpected readiness request: {url}")

    executor = N8nGatewayExecutor(
        N8nGatewayConfig(
            gateway_url="http://127.0.0.1:5678",
            gateway_token="api-key-v1",
        ),
        request=request,
    )

    status = executor.status()

    assert status["api_key_configured"] is True
    assert status["callback_secret_configured"] is False
    assert status["api_authenticated"] is True
    assert status["activation_ready"] is False
    assert status["readiness_code"] == "callback_secret_required"


def test_n8n_headless_runtime_sets_redacted_bounded_execution_retention():
    launcher = (Path(__file__).resolve().parents[1] / "scripts" / "start-n8n-headless.sh").read_text(
        encoding="utf-8"
    )
    assert 'export EXECUTIONS_DATA_SAVE_ON_ERROR="none"' in launcher
    assert 'export EXECUTIONS_DATA_SAVE_ON_SUCCESS="none"' in launcher
    assert 'export EXECUTIONS_DATA_SAVE_ON_PROGRESS="false"' in launcher
    assert 'export EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS="false"' in launcher
    assert 'export EXECUTIONS_DATA_PRUNE="true"' in launcher
    assert 'export EXECUTIONS_DATA_MAX_AGE="168"' in launcher
    assert 'export EXECUTIONS_DATA_PRUNE_MAX_COUNT="2000"' in launcher
    assert 'export EXECUTIONS_DATA_MAX_DISPLAY_SIZE="1048576"' in launcher
    assert '"http://$N8N_HOST:$N8N_PORT/api/v1/executions?limit=1&includeData=false"' in launcher


def test_n8n_headless_runtime_waits_for_deployment_readiness_before_owner_bootstrap():
    launcher = (Path(__file__).resolve().parents[1] / "scripts" / "start-n8n-headless.sh").read_text(
        encoding="utf-8"
    )

    assert 'READINESS_URL="${HEALTH_URL}/readiness"' in launcher
    assert 'readiness_body="$(curl -fsS --max-time 2 "$READINESS_URL"' in launcher
    assert 'wait_for_readiness "$(listener_pid)"' in launcher
    assert 'wait_for_readiness ""' in launcher
    assert 'if ! api_is_ready || [ -z "${N8N_AUTOMATION_CALLBACK_SECRET:-}" ]; then' in launcher
    assert '"$SETUP_SCRIPT"' in launcher


def test_n8n_headless_runtime_allows_only_the_callback_hmac_builtin():
    launcher = (Path(__file__).resolve().parents[1] / "scripts" / "start-n8n-headless.sh").read_text(
        encoding="utf-8"
    )

    assert 'export N8N_BLOCK_ENV_ACCESS_IN_NODE="true" NODE_FUNCTION_ALLOW_BUILTIN="crypto"' in launcher
    assert 'NODE_FUNCTION_ALLOW_BUILTIN="*"' not in launcher


def test_n8n_headless_runtime_uses_a_launchd_safe_managed_root_without_secret_arguments():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "scripts" / "start-n8n-headless.sh").read_text(encoding="utf-8")
    helper = (root / "scripts" / "n8n-runtime-root.sh").read_text(encoding="utf-8")

    assert 'source "$PROJECT_ROOT/scripts/n8n-runtime-root.sh" "$PROJECT_ROOT"' in launcher
    assert 'launchctl submit -l "$N8N_LAUNCH_AGENT_LABEL"' in launcher
    assert 'N8N_ENCRYPTION_KEY="$(tr -d "\\r\\n" < "$encryption_key")"' in launcher
    assert 'export PATH="$(dirname "$node_bin"):${PATH:-/usr/bin:/bin}"' in launcher
    launchd_command = launcher.split("start_with_launchd() {", 1)[1].split("mkdir -p", 1)[0]
    assert 'N8N_AUTOMATION_GATEWAY_TOKEN' not in launchd_command
    assert 'N8N_AUTOMATION_CALLBACK_SECRET' not in launchd_command
    assert '${HOME}/Library/Application Support/StockAI-System/' in helper
    assert 'mv "$N8N_LEGACY_RUNTIME_ROOT" "$N8N_RUNTIME_ROOT"' in helper
    assert 'ln -s "$N8N_RUNTIME_ROOT" "$N8N_LEGACY_RUNTIME_ROOT"' in helper


def test_n8n_api_deployment_is_idempotent_and_lifecycle_calls_real_endpoints():
    calls = []
    listed = 0

    def request(method, url, headers, body, timeout):
        nonlocal listed
        payload = json.loads(body) if body else None
        calls.append((method, url, payload))
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/rest/settings"):
            return {"data": {"userManagement": {"showSetupOnFirstLoad": False}}}
        if url == "http://127.0.0.1:8000/health":
            return {"status": "ok", "system_id": "stock-ai-system"}
        if method == "GET" and "/workflows?limit=1" in url:
            return {"data": []}
        if method == "GET" and "/workflows?name=" in url:
            listed += 1
            return {"data": [] if listed == 1 else [{"id": "wf-existing", "name": "Stock AI Automation auto-1"}]}
        if method == "POST" and url.endswith("/api/v1/workflows"):
            return {"id": "wf-existing", "active": False}
        if method == "PUT" and url.endswith("/workflows/wf-existing"):
            return {"id": "wf-existing", "active": True}
        if method == "POST" and url.endswith("/publish"):
            return {"id": "wf-existing", "active": True}
        if method == "POST" and url.endswith("/unpublish"):
            return {"id": "wf-existing", "active": False}
        if method == "POST" and url.endswith("/archive"):
            return {"id": "wf-existing", "active": False, "isArchived": True}
        raise AssertionError(f"unexpected n8n request: {method} {url}")

    executor = N8nGatewayExecutor(
        N8nGatewayConfig(
            gateway_url="http://127.0.0.1:5678/api/v1",
            gateway_token="secret",
            callback_secret="callback-secret-v1",
        ),
        request=request,
    )
    compiled = WorkflowCompiler().compile(
        _intent(
            trigger={"type": "schedule", "frequency": "daily", "at": "08:00"},
            channels=("email",),
            kind=AutomationKind.CROSS_SYSTEM,
            actions=({"type": "email", "content": "report"},),
        ),
        credential_refs=("credential-ref://email/primary",),
    )
    adapter = HeadlessN8nAdapter(executor)

    first = adapter.activate(compiled, context={"automation_id": "auto-1", "idempotency_key": "same"})
    second = adapter.activate(compiled, context={"automation_id": "auto-1", "idempotency_key": "same"})
    paused = adapter.pause("wf-existing")
    resumed = adapter.resume("wf-existing")
    archived = adapter.archive("wf-existing")

    assert first.detail["deployment_operation"] == "created"
    assert second.detail["deployment_operation"] == "updated"
    assert paused.accepted and resumed.accepted and archived.accepted
    assert sum(1 for method, url, _ in calls if method == "POST" and url.endswith("/api/v1/workflows")) == 1
    assert sum(1 for method, url, _ in calls if method == "PUT" and url.endswith("/workflows/wf-existing")) == 1


def test_headless_n8n_gateway_failure_remains_a_recoverable_pending_submission():
    compiler = WorkflowCompiler()
    compiled = compiler.compile(
        _intent(
            goal="每日整理緯創資料並寄送 Email",
            trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
            channels=("email",),
            kind=AutomationKind.CROSS_SYSTEM,
            actions=({"type": "email", "content": "daily report"},),
        ),
        credential_refs=("credential-ref://email/primary",),
    )
    adapter = HeadlessN8nAdapter(
        lambda _request: (_ for _ in ()).throw(TimeoutError("gateway unavailable"))
    )

    outcome = adapter.activate(compiled)

    assert outcome.accepted is False
    assert outcome.backend_reference is None
    assert outcome.detail["submission_status"] == "pending"
    assert outcome.detail["durable_submission_required"] is True
    assert outcome.detail["recoverable"] is True
    assert outcome.detail["reason"] == "n8n API deployment failed: TimeoutError"


def test_n8n_rejects_tampered_compiler_definition_even_with_hex_digest():
    compiled = WorkflowCompiler().compile(
        _intent(
            trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
            kind=AutomationKind.RECURRING,
        ),
        credential_refs=("credential-ref://n8n/local",),
    )
    request = HeadlessN8nAdapter._request(compiled)  # type: ignore[attr-defined]
    request["compiled_definition"] = {
        **request["compiled_definition"],
        "trigger": {"type": "schedule", "frequency": "hourly", "at": "09:00"},
    }

    with pytest.raises(ValueError, match="digest does not match"):
        N8nGatewayExecutor._validate_contract(request)


def test_n8n_local_mode_rejects_remote_gateway_and_remote_mode_requires_allowlist():
    with pytest.raises(ValueError, match="loopback"):
        N8nGatewayExecutor(
            N8nGatewayConfig(
                gateway_url="https://automation.example.test",
                gateway_token="key",
                callback_secret="callback-secret-v1",
            )
        )
    with pytest.raises(ValueError, match="HTTPS and an explicit host allowlist"):
        N8nGatewayExecutor(
            N8nGatewayConfig(
                gateway_url="http://automation.example.test",
                gateway_token="key",
                callback_secret="callback-secret-v1",
                deployment_mode="remote",
                remote_host_allowlist=("automation.example.test",),
            )
        )
    with pytest.raises(ValueError, match="TLS certificate pin"):
        N8nGatewayExecutor(
            N8nGatewayConfig(
                gateway_url="https://automation.example.test",
                gateway_token="key",
                callback_secret="callback-secret-v1",
                deployment_mode="remote",
                remote_host_allowlist=("automation.example.test",),
            )
        )
    with pytest.raises(ValueError, match="both a client certificate and key"):
        N8nGatewayExecutor(
            N8nGatewayConfig(
                gateway_url="https://automation.example.test",
                gateway_token="key",
                callback_secret="callback-secret-v1",
                deployment_mode="remote",
                remote_host_allowlist=("automation.example.test",),
                remote_tls_certificate_sha256="a" * 64,
                remote_mtls_certificate_path="/run/secrets/n8n-client.pem",
            )
        )
    N8nGatewayExecutor(
        N8nGatewayConfig(
            gateway_url="https://automation.example.test",
            gateway_token="key",
            callback_secret="callback-secret-v1",
            deployment_mode="remote",
            remote_host_allowlist=("automation.example.test",),
            remote_tls_certificate_sha256="a" * 64,
        )
    )


def test_n8n_remote_transport_verifies_the_configured_tls_certificate_pin():
    certificate = b"controlled-n8n-leaf-certificate"

    class Socket:
        def getpeercert(self, *, binary_form):
            assert binary_form is True
            return certificate

    class Response:
        class fp:
            class raw:
                _sock = Socket()

    _verify_peer_certificate_pin(Response(), sha256(certificate).hexdigest())
    with pytest.raises(ssl.SSLError, match="pin does not match"):
        _verify_peer_certificate_pin(Response(), "0" * 64)


def test_invalid_pending_n8n_submission_is_quarantined_without_crashing_startup(tmp_path):
    store, controller = _controller(tmp_path, n8n=HeadlessN8nAdapter())
    pending = controller.confirm_and_activate(
        _intent(
            goal="每日 n8n 啟動恢復測試",
            trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
            kind=AutomationKind.RECURRING,
        ),
        user_confirmed=True,
        credential_refs=("credential-ref://n8n/local",),
        now=NOW,
    )
    submission = store.list_submissions(status="pending")[0]
    broken_request = dict(submission["request"])
    broken_request["compiled"] = {
        **dict(broken_request["compiled"]),
        "credential_refs": [],
    }
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "update agent_automation_submissions set request_json=? where submission_id=?",
            (json.dumps(broken_request), submission["submission_id"]),
        )
        conn.commit()
    recovered = AutomationController(
        AutomationStore(store.path),
        n8n_backend=HeadlessN8nAdapter(
            lambda _request: {"accepted": True, "workflow_id": "must-not-run"}
        ),
    ).recover_pending_submissions(now=NOW + timedelta(minutes=1))

    assert len(recovered) == 1
    assert recovered[0].accepted is False
    assert recovered[0].detail["submission_status"] == "failed"
    reopened = AutomationStore(store.path)
    assert reopened.list_submissions(status="pending") == []
    assert reopened.list_submissions(status="failed")[0]["submission_id"] == submission["submission_id"]
    assert reopened.get_automation(pending.automation["automation_id"])["state"] == "failed"


def test_trigger_pipeline_filters_then_reanalyzes_and_notifies_only_on_change(tmp_path):
    sent = []
    store, controller = _controller(
        tmp_path,
        senders={"in_app": lambda payload: sent.append(payload) or {"accepted": True, "message_id": f"m-{len(sent)}"}},
    )
    created = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = created.automation["automation_id"]
    reanalysis_calls = []

    def reanalyze(intent, event, previous):
        reanalysis_calls.append((event, previous))
        return {
            "conclusion": "分批停利" if event["price"] >= 130 else "續抱",
            "confidence": 0.84,
            "evidence_ids": ["market-price", "institutional-flow"],
        }

    filtered = controller.handle_trigger(automation_id, {"price": 119}, reanalyze=reanalyze, now=NOW + timedelta(minutes=1))
    assert not filtered.condition_candidate and not filtered.reanalyzed
    assert not reanalysis_calls and not sent

    first = controller.handle_trigger(automation_id, {"price": 121}, reanalyze=reanalyze, now=NOW + timedelta(minutes=6))
    assert first.condition_candidate and first.reanalyzed
    assert first.meaningful_change and first.notified and len(sent) == 1

    unchanged = controller.handle_trigger(automation_id, {"price": 122}, reanalyze=reanalyze, now=NOW + timedelta(minutes=12))
    assert unchanged.reanalyzed and not unchanged.meaningful_change
    assert not unchanged.notified and len(sent) == 1
    stages = [item["stage"] for item in store.list_executions(automation_id)]
    assert stages[-3:] == ["cheap_filter", "decision_changed_gate", "decision_changed_gate"]


def test_trigger_execution_decision_and_notification_survive_process_reopen(tmp_path):
    sent = []
    store, controller = _controller(
        tmp_path,
        senders={"in_app": lambda payload: sent.append(payload) or {"accepted": True, "message_id": "durable-1"}},
    )
    created = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = created.automation["automation_id"]
    outcome = controller.handle_trigger(
        automation_id,
        {"price": 135, "source_event_id": "evt-1"},
        reanalyze=lambda intent, event, previous: {
            "conclusion": "分批停利",
            "confidence": 0.91,
            "evidence_ids": ["price-evt-1"],
        },
        now=NOW + timedelta(minutes=6),
    )
    assert outcome.execution_id and outcome.receipt

    reopened_store = AutomationStore(store.path)
    execution = reopened_store.get_execution(outcome.execution_id)
    delivery = reopened_store.get_delivery(outcome.receipt.delivery_id)
    automation = reopened_store.get_automation(automation_id)
    assert execution["status"] == "notified"
    assert execution["output"]["decision"]["evidence_ids"] == ["price-evt-1"]
    assert [item["stage"] for item in execution["stage_history"]] == [
        "trigger_pipeline",
        "cheap_filter",
        "reanalysis_budget",
        "agent_reanalysis",
        "decision_changed_gate",
    ]
    assert delivery["execution_id"] == outcome.execution_id
    assert delivery["provider_receipt"]["message_id"] == "durable-1"
    assert automation["current_decision"]["conclusion"] == "分批停利"

    restarted_controller = AutomationController(
        reopened_store,
        notifications=NotificationManager(reopened_store, {"in_app": lambda payload: {"accepted": True}}),
        internal_backend=InternalSchedulerBackend(lambda definition, dry_run: {"accepted": True}),
    )
    unchanged = restarted_controller.handle_trigger(
        automation_id,
        {"price": 136, "source_event_id": "evt-2"},
        reanalyze=lambda intent, event, previous: dict(previous),
        now=NOW + timedelta(minutes=12),
    )
    assert unchanged.reanalyzed and not unchanged.meaningful_change and not unchanged.notified
    assert reopened_store.get_execution(unchanged.execution_id)["status"] == "logged"


def test_pending_scheduler_contract_is_recovered_by_a_new_python_process(tmp_path):
    store, controller = _controller(tmp_path, internal=InternalSchedulerBackend())
    pending = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = pending.automation["automation_id"]
    script = """
import json
import sys
from open_stock_ai.agent_runtime.automation import AutomationController, AutomationStore

store = AutomationStore(sys.argv[1])
controller = AutomationController(
    store,
    reanalyze=lambda intent, event, previous: {'conclusion': 'child-process-reanalysis'},
)
results = controller.recover_pending_submissions()
automation = store.get_automation(sys.argv[2])
schedule = store.get_schedule(automation['backend_reference'])
print(json.dumps({
    'accepted': results[0].accepted,
    'state': automation['state'],
    'schedule_active': schedule['status'] == 'active',
    'has_registration_receipt': bool(results[0].detail.get('receipt_id')),
    'pending_count': len(store.list_submissions(status='pending')),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(store.path), automation_id],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    recovered = json.loads(completed.stdout)
    assert recovered == {
        "accepted": True,
        "state": AutomationState.ACTIVE.value,
        "schedule_active": True,
        "has_registration_receipt": True,
        "pending_count": 0,
    }
    assert AutomationStore(store.path).get_automation(automation_id)["state"] == AutomationState.ACTIVE.value


def test_notification_dedup_cooldown_fallback_ack_snooze_unsubscribe_and_expire(tmp_path):
    store, controller = _controller(tmp_path)
    created = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = created.automation["automation_id"]
    attempts = []
    manager = NotificationManager(
        store,
        {
            "telegram": lambda payload: attempts.append("telegram") or {"accepted": False, "error": "offline"},
            "line": lambda payload: attempts.append("line") or {"accepted": True, "receipt_id": "line-1"},
        },
    )
    policy = NotificationPolicy(
        channels=("telegram", "line"),
        cooldown_seconds=300,
        meaningful_only=True,
        expires_after_seconds=60,
        allow_fallback=True,
    )
    delivered = manager.deliver(
        automation_id=automation_id,
        user_id="user-1",
        decision={"conclusion": "停利"},
        payload={"message": "changed"},
        policy=policy,
        now=NOW,
    )
    assert delivered.status == DeliveryStatus.DELIVERED
    assert delivered.channel == "line" and delivered.fallback_used
    assert attempts == ["telegram", "line"]

    duplicate = manager.deliver(
        automation_id=automation_id,
        user_id="user-1",
        decision={"conclusion": "停利"},
        payload={"message": "same"},
        policy=policy,
        now=NOW + timedelta(seconds=10),
    )
    assert duplicate.status == DeliveryStatus.SUPPRESSED
    assert "duplicate" in (duplicate.reason or "")
    acknowledged = manager.acknowledge(delivered.delivery_id, now=NOW + timedelta(seconds=20))
    assert acknowledged.status == DeliveryStatus.ACKNOWLEDGED
    snoozed = manager.snooze(delivered.delivery_id, until=NOW + timedelta(hours=1), now=NOW + timedelta(seconds=30))
    assert snoozed.status == DeliveryStatus.SNOOZED

    unsubscribed = manager.unsubscribe(
        automation_id=automation_id,
        user_id="user-1",
        channel="telegram",
        now=NOW + timedelta(seconds=40),
    )
    assert unsubscribed.status == DeliveryStatus.UNSUBSCRIBED
    fallback_after_unsubscribe = manager.deliver(
        automation_id=automation_id,
        user_id="user-1",
        decision={"conclusion": "買進"},
        payload={"message": "new"},
        policy=policy,
        now=NOW + timedelta(hours=2),
    )
    assert fallback_after_unsubscribe.status == DeliveryStatus.DELIVERED
    assert fallback_after_unsubscribe.channel == "line"
    manager.unsubscribe(
        automation_id=automation_id,
        user_id="user-1",
        channel="line",
        now=NOW + timedelta(hours=2, seconds=1),
    )
    blocked = manager.deliver(
        automation_id=automation_id,
        user_id="user-1",
        decision={"conclusion": "減碼"},
        payload={"message": "all channels blocked"},
        policy=policy,
        now=NOW + timedelta(hours=3),
    )
    assert blocked.status == DeliveryStatus.SUPPRESSED
    assert "unsubscribed" in (blocked.reason or "")
    expired = manager.expire(delivered.delivery_id, now=NOW + timedelta(hours=2))
    assert expired.status == DeliveryStatus.EXPIRED
    reopened_manager = NotificationManager(AutomationStore(store.path), {})
    reopened_delivery = reopened_manager.store.get_delivery(delivered.delivery_id)
    assert reopened_delivery["status"] == DeliveryStatus.EXPIRED.value
    assert reopened_delivery["provider_receipt"]["receipt_id"] == "line-1"


def test_lifecycle_rejects_invalid_transition_and_supports_pause_resume(tmp_path, monkeypatch):
    lifecycle = Lifecycle()
    assert lifecycle.require(AutomationState.ACTIVE, AutomationState.PAUSED) == AutomationState.PAUSED
    with pytest.raises(ValueError, match="invalid automation lifecycle"):
        lifecycle.require(AutomationState.DRAFT, AutomationState.ACTIVE)

    _, controller = _controller(tmp_path)
    created = controller.confirm_and_activate(_intent(), user_confirmed=True, now=NOW)
    automation_id = created.automation["automation_id"]
    external_sync_calls = []
    monkeypatch.setattr(
        controller,
        "_sync_external_lifecycle",
        lambda observed_id, action: external_sync_calls.append((observed_id, action)),
    )
    assert controller.pause(automation_id, now=NOW)["state"] == "paused"
    assert controller.pause(automation_id, now=NOW)["state"] == "paused"
    assert controller.resume(automation_id, now=NOW)["state"] == "active"
    assert controller.archive(automation_id, now=NOW)["state"] == "archived"
    assert external_sync_calls == [
        (automation_id, "pause"),
        (automation_id, "pause"),
        (automation_id, "resume"),
        (automation_id, "archive"),
    ]


def test_pausing_a_pending_n8n_submission_cancels_recovery_without_deleting_its_receipt(tmp_path):
    store, controller = _controller(tmp_path)
    intent = _intent(
        trigger={"type": "schedule", "frequency": "daily", "at": "09:00"},
        kind=AutomationKind.RECURRING,
    )
    created = controller.confirm_and_activate(
        intent,
        user_confirmed=True,
        credential_refs=("credential-ref://stock-ai/local-callback",),
        now=NOW,
    )
    automation_id = str(created.automation["automation_id"])

    paused = controller.pause(automation_id, now=NOW)

    assert paused["state"] == AutomationState.PAUSED.value
    assert store.list_submissions(status="pending") == []
    cancelled = [item for item in store.list_submissions() if item["automation_id"] == automation_id]
    assert len(cancelled) == 1
    assert cancelled[0]["status"] == "cancelled"


def test_n8n_submission_lifecycle_matches_pause_resume_and_archive(tmp_path):
    class LifecycleExecutor:
        def __init__(self):
            self.calls = []

        def unpublish(self, workflow_id):
            self.calls.append(("unpublish", workflow_id))
            return {"accepted": True, "workflow_id": workflow_id}

        def publish(self, workflow_id):
            self.calls.append(("publish", workflow_id))
            return {"accepted": True, "workflow_id": workflow_id}

        def archive(self, workflow_id):
            self.calls.append(("archive", workflow_id))
            return {"accepted": True, "workflow_id": workflow_id}

    executor = LifecycleExecutor()
    store, controller = _controller(tmp_path, n8n=HeadlessN8nAdapter(executor))
    intent = _intent(kind=AutomationKind.CROSS_SYSTEM)
    intent_id = store.save_intent(intent, semantic_fingerprint(intent), now=NOW)
    automation = store.create_automation(
        intent_id=intent_id,
        intent=intent,
        fingerprint=semantic_fingerprint(intent),
        state=AutomationState.ACTIVE.value,
        now=NOW,
    )
    automation_id = str(automation["automation_id"])
    store.save_version(
        automation_id,
        intent=intent,
        compiled={"backend": "n8n"},
        artifact={},
        backend="n8n",
        backend_reference="workflow-1",
        now=NOW,
    )
    store.set_backend_reference(automation_id, "workflow-1", now=NOW)
    submission = store.save_submission(
        automation_id,
        backend="n8n",
        idempotency_key=f"{automation_id}:v1",
        request_payload={"compiled": {"backend": "n8n"}},
        operation="create",
        now=NOW,
    )
    store.complete_submission(
        str(submission["submission_id"]),
        status="active",
        response_payload={"accepted": True},
        backend_reference="workflow-1",
        now=NOW,
    )

    assert controller.pause(automation_id, now=NOW)["state"] == AutomationState.PAUSED.value
    assert store.get_submission(submission_id=str(submission["submission_id"]))["status"] == "paused"
    assert controller.resume(automation_id, now=NOW)["state"] == AutomationState.ACTIVE.value
    assert store.get_submission(submission_id=str(submission["submission_id"]))["status"] == "active"
    assert controller.archive(automation_id, now=NOW)["state"] == AutomationState.ARCHIVED.value
    assert store.get_submission(submission_id=str(submission["submission_id"]))["status"] == "archived"
    assert executor.calls == [
        ("unpublish", "workflow-1"),
        ("publish", "workflow-1"),
        ("archive", "workflow-1"),
    ]


def test_store_reconciles_legacy_active_n8n_receipts_for_paused_automation(tmp_path):
    store, _ = _controller(tmp_path)
    intent = _intent(kind=AutomationKind.CROSS_SYSTEM)
    intent_id = store.save_intent(intent, semantic_fingerprint(intent), now=NOW)
    automation = store.create_automation(
        intent_id=intent_id,
        intent=intent,
        fingerprint=semantic_fingerprint(intent),
        state=AutomationState.PAUSED.value,
        now=NOW,
    )
    automation_id = str(automation["automation_id"])
    submission = store.save_submission(
        automation_id,
        backend="n8n",
        idempotency_key=f"{automation_id}:legacy",
        request_payload={"compiled": {"backend": "n8n"}},
        operation="create",
        now=NOW,
    )
    store.complete_submission(
        str(submission["submission_id"]),
        status="active",
        response_payload={"accepted": True},
        now=NOW,
    )

    reopened = AutomationStore(store.path)

    reconciled = reopened.get_submission(submission_id=str(submission["submission_id"]))
    assert reconciled is not None and reconciled["status"] == "paused"


def test_archive_undeployed_testing_n8n_record_without_external_reference_preserves_audit(tmp_path):
    store, controller = _controller(tmp_path)
    intent = _intent(kind=AutomationKind.CROSS_SYSTEM)
    intent_id = store.save_intent(intent, semantic_fingerprint(intent), now=NOW)
    automation = store.create_automation(
        intent_id=intent_id,
        intent=intent,
        fingerprint=semantic_fingerprint(intent),
        state=AutomationState.TESTING.value,
        now=NOW,
    )
    automation_id = str(automation["automation_id"])
    store.save_version(
        automation_id,
        intent=intent,
        compiled={"backend": "n8n"},
        artifact={},
        backend="n8n",
        now=NOW,
    )
    submission = store.save_submission(
        automation_id,
        backend="n8n",
        idempotency_key=f"{automation_id}:testing",
        request_payload={"compiled": {"backend": "n8n"}},
        operation="create",
        now=NOW,
    )
    store.complete_submission(
        str(submission["submission_id"]),
        status="failed",
        response_payload={"reason": "deployment failed before workflow creation"},
        now=NOW,
    )

    archived = controller.archive(automation_id, now=NOW)

    assert archived["state"] == AutomationState.ARCHIVED.value
    assert store.get_submission(submission_id=str(submission["submission_id"]))["status"] == "failed"
    records = store.list_executions(automation_id)
    assert records[-1]["stage"] == "archive_without_external_workflow"


def test_n8n_gateway_recovery_replaces_only_its_exact_stale_key_label():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "setup-n8n-local-owner.sh").read_text()

    assert 'rest/api-keys?ownership=mine&label=Stock%20AI%20local%20gateway' in script
    assert 'select(.label == "Stock AI local gateway")' in script
    assert 'rest/api-keys/$existing_gateway_key_id' in script
    assert 'Removed stale Stock AI local gateway API key before replacement.' in script
    assert 'DELETE "$N8N_URL/rest/api-keys"' not in script


def test_semantic_fingerprint_covers_goal_symbol_trigger_condition_action_and_user():
    original = _intent()
    assert semantic_fingerprint(original) == semantic_fingerprint(_intent())
    changed_condition = replace(original, decision_logic={"field": "price", "operator": "gte", "value": 130})
    changed_user = replace(original, user_id="user-2")
    assert semantic_fingerprint(changed_condition) != semantic_fingerprint(original)
    assert semantic_fingerprint(changed_user) != semantic_fingerprint(original)
