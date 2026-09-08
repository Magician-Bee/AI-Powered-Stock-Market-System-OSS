from __future__ import annotations

from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.artifact_store import ArtifactStore
from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from open_stock_ai.agent_runtime.model_router import (
    MatrixBehavior,
    ModelMatrixAuditor,
    ProviderClass,
)
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore


@pytest.fixture
def final_system(tmp_path: Path):
    database = tmp_path / "runtime.sqlite"
    session_store = AgentSessionStore(database)
    session = session_store.create(
        session_id="AS-final",
        title="股市 Agent 最終架構",
        namespace="test",
    )
    run_store = AgentRunStore(database)
    request = {
        "objective": "緯創現在要不要賣？",
        "symbols": ["3231.TW"],
        "driver_id": "codex",
        "autonomy": "advisory",
        "max_steps": 8,
        "session_id": session["session_id"],
    }
    run_store.create_run("AR-final", request)
    runtime = FinalAgentRuntime(database)
    domain = runtime.create_forest(
        session_id=session["session_id"],
        run_id="AR-final",
        objective=request["objective"],
    )
    return runtime, session_store, domain, database


def test_scenarios_a_b_g_and_l_use_one_session_and_real_recursive_branches(final_system):
    runtime, sessions, domain, _ = final_system
    initial = runtime.forest("AS-final")
    assert initial and len(initial["branches"]) == 1
    assert runtime.automations.propose("台積電今天怎麼了？").requires_user_confirmation is False

    message_ids = []
    for index, requirement in enumerate(
        ("查投資組合", "查技術面", "查基本面", "查法人", "查外部來源", "建立風險 Critic")
    ):
        message = sessions.add_message(session_id="AS-final", role="user", content=requirement)
        message_ids.append(message["message_id"])
        runtime.steer(
            session_id="AS-final",
            message_id=message["message_id"],
            content=requirement,
            intent="soft_steer",
        )

    forest = runtime.forest("AS-final")
    assert forest and len(forest["branches"]) == 7
    assert sum(item["status"] in {"ready", "running"} for item in forest["branches"]) <= 4
    assert all(item["steps"] or item["parent_branch_id"] is not None for item in forest["branches"])
    assert runtime.objectives.current("AS-final").revision == 7
    assert sessions.title_history("AS-final")[0]["title"] == "股市 Agent 最終架構"


def test_scenarios_c_d_and_g_keep_decision_approval_and_steering_distinct(final_system):
    runtime, sessions, domain, _ = final_system
    interaction = runtime.create_interaction(
        session_id="AS-final",
        run_id="AR-final",
        branch_id=domain["branch_id"],
        waiting_state="waiting_decision",
        payload={
            "question": "Agent偏向分批停利，你要採用哪個方案？",
            "agent_preferred_option": "partial-profit",
            "options": ["partial-profit", "hold", "free_text"],
        },
    )
    assert interaction["status"] == "waiting_decision"
    resolved = runtime.respond_interaction(interaction["interaction_id"], {"option_id": "partial-profit"})
    assert resolved["status"] == "resolved"

    unsafe = runtime.create_interaction(
        session_id="AS-final",
        run_id="AR-final",
        branch_id=domain["branch_id"],
        waiting_state="waiting_approval",
        payload={"risk": "live_trade_without_controls", "safe_alternative": "paper_trade_with_limits"},
    )
    assert unsafe["status"] == "waiting_approval"

    message = sessions.add_message(session_id="AS-final", role="user", content="美國來源也要查")
    outcome = runtime.steer(
        session_id="AS-final",
        message_id=message["message_id"],
        content="美國來源也要查",
    )
    assert outcome["intent"] == "soft_steer"
    assert len(outcome["created_branch_ids"]) == 1
    assert domain["branch_id"] in outcome["affected_branch_ids"]


def test_scenario_h_artifact_selection_is_version_bound_and_conflict_safe(final_system, tmp_path: Path):
    runtime, _, _, database = final_system
    artifact_store = ArtifactStore(database, tmp_path / "artifacts")
    artifact = artifact_store.create_text(
        session_id="AS-final",
        run_id="AR-final",
        name="evidence.md",
        content="法人籌碼：單日",
    )
    first = runtime.ensure_artifact_version(
        artifact["artifact_id"],
        {"institutional_flow": "single_day"},
    )
    selection = runtime.select_artifact(
        session_id="AS-final",
        artifact_id=artifact["artifact_id"],
        artifact_version=first["version"],
        target_type="chart_node",
        path="法人籌碼",
        node_id="institutional-flow",
    )
    second = runtime.revise_artifact(
        artifact_id=artifact["artifact_id"],
        expected_version=selection["artifact_version"],
        content={"institutional_flow": "three_day_sum"},
        changed_by="user",
        reason="改成三天累計",
        affected_node_ids=("institutional-flow",),
    )
    assert second["version"] == 2
    assert second["validation_result"] == {
        "valid": True,
        "validated_by": "host",
        "checks": [
            {"name": "content", "passed": True},
            {"name": "affected_nodes", "passed": True, "node_count": 1},
        ],
        "failures": [],
        "dependency_validation": {
            "status": "recorded",
            "affected_node_ids": ["institutional-flow"],
        },
    }
    with pytest.raises(ValueError, match="affected_node_ids_must_be_unique"):
        runtime.revise_artifact(
            artifact_id=artifact["artifact_id"],
            expected_version=2,
            content={"institutional_flow": "invalid"},
            changed_by="user",
            reason="invalid duplicate node target",
            affected_node_ids=("institutional-flow", "institutional-flow"),
        )
    with pytest.raises(RuntimeError, match="changed from expected"):
        runtime.revise_artifact(
            artifact_id=artifact["artifact_id"],
            expected_version=1,
            content={"institutional_flow": "five_day_sum"},
            changed_by="user",
            reason="stale edit",
        )


def test_scenarios_i_and_j_compile_validate_dry_run_activate_and_deduplicate(final_system):
    runtime, _, _, _ = final_system
    runtime.automations.reanalyze = lambda intent, event, previous: {
        "conclusion": "scenario reanalysis",
        "event": dict(event),
    }
    intent = {
        "goal": "監控緯創下一個分批停利時機",
        "user_id": "user-final",
        "session_id": "AS-final",
        "symbol": "3231.TW",
        "kind": "condition_watch",
        "trigger": {"type": "price_crossing", "field": "price"},
        "observations": [{"type": "market_price"}],
        "analysis": [{"type": "strategy_reanalysis"}],
        "decision_logic": {"field": "price", "operator": "gte", "value": 120},
        "actions": [{"type": "notify", "message": "策略改變"}],
        "notification_policy": {"channels": ["in_app"], "meaningful_only": True},
    }
    activated = runtime.activate_automation(intent, confirmed=True)
    assert activated["automation"]["state"] == "active"
    assert [item["stage"] for item in runtime.automation_store.list_executions(activated["automation"]["automation_id"])] == ["dry_run", "activate"]
    reused = runtime.activate_automation(intent, confirmed=True)
    assert reused["dedup"] == "reuse"
    assert reused["automation"]["automation_id"] == activated["automation"]["automation_id"]
    kpis = runtime.kpis(run_id="AR-final")
    assert kpis["automation_activation"] == 2.0
    assert kpis["automation_duplicate"] == 1.0
    assert kpis["automation_duplicate_rate"] == 0.5


def test_p103_model_matrix_contract_is_complete_for_all_provider_classes():
    auditor = ModelMatrixAuditor()
    for provider in ProviderClass:
        for behavior in MatrixBehavior:
            auditor.record(provider, behavior, passed=True)
    assert auditor.complete
    assert auditor.gaps() == ()


def test_p70_dashboard_is_durable_and_exposes_operator_safe_metrics(final_system):
    runtime, _, domain, database = final_system
    runtime.project_runtime_event(
        "AR-final",
        {
            "event_id": "model-latency-1",
            "timestamp": "2026-08-08T12:00:00+00:00",
            "type": "model.provider.completed",
            "payload": {"provider": "openai-compatible", "model": "gpt-oss:20b", "duration_ms": 125.5},
        },
    )
    runtime.project_runtime_event(
        "AR-final",
        {
            "event_id": "tool-latency-1",
            "timestamp": "2026-08-08T12:00:01+00:00",
            "type": "tool.completed",
            "branch_id": domain["branch_id"],
            "payload": {"tool": "market.research_pack", "call_id": "tool-latency-1"},
        },
    )

    dashboard = runtime.observability_dashboard()
    assert dashboard["schema_version"] == "open_stock_ai.observability_dashboard.v1"
    assert dashboard["active_sessions"] == 1
    assert dashboard["active_branches"] == 1
    assert dashboard["model_latency_ms"] == 125.5
    assert dashboard["automation_count"] == 0
    assert dashboard["notification_count"] == 0
    assert {
        "task_completion_rate",
        "branch_recovery_rate",
        "user_intervention_rate",
        "average_tool_calls",
        "average_token_cost",
        "research_source_diversity",
        "evidence_freshness",
        "user_correction_incorporation_rate",
        "automation_duplicate_rate",
        "session_context_retrieval_precision",
    } <= set(dashboard)

    # Forest branch records remain useful audit history after a Run completes,
    # but they must not be misreported as currently active work.
    AgentRunStore(database).complete_run("AR-final", {"status": "completed"})
    after_completion = runtime.observability_dashboard()
    assert after_completion["active_sessions"] == 0
    assert after_completion["active_branches"] == 0
