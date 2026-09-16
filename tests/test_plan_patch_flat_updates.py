"""Provider flat update_node fields must make the declared plan revision."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from open_stock_ai.agent_runtime.contracts import AgentToolSpec
from open_stock_ai.agent_runtime.orchestrator import AgentOrchestrator, _normalize_turn
from open_stock_ai.agent_runtime.plan_graph import PlanGraph, PlanNode


def flat_update():
    return {"op": "update_node", "node_id": "obsolete-scope-repair", "status": "skipped",
            "metadata": {"superseded_reason": "A validated Host receipt already resolved this investigation."},
            "evidence_ids": ["verified-repair"]}


@pytest.mark.parametrize("encoded", [False, True])
def test_flat_update_preserves_status_reason_and_evidence_in_changes(encoded):
    operation = flat_update()
    original = deepcopy(operation)
    patch = {"operations_json": json.dumps([operation])} if encoded else {"operations": [operation]}
    normalized = _normalize_turn({"state": "complete", "plan_patch": patch})
    assert normalized["plan_patch"]["operations"][0]["changes"] == {
        "status": "skipped", "metadata": {**operation["metadata"], "evidence_ids": operation["evidence_ids"]}}
    assert operation == original


def test_explicit_nested_changes_remain_authoritative():
    operation = {**flat_update(), "changes": {"status": "blocked", "metadata": {"reason": "Still unresolved"}}}
    normalized = _normalize_turn({"state": "continue", "plan_patch": {"operations": [operation]}})
    assert normalized["plan_patch"]["operations"][0]["changes"] == operation["changes"]


@pytest.mark.parametrize("status", ["running", "completed"])
def test_flat_form_cannot_rewrite_an_immutable_started_node(status):
    plan = PlanGraph.create("Explain the verified repair")
    plan.nodes["obsolete-scope-repair"] = PlanNode(node_id="obsolete-scope-repair", node_type="reasoning",
                                                   title="started", status=status)
    patch = _normalize_turn({"state": "continue", "plan_patch": {"operations": [flat_update()]}})["plan_patch"]
    with pytest.raises(ValueError, match="cannot be rewritten"):
        plan.apply_patch(patch)
    assert plan.nodes["obsolete-scope-repair"].status == status


def test_flat_supersession_finishes_through_real_provider_orchestrator_path():
    objective = "[MODEL_TASK_KIND:current_information]\nExplain the verified Host repair."
    plan = PlanGraph.create(objective)
    plan.nodes["obsolete-scope-repair"] = PlanNode(node_id="obsolete-scope-repair", node_type="reasoning",
        title="scope investigation", status="blocked", mandatory=False)
    checkpoint = {"plan": plan.to_dict(), "trace": [{"call_id": "verified-repair", "node_id": "verified-repair",
        "tool": "web.fetch", "arguments": {}, "ok": True, "result": {"verified": True}, "validation": {"passed": True}}],
        "context_state": {"task_kind": "current_information"}, "transcript": []}
    before = deepcopy(checkpoint)

    class Tools:
        def manifest(self):
            return [AgentToolSpec(name="web.fetch", description="Read verified Host repair", category="web", input_schema={"type": "object"}).to_dict()]
        async def execute(self, *args):
            raise AssertionError("The already validated tool must not execute again")

    class Driver:
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            return {"state": "complete", "summary": "The Host repair is verified.\n\nIts earlier scope investigation is superseded by the retained evidence.\n\nThe original verification record remains unchanged for review.",
                    "tool_calls": [], "decision": None, "plan_patch": {"operations": [flat_update()]},
                    "completion_evaluation": {"criteria_met": True, "remaining_gaps": [], "evidence_ids": ["verified-repair"],
                        "criterion_results": [{"criterion": plan.completion_criteria[0], "met": True, "evidence_ids": ["verified-repair"]}]}}

    events = []
    result = asyncio.run(AgentOrchestrator(drivers={"scripted": Driver()}, tools=Tools(), default_driver="scripted").run(
        objective=objective, max_steps=1, resume_state={"checkpoint": {"payload": checkpoint}}, event_sink=events.append))
    assert result["status"] == "completed", {"validation": result.get("completion_validation"),
        "errors": [(e["type"], e.get("error"), e.get("result")) for e in events if "failed" in e["type"]]}
    assert result["completion_validation"]["passed"] is True
    node = next(n for n in result["plan"]["nodes"] if n["node_id"] == "obsolete-scope-repair")
    assert node["status"] == "skipped" and node["metadata"]["evidence_ids"] == ["verified-repair"]
    assert checkpoint == before
