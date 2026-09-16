"""A retained-evidence rejection may recover once without repeating research."""
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
    _prepare_recovery_retry_calls, _prior_repeated_tool_failure,
    _recovery_links_for_call, _recovery_tool_surface, _unresolved_failure_nodes,
)
from open_stock_ai.agent_runtime.workers.base import WorkerToolError
from stock_ai.agent_drivers import _turn_prompt
from stock_ai.agent_tools import StockAgentToolRegistry


OBJECTIVE = "[MODEL_TASK_KIND:market_decision]\n[MARKET_SCOPE]\n請執行全市場自主紙上交易流水線，沿用保留研究。"
CALL = {"id": "proposal-original", "name": "autonomy.propose_plan", "arguments": {
    "cycle_id": "AC-" + "1" * 64, "symbol": "3231.TW", "position_size_pct": 4.5,
    "stop_loss": 184, "target_price": 230, "entry_condition": "price_at_or_above",
    "trigger_price": 201, "rationale": "Retained evidence supports a conditional paper experiment.",
    "evidence_ids": ["tc-history-3231"],
}}
ERROR = "retained_campaign_evidence_not_found"


def context():
    result = AgentRunContext(run_id="r", session_id="s", symbols=(), autonomy="paper_execute", allow_paper_orders=True)
    bind_campaign_authorization(result, objective=OBJECTIVE, task_kind="market_decision")
    return result


def failure():
    return {"tool": CALL["name"], "arguments": deepcopy(CALL["arguments"]), "call_id": CALL["id"],
            "node_id": _call_node_id(CALL), "ok": False, "result": None,
            "error": {"category": "execution_failure", "exception_type": "WorkerToolError", "message": ERROR, "retryable": False},
            "recovery": {"action": "revise_plan", "requires_model_replan": True}}


def receipt():
    return {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": CALL["name"],
            "account_id": "autonomous-paper-v1", "cycle_id": CALL["arguments"]["cycle_id"],
            "campaign_receipt_id": "AE-" + "2" * 64,
            "plan": {"plan_id": "plan-3231", "account_id": "autonomous-paper-v1", "symbol": "3231.TW",
                     "definition": {"strategy_id": "agent_discretionary_proposal_v1",
                                    "metadata": {"cycle_id": CALL["arguments"]["cycle_id"]}},
                     "status": "waiting_entry", "state": {"status": "waiting_entry"}}}


def test_one_exact_proposal_retry_requires_a_plan_receipt_and_preserves_failed_node():
    trace, ctx, manifest = [failure()], context(), StockAgentToolRegistry().manifest()
    surface, requirements = _recovery_tool_surface(trace, manifest, context=ctx)
    assert CALL["name"] in {t["name"] for t in surface}
    assert requirements[0]["evidence_retry_available"] is True
    assert _prior_repeated_tool_failure(trace, CALL, context=ctx) is None
    retry = _prepare_recovery_retry_calls([CALL], trace, context=ctx)[0]
    assert retry["arguments"] == CALL["arguments"]
    assert _call_node_id(retry) != _call_node_id(CALL)
    assert _prepare_recovery_retry_calls([CALL], trace, context=ctx)[0] == retry
    for invalid in ({}, {**receipt(), "campaign_receipt_id": None}, {**receipt(), "cycle_id": "wrong"},
                    {**receipt(), "plan": {**receipt()["plan"], "symbol": "2330.TW"}}):
        assert _recovery_links_for_call(trace, retry, manifest, context=ctx, result=invalid) == []
    links = _recovery_links_for_call(trace, retry, manifest, context=ctx, result=receipt())
    assert len(links) == 1 and links[0]["failed_node_id"] == failure()["node_id"]
    trace.append({"tool": CALL["name"], "arguments": deepcopy(CALL["arguments"]),
                  "ok": True, "result": receipt(), "recovery_for": links})
    assert _recovery_tool_surface(trace, manifest, context=ctx) == ([], [])
    assert _unresolved_failure_nodes(PlanGraph.create(OBJECTIVE), trace) == []


@pytest.mark.parametrize("reason", ["foreign_symbol", "unauthorized", "advisory", "paper_disabled", "missing_context",
                                   "second_failure", "uncertain", "other_error", "changed_arguments", "result_present"])
def test_uncertain_unauthorized_or_repeated_proposals_do_not_get_the_retry(reason):
    trace, ctx, call = [failure()], context(), deepcopy(CALL)
    if reason == "foreign_symbol": ctx = replace(ctx, symbols=("2330.TW",))
    elif reason == "unauthorized": ctx.state["explicit_autonomous_campaign_authorized"] = False
    elif reason == "advisory": ctx = replace(ctx, autonomy="advisory")
    elif reason == "paper_disabled": ctx = replace(ctx, allow_paper_orders=False)
    elif reason == "missing_context": ctx = None
    elif reason == "second_failure": trace.append({**failure(), "node_id": "second"})
    elif reason == "uncertain": trace[0]["error"].update(category="timeout", retryable=True)
    elif reason == "other_error": trace[0]["error"]["message"] = "plan receipt unavailable"
    elif reason == "changed_arguments": call["arguments"]["position_size_pct"] = 5
    elif reason == "result_present": trace[0]["result"] = receipt()
    assert _prior_repeated_tool_failure(trace, call, context=ctx) is not None
    assert _prepare_recovery_retry_calls([call], trace, context=ctx) == [call]
    if reason != "changed_arguments":
        surface, _ = _recovery_tool_surface(trace, StockAgentToolRegistry().manifest(), context=ctx)
        assert CALL["name"] not in {t["name"] for t in surface}


def test_recovery_cannot_clear_an_unrelated_proposal_or_be_supplied_by_the_model():
    other = {**failure(), "node_id": "other-plan", "arguments": {**CALL["arguments"], "symbol": "2330.TW"}}
    trace, ctx, manifest = [failure(), other], context(), StockAgentToolRegistry().manifest()
    retry = _prepare_recovery_retry_calls([CALL], trace, context=ctx)[0]
    links = _recovery_links_for_call(trace, retry, manifest, context=ctx, result=receipt())
    assert [link["failed_node_id"] for link in links] == [failure()["node_id"]]
    read = {"id": "read", "name": "autonomy.evidence", "arguments": {"evidence_id": "AE-" + "3" * 64}}
    assert _recovery_links_for_call(trace, read, manifest, context=ctx, result=receipt()) == []
    normalized = _normalize_turn({"state": "continue", "tool_calls": [{**CALL, "_host_proposal_evidence_retry": 1}]})
    assert "_host_proposal_evidence_retry" not in normalized["tool_calls"][0]
    assert _call_node_id(normalized["tool_calls"][0]) == _call_node_id(CALL)


@pytest.mark.parametrize("restore_checkpoint", [False, True])
def test_orchestrator_retry_or_restart_keeps_original_failure_and_one_new_receipt(restore_checkpoint):
    registry = StockAgentToolRegistry()

    class Tools:
        def __init__(self): self.calls = []
        def manifest(self): return registry.manifest()
        async def execute(self, name, arguments, ctx):
            assert name == CALL["name"] and not ctx.symbols
            self.calls.append(deepcopy(arguments))
            if not restore_checkpoint and len(self.calls) == 1:
                raise WorkerToolError(ERROR)
            return receipt()

    class Driver:
        def __init__(self): self.inputs = []
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            self.inputs.append(turn)
            count = 1 if restore_checkpoint else 2
            calls = [{**CALL, "id": f"proposal-{len(self.inputs)}"}] if len(self.inputs) <= count else []
            return {"state": "continue", "summary": "沿用研究與原始計畫，重試已修復的證據轉接。", "tool_calls": calls, "decision": None}

    checkpoint = None
    if restore_checkpoint:
        plan = PlanGraph.create(OBJECTIVE)
        plan.nodes[failure()["node_id"]] = PlanNode(node_id=failure()["node_id"], node_type="tool",
            title="proposal rejected before plan creation", status="failed", tool_name=CALL["name"], arguments=CALL["arguments"],
            postconditions=({"predicate": "host_validated_mutation"},))
        checkpoint = {"plan": plan.to_dict(), "trace": [failure()], "context_state": {
            "task_kind": "market_decision", "explicit_autonomous_campaign_authorized": True,
            "retired_recovery_tools": CALL["name"]},
            "transcript": [{"role": "host", "type": "retired_recovery_tools", "content": {
                "tools": [CALL["name"]], "instruction": "Never use the retired proposal."}}]}
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
    assert len([event for event in events if event["type"] == "recovery.linked"]) == 1
    assert any(node["tool_name"] == CALL["name"] and node["status"] == "completed"
               and node["node_id"] != failure()["node_id"] for node in nodes.values())
    retry_turn = driver.inputs[0 if checkpoint else 1]
    names = {t["name"] for t in json.loads(_turn_prompt(retry_turn).split("\nTOOLS=", 1)[1].split("\nTRANSCRIPT=", 1)[0])}
    assert CALL["name"] in names
    assert "Never use the retired proposal." not in _turn_prompt(retry_turn)
    assert before == checkpoint
