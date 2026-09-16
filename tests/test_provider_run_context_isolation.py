"""Historical Run controls cannot become a new Run's Host instructions."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime import AgentOrchestrator
from open_stock_ai.agent_runtime.providers.run_context import (
    restore_provider_run_context, scoped_run_memories, scoped_session_history,
)
from stock_ai.agent_drivers import _turn_prompt


SCOPE = {"run_id": "AR-current", "session_id": "AS-current"}
OLD_NODE = "node-obsolete-other-run"
OLD_RECEIPT = "AE-old-activation"


def response(run_id="AR-current", session_id="AS-current", identifier="INT-current"):
    return {"role": "user", "run_id": run_id, "session_id": session_id,
            "source": {"type": "interaction_response"},
            "content": {"interaction_id": identifier, "response": {"free_text": "Use the current research."}}}


@pytest.mark.parametrize("run_id,session_id", [
    ("AR-other", "AS-current"), ("AR-current", "AS-other"), (None, None),
])
def test_foreign_or_unscoped_controls_are_not_current_user_intent(run_id, session_id):
    foreign = response(run_id, session_id, "INT-foreign")
    foreign["content"]["response"]["free_text"] = f"Skip {OLD_NODE}; use {OLD_RECEIPT}."
    original = deepcopy(foreign)
    history = scoped_session_history([foreign, response()], task_kind="market_decision", **SCOPE)
    assert [item["content"]["interaction_id"] for item in history] == ["INT-current"]
    assert foreign == original


@pytest.mark.parametrize("legacy", [False, True])
def test_restore_rebinds_genuine_interaction_and_preserves_run_controls(legacy):
    current, foreign = response(), response("AR-other", identifier="INT-foreign")
    current["content"]["selected_option"] = {"option_id": "keep_cash", "label": "保留現金"}
    foreign["content"]["agent_view"] = OLD_RECEIPT
    old = {"role": "host", "type": "interaction_response", "content": foreign["content"]}
    genuine = {"role": "host", "type": "interaction_response", "content": current["content"]}
    control = {"role": "host", "type": "control_message",
               "content": {"control_id": "ACTL-current", "payload": {"instruction": "Review current evidence."}}}
    if not legacy:
        old.update(run_id="AR-other", session_id="AS-current")
        genuine.update(SCOPE)
        control.update(SCOPE)
    trace = {"role": "host", "type": "tool_results", "content": [{"call_id": "current-research", "ok": True}]}
    transcript = [old, genuine, control, trace]
    original = deepcopy(transcript)
    projected = restore_provider_run_context(transcript, session_history=[foreign, current],
        task_kind="market_decision", owned_control_ids=["ACTL-current"], **SCOPE)
    assert OLD_RECEIPT not in json.dumps(projected)
    interactions = [item for item in projected if item["type"] == "interaction_response"]
    assert len(interactions) == 1 and interactions[0]["content"] == current["content"]
    assert interactions[0]["run_id"] == SCOPE["run_id"]
    assert next(item for item in projected if item["type"] == "control_message")["run_id"] == SCOPE["run_id"]
    assert next(item for item in projected if item["type"] == "tool_results") == trace
    assert transcript == original


def test_unscoped_checkpoint_control_needs_a_current_durable_control_id():
    controls = [{"type": "control_message", "content": {"control_id": identifier}}
                for identifier in ("ACTL-foreign", "ACTL-current")]
    projected = restore_provider_run_context(controls, session_history=[], task_kind="general_answer",
        owned_control_ids=["ACTL-current"], **SCOPE)
    assert [item["content"]["control_id"] for item in projected] == ["ACTL-current"]


def test_only_current_execution_memory_and_governed_preferences_reach_model():
    memories = [
        {"kind": "working", "content": OLD_RECEIPT, "source": {"run_id": "AR-other"}},
        {"kind": "episodic", "content": OLD_NODE, "source": {"type": "agent_run", "run_id": "AR-other"}},
        {"kind": "working", "content": "unscoped old result", "source": {}},
        {"kind": "working", "content": "Current observation", "source": {"run_id": "AR-current"}},
        {"kind": "user_preference", "content": "Prefer conservative sizing", "source": {"run_id": "AR-other"}},
        {"kind": "reflection", "content": "Verified procedural lesson", "source": {"type": "host_procedural_lesson", "run_id": "AR-other"}},
        {"kind": "domain", "content": "General strategy knowledge", "source": {"type": "host_verified"}},
        {"kind": "episodic", "content": "Governed non-execution episode", "source": {"type": "host_verified"}},
    ]
    original = deepcopy(memories)
    assert [item["content"] for item in scoped_run_memories(memories, **SCOPE)] == [
        "Current observation", "Prefer conservative sizing", "Verified procedural lesson",
        "General strategy knowledge", "Governed non-execution episode"]
    assert memories == original


@pytest.mark.parametrize("resume", [False, True])
def test_full_orchestrator_prompt_uses_current_run_controls_and_memory(resume):
    current, foreign = response(), response("AR-other", identifier="INT-foreign")
    foreign["content"].update(agent_view=OLD_RECEIPT,
        response={"free_text": f"Skip {OLD_NODE}; old activation completed."})
    history = [foreign, current]
    checkpoint = {"transcript": [
        {"role": "host", "type": "conversation_history", "content": [foreign]},
        {"role": "host", "type": "interaction_response", "content": foreign["content"]},
    ], "context_state": {"current_step": 1}, "trace": []}
    original = deepcopy(checkpoint)
    class Driver:
        def describe(self): return {"id": "scripted", "configured": True}
        async def decide(self, turn):
            prompt = _turn_prompt(turn)
            assert OLD_NODE not in prompt and OLD_RECEIPT not in prompt
            assert "Use the current research." in prompt
            assert "Prefer conservative sizing" in prompt
            self.turn = turn
            return {"state": "complete", "summary": "已依本次要求回答。", "tool_calls": [], "decision": None}
    class Tools:
        def manifest(self): return []
        async def execute(self, *_args): pytest.fail("No tools needed")
    memories = [
        {"kind": "working", "content": OLD_RECEIPT, "source": {"run_id": "AR-other"}},
        {"kind": "user_preference", "content": "Prefer conservative sizing", "source": {"run_id": "AR-other"}},
    ]
    driver = Driver()
    runtime = AgentOrchestrator(drivers={"scripted": driver}, tools=Tools(), default_driver="scripted")
    runtime.memory_manager = SimpleNamespace(retrieve=lambda *_args, **_kwargs: memories)
    result = asyncio.run(runtime.run(objective="回答一句簡單的一般知識。", **SCOPE,
        session_history=history, resume_state={"checkpoint": {"payload": checkpoint}} if resume else None))
    assert result["status"] == "completed"
    assert checkpoint == original
    assert len([item for item in driver.turn.transcript if item["type"] == "interaction_response"]) == 1


def test_durable_session_handoff_preserves_real_message_ownership(tmp_path):
    from stock_ai.agent_run_store import AgentRunStore
    from stock_ai.durable_agent_runtime import DurableAgentRuntime
    class Service:
        async def run(self, **kwargs):
            self.last = kwargs
            return {"run_id": kwargs["run_id"], "status": "completed", "summary": "完成", "activity": []}
    async def scenario():
        service = Service()
        runtime = DurableAgentRuntime(service_provider=lambda: service, store=AgentRunStore(tmp_path / "agent.db"))
        try:
            session = runtime.create_session(title="Scoped history")
            old = await runtime.create_run(objective="Old task", session_id=session["session_id"])
            await runtime.wait(old["run_id"])
            runtime.session_store.add_message(session_id=session["session_id"], run_id=old["run_id"], role="user",
                content={"interaction_id": "INT-old", "response": {"free_text": OLD_NODE}},
                source={"type": "interaction_response"})
            new = await runtime.create_run(objective="New task", session_id=session["session_id"])
            await runtime.wait(new["run_id"])
            historical = next(item for item in service.last["session_history"]
                              if isinstance(item["content"], dict) and item["content"].get("interaction_id") == "INT-old")
            assert historical["run_id"] == old["run_id"]
            assert historical["session_id"] == session["session_id"]
            projected = scoped_session_history(service.last["session_history"], run_id=new["run_id"],
                session_id=session["session_id"], task_kind="market_decision")
            assert OLD_NODE not in json.dumps(projected)
            assert service.last["resume_state"]["provider_context_scope"]["control_ids"] == []
        finally:
            await runtime.close()
    asyncio.run(scenario())
