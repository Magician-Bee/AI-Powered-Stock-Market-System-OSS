"""Offline recovery of one proven, pre-service activation scope rejection."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.orchestrator import AgentOrchestrator, _call_node_id, _normalize_turn
from open_stock_ai.agent_runtime.plan_graph import PlanGraph, PlanNode
from open_stock_ai.agent_runtime.repair.recovery_policy import (
    _host_recovery_calls, _prepare_recovery_retry_calls, _prior_repeated_tool_failure,
    _recovery_links_for_call, _recovery_tool_surface, _unresolved_failure_nodes,
)
from open_stock_ai.agent_runtime.workers.base import WorkerToolError
from stock_ai.agent_drivers import _turn_prompt
from stock_ai.agent_tools import StockAgentToolRegistry
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider


OBJECTIVE = "[MODEL_TASK_KIND:market_decision]\n[MARKET_SCOPE]\n請啟動全市場自主紙上交易流水線，使用隔離帳戶。"
CALL = {"id": "activate-original", "name": "autonomy.activate",
        "arguments": {"cycle_id": "AC-" + "1" * 64, "use_candidate_plans": False}}
ERROR = "instrument_mandate_cannot_activate_whole_account_campaign: persistent activation and future model reviews require a whole-market mandate"


def context():
    result = AgentRunContext(run_id="r", session_id="s", symbols=(), autonomy="paper_execute", allow_paper_orders=True)
    bind_campaign_authorization(result, objective=OBJECTIVE, task_kind="market_decision")
    return result


def failure():
    return {"tool": "autonomy.activate", "arguments": deepcopy(CALL["arguments"]), "call_id": CALL["id"],
            "node_id": _call_node_id(CALL), "ok": False, "result": None,
            "error": {"category": "execution_failure", "exception_type": "WorkerToolError", "message": ERROR, "retryable": False},
            "recovery": {"action": "revise_plan", "requires_model_replan": True}}


def receipt():
    return {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": "autonomy.activate",
            "account_id": "autonomous-paper-v1", "cycle_id": CALL["arguments"]["cycle_id"],
            "campaign_receipt_id": "AE-" + "2" * 64, "plans": [], "skipped": [],
            "management": {"account_id": "autonomous-paper-v1", "enabled": True, "results": [], "errors": []}}


def test_scope_rejection_has_one_exact_context_bound_retry_and_requires_real_receipt():
    trace, ctx = [failure()], context()
    manifest = StockAgentToolRegistry().manifest()
    surface, requirements = _recovery_tool_surface(trace, manifest, context=ctx)
    assert {t["name"] for t in surface} == {"autonomy.activate", "autonomy.status", "system.capabilities"}
    assert requirements[0]["scope_guard_retry_available"] is True
    assert _host_recovery_calls(trace=trace, recovery_surface=surface, objective=OBJECTIVE, symbols=[]) == []
    assert _prior_repeated_tool_failure(trace, CALL, context=ctx) is None
    retried = _prepare_recovery_retry_calls([CALL], trace, context=ctx)[0]
    assert _call_node_id(retried) != _call_node_id(CALL)
    assert _prepare_recovery_retry_calls([CALL], trace, context=ctx)[0] == retried
    for invalid in ({}, {**receipt(), "campaign_receipt_id": None}, {**receipt(), "cycle_id": "wrong"},
                    {**receipt(), "management": {**receipt()["management"], "errors": ["uncertain"]}}):
        assert _recovery_links_for_call(trace, retried, manifest, context=ctx, result=invalid) == []
    assert _recovery_links_for_call(trace, {"id": "status", "name": "autonomy.status", "arguments": {}},
                                    manifest, context=ctx, result=receipt()) == []
    links = _recovery_links_for_call(trace, retried, manifest, context=ctx, result=receipt())
    assert len(links) == 1 and links[0]["failed_node_id"] == failure()["node_id"]
    trace.append({"tool": "autonomy.activate", "arguments": deepcopy(CALL["arguments"]),
                  "ok": True, "result": receipt(), "recovery_for": links})
    assert _recovery_tool_surface(trace, manifest, context=ctx) == ([], [])
    assert _unresolved_failure_nodes(PlanGraph.create(OBJECTIVE), trace) == []


@pytest.mark.parametrize("reason", ["scoped", "unauthorized", "advisory", "paper_disabled", "missing_context",
                                   "second_failure", "uncertain", "other_error", "changed_arguments", "result_present"])
def test_scope_retry_never_admits_scoped_unauthorized_uncertain_or_repeated_mutations(reason):
    trace, ctx, call = [failure()], context(), deepcopy(CALL)
    if reason == "scoped": ctx = replace(ctx, symbols=("2330.TW",))
    elif reason == "unauthorized": ctx.state["explicit_autonomous_campaign_authorized"] = False
    elif reason == "advisory": ctx = replace(ctx, autonomy="advisory")
    elif reason == "paper_disabled": ctx = replace(ctx, allow_paper_orders=False)
    elif reason == "missing_context": ctx = None
    elif reason == "second_failure": trace.append({**failure(), "node_id": "second"})
    elif reason == "uncertain": trace[0]["error"].update(category="timeout", retryable=True)
    elif reason == "other_error": trace[0]["error"]["message"] = "activation receipt unavailable"
    elif reason == "changed_arguments": call["arguments"]["use_candidate_plans"] = True
    elif reason == "result_present": trace[0]["result"] = receipt()
    assert _prior_repeated_tool_failure(trace, call, context=ctx) is not None
    assert _prepare_recovery_retry_calls([call], trace, context=ctx) == [call]
    if reason != "changed_arguments":
        surface, _ = _recovery_tool_surface(trace, StockAgentToolRegistry().manifest(), context=ctx)
        assert "autonomy.activate" not in {t["name"] for t in surface}


def test_model_cannot_supply_activation_retry_identity():
    normalized = _normalize_turn({"state": "continue", "tool_calls": [{**CALL, "_host_activation_scope_retry": 1}]})
    assert "_host_activation_scope_retry" not in normalized["tool_calls"][0]
    assert _call_node_id(normalized["tool_calls"][0]) == _call_node_id(CALL)


@pytest.mark.parametrize("restore_wrong_scope_checkpoint", [False, True])
def test_real_registry_scope_guard_then_one_retry_preserves_failed_receipt(restore_wrong_scope_checkpoint, monkeypatch):
    from stock_ai import autonomous_trading_service as service_module
    monkeypatch.setattr(service_module, "get_autonomous_campaign", lambda: pytest.fail("scope guard must run before service construction"))
    registry = StockAgentToolRegistry()

    class Tools:
        def __init__(self): self.calls = []
        def manifest(self): return registry.manifest()
        async def execute(self, name, arguments, ctx):
            assert name == "autonomy.activate" and not ctx.symbols
            self.calls.append(deepcopy(arguments))
            if not restore_wrong_scope_checkpoint and len(self.calls) == 1:
                try:
                    await AutonomousTradingToolProvider().execute(name, arguments, replace(ctx, symbols=("AC",)))
                except PermissionError as exc:
                    raise WorkerToolError(str(exc)) from exc
                raise AssertionError("expected the real provider scope guard")
            return receipt()

    class Driver:
        def __init__(self): self.inputs = []
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            self.inputs.append(turn)
            count = 1 if restore_wrong_scope_checkpoint else 2
            calls = [{**CALL, "id": f"activate-{len(self.inputs)}"}] if len(self.inputs) <= count else []
            return {"state": "continue", "summary": "依 Host 已修正範圍啟用保留循環。", "tool_calls": calls, "decision": None}

    checkpoint = None
    if restore_wrong_scope_checkpoint:
        plan = PlanGraph.create(OBJECTIVE)
        plan.nodes[failure()["node_id"]] = PlanNode(node_id=failure()["node_id"], node_type="tool",
            title="activation rejected before service", status="failed", tool_name="autonomy.activate", arguments=CALL["arguments"],
            postconditions=({"predicate": "host_validated_mutation"},))
        checkpoint = {"plan": plan.to_dict(), "trace": [failure()], "context_state": {
            "task_kind": "market_decision", "explicit_autonomous_campaign_authorized": True,
            "routing": {"intent": "analyze", "task_kind": "market_decision", "symbols": ["AC"]},
            "retired_recovery_tools": "autonomy.activate"},
            "transcript": [{"role": "host", "type": "retired_recovery_tools", "content": {
                "tools": ["autonomy.activate"], "instruction": "Never use the retired activation."}}]}
    before = deepcopy(checkpoint)
    driver, tools, events = Driver(), Tools(), []
    result = asyncio.run(AgentOrchestrator(drivers={"scripted": driver}, tools=tools, default_driver="scripted").run(
        objective=OBJECTIVE, symbols=[], autonomy="paper_execute", max_steps=3 if checkpoint is None else 2,
        resume_state={"checkpoint": {"payload": checkpoint}} if checkpoint else None, event_sink=events.append))
    assert tools.calls == [CALL["arguments"]] * (1 if checkpoint else 2), {
        "status": result.get("status"), "events": [e for e in events if e["type"] in {
            "tool.failed", "plan.compile.failed", "plan.execution.blocked", "run.failed", "repair.recovery_surface_enforced"}]}
    failures = [item for item in result["tool_trace"] if item.get("ok") is False]
    assert len(failures) == 1 and failures[0]["error"]["message"] == ERROR
    nodes = {node["node_id"]: node for node in result["plan"]["nodes"]}
    assert nodes[failure()["node_id"]]["status"] == "failed"
    links = [event for event in events if event["type"] == "recovery.linked"]
    assert len(links) == 1
    assert any(node["tool_name"] == "autonomy.activate" and node["status"] == "completed"
               and node["node_id"] != failure()["node_id"] for node in nodes.values())
    retry_turn = driver.inputs[0 if checkpoint else 1]
    names = {t["name"] for t in json.loads(_turn_prompt(retry_turn).split("\nTOOLS=", 1)[1].split("\nTRANSCRIPT=", 1)[0])}
    assert "autonomy.activate" in names
    assert "Never use the retired activation." not in _turn_prompt(retry_turn)
    assert before == checkpoint
