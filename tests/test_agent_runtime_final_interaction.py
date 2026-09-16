from __future__ import annotations

import asyncio

import pytest

from open_stock_ai.agent_runtime.artifacts import (
    ArtifactVersionManager,
    InMemoryArtifactVersionStore,
    OptimisticVersionConflict,
    SelectionContextManager,
)
from open_stock_ai.agent_runtime.forest import (
    BranchManager,
    BranchStatus,
    CheckpointLevel,
    ForestLimitError,
    ForestLimits,
    InMemoryCheckpointStore,
    JoinStatus,
    ScopedCheckpointManager,
    StepStatus,
    TaskForest,
)
from open_stock_ai.agent_runtime.interaction import (
    ArbitrationDecision,
    DecisionCheckpoint,
    DecisionOption,
    InteractionRequest,
    ProposalArbitrator,
    ReflectionCheckpoint,
    SteeringIntent,
    SteeringRouter,
    UserInputKind,
    WaitingState,
)
from open_stock_ai.agent_runtime.session import (
    InMemoryObjectiveStore,
    ObjectiveManager,
    TitleGenerator,
    TopicShiftDetector,
)
from open_stock_ai.agent_runtime.session.title_generator import SessionTitleHistory


def _domain(*, limits: ForestLimits | None = None):
    objective_manager = ObjectiveManager(InMemoryObjectiveStore())
    objective = objective_manager.create_initial(
        session_id="AS-1",
        objective="分析緯創持股策略",
        requirements=("查台灣來源",),
        message_id="MSG-1",
    )
    forest = TaskForest.create(
        session_id="AS-1",
        objective_version_id=objective.objective_id,
        root_goal=objective.objective,
        limits=limits,
    )
    branch_manager = BranchManager(forest)
    return objective_manager, forest, branch_manager


def test_objective_versions_are_immutable_and_branch_binds_revision() -> None:
    objectives, forest, branches = _domain()
    first = objectives.current("AS-1")
    assert first is not None

    second = objectives.revise(
        session_id="AS-1",
        objective="分析緯創是否分批停利並建立低干擾監控",
        added_requirements=("查美國來源", "不要一次全部賣出"),
        constraints=("通知不可過於頻繁",),
        message_id="MSG-2",
    )
    research = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="驗證美國 AI 伺服器供應鏈變化",
        objective_version_id=second.objective_id,
    ).branch

    assert first.objective == "分析緯創持股策略"
    assert second.revision == 2
    assert second.supersedes == first.objective_id
    assert research.objective_version_id == second.objective_id
    assert [item.revision for item in objectives.history("AS-1")] == [1, 2]


def test_title_generation_tracks_history_and_detects_topic_shift() -> None:
    history = SessionTitleHistory("AS-1")
    generator = TitleGenerator()
    first = asyncio.run(
        generator.generate(
            history,
            understood_intent="請幫我分析緯創續抱與停利策略。也要考慮風險。",
        )
    )
    second = asyncio.run(
        generator.generate(
            history,
            understood_intent="設計 n8n 自主監控架構",
            previous_intent="請幫我分析緯創續抱與停利策略。也要考慮風險。",
        )
    )

    detector = TopicShiftDetector()
    assert "緯創續抱與停利策略" in first.title
    assert second.previous_title == first.title
    assert second.reason == "topic_shift"
    assert detector.detect(first.title, second.title).shifted
    assert not detector.detect(first.title, "緯創持股停利風險分析").shifted


def test_recursive_forest_enforces_depth_active_and_duplicate_limits() -> None:
    _, forest, branches = _domain(
        limits=ForestLimits(max_depth=2, max_active_branches=2, max_nodes=12)
    )
    research = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="研究外部來源",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    duplicate = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="研究外部來源",
        objective_version_id=forest.root_objective_version_id,
    )
    us = branches.spawn_child(
        parent_branch_id=research.branch_id,
        objective="研究美國來源",
        objective_version_id=forest.root_objective_version_id,
    ).branch

    assert duplicate.branch.branch_id == research.branch_id
    assert duplicate.merged_duplicate
    assert us.depth == 2
    assert us.status == BranchStatus.PENDING
    with pytest.raises(ForestLimitError, match="depth"):
        branches.spawn_child(
            parent_branch_id=us.branch_id,
            objective="研究美國公司官方來源",
            objective_version_id=forest.root_objective_version_id,
        )


def test_local_plan_is_ordered_and_branch_budget_is_enforced() -> None:
    _, forest, branches = _domain()
    branch = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="技術分析",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    fetch = branches.add_local_step(branch.branch_id, "取得日 K")
    calculate = branches.add_local_step(
        branch.branch_id,
        "計算波動",
        dependencies=(fetch.step_id,),
    )

    assert [item.step_id for item in branch.local_plan.ready_steps()] == [fetch.step_id]
    branch.local_plan.start(fetch.step_id)
    branch.local_plan.complete(fetch.step_id, {"rows": 120})
    assert calculate.status == StepStatus.READY
    branch.budget.charge(tokens=15_000, seconds=30)
    with pytest.raises(ForestLimitError, match="token"):
        branch.budget.charge(tokens=1_001)


def test_join_detects_conflict_and_creates_a_critic_branch() -> None:
    _, forest, branches = _domain()
    technical = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="技術分析",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    institutional = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="法人分析",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    branches.complete(
        technical.branch_id,
        conclusion="短期結構偏多",
        decision="bullish",
        evidence_ids=("EV-T",),
        confidence=0.7,
    )
    branches.complete(
        institutional.branch_id,
        conclusion="法人流向偏空",
        decision="bearish",
        evidence_ids=("EV-I",),
        confidence=0.8,
    )
    join = forest.add_join(
        required_branch_ids=(technical.branch_id, institutional.branch_id),
        objective="整合技術與法人證據",
    )

    assert branches.resolve_join(join.join_id) is None
    assert join.status == JoinStatus.CONFLICT
    assert join.conflict_branch_id is not None
    critic = forest.branches[join.conflict_branch_id]
    assert critic.local_state["conflicting_branch_ids"] == [
        technical.branch_id,
        institutional.branch_id,
    ]


def test_partial_completion_and_smallest_checkpoint_restore() -> None:
    _, forest, branches = _domain()
    research = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="外部研究",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    branches.complete(
        research.branch_id,
        conclusion="台灣來源已完成，美國來源暫時不可用",
        evidence_ids=("EV-TW",),
        unmet_criteria=("美國來源交叉驗證",),
    )
    assert research.status == BranchStatus.PARTIALLY_COMPLETED
    assert research.result is not None and research.result.partial

    checkpoints = ScopedCheckpointManager(InMemoryCheckpointStore())
    checkpoints.save(
        level=CheckpointLevel.SESSION,
        session_id="AS-1",
        payload={"scope": "session"},
        sequence=1,
    )
    checkpoints.save(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id=research.branch_id,
        payload={"scope": "branch"},
        sequence=2,
    )
    step = checkpoints.save(
        level=CheckpointLevel.STEP,
        session_id="AS-1",
        run_id="AR-1",
        branch_id=research.branch_id,
        step_id="BST-1",
        payload={"scope": "step"},
        sequence=3,
    )
    restored = checkpoints.restore_smallest(
        session_id="AS-1",
        run_id="AR-1",
        branch_id=research.branch_id,
        step_id="BST-1",
    )
    assert restored == step


def test_reflection_precedes_decision_and_waiting_states_are_distinct() -> None:
    reflection = ReflectionCheckpoint.create(
        session_id="AS-1",
        branch_id="BR-RISK",
        problem="如何降低獲利回吐風險？",
        tentative_judgment="先分批停利較平衡",
        evidence_summary=("波動升高", "仍有上行催化"),
        preferred_option="先減碼 40%",
        alternatives=("減碼 60%", "暫不減碼"),
        unknowns=("下一季訂單能見度",),
        important_risks=("一次退出可能錯過上行",),
        user_decision_question="你希望採用哪種減碼方式？",
    )
    checkpoint = DecisionCheckpoint.create(
        session_id="AS-1",
        branch_id="BR-RISK",
        reflection_id=reflection.reflection_id,
        question="你希望採用哪種減碼方式？",
        agent_preferred_option="OPT-A",
        agent_view="目前證據較支持先分批停利。",
        options=(
            DecisionOption("OPT-A", "減碼 40%", "兼顧獲利與參與", True),
            DecisionOption("OPT-B", "減碼 60%", "降低回吐"),
            DecisionOption("OPT-C", "暫不減碼", "保留上行"),
        ),
    )
    clarification = InteractionRequest.clarification(
        session_id="AS-1", prompt="請確認持股成本"
    )
    approval = InteractionRequest.approval(
        session_id="AS-1", prompt="是否批准模擬下單？"
    )

    assert "chain" not in reflection.public_summary()
    assert checkpoint.waiting_state == WaitingState.DECISION
    assert clarification.waiting_state == WaitingState.USER_INPUT
    assert approval.waiting_state == WaitingState.APPROVAL
    response = checkpoint.respond(free_text="先減碼 30%，一週後再評估")
    assert response["free_text"].startswith("先減碼 30%")
    assert checkpoint.resolved


def test_proposal_arbitration_never_blindly_applies_high_impact_input() -> None:
    arbitrator = ProposalArbitrator()
    modified = arbitrator.arbitrate(
        "不要看外資，只看價格",
        evidence_compatible=False,
        risk_level="medium",
        impact_level="global",
        safe_alternative="把法人降為輔助加權條件",
    )
    rejected = arbitrator.arbitrate(
        "請直接繞過風控下單",
        evidence_compatible=True,
        risk_level="prohibited",
        input_kind=UserInputKind.PROPOSAL,
    )

    assert modified.input_kind == UserInputKind.PROPOSAL
    assert modified.decision == ArbitrationDecision.MODIFY
    assert "輔助加權" in modified.recommendation
    assert rejected.decision == ArbitrationDecision.REJECT


def test_soft_hard_and_fork_steering_only_touch_required_branches() -> None:
    objectives, forest, branches = _domain(
        limits=ForestLimits(max_depth=5, max_active_branches=8, max_nodes=40)
    )
    technical = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="技術分析 3231",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    research = branches.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective="外部研究 3231",
        objective_version_id=forest.root_objective_version_id,
    ).branch
    technical.evidence_ids.append("EV-OLD")
    router = SteeringRouter(
        forest=forest,
        branch_manager=branches,
        objective_manager=objectives,
    )

    soft = router.route(
        session_id="AS-1",
        message_id="MSG-2",
        message="順便查美國資料",
        target_branch_id=research.branch_id,
        intent=SteeringIntent.SOFT_STEER,
    )
    assert len(soft.created_branch_ids) == 1
    assert technical.status != BranchStatus.CANCELLED
    assert technical.branch_id in soft.preserved_branch_ids

    hard = router.route(
        session_id="AS-1",
        message_id="MSG-3",
        message="股票代號錯了，不是 3231，是 2382",
        intent=SteeringIntent.HARD_STEER,
        affected_branch_ids=(technical.branch_id,),
        replacement_objective="分析 2382 緯創持股策略",
    )
    assert technical.status == BranchStatus.CANCELLED
    assert technical.local_state["preserved_evidence_ids"] == ["EV-OLD"]
    assert research.status != BranchStatus.CANCELLED
    assert len(hard.created_branch_ids) == 1
    replacement = forest.branches[hard.created_branch_ids[0]]
    assert replacement.local_state["replaces_branch_id"] == technical.branch_id

    forked = router.route(
        session_id="AS-1",
        message_id="MSG-4",
        message="原本緯創分析繼續，再幫我比較廣達",
        intent=SteeringIntent.FORK_BRANCH,
    )
    assert len(forked.created_branch_ids) == 1
    assert forest.branches[forked.created_branch_ids[0]].parent_branch_id == (
        forest.root_branch.branch_id
    )
    assert research.branch_id in forked.preserved_branch_ids


def test_natural_correction_and_parallel_request_are_classified_by_the_host() -> None:
    # The UI sends the user's ordinary words to the active Session.  Runtime
    # topology must be inferred by the Host, not demanded from the user.
    assert SteeringRouter.classify(None, "改為比較台積電與台新新光金的風險差異。") is SteeringIntent.HARD_STEER
    assert SteeringRouter.classify(None, "另外分析華邦電的風險。") is SteeringIntent.FORK_BRANCH


def test_artifact_selection_uses_optimistic_versioning_and_audit_history() -> None:
    store = InMemoryArtifactVersionStore()
    versions = ArtifactVersionManager(store)
    first = versions.create(
        artifact_id="ART-1",
        content="risk_score: 75\n",
        changed_by="agent",
        reason="Initial automation draft",
        message_id="MSG-1",
        affected_node_ids=("NODE-RISK",),
        validation_result={"valid": True},
    )
    selections = SelectionContextManager(store)
    selection = selections.select(
        session_id="AS-1",
        artifact_id="ART-1",
        artifact_version=first.version,
        selector="automation.conditions.risk_score",
        branch_id="BR-AUTO",
        node_id="NODE-RISK",
        context_label="Automation > Risk Score",
    )
    request = selections.change_request("AS-1", "不要固定 75，依波動判斷")
    assert request["expected_version"] == 1
    assert request["selector"] == "automation.conditions.risk_score"

    second = versions.revise(
        artifact_id="ART-1",
        expected_version=1,
        content="risk_score: dynamic\n",
        changed_by="user",
        reason="Use volatility-aware threshold",
        message_id="MSG-2",
        affected_node_ids=("NODE-RISK",),
        validation_result={"valid": True},
    )
    assert second.base_version == 1
    with pytest.raises(OptimisticVersionConflict) as conflict:
        selections.assert_current(selection)
    assert conflict.value.current_version == 2
    with pytest.raises(OptimisticVersionConflict):
        versions.revise(
            artifact_id="ART-1",
            expected_version=1,
            content="stale write",
            changed_by="agent",
            reason="Stale edit",
        )

    diff = versions.compare("ART-1", 1, 2)
    assert "-risk_score: 75" in diff
    assert "+risk_score: dynamic" in diff
    restored = versions.undo(
        artifact_id="ART-1",
        expected_version=2,
        changed_by="user",
        message_id="MSG-3",
    )
    assert restored.version == 3
    assert restored.restored_from_version == 1
    assert restored.content == first.content
