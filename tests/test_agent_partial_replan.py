"""Replan incomplete durable runs in place; no cloud provider or trading account."""
import asyncio
from copy import deepcopy

import pytest

from open_stock_ai.agent_runtime.checkpoint_manager import CheckpointManager
from open_stock_ai.agent_runtime.checkpoint_store import CheckpointStore
from open_stock_ai.agent_runtime.plan_graph import PlanGraph, PlanNode
from open_stock_ai.agent_runtime.plan_manager import PlanManager
from stock_ai import agent_api
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime


INSTRUCTION = "保留已驗證的啟用收據，只讀取最新 autonomy.status 後完成回答。"
ACTIVATION = {"call_id": "activation-already-succeeded", "node_id": "activate-done",
              "tool": "autonomy.activate", "arguments": {"cycle_id": "AC-retained"}, "ok": True,
              "result": {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper",
                         "action": "autonomy.activate", "cycle_id": "AC-retained", "account_id": "offline",
                         "campaign_receipt_id": "AE-" + "a"*64, "plans": [], "skipped": [],
                         "management": {"account_id": "offline", "enabled": True, "results": [], "errors": []}}}


@pytest.mark.parametrize("prior_status", ["partially_completed", "max_steps_reached"])
def test_replan_api_queues_one_control_continues_same_run_and_preserves_success(tmp_path, monkeypatch, prior_status):
    async def scenario():
        path = tmp_path / "replan.sqlite"
        store = AgentRunStore(path)
        plans = PlanManager(path)
        checkpoints = CheckpointManager(CheckpointStore(path))
        calls, received_controls = [], []
        class OfflineService:
            async def run(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    plan = PlanGraph.create(kwargs["objective"])
                    plan.nodes["activate-done"] = PlanNode(node_id="activate-done", node_type="tool", title="Completed activation",
                        tool_name="autonomy.activate", arguments={"cycle_id": "AC-retained"}, status="completed")
                    plans.create(session_id=kwargs["session_id"], run_id=kwargs["run_id"], objective=kwargs["objective"], plan=plan)
                    checkpoints.save(session_id=kwargs["session_id"], run_id=kwargs["run_id"], sequence=1, plan=plan,
                        transcript=[], trace=[deepcopy(ACTIVATION)], context_state={"current_step": 16})
                    await kwargs["event_sink"]({"sequence": 1, "type": "step.started", "step": 16,
                                                "run_id": kwargs["run_id"], "summary": "Budget boundary"})
                    return {"schema_version": "open_stock_ai.agent_run.v2", "run_id": kwargs["run_id"],
                            "status": prior_status, "summary": "Awaiting fresh status", "tool_trace": [deepcopy(ACTIVATION)],
                            "goal_completion_gaps": ["cost_budget_exhausted"], "activity": []}
                # The real durable runtime must preserve completed work; this
                # offline executor only observes status, never invokes activation.
                resumed = kwargs["resume_state"]
                assert resumed["next_step"] == 17
                saved = resumed["checkpoint"]["payload"]
                assert saved["trace"] == [ACTIVATION]
                assert resumed["recovery_context"]["tool_trace"] == [ACTIVATION]
                activation_node = next(n for n in saved["plan"]["nodes"] if n["node_id"] == "activate-done")
                assert activation_node["status"] == "completed"
                received_controls.extend(runtime.consume_controls(kwargs["run_id"]))
                assert runtime.consume_controls(kwargs["run_id"]) == []
                return {"schema_version": "open_stock_ai.agent_run.v2", "run_id": kwargs["run_id"],
                        "status": "completed", "summary": "Latest status observed; prior activation preserved.",
                        "tool_trace": saved["trace"] + [{"call_id": "fresh-status", "tool": "autonomy.status", "ok": True,
                                                        "result": {"account_id": "offline", "enabled": True}}], "activity": []}
        service = OfflineService()
        runtime = DurableAgentRuntime(service_provider=lambda: service, store=store, plan_manager=plans, checkpoint_manager=checkpoints)
        monkeypatch.setattr(agent_api, "get_agent_run_runtime", lambda: runtime)
        try:
            created = await runtime.create_run(objective="Finish retained campaign status verification", max_steps=18)
            first = await runtime.wait(created["run_id"])
            assert first["status"] == prior_status and store.get_run(created["run_id"])["terminal"] is True
            reopened = await agent_api.replan_agent_run(created["run_id"], agent_api.AgentReplanRequest(instruction=INSTRUCTION))
            assert reopened["run_id"] == created["run_id"] and reopened["max_steps"] == 24
            final = await runtime.wait(created["run_id"])
            assert final["status"] == "completed"
            assert len(calls) == 2 and calls[1]["max_steps"] == 24
            assert calls[0]["run_id"] == calls[1]["run_id"] == created["run_id"]
            assert calls[0]["session_id"] == calls[1]["session_id"]
            assert [(c["control_type"], c["payload"]["instruction"]) for c in received_controls] == [("replan", INSTRUCTION)]
            assert len(store.list_runs()) == 1 and store.get_run(created["run_id"])["resume_count"] == 1
            continuation = [e for e in runtime.events(created["run_id"]) if e["type"] == "run.continuation_requested"]
            assert len(continuation) == 1 and continuation[0]["payload"]["previous_max_steps"] == 18
            assert [row["call_id"] for row in final["tool_trace"] if row["tool"] == "autonomy.activate"] == [ACTIVATION["call_id"]]
            with store._connect() as conn:
                rows = conn.execute("select control_type,status from agent_control_messages where run_id=?", (created["run_id"],)).fetchall()
            assert [tuple(row) for row in rows] == [("replan", "consumed")]
        finally:
            await runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_replan_rejects_true_terminal_states_before_control_or_provider(tmp_path, status):
    async def scenario():
        store = AgentRunStore(tmp_path / "terminal.sqlite")
        store.create_run("AR-terminal", {"objective": "Already terminal", "max_steps": 18})
        if status == "completed":
            store.complete_run("AR-terminal", {"status": status, "summary": "Finished"})
        elif status == "cancelled":
            store.cancel_run("AR-terminal")
        else:
            store.fail_run("AR-terminal", {"type": "OfflineFailure"})
        def forbidden():
            pytest.fail("Terminal rejection must not initialize a provider")
        runtime = DurableAgentRuntime(service_provider=forbidden, store=store)
        before = store.get_run("AR-terminal")
        try:
            with pytest.raises(ValueError, match="terminal run cannot be replanned"):
                await runtime.request_replan("AR-terminal", instruction=INSTRUCTION)
            assert store.get_run("AR-terminal") == before
            assert store.consume_control_messages("AR-terminal") == []
            assert runtime._tasks == {}
        finally:
            await runtime.close()
    asyncio.run(scenario())
