from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization
from open_stock_ai.agent_runtime.context_broker import ContextBroker
from open_stock_ai.agent_runtime.context_broker_v2 import ContextBrokerV2
from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentTurnInput
from open_stock_ai.agent_runtime.environment_snapshot import EnvironmentSnapshotBuilder
from open_stock_ai.agent_runtime.orchestrator import AgentOrchestrator, _call_node_id, _normalize_turn
from open_stock_ai.agent_runtime.plan_graph import PlanGraph, PlanNode
from open_stock_ai.agent_runtime.policy_engine import PolicyEngine
from open_stock_ai.agent_runtime.repair.recovery_policy import (
    _host_recovery_calls, _prior_repeated_tool_failure, _recovery_links_for_call,
    _recovery_tool_surface, _retired_recovery_tool_names, _unresolved_failure_nodes,
)
from stock_ai.agent_drivers import _turn_prompt
from stock_ai.agent_tools import StockAgentToolRegistry


OBJECTIVE = "[MODEL_TASK_KIND:market_decision]\n[MARKET_SCOPE]\n請啟動台灣全市場自主紙上交易流水線，在隔離帳戶研究、保存計畫、啟用執行及管理持倉。"
AUTONOMY = {"autonomy.status", "autonomy.coverage", "autonomy.research", "autonomy.evidence", "autonomy.propose_plan",
            "autonomy.activate", "autonomy.manage", "autonomy.close_plan"}
RESEARCH_CALL = {"id": "research-original", "name": "autonomy.research", "arguments": {"deep_limit": 2}}


def failure():
    return {"tool": "autonomy.research", "call_id": RESEARCH_CALL["id"], "node_id": _call_node_id(RESEARCH_CALL),
            "arguments": {"deep_limit": 2}, "ok": False,
            "error": {"category": "timeout", "retryable": True},
            "recovery": {"action": "revise_plan", "requires_model_replan": True}}


def old_trace():
    failed = failure()
    return [failed, {"tool": "web.research", "ok": True, "call_id": "web-1", "node_id": "web-node",
                     "result": {"source_count": 2}, "recovery_for": [{"failed_node_id": failed["node_id"],
                     "failed_call_id": failed["call_id"], "recovery_call_id": "web-1", "recovery_tool": "web.research"}]}]


def cycle():
    return {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "cycle_id": "cycle-retained",
            "account_id": "autonomous-paper-v1", "deep_success_count": 2, "candidates": []}


def prompt_tools(turn):
    # Exercise the same final Codex prompt adapter, not only the broker helper.
    return json.loads(_turn_prompt(turn).split("\nTOOLS=", 1)[1].split("\nTRANSCRIPT=", 1)[0])


@pytest.fixture
def registry():
    return StockAgentToolRegistry()


def test_real_registry_campaign_schemas_survive_initial_and_expanded_prompt_budget(registry):
    for step in (1, 4):
        disclosed = ContextBroker().disclose_capabilities(registry.manifest(), task_kind="market_decision",
            step=step, autonomous_campaign=True, phase="research" if step == 1 else None)
        package = ContextBrokerV2().assemble(current_objective=OBJECTIVE, current_user_message=OBJECTIVE,
            active_branch={"objective": OBJECTIVE}, tool_schemas=disclosed,
            plan={"completion_criteria": ["retained cycle and activation receipt"], "audit_summary": "x" * 16000}, turn=step)
        turn = AgentTurnInput(run_id="r", objective=OBJECTIVE, system_prompt="Host only", transcript=(),
                             tools=package.tool_schemas, output_schema={}, metadata={})
        names = {tool["name"] for tool in prompt_tools(turn)}
        required = {"autonomy.status", "autonomy.coverage", "autonomy.research", "autonomy.evidence"} if step == 1 else AUTONOMY
        assert required <= names
        assert not any(name.startswith(("paper.", "portfolio.")) for name in names)
        assert package.estimated_tokens <= 12000
        original = {tool["name"]: tool for tool in registry.manifest()}
        for tool in prompt_tools(turn):
            assert tool["input_schema"] == original[tool["name"]]["input_schema"]


def test_web_success_cannot_replace_retained_cycle_or_permanently_retire_research(registry):
    trace = old_trace()
    surface, requirements = _recovery_tool_surface(trace, registry.manifest())
    assert {item["name"] for item in surface} == {"autonomy.research", "autonomy.status", "autonomy.evidence", "system.capabilities"}
    assert requirements[0]["requires_retained_cycle"]
    assert "autonomy.research" not in _retired_recovery_tool_names(trace)
    assert _unresolved_failure_nodes(PlanGraph.create(OBJECTIVE), trace) == [failure()["node_id"]]
    assert _recovery_links_for_call(trace, {"id": "web-2", "name": "web.research", "arguments": {}},
                                    registry.manifest(), result={"source_count": 3}) == []
    assert _recovery_links_for_call(trace, RESEARCH_CALL, registry.manifest(), result={"cycle_id": "fake"}) == []
    links = _recovery_links_for_call(trace, RESEARCH_CALL, registry.manifest(), result=cycle())
    assert [link["failed_node_id"] for link in links] == [failure()["node_id"]]
    trace.append({"tool": "autonomy.research", "ok": True, "result": cycle(), "recovery_for": links})
    assert _recovery_tool_surface(trace, registry.manifest()) == ([], [])
    assert not _unresolved_failure_nodes(PlanGraph.create(OBJECTIVE), trace)
    assert "autonomy.research" not in _retired_recovery_tool_names(trace)


def test_retry_is_bounded_across_restored_trace_and_preserves_deep_limit(registry):
    trace = old_trace()
    assert _prior_repeated_tool_failure(trace, RESEARCH_CALL) is None
    surface, _ = _recovery_tool_surface(trace, registry.manifest())
    calls = _host_recovery_calls(trace=trace, recovery_surface=surface, objective=OBJECTIVE, symbols=[])
    assert [(call["name"], call["arguments"]) for call in calls] == [("autonomy.research", {"deep_limit": 2})]
    trace.append({**failure(), "node_id": "retry-failed", "call_id": "retry-failed"})
    assert _prior_repeated_tool_failure(trace, RESEARCH_CALL) is not None
    assert _host_recovery_calls(trace=trace, recovery_surface=surface, objective=OBJECTIVE, symbols=[]) == []
    lookup = {"id": "readback", "name": "autonomy.research", "arguments": {"cycle_id": "cycle-retained"}}
    assert _prior_repeated_tool_failure(trace, lookup) is None
    trace.append({**failure(), "arguments": lookup["arguments"]})
    assert _prior_repeated_tool_failure(trace, lookup) is not None


@pytest.mark.parametrize("category,retryable", [("permission", False), ("invalid_arguments", True), ("timeout", False)])
def test_nontransient_research_failure_does_not_gain_retry(category, retryable):
    failed = failure()
    failed["error"] = {"category": category, "retryable": retryable}
    assert _prior_repeated_tool_failure([failed], RESEARCH_CALL) is failed
    legacy = {**failed, "tool": "market.analyze_symbol"}
    assert _prior_repeated_tool_failure([legacy], {**RESEARCH_CALL, "name": "market.analyze_symbol"}) is legacy


def test_real_orchestrator_resumes_old_retired_checkpoint_and_executes_same_args_once(registry):
    class Tools:
        calls = []
        def manifest(self): return registry.manifest()
        async def execute(self, name, arguments, context):
            assert name == "autonomy.research"
            assert context.state["explicit_autonomous_campaign_authorized"] is True
            self.calls.append((name, arguments))
            return cycle()

    class Driver:
        inputs = []
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            self.inputs.append(turn)
            calls = [{**RESEARCH_CALL, "id": "research-retry"}] if len(self.inputs) == 1 else []
            return {"state": "continue", "summary": "先讀取保留研究 cycle，再核對隔離帳戶。", "tool_calls": calls, "decision": None}

    trace = old_trace()
    plan = PlanGraph.create(OBJECTIVE)
    # Old code marked this node completed via web recovery. Preserve it and
    # create a new Host retry node rather than mutating an immutable receipt.
    original_node = failure()["node_id"]
    plan.nodes[original_node] = PlanNode(node_id=original_node, node_type="tool", title="original research",
        status="completed", tool_name="autonomy.research", arguments={"deep_limit": 2})
    plan.nodes["legacy-completed"] = PlanNode(node_id="legacy-completed", node_type="tool", title="old account read",
        status="completed", tool_name="portfolio.snapshot", arguments={})
    checkpoint = {"plan": plan.to_dict(), "trace": trace,
        "context_state": {"task_kind": "market_decision", "current_step": 1,
            "explicit_autonomous_campaign_authorized": True, "retired_recovery_tools": "autonomy.research"},
        "transcript": [{"role": "host", "type": "retired_recovery_tools", "content": {
            "tools": ["autonomy.research"], "instruction": "Do not request these tools again."}}]}
    before = deepcopy(checkpoint)
    driver, tools = Driver(), Tools()
    events = []
    result = asyncio.run(AgentOrchestrator(drivers={"scripted": driver}, tools=tools, default_driver="scripted").run(
        objective=OBJECTIVE, autonomy="paper_execute", symbols=[], max_steps=2,
        resume_state={"checkpoint": {"payload": checkpoint}}, event_sink=events.append))
    assert tools.calls == [("autonomy.research", {"deep_limit": 2})]
    assert "autonomy.research" in {t["name"] for t in prompt_tools(driver.inputs[0])}
    assert AUTONOMY <= {t["name"] for t in prompt_tools(driver.inputs[1])}
    assert "Do not request these tools again." not in _turn_prompt(driver.inputs[0])
    assert not [event for event in events if event["type"] == "tool.failed"]
    assert result["plan"]["nodes"][0]["status"] == "completed"
    assert len([event for event in events if event["type"] == "recovery.linked"]) == 1
    assert before["trace"] == checkpoint["trace"]


def test_model_cannot_supply_the_host_retry_node_identity():
    normalized = _normalize_turn({"state": "continue", "summary": "retry", "tool_calls": [
        {**RESEARCH_CALL, "_host_research_retry": 1}], "decision": None})
    assert "_host_research_retry" not in normalized["tool_calls"][0]
    assert _call_node_id(normalized["tool_calls"][0]) == _call_node_id(RESEARCH_CALL)


def test_campaign_account_is_not_legacy_snapshot_and_legacy_mutation_is_denied(registry, tmp_path):
    context = AgentRunContext(run_id="r", autonomy="paper_execute", symbols=(), allow_paper_orders=True,
                              state={"task_kind": "market_decision", "explicit_paper_order_authorized": True})
    bind_campaign_authorization(context, objective=OBJECTIVE, task_kind="market_decision")
    builder = EnvironmentSnapshotBuilder(project_root=tmp_path, capability_manifest=registry.manifest,
        ui_snapshot=lambda: {}, account_snapshot=lambda: pytest.fail("Must not read the legacy account"))
    assert builder.build(context).state["account"]["source_tool"] == "autonomy.status"
    legacy = [t for t in registry.manifest() if t["name"] in {"paper.submit_order", "paper.mark_to_market"}]
    for tool in legacy:
        assert PolicyEngine().evaluate(tool=tool, arguments={}, context=context).action == "deny"
    bind_campaign_authorization(context, objective="紙上買進2330.TW一股", task_kind="market_decision")
    tool = next(t for t in legacy if t["name"] == "paper.submit_order")
    assert PolicyEngine().evaluate(tool=tool, arguments={}, context=context).action == "allow"
