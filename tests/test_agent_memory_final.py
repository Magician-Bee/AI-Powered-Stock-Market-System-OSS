from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.agent_runtime.branch_result import BranchResultCompressor
from open_stock_ai.agent_runtime.context_broker_v2 import (
    ContextBrokerV2,
    ModelContextProfile,
    ModelRole,
    ModelRoleRouter,
    ModelTier,
    TokenBudgetExceeded,
    TokenBudgetManager,
)
from open_stock_ai.agent_runtime.memory import (
    BackgroundMemoryPipeline,
    MemoryCandidate,
    MemoryDecision,
    MemoryGovernanceEngine,
    MemoryLayer,
    MemoryStatus,
)
from open_stock_ai.agent_runtime.memory.policies import (
    canonical_memory_layer,
    validate_memory,
)
from open_stock_ai.agent_runtime.session.title_generator import (
    SessionTitleGenerator,
    SessionTitleHistory,
)
from open_stock_ai.agent_runtime.session.topic_shift_detector import TopicShiftDetector


def _candidate(
    content: str,
    *,
    kind: str = "project_state",
    source: str = "host_verified",
    semantic_key: str | None = None,
    run_id: str | None = "run-1",
    fingerprint: str | None = None,
    host_verified: bool = False,
    expires_at: str | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        kind=kind,
        importance=0.9,
        future_relevance=0.85,
        confidence=0.95,
        source=source,
        durability="long_term",
        semantic_key=semantic_key,
        run_id=run_id,
        fingerprint=fingerprint,
        host_verified=host_verified,
        expires_at=expires_at,
    )


def test_six_memory_layers_and_legacy_mapping_are_explicit():
    assert {layer.value for layer in MemoryLayer} == {
        "working",
        "session",
        "user_preference",
        "project_state",
        "episodic",
        "procedural",
    }
    assert canonical_memory_layer("conversation") is MemoryLayer.SESSION
    assert canonical_memory_layer("project") is MemoryLayer.PROJECT_STATE
    assert canonical_memory_layer("reflection") is MemoryLayer.PROCEDURAL


def test_candidate_scoring_and_all_consolidation_decisions():
    engine = MemoryGovernanceEngine()
    first = engine.consider(_candidate("Artifact 可點擊修改", semantic_key="artifact-edit"))
    merged = engine.consider(_candidate(" Artifact   可點擊修改 ", semantic_key="artifact-edit"))
    superseded = engine.consider(
        _candidate(
            "Artifact 必須支援選取後局部修改",
            semantic_key="artifact-edit",
            source="user_explicit",
        )
    )
    rejected = engine.consider(
        MemoryCandidate(
            content="一次性的盤中價格",
            kind="episodic",
            importance=0.1,
            future_relevance=0.1,
            confidence=0.2,
            source="model_inference",
            durability="long_term",
        )
    )
    expired = engine.consider(
        _candidate(
            "已過期狀態",
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
    )

    assert first.decision is MemoryDecision.SAVE
    assert merged.decision is MemoryDecision.MERGE
    assert merged.previous_record and merged.previous_record.status is MemoryStatus.SUPERSEDED
    assert superseded.decision is MemoryDecision.SUPERSEDE
    assert superseded.record and superseded.record.supersedes_id == merged.record.memory_id
    assert rejected.decision is MemoryDecision.REJECT
    assert expired.decision is MemoryDecision.EXPIRE


def test_conflict_audit_is_append_only_and_records_supersession():
    engine = MemoryGovernanceEngine()
    old = engine.consider(
        _candidate("使用者不需要外部資料", semantic_key="external-sources")
    )
    new = engine.consider(
        _candidate(
            "股票分析一定要查外部來源",
            semantic_key="external-sources",
            source="user_explicit",
        )
    )

    assert new.decision is MemoryDecision.SUPERSEDE
    assert new.previous_record and new.previous_record.memory_id == old.record.memory_id
    assert new.previous_record.status is MemoryStatus.SUPERSEDED
    assert len(engine.audit_log) == 2
    assert engine.audit_log[-1].previous_memory_id == old.record.memory_id
    with pytest.raises(FrozenInstanceError):
        engine.audit_log[-1].reason = "rewritten"  # type: ignore[misc]


def test_procedural_memory_requires_repetition_or_host_verification():
    engine = MemoryGovernanceEngine()
    first = engine.consider(
        _candidate(
            "Ollama schema mode失敗時改用JSON repair",
            kind="procedural",
            fingerprint="ollama-schema-failure",
            run_id="run-1",
        )
    )
    second = engine.consider(
        _candidate(
            "Ollama schema mode失敗時改用JSON repair",
            kind="procedural",
            fingerprint="ollama-schema-failure",
            run_id="run-2",
        )
    )
    verified = engine.consider(
        _candidate(
            "Host確認的工具修復流程",
            kind="procedural",
            fingerprint="host-tool-repair",
            run_id="run-3",
            host_verified=True,
        )
    )

    assert first.decision is MemoryDecision.REJECT
    assert second.decision is MemoryDecision.SAVE
    assert verified.decision is MemoryDecision.SAVE


def test_user_preferences_are_advisory_and_cannot_be_execution_rules():
    engine = MemoryGovernanceEngine()
    result = engine.consider(
        _candidate(
            "使用者通常偏保守",
            kind="user_preference",
            semantic_key="risk-style",
            source="user_explicit",
        )
    )

    assert result.record and result.record.advisory is True
    assert result.record.source["enforcement"] == "contextual_only"
    with pytest.raises(ValueError, match="advisory"):
        validate_memory(
            kind="user_preference",
            fact_type="preference",
            source={"type": "user_instruction", "enforce_as_rule": True},
            expires_at=None,
        )


def test_title_generation_uses_understood_intent_and_preserves_history():
    requested: list[dict] = []

    async def model(payload: dict) -> str:
        requested.append(payload)
        if payload["reason"] == "topic_shift":
            return "n8n自主監控架構"
        return "緯創續抱與停利分析"

    async def scenario():
        history = SessionTitleHistory("session-1")
        generator = SessionTitleGenerator(model)
        first = await generator.generate(
            history,
            understood_intent="分析緯創是否續抱以及合理停利點",
        )
        revised = await generator.generate(
            history,
            understood_intent="規劃n8n自主監控但不作為第二大腦",
            previous_intent="分析緯創是否續抱以及合理停利點",
        )
        return history, first, revised

    history, first, revised = asyncio.run(scenario())
    assert first.title == "緯創續抱與停利分析"
    assert revised.title == "n8n自主監控架構"
    assert revised.previous_title == first.title
    assert [item.reason for item in history.revisions] == [
        "temporary",
        "first_intent_understood",
        "topic_shift",
    ]
    assert requested[0]["task"] == "generate_session_title"


def test_topic_shift_detector_distinguishes_related_followup():
    detector = TopicShiftDetector()
    related = detector.detect("緯創續抱與停利分析", "緯創停利價格再評估")
    shifted = detector.detect("緯創續抱與停利分析", "n8n自主監控架構")
    assert related.shifted is False
    assert shifted.shifted is True


def test_context_broker_v2_selects_branch_context_and_small_model_surface():
    memories = [
        {"content": "低相關", "score": 0.1},
        {
            "content": "不希望寫死工作流",
            "kind": "user_preference",
            "score": 0.95,
        },
    ]
    tools = [
        {"name": f"tool.{index}", "description": "x", "input_schema": {"type": "object"}}
        for index in range(20)
    ]
    parent = BranchResultCompressor().compress(
        conclusion="台股來源已確認主要事實",
        confidence=0.84,
        raw_outputs=[{"large": "x" * 5000}],
        evidence_ids=["EV-1"],
        branch_id="branch-tw",
    )
    package = ContextBrokerV2().assemble(
        current_objective="比較台美來源後形成投資判斷",
        current_user_message="再查美國來源",
        active_branch={"branch_id": "branch-us", "objective": "補充美國來源"},
        relevant_memory=(item for item in memories),
        required_parent_result=parent,
        relevant_evidence=[{"evidence_id": "EV-US"}],
        recent_decisions=[{"decision": "需要交叉驗證"}],
        tool_schemas=tools,
        failure_ledger=[{"fingerprint": "source-timeout"}],
        output_contract={"type": "branch_result"},
        profile=ModelContextProfile.for_tier(ModelTier.SMALL),
        turn=1,
    )
    payload = package.to_provider_payload()

    assert set(payload) >= {
        "task",
        "context",
        "tool_schemas",
        "evidence",
        "plan",
        "memory",
        "output_contract",
    }
    assert payload["context"]["active_branch"]["branch_id"] == "branch-us"
    assert payload["context"]["required_parent_result"]["conclusion"] == "台股來源已確認主要事實"
    assert len(payload["tool_schemas"]) == 12
    assert payload["memory"][0]["advisory"] is True
    assert payload["memory"][0]["enforcement"] == "contextual_only"
    assert payload["omitted_counts"]["tools"] == 8


def test_context_broker_v2_marks_explicit_preference_recall_as_contextual_only():
    package = ContextBrokerV2().assemble(
        current_objective="依照我先前的架構偏好，指出最重要的一項。",
        current_user_message="依照我先前的架構偏好，指出最重要的一項。",
        active_branch={"branch_id": "branch-preference", "objective": "回憶架構偏好"},
        relevant_memory=(
            {"content": "一般工作摘要", "kind": "working", "relevance_score": 20},
            {
                "content": "工作流不可寫死；n8n 不是第二大腦。",
                "kind": "user_preference",
                "relevance_score": 10_000,
            },
        ),
    )
    payload = package.to_provider_payload()

    assert payload["memory"][0]["kind"] == "user_preference"
    guidance = payload["context"]["preference_recall_guidance"]
    assert guidance["user_preferences"] == ["工作流不可寫死；n8n 不是第二大腦。"]
    assert "contextual-only" in guidance["instruction"]


def test_branch_result_compresses_raw_outputs_to_stable_contract():
    raw = [{"html": "x" * 10_000}, {"quote": 123}]
    result = BranchResultCompressor().compress(
        conclusion="來源交叉驗證完成",
        confidence=0.84,
        raw_outputs=raw,
        evidence_ids=["EV-1", "EV-1", "EV-2"],
        contradictions=["EV-2時間較舊"],
        remaining_gaps=["缺公司正式公告"],
    )
    payload = result.to_dict()

    assert payload["evidence_ids"] == ["EV-1", "EV-2"]
    assert payload["compressed_from_chars"] > 10_000
    assert payload["output_digest"]
    assert "html" not in payload


def test_budget_role_routing_and_background_work_contract():
    budgets = TokenBudgetManager()
    budgets.register("session-1", scope_type="session", limit=120)
    budgets.register(
        "branch-1",
        scope_type="branch",
        limit=100,
        parent_scope_id="session-1",
    )
    assert budgets.consume("branch-1", 40).remaining == 60
    assert budgets.get("session-1").remaining == 80
    with pytest.raises(TokenBudgetExceeded):
        budgets.consume("branch-1", 61)

    router = ModelRoleRouter()
    assert router.route(ModelRole.REPAIR) is ModelTier.SMALL
    assert router.route(ModelRole.PLANNER, complexity=0.9) is ModelTier.STRONG
    assert router.route(ModelRole.CRITIC, risk="high") is ModelTier.STRONG

    async def background_scenario():
        pipeline = BackgroundMemoryPipeline()
        task = pipeline.schedule(lambda: "memory-extracted")
        assert not task.cancelled()
        return await task

    assert asyncio.run(background_scenario()) == "memory-extracted"
