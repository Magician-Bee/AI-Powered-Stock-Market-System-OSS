from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.approval_manager import (
    ApprovalManager,
    ApprovalRequiredError,
)
from open_stock_ai.agent_runtime.checkpoint_manager import CheckpointManager
from open_stock_ai.agent_runtime.checkpoint_store import CheckpointStore
from open_stock_ai.agent_runtime.contracts import (
    AgentRunContext,
    AgentToolSpec,
    build_runtime_event,
)
from open_stock_ai.agent_runtime.error_taxonomy import classify_error
from open_stock_ai.agent_runtime.memory import MemoryManager, MemoryStore
from open_stock_ai.agent_runtime.recovery_engine import RecoveryEngine
from open_stock_ai.agent_runtime.orchestrator import (
    _classify_task,
    _completed_critic_observation,
    _critic_disclosed_tools,
    _pending_verified_paper_order,
    _partition_redundant_critic_calls,
)
from open_stock_ai.agent_runtime.plan_compiler import PlanCompiler
from open_stock_ai.agent_runtime.plan_graph import PlanGraph
from open_stock_ai.agent_runtime.plan_manager import PlanManager
from open_stock_ai.agent_runtime.policy_engine import PolicyEngine
from open_stock_ai.agent_runtime.providers import CodexProvider, ProviderRegistry
from open_stock_ai.agent_runtime.rollback_manager import RollbackManager
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.validators import ValidatorEngine, _final_summary_scope_check
from open_stock_ai.agent_runtime.workflow_runtime import WorkflowRuntime
from open_stock_ai.agent_runtime.workflow_store import WorkflowStore
from open_stock_ai.agent_runtime.scheduler import SchedulePlanner, condition_matches
from open_stock_ai.agent_runtime.workers import WorkerSupervisor, WorkerToolError, profile_for
from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from stock_ai.agent_general_tools import GeneralAgentToolProvider
from stock_ai.market_calendar import default_taiwan_market_calendar
from stock_ai.agent_drivers import _model_transcript, _turn_prompt
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime
from stock_ai.capability_registry import BoundCapabilityProvider, CapabilityRegistry
from stock_ai.tool_providers.git import GitToolProvider
from stock_ai.tool_providers.runtime import AgentRuntimeToolProvider


def _run_store(path: Path, *, run_id: str = "AR-v2", session_id: str = "AS-v2") -> AgentRunStore:
    sessions = AgentSessionStore(path)
    sessions.create(session_id=session_id, namespace="test", title="test")
    store = AgentRunStore(path)
    store.create_run(
        run_id,
        {
            "objective": "test",
            "symbols": [],
            "driver_id": "codex",
            "autonomy": "advisory",
            "max_steps": 6,
            "session_id": session_id,
        },
    )
    return store


def test_plan_manager_rebases_a_stale_checkpoint_revision_instead_of_failing(tmp_path):
    """Recovery must not die when an earlier pass already revised the plan."""

    path = tmp_path / "plan-rebase.sqlite"
    _run_store(path)
    manager = PlanManager(path)
    original = PlanGraph.create("repair a recoverable task")
    manager.create(
        session_id="AS-v2",
        run_id="AR-v2",
        objective="repair a recoverable task",
        plan=original,
    )
    stale_checkpoint_graph = PlanGraph.from_dict(original.to_dict())

    first = manager.revise(
        original,
        run_id="AR-v2",
        patch={"operations": [{"op": "add_assumption", "value": "first repair pass"}]},
        reason_summary="first durable repair",
    )
    rebased = manager.revise(
        stale_checkpoint_graph,
        run_id="AR-v2",
        patch={"operations": [{"op": "add_constraint", "value": "preserve completed work"}]},
        reason_summary="checkpoint recovery rebase",
    )

    assert first.revision_number == 2
    assert rebased.revision_number == 3
    assert rebased.assumptions == ["first repair pass"]
    assert "preserve completed work" in rebased.constraints
    assert "Live brokerage is unavailable." in rebased.constraints
    assert [item["revision"] for item in manager.revisions("AR-v2")] == [1, 2, 3]


def test_plan_manager_does_not_allow_a_stale_checkpoint_to_overwrite_latest_revision(tmp_path):
    """A resumed checkpoint cannot turn a revision-3 plan row back into revision 1."""

    path = tmp_path / "plan-stale-save.sqlite"
    _run_store(path)
    manager = PlanManager(path)
    original = PlanGraph.create("preserve durable plan state")
    manager.create(
        session_id="AS-v2",
        run_id="AR-v2",
        objective="preserve durable plan state",
        plan=original,
    )
    stale_checkpoint = PlanGraph.from_dict(original.to_dict())
    manager.revise(
        original,
        run_id="AR-v2",
        patch={"operations": [{"op": "add_assumption", "value": "first durable pass"}]},
        reason_summary="first durable pass",
    )
    latest = manager.revise(
        stale_checkpoint,
        run_id="AR-v2",
        patch={"operations": [{"op": "add_constraint", "value": "second durable pass"}]},
        reason_summary="second durable pass",
    )

    manager.save_state(stale_checkpoint, run_id="AR-v2")
    recovered = manager.for_run("AR-v2")

    assert latest.revision_number == 3
    assert recovered is not None
    assert recovered.revision_number == 3
    assert recovered.assumptions == ["first durable pass"]
    assert "second durable pass" in recovered.constraints


def test_plan_manager_replan_preserves_failed_node_state(tmp_path):
    """A recovery replan must not resurrect a failed capability as pending."""

    path = tmp_path / "plan-repair-state.sqlite"
    _run_store(path)
    manager = PlanManager(path)
    original = PlanGraph.create("repair a failed source")
    original.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "failed-source",
                        "node_type": "tool",
                        "title": "failed source",
                        "tool_name": "web.research",
                        "arguments": {"query": "source"},
                    },
                }
            ]
        }
    )
    manager.create(
        session_id="AS-v2",
        run_id="AR-v2",
        objective="repair a failed source",
        plan=original,
    )
    original.mark("failed-source", "failed")
    manager.save_state(original, run_id="AR-v2")

    revised = manager.revise(
        original,
        run_id="AR-v2",
        patch={"operations": [{"op": "add_assumption", "value": "use an alternate source"}]},
        reason_summary="replan around failed source",
    )

    assert revised.nodes["failed-source"].status == "failed"
    assert manager.for_run("AR-v2").nodes["failed-source"].status == "failed"


def test_max_steps_store_blocks_running_steps_and_can_reopen_with_a_larger_budget(tmp_path):
    path = tmp_path / "max-steps.sqlite"
    store = _run_store(path)
    plan = PlanGraph.create("需要更多步驟", plan_id="AP-limit")
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "unfinished",
                        "node_type": "reasoning",
                        "title": "尚未完成",
                        "status": "running",
                        "order_index": 1,
                    },
                }
            ]
        }
    )
    store.append_event(
        "AR-v2",
        build_runtime_event(
            "plan.proposed",
            sequence=1,
            run_id="AR-v2",
            session_id="AS-v2",
            payload={"plan": plan.to_dict()},
        ),
    )
    store.append_event(
        "AR-v2",
        build_runtime_event(
            "model.turn.started",
            sequence=2,
            run_id="AR-v2",
            session_id="AS-v2",
            payload={"step": 1, "summary": "仍在執行"},
        ),
    )

    store.complete_run(
        "AR-v2",
        {"status": "max_steps_reached", "summary": "尚未完成"},
    )

    limited = store.get_run("AR-v2")
    assert limited and limited["status"] == "max_steps_reached"
    assert limited["terminal"] is True
    assert store.steps("AR-v2")[0]["status"] == "blocked"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "select status from agent_steps where run_id='AR-v2' and step=1"
        ).fetchone()[0] == "blocked"

    reopened = store.prepare_continuation("AR-v2", max_steps=18)
    assert reopened["status"] == "queued"
    assert reopened["terminal"] is False
    assert reopened["max_steps"] == 18
    assert reopened["request"]["max_steps"] == 18
    assert store.steps("AR-v2")[0]["status"] == "ready"


def test_latest_schema_contains_durable_runtime_entities(tmp_path):
    path = tmp_path / "runtime.sqlite"
    with sqlite3.connect(path) as conn:
        apply_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'")
        }
        columns = {row[1] for row in conn.execute("pragma table_info(agent_runs)")}
        version = conn.execute("pragma user_version").fetchone()[0]

    assert version == LATEST_SCHEMA_VERSION == 47
    assert {
        "agent_sessions",
        "agent_messages",
        "agent_plans",
        "agent_plan_revisions",
        "agent_checkpoints",
        "agent_memory",
        "agent_artifacts",
        "agent_workflows",
        "agent_workers",
        "agent_control_messages",
        "provider_configs",
        "agent_ui_state",
        "agent_ui_commands",
        "agent_runtime_events",
        "agent_memory_fts",
        "agent_objective_versions",
        "agent_task_forests",
        "agent_branches",
        "agent_branch_plans",
        "agent_branch_steps",
        "agent_branch_dependencies",
        "agent_join_nodes",
        "agent_decision_checkpoints",
        "agent_user_proposals",
        "agent_proposal_evaluations",
        "agent_failure_ledger",
        "agent_repair_attempts",
        "agent_failure_fingerprints",
        "agent_memory_candidates",
        "agent_memory_conflicts",
        "agent_memory_supersession",
        "agent_procedural_lessons",
        "agent_artifact_versions",
        "agent_artifact_selections",
        "agent_automation_intents",
        "agent_automations",
        "agent_automation_versions",
        "agent_automation_executions",
        "agent_notification_deliveries",
        "agent_session_titles",
        "agent_session_title_history",
        "agent_evidence",
        "agent_evidence_edges",
        "agent_kpi_events",
        "agent_token_budgets",
    } <= tables
    assert {"session_id", "plan_id", "checkpoint_id", "parent_run_id", "resume_count"} <= columns


def test_durable_plan_projection_removes_nodes_deleted_by_a_revision(tmp_path):
    store = _run_store(tmp_path / "runtime.sqlite")
    store.append_event(
        "AR-v2",
        {
            "event_id": "E-plan-1",
            "sequence": 1,
            "type": "plan.proposed",
            "run_id": "AR-v2",
            "session_id": "AS-v2",
            "payload": {
                "plan": {
                    "plan_id": "AP-v2",
                    "revision_number": 1,
                    "nodes": [
                        {"node_id": "keep", "title": "保留", "status": "completed"},
                        {"node_id": "remove", "title": "移除", "status": "pending"},
                    ],
                }
            },
        },
    )
    store.append_event(
        "AR-v2",
        {
            "event_id": "E-plan-2",
            "sequence": 2,
            "type": "plan.revised",
            "run_id": "AR-v2",
            "session_id": "AS-v2",
            "payload": {
                "plan": {
                    "plan_id": "AP-v2",
                    "revision_number": 2,
                    "nodes": [
                        {"node_id": "keep", "title": "保留", "status": "completed"}
                    ],
                }
            },
        },
    )

    assert [step["node_id"] for step in store.steps("AR-v2")] == ["keep"]


def test_current_schema_check_does_not_repeat_backfills_or_rebuild_triggers(tmp_path):
    path = tmp_path / "runtime.sqlite"
    with sqlite3.connect(path) as conn:
        apply_migrations(conn)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        apply_migrations(conn)

    normalized = [statement.strip().casefold() for statement in statements]
    assert not any(statement.startswith("update data_revisions") for statement in normalized)
    assert not any(statement.startswith("drop trigger") for statement in normalized)
    assert not any(statement.startswith("create trigger") for statement in normalized)


def test_plan_graph_is_dynamic_but_started_nodes_are_immutable():
    plan = PlanGraph.create("inspect")
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "read",
                        "node_type": "tool",
                        "title": "read",
                        "tool_name": "project.read_file",
                        "arguments": {"path": "README.md"},
                    },
                }
            ]
        }
    )
    plan.mark("read", "completed")

    with pytest.raises(ValueError, match="cannot be rewritten"):
        plan.apply_patch(
            {
                "operations": [
                    {"op": "update_node", "node_id": "read", "changes": {"title": "rewrite"}}
                ]
            }
        )


def test_invalid_plan_patch_is_atomic_and_does_not_poison_followup_host_nodes():
    plan = PlanGraph.create("analyze 2330.TW")
    before = plan.to_dict()

    with pytest.raises(ValueError, match="Unknown dependency node: n1"):
        plan.apply_patch(
            {
                "operations": [
                    {
                        "op": "add_node",
                        "node": {
                            "node_id": "n2",
                            "node_type": "reasoning",
                            "title": "評估 2330.TW 風險",
                            "dependencies": ["n1"],
                        },
                    }
                ]
            }
        )

    assert plan.to_dict() == before
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "tool-1",
                        "node_type": "tool",
                        "title": "取得 2330.TW 行情",
                        "tool_name": "market.research_pack",
                    },
                }
            ]
        }
    )
    assert list(plan.nodes) == ["tool-1"]


def test_plan_compiler_rejects_unknown_schema_permission_and_cycles():
    tool = AgentToolSpec(
        name="project.write_file",
        description="write",
        category="project_write",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
        mutating=True,
        requires_project_execution=True,
    ).to_dict()
    plan = PlanGraph.create("write")
    with pytest.raises(ValueError, match="cycle"):
        plan.apply_patch(
            {
                "operations": [
                    {
                        "op": "add_node",
                        "node": {
                            "node_id": "write",
                            "node_type": "tool",
                            "title": "write",
                            "tool_name": "project.write_file",
                            "arguments": {"unexpected": True},
                            "dependencies": ["finish"],
                        },
                    },
                    {
                        "op": "add_node",
                        "node": {
                            "node_id": "finish",
                            "node_type": "finalize",
                            "title": "finish",
                            "dependencies": ["write"],
                        },
                    },
                ]
            }
        )
    plan = PlanGraph.create("write")
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "write",
                        "node_type": "tool",
                        "title": "write",
                        "tool_name": "project.write_file",
                        "arguments": {"unexpected": True},
                    },
                }
            ]
        }
    )
    result = PlanCompiler([tool]).compile(plan, autonomy="advisory")
    codes = {error["code"] for error in result.errors}
    assert result.valid is False
    assert {"invalid_tool_arguments", "project_execution_required", "mutation_requires_postcondition"} <= codes


def test_validator_enforces_tool_specific_and_host_grounded_completion_criteria():
    validator = ValidatorEngine()
    terminal = validator.validate_tool_result(
        tool={"name": "terminal.run", "mutating": True, "output_schema": {"type": "object"}},
        arguments={"command": "pytest -q"},
        result={"exit_code": 1, "timed_out": False, "stderr": "failed"},
        before={"hash": "before"},
        after={"hash": "after"},
    )
    assert terminal.passed is False
    assert any(
        check["name"] == "terminal_process_succeeded" and check["passed"] is False
        for check in terminal.checks
    )

    plan = PlanGraph.create("verify tests")
    plan.completion_criteria = ["所有測試通過"]
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "test",
                        "node_type": "tool",
                        "title": "tests",
                        "tool_name": "terminal.run",
                        "arguments": {"command": "pytest -q"},
                        "status": "completed",
                        "postconditions": [{"path": "exit_code", "equals": 0}],
                    },
                }
            ]
        }
    )
    evidence = {
        "call-test": {
            "call_id": "call-test",
            "node_id": "test",
            "tool": "terminal.run",
            "ok": True,
            "validation": {"passed": True},
        }
    }
    completed = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={
            "criteria_met": True,
            "criterion_results": [
                {
                    "criterion": "所有測試通過",
                    "met": True,
                    "evidence_ids": ["call-test"],
                }
            ],
            "evidence_ids": ["call-test"],
            "remaining_gaps": [],
        },
        known_evidence_ids={"call-test", "test"},
        evidence_catalog=evidence,
    )
    assert completed.passed is True

    placeholder = validator.validate_completion(
        state="complete",
        final_summary="No further action required.",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={
            "criteria_met": True,
            "criterion_results": [
                {
                    "criterion": "所有測試通過",
                    "met": True,
                    "evidence_ids": ["call-test"],
                }
            ],
            "evidence_ids": ["call-test"],
            "remaining_gaps": [],
        },
        known_evidence_ids={"call-test", "test"},
        evidence_catalog=evidence,
    )
    assert placeholder.passed is False
    assert next(
        check for check in placeholder.checks if check["name"] == "final_answer_is_meaningful"
    )["passed"] is False

    wrong_type = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={
            "criteria_met": True,
            "criterion_results": [
                {
                    "criterion": "所有測試通過",
                    "met": True,
                    "evidence_ids": ["ui-only"],
                }
            ],
            "evidence_ids": ["ui-only"],
            "remaining_gaps": [],
        },
        known_evidence_ids={"ui-only", "test"},
        evidence_catalog={
            "ui-only": {
                "tool": "ui.navigate",
                "ok": True,
                "validation": {"passed": True},
            }
        },
    )
    assert wrong_type.passed is False


def test_validator_rejects_generic_success_and_accepts_persisted_paper_order_receipt():
    validator = ValidatorEngine()
    generic = validator.validate_tool_result(
        tool={
            "name": "custom.mutation",
            "mutating": True,
            "output_schema": {"type": "object"},
        },
        arguments={},
        result={"success": True},
    )
    assert generic.passed is False
    assert any(
        check["name"] == "mutation_before_after_evidence" and check["host_receipt"] is False
        for check in generic.checks
    )

    order = {
        "order_id": "PB-AGENT-123",
        "symbol": "2330.TW",
        "side": "buy",
        "status": "filled",
    }
    paper = validator.validate_tool_result(
        tool={
            "name": "paper.submit_order",
            "mutating": True,
            "output_schema": {"type": "object"},
        },
        arguments={"symbol": "2330.TW", "side": "add"},
        result={
            "broker": {"order": order},
            "account": {"open_orders": [], "recent_orders": [order]},
        },
    )
    assert paper.passed is True
    assert any(
        check["name"] == "paper_order_persisted" and check["passed"] is True
        for check in paper.checks
    )


def test_completion_validator_allows_stable_general_answer_without_tool_evidence():
    validator = ValidatorEngine()
    plan = PlanGraph.create("1 + 1")
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {
                "criterion": "The user objective is satisfied by validated evidence.",
                "met": True,
                "evidence_ids": [],
            }
        ],
        "evidence_ids": [],
        "remaining_gaps": [],
    }

    general = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=0,
        evidence_required=False,
        completion_evaluation=evaluation,
        known_evidence_ids=set(),
        evidence_catalog={},
    )
    current_fact = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=0,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids=set(),
        evidence_catalog={},
    )

    assert general.passed is True
    assert current_fact.passed is False


def test_completion_validator_accepts_only_advisory_semantic_uncertainty():
    validator = ValidatorEngine()
    plan = PlanGraph.create("分析 2330.TW 並提供決策")
    plan.completion_criteria = ["完成 2330.TW 行情、技術面、新聞與風險分析並給出決策"]
    evidence = {
        "call-research": {
            "call_id": "call-research",
            "tool": "market.research_pack",
            "ok": True,
            "validation": {"passed": True},
            "result": {"symbol": "2330.TW", "price": 2280, "recommendation": "watch"},
        }
    }
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {
                "criterion": plan.completion_criteria[0],
                "met": True,
                "evidence_ids": ["call-research"],
            }
        ],
        "evidence_ids": ["call-research"],
        "remaining_gaps": [],
    }

    completed = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"call-research"},
        evidence_catalog=evidence,
    )
    semantic = next(
        check for check in completed.checks if check["name"] == "semantic_validation_layer"
    )

    assert completed.passed is True
    assert semantic["passed"] is True
    assert semantic["advisory_uncertain_entailment_accepted"] is True
    assert semantic["hard_failure_codes"] == []

    with_gap = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={**evaluation, "remaining_gaps": ["尚未完成風險分析"]},
        known_evidence_ids={"call-research"},
        evidence_catalog=evidence,
    )
    missing_evidence = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={
            **evaluation,
            "criterion_results": [
                {
                    "criterion": plan.completion_criteria[0],
                    "met": True,
                    "evidence_ids": ["unknown-call"],
                }
            ],
            "evidence_ids": ["unknown-call"],
        },
        known_evidence_ids={"call-research"},
        evidence_catalog=evidence,
    )

    assert with_gap.passed is False
    assert missing_evidence.passed is False
    missing_semantic = next(
        check
        for check in missing_evidence.checks
        if check["name"] == "semantic_validation_layer"
    )
    assert "semantic_claim_missing_evidence" in missing_semantic["hard_failure_codes"]


def test_completion_validator_isolates_failed_branch_only_with_grounded_alternative():
    validator = ValidatorEngine()
    plan = PlanGraph.create("研究 2330.TW")
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "failed-search",
                        "node_type": "tool",
                        "title": "失敗的代號搜尋",
                        "tool_name": "market.search_taiwan_securities",
                        "status": "failed",
                    },
                },
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "fallback-research",
                        "node_type": "tool",
                        "title": "替代公開來源研究",
                        "tool_name": "web.research",
                        "status": "completed",
                    },
                },
            ]
        }
    )
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {
                "criterion": "The user objective is satisfied by validated evidence.",
                "met": True,
                "evidence_ids": ["fallback-evidence"],
            }
        ],
        "evidence_ids": ["fallback-evidence"],
        "remaining_gaps": [],
    }
    evidence = {
        "fallback-evidence": {
            "tool": "web.research",
            "ok": True,
            "validation": {"passed": True},
            "result": {"source_count": 3},
        }
    }

    covered = validator.validate_completion(
        state="complete",
        final_summary="已根據三個公開來源完成摘要。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"fallback-evidence"},
        evidence_catalog=evidence,
        failure_recovery_coverage={"failed-search": ["fallback-evidence"]},
    )
    invented_numbers = validator.validate_completion(
        state="complete",
        final_summary="2330.TW 收盤 2,425.00，跌幅 0.8%，RSI 48。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"fallback-evidence"},
        evidence_catalog=evidence,
        failure_recovery_coverage={"failed-search": ["fallback-evidence"]},
    )
    placeholder_measurements = validator.validate_completion(
        state="complete",
        final_summary="Latest quarterly report shows revenue growth of X% and EPS of Y.",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"fallback-evidence"},
        evidence_catalog=evidence,
        failure_recovery_coverage={"failed-search": ["fallback-evidence"]},
    )
    uncovered = validator.validate_completion(
        state="complete",
        final_summary="已完成。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=0,
        evidence_required=False,
        completion_evaluation={**evaluation, "criterion_results": []},
        known_evidence_ids=set(),
        evidence_catalog={},
    )

    assert covered.passed is True
    isolated = next(
        check for check in covered.checks
        if check["name"] == "failed_plan_nodes_are_nonblocking"
    )
    assert isolated["covered_by_alternative_host_evidence"] is True
    uncovered_recovery = validator.validate_completion(
        state="complete",
        final_summary="已根據三個公開來源完成摘要。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"fallback-evidence"},
        evidence_catalog=evidence,
    )
    assert uncovered_recovery.passed is False
    uncovered_branch = next(
        check for check in uncovered_recovery.checks
        if check["name"] == "failed_plan_nodes_are_nonblocking"
    )
    assert uncovered_branch["unresolved_failed_nodes"] == ["failed-search"]
    assert invented_numbers.passed is False
    numeric = next(
        check for check in invented_numbers.checks
        if check["name"] == "final_answer_numeric_claims_are_evidence_grounded"
    )
    assert {"48", "2425", "0.8"} <= set(numeric["unsupported_claims"])
    assert placeholder_measurements.passed is False
    placeholder_check = next(
        check for check in placeholder_measurements.checks
        if check["name"] == "final_answer_has_no_placeholder_measurements"
    )
    assert any("X%" in item for item in placeholder_check["placeholders"])
    assert any("EPS" in item for item in placeholder_check["placeholders"])
    assert uncovered.passed is False


def test_completion_validator_rejects_monthly_measurements_cross_wired_between_periods():
    validator = ValidatorEngine()
    plan = PlanGraph.create("整理官方月營收")
    evidence = {
        "monthly-revenue": {
            "tool": "market.monthly_revenue",
            "ok": True,
            "validation": {"passed": True},
            "result": {
                "symbol": "2887.TW",
                "items": [
                    {
                        "period": "2026-07",
                        "current_revenue": 18_052_275,
                        "mom_change_percent": 1.78,
                        "yoy_change_percent": -15.63,
                    },
                    {
                        "period": "2026-06",
                        "current_revenue": 17_736_690,
                        "mom_change_percent": 19.49,
                        "yoy_change_percent": 329.5,
                    },
                ],
            },
        }
    }
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {
                "criterion": "已依官方月營收完成摘要",
                "met": True,
                "evidence_ids": ["monthly-revenue"],
            }
        ],
        "evidence_ids": ["monthly-revenue"],
        "remaining_gaps": [],
    }
    common = {
        "state": "complete",
        "has_pending_tool_calls": False,
        "plan": plan.to_dict(),
        "successful_observations": 1,
        "evidence_required": True,
        "completion_evaluation": evaluation,
        "known_evidence_ids": {"monthly-revenue"},
        "evidence_catalog": evidence,
    }

    correct = validator.validate_completion(
        final_summary="2026年7月營收為18,052,275千元，較前月增長1.78%，較去年同期下降15.63%。",
        **common,
    )
    cross_wired = validator.validate_completion(
        final_summary="2026年7月營收為17,736,690千元，較前月增長19.49%，較去年同期下降15.63%。",
        **common,
    )

    correct_period_check = next(
        check for check in correct.checks
        if check["name"] == "final_answer_period_measurements_are_evidence_bound"
    )
    cross_wired_period_check = next(
        check for check in cross_wired.checks
        if check["name"] == "final_answer_period_measurements_are_evidence_bound"
    )
    numeric_check = next(
        check for check in cross_wired.checks
        if check["name"] == "final_answer_numeric_claims_are_evidence_grounded"
    )
    assert correct_period_check["passed"] is True
    assert numeric_check["passed"] is True
    assert cross_wired.passed is False
    assert cross_wired_period_check["unsupported_claims"][0]["period"] == "2026-07"


def test_completion_accepts_grounded_display_rounding_and_negative_trade_constraint():
    validator = ValidatorEngine()
    plan = PlanGraph.create("Critic 驗證 2330.TW")
    plan.completion_criteria = (
        "取得 Host 驗證技術與風險觀察",
        "Critic 未執行交易、下單或自動化並完成本地結論",
    )
    evidence = {
        "critic-market": {
            "call_id": "critic-market",
            "tool": "market.analyze_symbol",
            "ok": True,
            "validation": {"passed": True},
            "result": {
                "rsi_14": 6.16458923238,
                "institutional_net": -1587400,
            },
        }
    }
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {"criterion": criterion, "met": True, "evidence_ids": ["critic-market"]}
            for criterion in plan.completion_criteria
        ],
        "evidence_ids": ["critic-market"],
        "remaining_gaps": [],
    }

    result = validator.validate_completion(
        state="complete",
        objective="分析 2330.TW 並由 Critic 挑戰，不要交易或建立自動化。",
        task_kind="market_information",
        final_summary="RSI 顯示為 6.16，法人淨賣超 1,587,400；僅分析，未交易。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=1,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"critic-market"},
        evidence_catalog=evidence,
    )

    assert result.passed is True
    numeric = next(
        check for check in result.checks
        if check["name"] == "final_answer_numeric_claims_are_evidence_grounded"
    )
    assert numeric["unsupported_claims"] == []
    criteria = next(
        check for check in result.checks
        if check["name"] == "completion_criteria_have_host_grounded_results"
    )
    assert criteria["criteria"][1]["required_tool_prefixes"] == []


def test_completion_validator_rejects_a_preface_for_a_comprehensive_request():
    validator = ValidatorEngine()
    plan = PlanGraph.create("完整分析 2330.TW")
    evidence = {
        "market-one": {
            "call_id": "market-one",
            "tool": "market.analyze_symbol",
            "ok": True,
            "validation": {"passed": True},
            "result": {
                "symbol": "2330.TW",
                "recommendation_bucket": "watch",
                "portfolio_status": {"total_position_size_pct": 0},
                "technical_features": {"rsi_14": 53},
                "risk_summary": {"approved": False},
                "pipeline_workspace": {"source": "FinGPT"},
            },
        },
        "revenue-one": {
            "call_id": "revenue-one",
            "tool": "market.monthly_revenue",
            "ok": True,
            "validation": {"passed": True},
            "result": {"items": [{"period": "2026-06"}]},
        },
        "institutional-one": {
            "call_id": "institutional-one",
            "tool": "market.institutional_flow",
            "ok": True,
            "validation": {"passed": True},
            "result": {"items": [{"trade_date": "2026-08-03"}]},
        },
    }
    evaluation = {
        "criteria_met": True,
        "criterion_results": [
            {
                "criterion": "The user objective is satisfied by validated evidence.",
                "met": True,
                    "evidence_ids": [
                        "market-one", "revenue-one", "institutional-one",
                    ],
            }
        ],
        "evidence_ids": ["market-one", "revenue-one", "institutional-one"],
        "remaining_gaps": [],
    }
    objective = (
        "請完整分析 2330.TW 是否應該賣出：需要持股、技術、基本面、法人、"
        "外部研究與風險反方檢視；只做分析。"
    )

    preface = validator.validate_completion(
        state="complete",
        objective=objective,
        task_kind="market_information",
        final_summary="以下是完整分析，所有結論都以驗證過的證據為準。",
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=3,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"market-one", "revenue-one", "institutional-one"},
        evidence_catalog=evidence,
    )
    detailed = validator.validate_completion(
        state="complete",
        objective=objective,
        task_kind="market_information",
        final_summary=(
            "持股面：目前沒有讀到實際持股數量，因此不能假設曝險。\n"
            "技術面：已驗證的行情研究結果支持目前以觀察為主，沒有足夠證據支持立即賣出。\n"
            "基本面與法人面：外部研究已完成，但沒有取得可直接核對的法人持股變化，這部分明確列為資料限制。\n"
            "風險反方：若後續風險條件惡化，結論可能改變；相反地，現在提前退出也可能失去後續參與。\n"
            "結論：依目前證據維持觀察，不執行交易，也不建立任何自動化。"
        ),
        has_pending_tool_calls=False,
        plan=plan.to_dict(),
        successful_observations=3,
        evidence_required=True,
        completion_evaluation=evaluation,
        known_evidence_ids={"market-one", "revenue-one", "institutional-one"},
        evidence_catalog=evidence,
    )

    assert preface.passed is False
    scope = next(
        check for check in preface.checks
        if check["name"] == "final_answer_covers_requested_scope"
    )
    assert scope["passed"] is False
    assert scope["comprehensive_request"] is True
    assert detailed.passed is True


def test_routine_paper_order_does_not_require_a_long_form_final_summary():
    scope = _final_summary_scope_check(
        objective=(
            "請分析 3105.TWO，並以最新已驗證價格完成一筆 100 股買進的"
            "紙上模擬交易；這不是實盤交易。"
        ),
        task_kind="market_decision",
        final_summary="紙上交易已由 Host 驗證完成，未送往實盤券商。",
        evidence_required=True,
    )

    assert scope["routine_paper_execution"] is True
    assert scope["comprehensive_request"] is False
    assert scope["passed"] is True


def test_provider_registry_only_activates_codex():
    registry = ProviderRegistry((CodexProvider(object()),), primary="codex")
    description = registry.describe()
    enabled = [item for item in description["items"] if item["enabled"]]
    disabled = [item for item in description["items"] if not item["enabled"]]

    assert [item["provider_id"] for item in enabled] == ["codex"]
    assert all(item["process_started"] is False and item["model_loaded"] is False for item in disabled)
    with pytest.raises(ValueError, match="disabled"):
        registry.get("local-openai")


def test_codex_provider_binds_and_reuses_the_exact_project_root():
    class Runtime:
        def __init__(self):
            self.started = []
            self.closed = []

        async def start_agent_run(self, run_id, *, project_root=None, model="", reasoning_effort=""):
            self.started.append((run_id, project_root, model, reasoning_effort))

        def agent_session_metadata(self, run_id):
            assert run_id == "AR-root"
            return {"model": "selected-model", "reasoning_effort": "high"}

        async def close_agent_run(self, run_id):
            self.closed.append(run_id)

    runtime = Runtime()
    provider = CodexProvider(runtime, model="selected-model", reasoning_effort="high")

    async def scenario():
        await provider.start_session("AR-root", project_root="/project/root")
        await provider.compact_context("AR-root")
        await provider.close_session("AR-root")

    asyncio.run(scenario())

    assert runtime.started == [
        ("AR-root", "/project/root", "selected-model", "high"),
        ("AR-root", "/project/root", "selected-model", "high"),
    ]
    assert runtime.closed == ["AR-root", "AR-root"]


def test_session_messages_are_namespace_scoped_and_ordered(tmp_path):
    store = AgentSessionStore(tmp_path / "runtime.sqlite")
    one = store.create(namespace="one", title="one")
    two = store.create(namespace="two", title="two")
    store.add_message(session_id=one["session_id"], role="user", content={"text": "hello"})
    store.add_message(session_id=one["session_id"], role="assistant", content={"text": "world"})

    assert [item["role"] for item in store.messages(one["session_id"])] == ["user", "assistant"]
    assert [item["session_id"] for item in store.list(namespace="one")] == [one["session_id"]]
    assert [item["session_id"] for item in store.list(namespace="two")] == [two["session_id"]]


def test_child_run_messages_preserve_root_session_focus_and_repair_legacy_focus(tmp_path):
    path = tmp_path / "runtime.sqlite"
    sessions = AgentSessionStore(path)
    session = sessions.create(namespace="one", title="root focus")
    runs = AgentRunStore(path)
    root_id = "AR-root-focus"
    child_id = "AR-child-focus"
    base_request = {
        "objective": "root",
        "symbols": [],
        "driver_id": "codex",
        "autonomy": "advisory",
        "max_steps": 6,
        "session_id": session["session_id"],
    }
    runs.create_run(root_id, base_request)
    runs.create_run(
        child_id,
        {**base_request, "objective": "child", "parent_run_id": root_id},
    )
    sessions.add_message(
        session_id=session["session_id"],
        run_id=root_id,
        role="user",
        content={"text": "root"},
    )
    sessions.add_message(
        session_id=session["session_id"],
        run_id=child_id,
        role="assistant",
        content={"text": "child"},
        set_active_run=False,
    )
    assert sessions.get(session["session_id"])["active_run_id"] == root_id

    # Older runtimes persisted the child as last_run_id. Reading either the
    # detail or list projection must still recover the foreground root Run.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "update agent_sessions set last_run_id=? where session_id=?",
            (child_id, session["session_id"]),
        )
        conn.commit()
    assert sessions.get(session["session_id"])["active_run_id"] == root_id
    assert sessions.list(namespace="one")[0]["active_run_id"] == root_id


def test_approval_is_bound_to_exact_arguments_and_cannot_self_approve(tmp_path):
    path = tmp_path / "runtime.sqlite"
    _run_store(path)
    manager = ApprovalManager(path)
    with pytest.raises(ApprovalRequiredError) as requested:
        manager.require(
            run_id="AR-v2",
            step_id="tool-write",
            tool_name="project.write_file",
            arguments={"path": "a.txt", "content": "one"},
            resource_scope={"path": "a.txt"},
            risk_class="local_reversible",
        )
    approval = requested.value.approval
    with pytest.raises(PermissionError, match="cannot approve"):
        manager.resolve(
            approval["approval_id"],
            approved=True,
            decided_by="codex",
            challenge="invalid-challenge",
        )
    challenge = manager.issue_challenge(approval["approval_id"])["challenge"]
    approved = manager.resolve(
        approval["approval_id"],
        approved=True,
        decided_by="local_user",
        challenge=challenge,
    )
    assert approved["status"] == "approved"
    with pytest.raises(ApprovalRequiredError) as changed:
        manager.require(
            run_id="AR-v2",
            step_id="tool-write",
            tool_name="project.write_file",
            arguments={"path": "a.txt", "content": "changed"},
            resource_scope={"path": "a.txt"},
            risk_class="local_reversible",
        )
    assert changed.value.approval["argument_digest"] != approved["argument_digest"]


def test_denied_approval_cannot_be_replayed_with_the_same_arguments(tmp_path):
    path = tmp_path / "runtime.sqlite"
    _run_store(path)
    manager = ApprovalManager(path)
    with pytest.raises(ApprovalRequiredError) as requested:
        manager.require(
            run_id="AR-v2",
            step_id="tool-delete",
            tool_name="project.delete_file",
            arguments={"path": "unsafe.txt"},
            resource_scope={"path": "unsafe.txt"},
            risk_class="local_destructive",
        )
    approval_id = requested.value.approval["approval_id"]
    challenge = manager.issue_challenge(approval_id)["challenge"]
    approval = manager.resolve(
        approval_id,
        approved=False,
        decided_by="local_user",
        challenge=challenge,
    )
    assert approval["status"] == "denied"
    with pytest.raises(PermissionError, match="revise the plan"):
        manager.require(
            run_id="AR-v2",
            step_id="tool-delete",
            tool_name="project.delete_file",
            arguments={"path": "unsafe.txt"},
            resource_scope={"path": "unsafe.txt"},
            risk_class="local_destructive",
        )


def test_checkpoint_roundtrip_restores_plan_trace_and_context(tmp_path):
    path = tmp_path / "runtime.sqlite"
    _run_store(path)
    plan = PlanGraph.create("resume")
    manager = CheckpointManager(CheckpointStore(path))
    saved = manager.save(
        session_id="AS-v2",
        run_id="AR-v2",
        sequence=9,
        plan=plan,
        transcript=[{"role": "host"}],
        trace=[{"tool": "project.read_file", "ok": True}],
        context_state={"current_step": 3, "non_json": object()},
    )
    restored = manager.restore("AR-v2")

    assert restored is not None
    assert restored["checkpoint_id"] == saved["checkpoint_id"]
    assert restored["payload"]["context_state"] == {"current_step": 3}
    assert restored["payload"]["trace"][0]["ok"] is True


def test_memory_excludes_expired_live_state_and_isolates_namespaces(tmp_path):
    store = MemoryStore(tmp_path / "runtime.sqlite")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.write(
        namespace="one",
        kind="domain",
        fact_type="temporary_state",
        content="live quote 100",
        source={"type": "market", "contains_live_market_state": True},
        expires_at=expired,
    )
    store.write(
        namespace="two",
        kind="project",
        fact_type="fact",
        content="durable project fact",
        source={"type": "project_file"},
    )
    assert store.search(namespace="one", query="quote") == []
    assert store.search(namespace="two", query="project")[0]["content"] == "durable project fact"


def test_memory_search_matches_chinese_without_whitespace_tokenization(tmp_path):
    store = MemoryStore(tmp_path / "runtime.sqlite")
    stored = store.write(
        namespace="zh",
        kind="user_preference",
        fact_type="preference",
        content="使用者偏好深色介面，而且希望按鈕尺寸統一。",
        source={"type": "user_instruction"},
    )

    matches = store.search(namespace="zh", query="我之前說的深色介面偏好是什麼？")

    assert matches and matches[0]["memory_id"] == stored["memory_id"]


def test_memory_fts_ranks_same_session_and_replaces_working_summary(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = MemoryStore(path)
    manager = MemoryManager(store, project_root=tmp_path)
    sessions = AgentSessionStore(path)
    sessions.create(session_id="AS-other", namespace="test", title="other")
    sessions.create(session_id="AS-current", namespace="test", title="current")
    runs = AgentRunStore(path)
    for run_id, session_id in (
        ("AR-other", "AS-other"),
        ("AR-current", "AS-current"),
        ("AR-one", "AS-current"),
        ("AR-two", "AS-current"),
    ):
        runs.create_run(
            run_id,
            {
                "objective": run_id,
                "symbols": [],
                "driver_id": "codex",
                "autonomy": "advisory",
                "max_steps": 1,
                "session_id": session_id,
            },
        )
    other = store.write(
        namespace=manager.namespace,
        session_id="AS-other",
        run_id="AR-other",
        kind="episodic",
        fact_type="fact",
        content="專案按鈕尺寸需要統一並保留深色介面。",
        source={"type": "agent_run"},
    )
    same = store.write(
        namespace=manager.namespace,
        session_id="AS-current",
        run_id="AR-current",
        kind="episodic",
        fact_type="fact",
        content="專案按鈕尺寸需要統一並保留深色介面。",
        source={"type": "agent_run"},
    )
    ranked = manager.retrieve("按鈕尺寸與深色介面", session_id="AS-current")
    assert ranked[0]["memory_id"] == same["memory_id"]
    assert other["memory_id"] in {item["memory_id"] for item in ranked}

    first = manager.remember_run(
        session_id="AS-current",
        run_id="AR-one",
        objective="第一個任務",
        summary="第一個結果",
        evidence={"verified": True},
    )
    second = manager.remember_run(
        session_id="AS-current",
        run_id="AR-two",
        objective="第二個任務",
        summary="第二個結果",
        evidence={"verified": True},
    )
    working = store.search(
        namespace=manager.namespace,
        query="第二個任務",
        kinds=["working"],
        session_id="AS-current",
    )
    assert len(working) == 1
    assert working[0]["memory_id"] == second["working_memory_id"]
    assert working[0]["memory_id"] != first["working_memory_id"]


def test_worker_supervisor_health_crash_recovery_and_cancellation(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.sqlite"
        store = _run_store(path)
        supervisor = WorkerSupervisor(path, socket_path=tmp_path / "agent.sock")
        await supervisor.start()
        if supervisor._server is None:
            health = {"socket_unavailable": True}
        else:
            reader, writer = await asyncio.open_unix_connection(str(supervisor.socket_path))
            writer.write(b'{"op":"health"}\n')
            await writer.drain()
            health = json.loads((await reader.readline()).decode())
            writer.close()
            await writer.wait_closed()

        async def slow(_):
            await asyncio.sleep(30)
            return {"ok": True}

        task = asyncio.create_task(
            supervisor.execute(
                run_id="AR-v2",
                step_id="slow",
                worker_type="project",
                timeout_seconds=60,
                payload={},
                handler=slow,
            )
        )
        await asyncio.sleep(0)
        cancelled = await supervisor.cancel_run("AR-v2")
        with pytest.raises(asyncio.CancelledError):
            await task
        await supervisor.close()
        return health, cancelled, store

    health, cancelled, _ = asyncio.run(exercise())
    assert health.get("ok") is True or health["socket_unavailable"] is True
    assert cancelled == 1
    assert profile_for("terminal").isolation == "worker_process_and_sandbox_process_group"
    assert profile_for("browser").isolation == "worker_process_with_isolated_playwright_profile"


def test_worker_supervisor_records_http_capability_rejection_as_failed_not_crashed(tmp_path):
    class QuoteRejected(Exception):
        status_code = 422
        detail = "A last trade or exchange close is required"

    async def exercise():
        path = tmp_path / "runtime.sqlite"
        _run_store(path)
        supervisor = WorkerSupervisor(path, socket_path=tmp_path / "agent.sock")

        async def rejected(_):
            raise QuoteRejected()

        with pytest.raises(WorkerToolError, match="QuoteRejected 422"):
            await supervisor.execute(
                run_id="AR-v2",
                step_id="quote",
                worker_type="in_process",
                timeout_seconds=30,
                payload={},
                handler=rejected,
            )
        with sqlite3.connect(path) as conn:
            return conn.execute(
                "select status, error_json from agent_workers where run_id=?",
                ("AR-v2",),
            ).fetchone()

    row = asyncio.run(exercise())
    assert row is not None
    assert row[0] == "failed"
    assert "QuoteRejected 422" in row[1]


def test_execution_quote_gap_is_a_single_retryable_market_condition():
    classified = classify_error(
        WorkerToolError("HTTPException 422: A last trade or exchange close is required")
    )
    recovery = RecoveryEngine().decide(
        classified,
        attempt=1,
        max_attempts=2,
        mutation_started=False,
        rollback_available=False,
    )
    exhausted = RecoveryEngine().decide(
        classified,
        attempt=2,
        max_attempts=2,
        mutation_started=False,
        rollback_available=False,
    )

    assert classified.category == "market_quote_pending"
    assert classified.retryable is True
    assert classified.action_hints == ("retry_same_step", "choose_alternate_tool")
    assert recovery.action == "retry_same_step"
    assert exhausted.action == "revise_plan"


def test_worker_supervisor_falls_back_to_private_temp_socket_when_runtime_socket_is_denied(tmp_path, monkeypatch):
    class FakeServer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    attempted: list[Path] = []

    async def exercise() -> WorkerSupervisor:
        path = tmp_path / "runtime.sqlite"
        _run_store(path)
        supervisor = WorkerSupervisor(path, socket_path=Path("/tmp/stock-ai-test-agent.sock"))

        async def start_socket_server() -> None:
            attempted.append(supervisor.socket_path)
            if len(attempted) == 1:
                raise PermissionError("managed session denies project Unix sockets")
            supervisor._server = FakeServer()

        monkeypatch.setattr(supervisor, "_start_socket_server", start_socket_server)
        await supervisor.start()
        return supervisor

    supervisor = asyncio.run(exercise())
    assert len(attempted) == 2
    assert attempted[0] == Path("/tmp/stock-ai-test-agent.sock").resolve()
    assert attempted[1].parent.parent == Path("/tmp")
    assert supervisor.socket_path == attempted[1]


def test_project_worker_executes_in_a_real_child_process(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.sqlite"
        _run_store(path)
        (tmp_path / "proof.txt").write_text("worker evidence\n", encoding="utf-8")
        supervisor = WorkerSupervisor(
            path,
            socket_path=tmp_path / "agent.sock",
            project_root=tmp_path,
        )
        await supervisor.start()

        async def must_not_run_in_host(_):
            raise AssertionError("isolated project worker unexpectedly used the host handler")

        worker_id, result = await supervisor.execute(
            run_id="AR-v2",
            step_id="read",
            worker_type="project",
            timeout_seconds=30,
            payload={
                "tool": "project.read_file",
                "arguments": {"path": "proof.txt"},
                "audit_arguments": {"path": "proof.txt"},
                "context": {
                    "run_id": "AR-v2",
                    "session_id": "AS-v2",
                    "autonomy": "advisory",
                    "symbols": [],
                },
            },
            handler=must_not_run_in_host,
        )
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "select status, pid from agent_workers where worker_id=?",
                (worker_id,),
            ).fetchone()
        child_pids = [
            channel.process.pid
            for channel in supervisor._processes.values()
        ]
        await supervisor.close()
        return result, row, child_pids

    result, row, child_pids = asyncio.run(exercise())
    assert "worker evidence" in result["content"]
    assert row and row[0] == "completed"
    assert row[1] in child_pids
    assert row[1] != os.getpid()


def test_scheduler_planner_and_condition_watcher_validate_durable_triggers():
    planner = SchedulePlanner()
    cron = planner.prepare(
        {"trigger_type": "cron", "cron_expression": "*/15 * * * *"},
        now=datetime(2026, 7, 18, 10, 1, tzinfo=timezone.utc),
    )
    assert cron["next_run_at"].startswith("2026-07-18T10:15:00")
    assert condition_matches({"quote": {"price": 101}}, {"path": "quote.price", "greater_than": 100})
    with pytest.raises(ValueError, match="misfire_policy"):
        planner.prepare({"trigger_type": "event", "event_type": "quote", "misfire_policy": "repeat"})


def test_scheduler_planner_skips_taiwan_holiday_and_accepts_misfire_policies():
    planner = SchedulePlanner(default_taiwan_market_calendar())
    next_run = planner.prepare(
        {
            "trigger_type": "cron",
            "cron_expression": "0 1 * * *",
            "market_calendar": "taiwan",
            "misfire_policy": "skip",
        },
        now=datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc),
    )
    # 01:00 UTC is 09:00 Asia/Taipei; 2026-09-25 is the checked-in TWSE
    # Mid-Autumn and Teacher's Day closures, so the next candidate is
    # Tuesday 2026-09-29.
    assert next_run["market_calendar"] == "taiwan"
    assert next_run["misfire_policy"] == "skip"
    assert next_run["next_run_at"].startswith("2026-09-29T01:00:00")
    assert planner.prepare(
        {
            "trigger_type": "interval",
            "interval_seconds": 60,
            "next_run_at": "2026-09-24T01:00:00+00:00",
            "market_calendar": "twse",
            "misfire_policy": "catch_up",
        }
    )["misfire_policy"] == "catch_up"


def test_scheduler_claim_is_atomic_across_runtime_processes(tmp_path):
    path = tmp_path / "runtime.sqlite"
    first = AgentRunStore(path)
    second = AgentRunStore(path)
    now = datetime.now(timezone.utc)
    schedule = first.create_schedule(
        "ASCHED-lease",
        {
            "name": "lease",
            "objective": "inspect",
            "trigger_type": "one_shot",
            "next_run_at": (now - timedelta(seconds=1)).isoformat(),
            "misfire_policy": "run_once",
            "enabled": True,
        },
    )
    lease_until = (now + timedelta(seconds=30)).isoformat()
    claimed = first.claim_due_schedules(
        now.isoformat(),
        owner="runtime-one",
        lease_expires_at=lease_until,
    )
    assert [item["schedule_id"] for item in claimed] == [schedule["schedule_id"]]
    assert second.claim_due_schedules(
        now.isoformat(),
        owner="runtime-two",
        lease_expires_at=lease_until,
    ) == []
    assert second.claim_event_schedule(
        schedule["schedule_id"],
        owner="runtime-two",
        at=now.isoformat(),
        lease_expires_at=lease_until,
    ) is False
    first.release_schedule_claim(schedule["schedule_id"], owner="runtime-one")
    assert second.claim_due_schedules(
        now.isoformat(),
        owner="runtime-two",
        lease_expires_at=lease_until,
    )


def test_project_write_returns_diff_and_exact_run_scoped_rollback(tmp_path):
    provider = GeneralAgentToolProvider(tmp_path)
    context = AgentRunContext(
        run_id="AR-project",
        session_id="AS-project",
        autonomy="project_execute",
        symbols=(),
        allow_project_actions=True,
    )
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    written = asyncio.run(
        provider.execute(
            "project.write_file",
            {
                "path": "note.txt",
                "content": "after\n",
                "expected_before_sha256": hashlib.sha256(b"before\n").hexdigest(),
            },
            context,
        )
    )
    assert written["before_sha256"] != written["after_sha256"]
    assert "-before" in written["diff"] and "+after" in written["diff"]
    rolled_back = asyncio.run(
        provider.execute(
            "project.rollback_change",
            {"rollback_token": written["rollback_token"]},
            context,
        )
    )
    assert rolled_back["confirmed"] is True
    assert target.read_text(encoding="utf-8") == "before\n"


def test_project_move_and_delete_have_exact_multi_path_rollback(tmp_path):
    provider = GeneralAgentToolProvider(tmp_path)
    context = AgentRunContext(
        run_id="AR-files",
        session_id="AS-files",
        autonomy="project_execute",
        symbols=(),
        allow_project_actions=True,
    )
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source\n", encoding="utf-8")
    destination.write_text("destination\n", encoding="utf-8")

    moved = asyncio.run(
        provider.execute(
            "project.move_file",
            {
                "source": "source.txt",
                "destination": "destination.txt",
                "expected_source_sha256": hashlib.sha256(b"source\n").hexdigest(),
                "expected_destination_sha256": hashlib.sha256(b"destination\n").hexdigest(),
            },
            context,
        )
    )
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "source\n"
    asyncio.run(
        provider.execute(
            "project.rollback_change",
            {"rollback_token": moved["rollback_token"]},
            context,
        )
    )
    assert source.read_text(encoding="utf-8") == "source\n"
    assert destination.read_text(encoding="utf-8") == "destination\n"

    deleted = asyncio.run(
        provider.execute(
            "project.delete_file",
            {
                "path": "source.txt",
                "expected_before_sha256": hashlib.sha256(b"source\n").hexdigest(),
            },
            context,
        )
    )
    assert not source.exists()
    asyncio.run(
        provider.execute(
            "project.rollback_change",
            {"rollback_token": deleted["rollback_token"]},
            context,
        )
    )
    assert source.read_text(encoding="utf-8") == "source\n"


def test_rollback_manager_restores_host_issued_project_token(tmp_path):
    provider = GeneralAgentToolProvider(tmp_path)
    context = AgentRunContext(
        run_id="AR-rollback",
        session_id="AS-rollback",
        autonomy="project_execute",
        symbols=(),
        allow_project_actions=True,
    )
    target = tmp_path / "state.txt"
    target.write_text("before\n", encoding="utf-8")
    result = asyncio.run(
        provider.execute(
            "project.write_file",
            {
                "path": "state.txt",
                "content": "after\n",
                "expected_before_sha256": hashlib.sha256(b"before\n").hexdigest(),
            },
            context,
        )
    )
    restored = asyncio.run(
        RollbackManager().rollback_if_available(
            tool_name="project.write_file",
            result=result,
            context=context,
            execute=provider.execute,
        )
    )
    assert restored and restored["confirmed"] is True
    assert target.read_text(encoding="utf-8") == "before\n"


def test_runtime_capabilities_cover_memory_workflows_artifacts_and_subagents():
    manifest = AgentRuntimeToolProvider().manifest()
    names = {item["name"] for item in manifest}
    assert {
        "agent.run_subtasks",
        "memory.search",
        "memory.write",
        "memory.update",
        "memory.archive",
        "workflow.list",
        "workflow.save_current",
        "workflow.run",
        "artifact.create_text",
        "artifact.list",
        "artifact.create_structured",
        "artifact.patch_structured",
    } <= names
    subtasks = next(item for item in manifest if item["name"] == "agent.run_subtasks")
    assert subtasks["execution_backend"] == "agent_runtime"
    assert subtasks["timeout_seconds"] == 1800


def test_recursive_agent_worker_honours_long_running_timeout_contract():
    from open_stock_ai.agent_runtime.workers.base import profile_for

    profile = profile_for("agent_runtime")

    assert profile.isolation == "supervised_recursive_agent_tasks"
    assert profile.timeout_for(1800) == 1800


def test_policy_is_action_risk_based_and_real_brokerage_is_always_denied():
    context = AgentRunContext(run_id="AR", session_id="AS", autonomy="advisory", symbols=())
    engine = PolicyEngine()
    read = engine.evaluate(tool={"name": "market.quote", "risk_class": "read_only"}, arguments={}, context=context)
    mutation = engine.evaluate(
        tool={"name": "project.write_file", "risk_class": "local_reversible", "mutating": True},
        arguments={"path": "README.md"},
        context=context,
    )
    live = engine.evaluate(
        tool={"name": "live.submit_order", "risk_class": "financial_real_action", "mutating": True},
        arguments={},
        context=context,
    )
    broker_submit = engine.evaluate(
        tool={"name": "broker.order.submit", "risk_class": "read_only", "mutating": False},
        arguments={},
        context=context,
    )
    assert read.action == "allow"
    assert mutation.action == "require_approval"
    assert live.action == "deny"
    assert broker_submit.action == "deny"


def test_policy_allows_explicit_local_paper_order_only_in_paper_execution_mode():
    engine = PolicyEngine()
    context = AgentRunContext(
        run_id="AR-paper",
        session_id="AS-paper",
        autonomy="paper_execute",
        symbols=("3105.TWO",),
        allow_paper_orders=True,
    )
    context.state["explicit_paper_order_authorized"] = True

    decision = engine.evaluate(
        tool={
            "name": "paper.submit_order",
            "risk_class": "financial_paper",
            "mutating": True,
            "requires_paper_execution": True,
        },
        arguments={"symbol": "3105.TWO", "side": "buy", "quantity_lots": 1},
        context=context,
    )

    assert decision.action == "allow"
    assert decision.required_approval is False


def test_pending_verified_paper_order_reuses_only_the_exact_preview_arguments():
    trace = [
        {
            "ok": True,
            "tool": "paper.preview_order",
            "arguments": {"symbol": "3105.TWO", "side": "buy", "quantity_lots": 1},
            "result": {"can_submit": True},
        }
    ]

    assert _pending_verified_paper_order(trace) == {
        "symbol": "3105.TWO",
        "side": "buy",
        "quantity_lots": 1,
    }
    trace.append({"ok": True, "tool": "paper.submit_order", "arguments": {}, "result": {}})
    assert _pending_verified_paper_order(trace) is None


def test_workflow_runtime_clones_a_fresh_editable_plan():
    plan = PlanGraph.create("original")
    plan.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {"node_id": "finish", "node_type": "finalize", "title": "finish"},
                }
            ]
        }
    )
    plan.mark("finish", "completed")
    cloned, compiled = WorkflowRuntime([]).instantiate({"plan": plan.to_dict()}, objective="new objective")
    assert cloned.plan_id != plan.plan_id
    assert cloned.objective == "new objective"
    assert cloned.nodes["finish"].status == "pending"
    assert compiled["valid"] is True


def test_saved_workflow_runs_as_a_fresh_durable_plan(tmp_path):
    class Service:
        class Tools:
            @staticmethod
            def manifest():
                return []

        tools = Tools()

        def __init__(self):
            self.calls = []

        async def run(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": kwargs["run_id"],
                "status": "completed",
                "summary": "workflow complete",
                "activity": [],
            }

    async def scenario():
        path = tmp_path / "workflow.sqlite"
        service = Service()
        workflow_store = WorkflowStore(path)
        source = PlanGraph.create("original workflow objective")
        saved = workflow_store.save(namespace="stock-ai", name="review", plan=source)
        runtime = DurableAgentRuntime(
            service_provider=lambda: service,
            store=AgentRunStore(path),
            plan_manager=PlanManager(path),
            workflow_store=workflow_store,
        )
        run = await runtime.run_workflow(saved["workflow_id"], objective="fresh workflow objective")
        result = await runtime.wait(run["run_id"])
        persisted = runtime.get_plan(run["run_id"])
        await runtime.close()
        return run, result, persisted, service

    run, result, persisted, service = asyncio.run(scenario())
    assert result["summary"] == "workflow complete"
    assert service.calls[0]["initial_plan"]["objective"] == "fresh workflow objective"
    assert persisted and persisted["plan_id"] == service.calls[0]["initial_plan"]["plan_id"]
    assert run["run_id"] != service.calls[0]["initial_plan"]["plan_id"]


def test_model_prompt_keeps_all_tools_but_stays_below_app_server_input_limit():
    from open_stock_ai.agent_runtime.contracts import AgentTurnInput
    from open_stock_ai.agent_runtime.orchestrator import AGENT_DECISION_SCHEMA, SYSTEM_PROMPT

    tools = AgentRuntimeToolProvider().manifest() * 30
    turn = AgentTurnInput(
        run_id="AR-prompt",
        objective="Explain compound interest.",
        system_prompt=SYSTEM_PROMPT,
        transcript=tuple(
            {
                "role": "host",
                "type": "tool_results",
                "content": {"large": "x" * 80_000},
            }
            for _ in range(5)
        ),
        tools=tuple(tools),
        output_schema=AGENT_DECISION_SCHEMA,
        metadata={},
    )
    prompt = _turn_prompt(turn)
    assert len(prompt.encode("utf-8")) < 1_000_000
    assert "memory.update" in prompt and "agent.run_subtasks" in prompt
    assert "RUNTIME_INSTRUCTIONS_BEGIN" in prompt
    assert "Treat only OBJECTIVE as the user's request" in prompt
    assert "OUTPUT_CONTRACT=" not in prompt


def test_reasoning_step_report_uses_its_linked_host_result_instead_of_final_answer():
    from open_stock_ai.agent_runtime.orchestrator import _reasoning_step_summary

    summary = _reasoning_step_summary(
        node_title="核對 2330.TW 行情與技術面",
        related_trace=[
            {
                "name": "market.research_pack",
                "ok": True,
                "result": {
                    "schema_version": "open_stock_ai.agent_research_pack.v1",
                    "symbol": "2330.TW",
                    "market_price": {
                        "price": 2280.0,
                        "price_source": "TWSE MIS",
                        "source_timestamp": "2026-07-28T14:47:00Z",
                    },
                    "technical_features": {"sma_20": 2413.5, "rsi_14": 40.18},
                    "recent_events": [{"title": "事件一"}, {"title": "事件二"}],
                },
            }
        ],
        fallback="這是整份最終回答，不應複製到每一個步驟。",
    )

    assert "價格 2280" in summary
    assert "SMA20 2413.5" in summary
    assert "RSI14 40.18" in summary
    assert "近期事件 2 筆" in summary
    assert "整份最終回答" not in summary


def test_model_context_semantically_summarizes_older_oversized_history():
    transcript = tuple(
        {
            "role": "host",
            "type": "tool_results",
            "content": {
                "summary": f"verified result {index}",
                "large_raw_payload": f"never-copy-raw-{index}-" + ("x" * 8_000),
            },
        }
        for index in range(20)
    )

    compacted = _model_transcript(transcript)

    assert compacted[0]["type"] == "compacted_history_summary"
    assert compacted[0]["content"]["semantic_compaction"] is True
    assert "never-copy-raw-0" not in json.dumps(compacted, ensure_ascii=False)
    assert len(json.dumps(compacted, ensure_ascii=False)) < 70_000


def test_model_context_preserves_verified_market_evidence_when_tool_result_is_compacted():
    events = [
        {
            "event_time": f"2026-07-{index + 1:02d}T10:00:00+08:00",
            "event_type": "news",
            "title": f"台積電事件 {index + 1}",
            "summary": "重要新聞摘要 " + ("內容" * 1_500),
            "sentiment": "neutral",
            "estimated_impact_direction": "mixed",
            "source_url": f"https://example.test/news/{index + 1}",
        }
        for index in range(20)
    ]
    transcript = (
        {
            "role": "host",
            "type": "tool_results",
            "content": [
                {
                    "id": "research-2330",
                    "name": "market.research_pack",
                    "ok": True,
                    "result": {
                        "schema_version": "open_stock_ai.agent_research_pack.v1",
                        "symbol": "2330.TW",
                        "generated_at": "2026-07-28T22:04:39+08:00",
                        "market_price": {
                            "price": 2280.0,
                            "price_source": "TWSE MIS",
                            "source_timestamp": "2026-07-28T22:04:39+08:00",
                            "is_realtime": True,
                        },
                        "technical_features": {"rsi_14": 40.18, "sma_20": 2413.5},
                        "recent_events": events,
                        "pipeline_workspace": {
                            "schema_version": "open_stock_ai.agent_workspace.v1",
                            "symbol": "2330.TW",
                            "recommendation_bucket": "watch",
                            "risk_summary": {"approved": False, "position_size_pct": 0},
                        },
                    },
                    "validation": {
                        "passed": True,
                        "validator": "default",
                        "evidence_hash": "EV-verified-2330",
                    },
                }
            ],
        },
    )

    compacted = _model_transcript(transcript)
    encoded = json.dumps(compacted, ensure_ascii=False)
    observation = compacted[0]["content"]["observations"][0]

    assert compacted[0]["content"]["semantic_compaction"] is True
    assert observation["name"] == "market.research_pack"
    assert observation["result"]["market_price"]["price"] == 2280.0
    assert observation["result"]["market_price"]["price_source"] == "TWSE MIS"
    assert observation["result"]["technical_features"]["rsi_14"] == 40.18
    assert observation["result"]["recent_events"][0]["title"] == "台積電事件 1"
    assert len(observation["result"]["recent_events"]) == 10
    assert observation["validation"]["evidence_hash"] == "EV-verified-2330"
    assert len(encoded) < 24_000


def test_explicit_non_stock_instruction_is_not_classified_as_market_information():
    assert _classify_task("請用一句話解釋複利，不要分析股票。") == "general_answer"


def test_git_provider_uses_scoped_argv_operations(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    provider = GitToolProvider(tmp_path)
    context = AgentRunContext(
        run_id="AR-git",
        session_id="AS-git",
        autonomy="project_execute",
        symbols=(),
        allow_project_actions=True,
    )
    status = asyncio.run(provider.execute("git.status", {}, context))
    branch = asyncio.run(provider.execute("git.create_branch", {"name": "codex/test"}, context))
    assert status["exit_code"] == 0
    assert branch["branch"] == "codex/test"
    assert branch["before_sha256"] == branch["after_sha256"]


def test_capability_registry_validates_arguments_before_provider_execution():
    calls = []
    spec = AgentToolSpec(
        name="demo.read",
        description="demo",
        category="demo",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
    )

    async def execute(name, arguments, context):
        calls.append((name, arguments, context.run_id))
        return {"ok": True}

    registry = CapabilityRegistry(
        (
            BoundCapabilityProvider(
                provider_id="demo",
                specs={spec.name: spec},
                executor=execute,
            ),
        )
    )
    context = AgentRunContext(run_id="AR", session_id="AS", autonomy="advisory", symbols=())
    with pytest.raises(ValueError, match="Invalid arguments"):
        asyncio.run(registry.execute("demo.read", {"wrong": True}, context))
    assert calls == []


def test_capability_registry_host_firewall_denies_live_brokerage_before_provider_dispatch():
    calls = []
    unsafe = AgentToolSpec(
        name="broker.order.submit",
        description="must never be exposed to an Agent",
        category="broker_order_execution",
        risk_class="financial_real_action",
        mutating=True,
        input_schema={"type": "object", "additionalProperties": False, "properties": {}},
    )

    async def execute(name, arguments, context):
        calls.append((name, arguments, context.run_id))
        return {"submitted": True}

    registry = CapabilityRegistry(
        (
            BoundCapabilityProvider(
                provider_id="unsafe_test_provider",
                specs={unsafe.name: unsafe},
                executor=execute,
            ),
        )
    )
    context = AgentRunContext(run_id="AR", session_id="AS", autonomy="full_execute", symbols=())

    with pytest.raises(PermissionError, match="permanently denied"):
        registry.manifest()
    with pytest.raises(PermissionError, match="permanently denied"):
        asyncio.run(registry.execute("broker.order.submit", {}, context))
    with pytest.raises(PermissionError, match="permanently denied"):
        asyncio.run(registry.execute("live.submit_order", {}, context))
    assert calls == []


def test_scheduler_supports_interval_cron_dedup_pause_and_resume(tmp_path):
    store = AgentRunStore(tmp_path / "runtime.sqlite")
    runtime = DurableAgentRuntime(service_provider=lambda: None, store=store)
    now = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    first = runtime.create_schedule(
        {
            "name": "interval",
            "objective": "inspect",
            "trigger_type": "interval",
            "next_run_at": now,
            "interval_seconds": 300,
            "dedup_key": "same",
            "autonomy": "advisory",
        }
    )
    duplicate = runtime.create_schedule(
        {
            "name": "duplicate",
            "objective": "inspect",
            "trigger_type": "interval",
            "next_run_at": now,
            "interval_seconds": 300,
            "dedup_key": "same",
            "autonomy": "advisory",
        }
    )
    cron = runtime.create_schedule(
        {
            "name": "cron",
            "objective": "inspect",
            "trigger_type": "cron",
            "cron_expression": "*/15 * * * *",
            "autonomy": "advisory",
        }
    )
    assert duplicate["schedule_id"] == first["schedule_id"]
    assert cron["next_run_at"] is not None
    assert runtime.disable_schedule(first["schedule_id"])["enabled"] is False
    assert runtime.resume_schedule(first["schedule_id"])["enabled"] is True


def test_runtime_memory_mutations_are_candidates_governed_by_host(tmp_path, monkeypatch):
    import stock_ai.agent_service as agent_service

    manager = MemoryManager(MemoryStore(tmp_path / "governed-memory.sqlite"), project_root=tmp_path)

    class Service:
        memory_manager = manager

    monkeypatch.setattr(agent_service, "get_agent_service", lambda: Service())
    monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: object())

    provider = AgentRuntimeToolProvider()
    context = AgentRunContext(
        run_id="AR-governed",
        session_id="AS-governed",
        autonomy="advisory",
        symbols=(),
    )
    written = asyncio.run(
        provider.execute(
            "memory.write",
            {
                "kind": "project",
                "fact_type": "fact",
                "content": "Agent Runtime 使用事件溯源保存狀態。",
            },
            context,
        )
    )

    assert written["schema_version"] == "open_stock_ai.memory_candidate_result.v1"
    assert written["candidate_id"].startswith("MCAN-")
    assert written["direct_write"] is False
    assert written["requires_host_governance"] is True
    assert manager.governance.candidates()[0]["candidate_id"] == written["candidate_id"]

    original = manager.store.write(
        namespace=manager.namespace,
        kind="project_state",
        fact_type="fact",
        content="舊的專案說明",
        source={"type": "host_verified"},
    )
    corrected = asyncio.run(
        provider.execute(
            "memory.update",
            {
                "memory_id": original["memory_id"],
                "content": "修正後的專案說明",
            },
            context,
        )
    )

    assert corrected["schema_version"] == "open_stock_ai.memory_correction_candidate.v1"
    assert corrected["candidate_id"].startswith("MCAN-")
    assert corrected["corrects_memory_id"] == original["memory_id"]
    assert corrected["direct_update"] is False
    assert corrected["requires_host_governance"] is True
    assert manager.store.get(original["memory_id"])["content"] == "舊的專案說明"


def test_runtime_subtasks_link_parallel_child_runs_to_the_parent_forest(monkeypatch):
    import stock_ai.agent_service as agent_service

    class FinalRuntime:
        def __init__(self):
            self.calls = []

        def create_subtask_branches(self, **kwargs):
            self.calls.append(kwargs)
            return [
                {"branch_id": "BR-official", "objective": "Official source"},
                {"branch_id": "BR-independent", "objective": "Independent source"},
            ]

    class Runtime:
        def __init__(self):
            self.final_runtime = FinalRuntime()
            self.created = []

        def get_run(self, _run_id):
            return {"parent_run_id": None}

        async def create_run(self, **kwargs):
            self.created.append(kwargs)
            return {"run_id": f"AR-child-{len(self.created)}"}

        async def wait(self, run_id):
            return {"run_id": run_id, "status": "completed", "summary": "verified"}

    runtime = Runtime()
    monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: runtime)
    monkeypatch.setattr(agent_service, "get_agent_service", lambda: object())
    provider = AgentRuntimeToolProvider()
    result = asyncio.run(
        provider.execute(
            "agent.run_subtasks",
            {"objectives": ["Official source", "Independent source"], "role": "researcher"},
            AgentRunContext(
                run_id="AR-parent",
                session_id="AS-parent",
                autonomy="advisory",
                symbols=("2330.TW",),
                state={
                    "current_plan_node_id": "delegate",
                    "task_kind": "market_information",
                },
            ),
        )
    )

    assert runtime.final_runtime.calls == [{
        "session_id": "AS-parent",
        "run_id": "AR-parent",
        "source_node_id": "delegate",
        "objectives": ("Official source", "Independent source"),
        "role": "researcher",
    }]
    assert [item["run_metadata"]["source_branch_id"] for item in runtime.created] == [
        "BR-official", "BR-independent",
    ]
    assert result["parallel_requested"] is True
    assert result["role"] == "researcher"
    assert result["branch_ids"] == ["BR-official", "BR-independent"]
    assert all(
        child["objective"].startswith("[MODEL_TASK_KIND:market_information]\nRole: researcher")
        for child in runtime.created
    )
    assert all(
        _classify_task(child["objective"]) == "market_information"
        for child in runtime.created
    )
    assert all("[HOST_BOUNDARY:research_only]" in child["objective"] for child in runtime.created)
    assert _classify_task("Role: researcher\nTask: market.analyze_symbol 2887.TW") == "market_decision"


def test_runtime_critic_enforces_enough_steps_for_evidence_and_synthesis(monkeypatch):
    import stock_ai.agent_service as agent_service

    class FinalRuntime:
        def create_subtask_branches(self, **_kwargs):
            return [{"branch_id": "BR-critic", "objective": "Independent critic"}]

    class Runtime:
        def __init__(self):
            self.final_runtime = FinalRuntime()
            self.created = []

        def get_run(self, _run_id):
            return {"parent_run_id": None}

        async def create_run(self, **kwargs):
            self.created.append(kwargs)
            return {"run_id": "AR-critic"}

        async def wait(self, run_id):
            return {"run_id": run_id, "status": "completed", "summary": "challenged"}

    runtime = Runtime()
    monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: runtime)
    monkeypatch.setattr(agent_service, "get_agent_service", lambda: object())
    result = asyncio.run(
        AgentRuntimeToolProvider().execute(
            "agent.run_subtasks",
            {
                "objectives": ["Independent critic"],
                "role": "critic",
                "max_steps": 4,
            },
            AgentRunContext(
                run_id="AR-parent",
                session_id="AS-parent",
                autonomy="advisory",
                symbols=("2330.TW",),
                state={"current_plan_node_id": "critic-node"},
            ),
        )
    )

    assert runtime.created[0]["max_steps"] == 6
    assert runtime.created[0]["run_metadata"]["subagent_role"] == "critic"
    assert runtime.created[0]["run_metadata"]["requested_focus"] == "Independent critic"
    assert "[MODEL_TASK_KIND:market_information]" in runtime.created[0]["objective"]
    assert "for 2330.TW" in runtime.created[0]["objective"]
    assert "Do not delegate" in runtime.created[0]["objective"]
    assert result["role"] == "critic"


def test_runtime_critic_cannot_recursively_delegate(monkeypatch):
    import stock_ai.agent_service as agent_service

    class Runtime:
        def __init__(self):
            self.final_runtime = object()

        def get_run(self, _run_id):
            return {
                "objective": "Role: critic\nTask: independently challenge the parent conclusion",
                "parent_run_id": "AR-parent",
            }

    runtime = Runtime()
    monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: runtime)
    monkeypatch.setattr(agent_service, "get_agent_service", lambda: object())

    with pytest.raises(PermissionError, match="cannot recursively delegate"):
        asyncio.run(
            AgentRuntimeToolProvider().execute(
                "agent.run_subtasks",
                {"objectives": ["Create another critic"], "role": "critic"},
                AgentRunContext(
                    run_id="AR-critic-child",
                    session_id="AS-parent",
                    autonomy="advisory",
                    symbols=("2330.TW",),
                ),
            )
        )


def test_critic_capability_budget_hides_recursive_delegate_and_closes_after_evidence():
    manifest = [
        {"name": "market.analyze_symbol"},
        {"name": "web.research"},
        {"name": "agent.run_subtasks"},
    ]

    scoped = _critic_disclosed_tools(manifest, successful_observations=0)
    assert [item["name"] for item in scoped] == [
        "market.analyze_symbol",
        "web.research",
    ]
    assert _critic_disclosed_tools(manifest, successful_observations=3) == []


def test_parent_detects_completed_critic_and_can_close_delegate_capability():
    assert _completed_critic_observation(
        [
            {
                "tool": "agent.run_subtasks",
                "ok": True,
                "result": {
                    "role": "critic",
                    "items": [{"run_id": "AR-child", "result": {"status": "completed"}}],
                },
            }
        ]
    ) is True
    assert _completed_critic_observation(
        [{"tool": "agent.run_subtasks", "ok": False, "result": {"role": "critic"}}]
    ) is False

    trace = [
        {
            "tool": "agent.run_subtasks",
            "ok": True,
            "result": {
                "role": "critic",
                "items": [{"run_id": "AR-child", "result": {"status": "completed"}}],
            },
        }
    ]
    allowed, blocked = _partition_redundant_critic_calls(
        [
            {"id": "repeat", "name": "agent.run_subtasks", "arguments": {"symbol": "2330.TW"}},
            {"id": "market", "name": "market.analyze_symbol", "arguments": {"symbol": "2330.TW"}},
        ],
        trace=trace,
    )
    assert [item["id"] for item in allowed] == ["market"]
    assert [item["id"] for item in blocked] == ["repeat"]


def test_cancelled_subtask_worker_cancels_every_created_child_run(monkeypatch):
    import stock_ai.agent_service as agent_service

    class FinalRuntime:
        def create_subtask_branches(self, **_kwargs):
            return [{"branch_id": "BR-child", "objective": "Independent critic"}]

    class Runtime:
        def __init__(self):
            self.final_runtime = FinalRuntime()
            self.created = asyncio.Event()
            self.cancelled = []

        def get_run(self, _run_id):
            return {"parent_run_id": None}

        async def create_run(self, **_kwargs):
            self.created.set()
            return {"run_id": "AR-child"}

        async def wait(self, _run_id):
            await asyncio.Event().wait()

        async def cancel(self, run_id):
            self.cancelled.append(run_id)
            return {"run_id": run_id, "status": "cancelled"}

    async def scenario():
        runtime = Runtime()
        monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: runtime)
        monkeypatch.setattr(agent_service, "get_agent_service", lambda: object())
        task = asyncio.create_task(
            AgentRuntimeToolProvider().execute(
                "agent.run_subtasks",
                {"objectives": ["Independent critic"], "role": "critic"},
                AgentRunContext(
                    run_id="AR-parent",
                    session_id="AS-parent",
                    autonomy="advisory",
                    symbols=("2330.TW",),
                    state={"current_plan_node_id": "critic-node"},
                ),
            )
        )
        await runtime.created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return runtime

    runtime = asyncio.run(scenario())

    assert runtime.cancelled == ["AR-child"]
