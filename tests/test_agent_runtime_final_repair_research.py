from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.agent_runtime.budget_manager import (
    MarginalValueCheck,
    QuestionBudget,
    TokenBudgetManager,
)
from open_stock_ai.agent_runtime.model_router import (
    CapabilityComposer,
    MatrixBehavior,
    ModelMatrixAuditor,
    ModelProfile,
    ModelRole,
    ModelRouter,
    ProviderClass,
    ProviderMode,
    ProviderNeutralRequest,
)
from open_stock_ai.agent_runtime.observability import (
    BackgroundWorkQueue,
    KPICollector,
    ToolResultProvenance,
    with_provenance,
)
from open_stock_ai.agent_runtime.orchestrator import _canonical_tool_evidence
from open_stock_ai.agent_runtime.repair import (
    BranchOutcome,
    ChaosFault,
    CompletionGate,
    DeterministicRepair,
    ErrorReceipt,
    FailureFingerprint,
    ModelPatchValidator,
    PartialCompletionReport,
    PatchValidationError,
    RecoveryLevel,
    RecoveryStrategyLadder,
    error_receipt_for_fault,
)
from open_stock_ai.agent_runtime.research import (
    BranchSourceRecovery,
    ConflictResolver,
    Evidence,
    EvidenceGraph,
    ResearchPlanner,
)


def _receipt(*, branch_id: str = "branch-us") -> ErrorReceipt:
    return ErrorReceipt(
        category="invalid_arguments",
        component="web.research",
        location="$.tool_calls[0].arguments",
        expected="object",
        actual="string",
        retryable=True,
        branch_id=branch_id,
    )


def _fingerprint(receipt: ErrorReceipt) -> FailureFingerprint:
    return FailureFingerprint.from_receipt(
        receipt,
        provider="ollama",
        model="qwen",
        tool="web.research",
        schema_version="v1",
    )


def test_error_receipt_and_failure_fingerprint_are_stable_and_structured() -> None:
    receipt = _receipt()
    first = _fingerprint(receipt)
    second = _fingerprint(receipt)

    assert receipt.to_dict()["schema_version"] == "open_stock_ai.error_receipt.v1"
    assert receipt.to_dict()["completed_work_preserved"] is True
    assert first.digest == second.digest
    assert first.to_dict()["error_path"] == "$.tool_calls[0].arguments"


def test_deterministic_repair_only_changes_known_format_and_shape() -> None:
    repaired = DeterministicRepair().repair(
        b'```json\n{"status_alias":"final","actions":[],}\n```',
        aliases={"status_alias": "status"},
    )

    assert repaired.value == {"status": "final", "actions": []}
    assert set(repaired.strategies) == {
        "decode_utf8",
        "remove_json_fence",
        "remove_trailing_comma",
        "map_safe_aliases",
    }
    with pytest.raises(ValueError, match="ambiguous"):
        DeterministicRepair().repair({"status": "final", "state": "complete"}, aliases={"state": "status"})
    with pytest.raises(ValueError, match="cannot be repaired"):
        DeterministicRepair().repair('{"symbol": 2330 unquoted meaning}')


def test_model_local_patch_is_scoped_atomic_and_schema_validated() -> None:
    receipt = _receipt()
    document = {
        "tool_calls": [{"name": "web.research", "arguments": "{}"}],
        "protected": "unchanged",
    }
    schema = {
        "type": "object",
        "required": ["tool_calls", "protected"],
        "additionalProperties": False,
        "properties": {
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                },
            },
            "protected": {"type": "string"},
        },
    }
    patched = ModelPatchValidator().apply(
        document,
        {
            "repair_for": receipt.error_id,
            "patch": [{"op": "replace", "path": "$.tool_calls[0].arguments", "value": {"query": "Wistron US demand"}}],
        },
        receipt=receipt,
        allowed_scopes=("$.tool_calls[0].arguments",),
        expected_schema=schema,
    )

    assert patched["tool_calls"][0]["arguments"]["query"] == "Wistron US demand"
    assert document["tool_calls"][0]["arguments"] == "{}"
    with pytest.raises(PatchValidationError, match="outside allowed scope"):
        ModelPatchValidator().apply(
            document,
            {"repair_for": receipt.error_id, "patch": [{"op": "replace", "path": "$.protected", "value": "changed"}]},
            receipt=receipt,
            allowed_scopes=("$.tool_calls[0].arguments",),
            expected_schema=schema,
        )


def test_identical_retry_is_blocked_and_strategy_escalates() -> None:
    receipt = _receipt()
    ladder = RecoveryStrategyLadder()
    fingerprint = _fingerprint(receipt)
    first = ladder.decide(receipt, fingerprint=fingerprint, output="same", arguments={"x": 1})
    second = ladder.decide(
        receipt,
        fingerprint=fingerprint,
        output="same",
        arguments={"x": 1},
        previous_level=first.level,
    )

    assert first.level is RecoveryLevel.HOST_REPAIR
    assert second.level is RecoveryLevel.MODEL_LOCAL_PATCH
    assert second.identical_retry_blocked is True
    assert ladder.guard.identical_retry_count == 1


@pytest.mark.parametrize(
    ("fault", "minimum_level"),
    [
        (ChaosFault.INVALID_JSON, RecoveryLevel.HOST_REPAIR),
        (ChaosFault.WRONG_ARGUMENTS, RecoveryLevel.HOST_REPAIR),
        (ChaosFault.TOOL_TIMEOUT, RecoveryLevel.RETRY_TEMPORARY_FAILURE),
        (ChaosFault.API_500, RecoveryLevel.RETRY_TEMPORARY_FAILURE),
        (ChaosFault.API_RATE_LIMIT, RecoveryLevel.RETRY_TEMPORARY_FAILURE),
        (ChaosFault.NETWORK_DROP, RecoveryLevel.RETRY_TEMPORARY_FAILURE),
        (ChaosFault.WEB_SOURCE_MISSING, RecoveryLevel.ALTERNATIVE_SOURCE),
        (ChaosFault.BROWSER_CRASH, RecoveryLevel.ALTERNATIVE_TOOL),
        (ChaosFault.PROVIDER_CRASH, RecoveryLevel.PROVIDER_FALLBACK),
        (ChaosFault.HOST_RESTART, RecoveryLevel.REPLAN_BRANCH),
        (ChaosFault.DB_RESTART, RecoveryLevel.REPLAN_BRANCH),
        (ChaosFault.DUPLICATE_EVENT, RecoveryLevel.HOST_REPAIR),
        (ChaosFault.USER_INTERRUPT, RecoveryLevel.ASK_USER),
        (ChaosFault.CONFLICTING_USER_MESSAGE, RecoveryLevel.ASK_USER),
    ],
)
def test_chaos_error_injection_maps_to_recoverable_strategy(
    fault: ChaosFault,
    minimum_level: RecoveryLevel,
) -> None:
    receipt = error_receipt_for_fault(fault, branch_id="isolated-branch")
    decision = RecoveryStrategyLadder().decide(
        receipt,
        fingerprint=_fingerprint(receipt),
        output={"fault": fault.value},
        arguments={},
    )

    assert decision.level is minimum_level
    assert decision.preserve_other_branches is True
    assert decision.branch_id == "isolated-branch"


def test_full_l0_l9_ladder_is_ordered_and_terminal() -> None:
    receipt = _receipt()
    ladder = RecoveryStrategyLadder()
    decision = ladder.decide(receipt, fingerprint=_fingerprint(receipt), output="x", arguments={})
    observed = [decision.level]
    for _ in range(12):
        decision = ladder.next(decision)
        observed.append(decision.level)

    assert observed[:10] == list(RecoveryLevel)
    assert observed[-1] is RecoveryLevel.PARTIAL_COMPLETION


def test_partial_completion_and_host_completion_gate() -> None:
    outcomes = [
        BranchOutcome("portfolio", "completed"),
        BranchOutcome("source-us", "failed", missing_evidence=("US filing",), confidence_impact=0.2, next_recovery="alternative_source"),
    ]
    report = PartialCompletionReport.from_outcomes(outcomes)
    gate = CompletionGate().evaluate(
        required_branches={"portfolio", "source-us"},
        outcomes=outcomes,
        evidence_sufficient=False,
        open_blocking_questions=0,
        pending_approvals=0,
        critical_tool_failures=1,
        final_result_valid=True,
    )

    assert report.status == "partially_completed"
    assert report.completed_branches == ("portfolio",)
    assert report.missing_evidence == ("US filing",)
    assert gate.complete is False
    assert "insufficient_evidence" in gate.blockers


def test_research_planner_requires_external_primary_and_independent_sources() -> None:
    plan = ResearchPlanner().plan("分析緯創現在該不該賣", symbols=("3231.TW",))

    assert len(plan.questions) == 5
    assert any(item.primary_required for item in plan.source_requirements)
    assert all(item.minimum_independent_sources >= 1 for item in plan.questions)
    assert "material_conflicts_resolved_or_explicitly_reported" in plan.stop_criteria


def test_evidence_graph_tracks_support_conflict_diversity_and_freshness() -> None:
    now = datetime.now(timezone.utc)
    graph = EvidenceGraph()
    graph.add_conclusion("CON-mid-bullish")
    ir = Evidence(
        claim="AI server demand remains strong",
        source_type="company_ir",
        source="https://example.test/ir",
        published_at=(now - timedelta(hours=2)).isoformat(),
        observed_at=now.isoformat(),
        confidence=0.93,
        supports=("CON-mid-bullish",),
        source_role="primary",
    )
    news = Evidence(
        claim="Short-term demand may normalize",
        source_type="reputable_news",
        source="https://example.test/news",
        published_at=(now - timedelta(hours=3)).isoformat(),
        observed_at=now.isoformat(),
        confidence=0.7,
        contradicts=("CON-mid-bullish",),
        source_role="independent_news",
    )
    graph.add_evidence(ir)
    graph.add_evidence(news)

    assert graph.evidence_for("CON-mid-bullish", "supports") == (ir,)
    assert graph.evidence_for("CON-mid-bullish", "contradicts") == (news,)
    assert graph.source_diversity == 2
    assert graph.freshness_score(now=now) == 1.0
    resolution = ConflictResolver().resolve([ir], [news])
    assert resolution.status == "resolved"
    assert resolution.preferred_evidence_id == ir.evidence_id


def test_source_missing_recovers_only_failed_branch() -> None:
    recovery = BranchSourceRecovery().plan(
        failed_branch_id="research-us-source-3",
        failed_source="source-3",
        active_branches=("portfolio", "technical", "research-tw", "research-us-source-3"),
        alternative_sources=("source-4",),
    )

    assert recovery.action == "alternative_source"
    assert recovery.unaffected_branches == ("portfolio", "technical", "research-tw")
    assert recovery.restart_whole_task is False


def test_tool_provenance_and_kpis_cover_final_success_metrics() -> None:
    provenance = ToolResultProvenance.create(
        tool="web.research",
        provider="browser",
        source="https://example.test",
        freshness="current",
        latency_ms=120,
        success=True,
        request_id="REQ-1",
    )
    envelope = with_provenance({"claim": "verified"}, provenance)
    collector = KPICollector()
    collector.set_session_active("S1", True)
    collector.set_branch_active("B1", True)
    collector.record_task(completed=True)
    collector.record_tool(latency_ms=120, success=True)
    collector.record_model(latency_ms=300, tokens=1000, cost=0.02)
    collector.record_repair(succeeded=True)
    collector.record_branch_recovery(succeeded=True)
    collector.record_research(sources={"ir", "exchange"}, freshness_scores=[1.0, 0.7], cost=0.01)
    collector.record_automation(duplicate=False)
    collector.record_notification(false_trigger=False)
    collector.record_correction(incorporated=True)
    collector.record_context_retrieval(relevant=3, returned=4)
    snapshot = collector.snapshot()

    assert envelope["provenance"]["schema_version"] == "open_stock_ai.tool_result_provenance.v1"
    assert snapshot["task_completion_rate"] == 1.0
    assert snapshot["repair_success_rate"] == 1.0
    assert snapshot["branch_recovery_rate"] == 1.0
    assert snapshot["repeated_failure_rate"] == 0.0
    assert snapshot["research_source_diversity"] == 2
    assert snapshot["session_context_retrieval_precision"] == 0.75


def test_canonical_tool_evidence_separates_model_worker_tool_and_web_sources() -> None:
    records = _canonical_tool_evidence(
        observation={
            "ok": True,
            "worker_id": "AWRK-1",
            "result": {
                "schema_version": "open_stock_ai.web_research.v1",
                "query": "2330 risk",
                "source_count": 2,
                "sources": [
                    {"title": "TWSE filing", "url": "https://example.test/twse"},
                    {"title": "Independent report", "url": "https://example.test/report"},
                ],
            },
        },
        call={"id": "CALL-web", "name": "web.research"},
        metadata={"provider": "project_terminal_web"},
    )

    assert len(records) == 2
    assert records[0]["evidence_id"].startswith("EV-")
    assert records[0]["evidence_id"] != records[1]["evidence_id"]
    assert records[0]["tool_provider"] == "project_terminal_web"
    assert records[0]["worker_id"] == "AWRK-1"
    assert {item["source_url"] for item in records} == {
        "https://example.test/twse",
        "https://example.test/report",
    }
    assert all(item["source"] != "AWRK-1" for item in records)


def test_canonical_tool_evidence_deduplicates_repeated_source_within_run_only() -> None:
    observation = {
        "ok": True,
        "result": {
            "schema_version": "stock_ai.institutional_flow_evidence.v1",
            "items": [
                {
                    "name": "台積電",
                    "source": "TWSE official T86 daily institutional flow",
                    "source_url": "https://www.twse.com.tw/rwd/zh/fund/T86",
                    "trade_date": "2026-08-08",
                }
            ],
        },
    }
    metadata = {"provider": "stock_core"}
    first = _canonical_tool_evidence(
        observation=observation,
        call={"id": "CALL-one", "name": "market.institutional_flow"},
        metadata=metadata,
        run_id="AR-one",
    )
    repeated = _canonical_tool_evidence(
        observation={
            **observation,
            "result": {
                **observation["result"],
                "items": [
                    {
                        **observation["result"]["items"][0],
                        "source_url": (
                            "https://www.twse.com.tw/rwd/zh/fund/T86"
                            "?date=latest&selectType=ALLBUT0999&response=json"
                        ),
                    }
                ],
            },
        },
        call={"id": "CALL-two", "name": "market.institutional_flow"},
        metadata=metadata,
        run_id="AR-one",
    )
    other_run = _canonical_tool_evidence(
        observation=observation,
        call={"id": "CALL-three", "name": "market.institutional_flow"},
        metadata=metadata,
        run_id="AR-two",
    )

    assert first[0]["evidence_id"] == repeated[0]["evidence_id"]
    assert first[0]["evidence_id"] != other_run[0]["evidence_id"]
    assert first[0]["request_id"] != repeated[0]["request_id"]


def test_background_work_queue_returns_before_post_result_work_is_consumed() -> None:
    queue = BackgroundWorkQueue(max_workers=1)
    try:
        future = queue.submit(lambda value: value + "-indexed", "title")
        assert future.result(timeout=2) == "title-indexed"
    finally:
        queue.close()


def test_token_marginal_value_and_question_budgets_prevent_fake_busy_work() -> None:
    tokens = TokenBudgetManager()
    tokens.allocate("branch", "B1", limit=1000, reserved=100)
    assert tokens.consume("branch", "B1", 400) == 500
    with pytest.raises(ValueError, match="exceeded"):
        tokens.consume("branch", "B1", 501)

    value_check = MarginalValueCheck(threshold=0.2)
    assert value_check.evaluate(question_key="demand", probability_of_changing_decision=0.8, decision_impact=0.8, normalized_cost=0.1).create_branch is True
    assert value_check.evaluate(question_key="demand", probability_of_changing_decision=0.8, decision_impact=0.8, normalized_cost=0.1).reason == "duplicate_research_question"
    assert value_check.evaluate(question_key="trivia", probability_of_changing_decision=0.1, decision_impact=0.1, normalized_cost=0.1).create_branch is False

    questions = QuestionBudget(limit=1)
    assert questions.decide(impact=0.9, independently_verifiable=True) == "research_first"
    assert questions.decide(impact=0.9, independently_verifiable=False) == "ask_user"
    assert questions.decide(impact=0.9, independently_verifiable=False) == "state_assumption"


def test_capability_composition_is_multi_intent_and_phase_scoped() -> None:
    manifest = [{"name": name} for name in (
        "market.quote", "portfolio.read", "broker.account.positions", "web.research",
        "browser.read", "risk.assess", "scheduler.preview", "automation.plan",
        "automation.create", "notification.send", "broker.paper_order", "artifact.create", "interaction.ask",
    )]
    composer = CapabilityComposer()
    prefixes = composer.compose_prefixes(["market_decision", "automation"])

    assert {"market.", "web.", "automation.", "notification.", "scheduler."}.issubset(prefixes)
    assert {item["name"] for item in composer.disclose(manifest, detected_intents=["market_decision", "automation"], phase="research")} == {
        "market.quote", "portfolio.read", "web.research", "browser.read", "artifact.create", "interaction.ask",
    }
    assert composer.disclose(manifest, detected_intents=["market_decision", "automation"], phase="confirmation") == []
    assert {item["name"] for item in composer.disclose(manifest, detected_intents=["market_decision", "automation"], phase="confirmation", confirmed=True)} == {
        "automation.create", "notification.send", "broker.paper_order",
    }


def test_provider_neutral_role_router_uses_low_cost_repair_and_strong_critic() -> None:
    small = ModelProfile("local-small", "ollama", ProviderMode.STRUCTURED_JSON, frozenset({ModelRole.REPAIR, ModelRole.FORMATTER}), 8192, 0, 0.72, local=True)
    main = ModelProfile("main", "openai", ProviderMode.NATIVE_TOOL_CALLING, frozenset({ModelRole.PLANNER, ModelRole.RESEARCHER, ModelRole.CRITIC, ModelRole.REPAIR}), 128000, 2, 0.95, tool_calling=True)
    backup = ModelProfile("backup", "claude", ProviderMode.NATIVE_TOOL_CALLING, frozenset({ModelRole.CRITIC}), 200000, 3, 0.98, tool_calling=True)
    router = ModelRouter([small, main, backup])

    assert router.route(ModelRole.REPAIR).model_id == "local-small"
    assert router.route(ModelRole.CRITIC, high_risk=True).model_id == "backup"
    request = ProviderNeutralRequest("x" * 1000, {}, tuple({"name": str(index)} for index in range(20)), (), {}, (), {"type": "object"})
    compact = router.compact_for_small_model(request)
    assert len(compact.task) == 600
    assert len(compact.tool_schemas) == 12


def test_model_matrix_requires_every_provider_class_and_behavior() -> None:
    matrix = ModelMatrixAuditor()
    for provider in ProviderClass:
        for behavior in MatrixBehavior:
            matrix.record(provider, behavior, passed=True)
    assert matrix.complete is True

    matrix.record(ProviderClass.OLLAMA_WEAK, MatrixBehavior.REPAIR, passed=False)
    assert matrix.complete is False
    assert matrix.gaps()[0].provider_class is ProviderClass.OLLAMA_WEAK
