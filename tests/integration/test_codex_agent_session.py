from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentRunContext
from open_stock_ai.agent_runtime.contracts import AgentTurnInput
from stock_ai import agent_drivers
from stock_ai.agent_drivers import CodexAgentDriver


def test_codex_driver_keeps_one_run_id_across_start_multiple_turns_and_close(monkeypatch):
    calls = []

    async def start(run_id, *, project_root=None):
        calls.append(("start", run_id, project_root))

    async def turn(prompt, schema, *, run_id, event_sink):
        del prompt, schema, event_sink
        calls.append(("turn", run_id))
        return {"state": "complete", "summary": "done", "tool_calls": [], "decision": None}

    async def close(run_id):
        calls.append(("close", run_id))

    monkeypatch.setattr(agent_drivers.codex_runtime, "start_agent_run", start)
    monkeypatch.setattr(agent_drivers.codex_runtime, "run_agent_turn", turn)
    monkeypatch.setattr(agent_drivers.codex_runtime, "close_agent_run", close)
    driver = CodexAgentDriver()
    context = AgentRunContext(run_id="AR-one-hidden-thread", autonomy="advisory", symbols=())
    turn_input = AgentTurnInput(
        run_id=context.run_id,
        objective="explain compounding",
        system_prompt="Return JSON.",
        transcript=(),
        tools=(),
        output_schema={"type": "object"},
        metadata={"step": 1},
    )

    async def scenario():
        await driver.start_run(context)
        await driver.decide(turn_input)
        await driver.decide(turn_input)
        await driver.close_run(context.run_id)

    asyncio.run(scenario())
    assert calls == [
        ("start", "AR-one-hidden-thread", str(agent_drivers.PROJECT_ROOT)),
        ("turn", "AR-one-hidden-thread"),
        ("turn", "AR-one-hidden-thread"),
        ("close", "AR-one-hidden-thread"),
    ]
