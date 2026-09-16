"""Read campaign state once after each verified mutation, including restart."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from open_stock_ai.agent_runtime.orchestrator import AgentOrchestrator, _call_node_id, _normalize_turn
from open_stock_ai.agent_runtime.plan_graph import PlanGraph, PlanNode
from open_stock_ai.agent_runtime.repair.recovery_policy import _prepare_recovery_retry_calls, _reusable_observation
from stock_ai.agent_tools import StockAgentToolRegistry


OBJECTIVE = "[MARKET_SCOPE]\n請啟動全市場自主紙上交易流水線，核對隔離帳戶的狀態。"
STATUS = {"id": "status-same-id", "name": "autonomy.status", "arguments": {}}
ACTIVATE = {"id": "activate", "name": "autonomy.activate", "arguments": {"cycle_id": "AC-" + "1" * 64, "use_candidate_plans": False}}


def mutation(*, name="autonomy.activate", receipt_char="2"):
    result = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": name,
              "account_id": "autonomous-paper-v1", "cycle_id": ACTIVATE["arguments"]["cycle_id"],
              "campaign_receipt_id": "AE-" + receipt_char * 64, "plans": [], "skipped": [],
              "management": {"account_id": "autonomous-paper-v1", "enabled": True, "results": [], "errors": []}}
    return {"tool": name, "arguments": ACTIVATE["arguments"] if name == "autonomy.activate" else {},
            "call_id": "activate", "node_id": _call_node_id(ACTIVATE), "ok": True,
            "validation": {"passed": True}, "result": result}


def status(*, enabled):
    return {"schema_version": "open_stock_ai.autonomous_status.v1", "account_id": "autonomous-paper-v1",
            "enabled": enabled, "plans": [], "account": {"account_id": "autonomous-paper-v1", "cash": 1000000,
                                                           "equity": 1000000, "positions": [], "open_orders": []}}


def old_status():
    return {"tool": "autonomy.status", "arguments": {}, "call_id": STATUS["id"], "node_id": _call_node_id(STATUS),
            "ok": True, "validation": {"passed": True}, "result": status(enabled=False)}


def test_status_identity_and_reuse_are_scoped_to_last_verified_mutation():
    trace = [old_status()]
    assert _prepare_recovery_retry_calls([STATUS], trace) == [STATUS]
    assert _reusable_observation(trace, STATUS, {"idempotency": "none"})["result"]["enabled"] is False
    trace.append(mutation())
    refreshed = _prepare_recovery_retry_calls([STATUS], trace)[0]
    assert _call_node_id(refreshed) != _call_node_id(STATUS)
    assert _reusable_observation(trace, refreshed, {"idempotency": "arguments"}) is None
    trace.append({**old_status(), "node_id": _call_node_id(refreshed), "result": status(enabled=True)})
    assert _prepare_recovery_retry_calls([STATUS], trace)[0] == refreshed
    assert _reusable_observation(trace, refreshed, {"idempotency": "none"})["result"]["enabled"] is True
    trace.append(mutation(name="autonomy.manage", receipt_char="3"))
    after_manage = _prepare_recovery_retry_calls([STATUS], trace)[0]
    assert _call_node_id(after_manage) != _call_node_id(refreshed)
    assert _reusable_observation(trace, after_manage, {"idempotency": "arguments"}) is None


@pytest.mark.parametrize("change", [{"ok": False}, {"validation": {"passed": False}},
                                    {"result": {}}, {"tool": "market.research_pack"}])
def test_nonmutation_or_unvalidated_receipt_cannot_create_status_generations(change):
    assert _prepare_recovery_retry_calls([STATUS], [old_status(), {**mutation(), **change}]) == [STATUS]


def test_model_cannot_forge_status_read_generation():
    value = _normalize_turn({"state": "continue", "tool_calls": [{**STATUS, "_host_status_after_mutation": "fake"}]})
    assert value["tool_calls"] == [STATUS]


@pytest.mark.parametrize("restored", [False, True])
def test_real_orchestrator_refreshes_after_mutation_and_keeps_no_progress_dedupe(restored):
    registry = StockAgentToolRegistry()

    class Tools:
        def __init__(self): self.calls, self.enabled = [], restored
        def manifest(self): return registry.manifest()
        async def execute(self, name, arguments, context):
            self.calls.append(name)
            if name == "autonomy.activate":
                self.enabled = True
                return mutation()["result"]
            assert name == "autonomy.status"
            return status(enabled=self.enabled)

    class Driver:
        def __init__(self): self.turns = []
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            self.turns.append(turn)
            index = len(self.turns)
            call = ACTIVATE if not restored and index == 2 else STATUS
            response = {"state": "continue", "summary": "讀取變更後的隔離帳戶。", "tool_calls": [call], "decision": None}
            if not restored and index == 3:
                response["plan_patch"] = {"operations": [{"op": "update_node", "node_id": "nonexistent",
                                                           "changes": {"status": "completed"}}]}
            return response

    checkpoint = None
    if restored:
        plan = PlanGraph.create(OBJECTIVE)
        for call in (STATUS, ACTIVATE):
            plan.nodes[_call_node_id(call)] = PlanNode(node_id=_call_node_id(call), node_type="tool", title=call["name"],
                status="completed", tool_name=call["name"], arguments=call["arguments"],
                postconditions=({"predicate": "host_validated_mutation"},) if call is ACTIVATE else ())
        checkpoint = {"plan": plan.to_dict(), "trace": [old_status(), mutation()], "transcript": [],
                      "context_state": {"task_kind": "market_decision", "explicit_autonomous_campaign_authorized": True}}
    before = deepcopy(checkpoint)
    driver, tools, events = Driver(), Tools(), []
    result = asyncio.run(AgentOrchestrator(drivers={"scripted": driver}, tools=tools, default_driver="scripted").run(
        objective=OBJECTIVE, symbols=[], autonomy="paper_execute", max_steps=2 if restored else 4,
        resume_state={"checkpoint": {"payload": checkpoint}} if checkpoint else None, event_sink=events.append))
    assert tools.calls == (["autonomy.status"] if restored else ["autonomy.status", "autonomy.activate", "autonomy.status"])
    statuses = [item for item in result["tool_trace"] if item.get("tool") == "autonomy.status"]
    assert [item["result"]["enabled"] for item in statuses] == [False, True]
    assert statuses[0]["node_id"] != statuses[1]["node_id"]
    assert statuses[1]["validation"]["passed"] is True
    if not restored:
        assert any(e["type"] == "plan.patch.ignored" and e.get("preserved_tool_call_count") == 1 for e in events)
    current = [e for e in events if e["type"] == "plan.current"]
    assert any(any(t["name"] == "autonomy.status" for t in e["requested_tools"]) for e in current)
    if not restored:
        assert current[-1]["requested_tools"] == []  # No new mutation means no repeated read.
    assert checkpoint == before
