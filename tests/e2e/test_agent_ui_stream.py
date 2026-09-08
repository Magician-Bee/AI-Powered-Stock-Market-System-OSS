from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentOrchestrator
from stock_ai.agent_ui_bridge import AgentUIBridge
from stock_ai.tool_providers.ui import UIToolProvider


class _UIDriver:
    driver_id = "ui-e2e"

    def __init__(self):
        self.turn = 0
        self.inputs = []

    def describe(self):
        return {"id": self.driver_id, "configured": True}

    async def decide(self, turn):
        self.inputs.append(turn)
        self.turn += 1
        if self.turn == 1:
            return {
                "state": "continue",
                "summary": "Navigate through the UI bridge",
                "tool_calls": [{"id": "ui-1", "name": "ui.navigate", "arguments": {"view": "settings"}}],
                "decision": None,
            }
        return {"state": "complete", "summary": "Settings is visible", "tool_calls": [], "decision": None}


def test_agent_stream_contains_real_ui_command_acknowledgement_and_result():
    async def scenario():
        bridge = AgentUIBridge()
        bridge.update_state({"current_view": "home", "current_symbol": "2330.TW"})
        provider = UIToolProvider(bridge)
        driver = _UIDriver()
        runtime = AgentOrchestrator(drivers={driver.driver_id: driver}, tools=provider, default_driver=driver.driver_id)
        events = []
        task = asyncio.create_task(runtime.run(objective="請操作介面切換設定頁", event_sink=events.append))
        for _ in range(100):
            commands = bridge.commands_after(0)
            if commands:
                break
            await asyncio.sleep(0.01)
        command = commands[0]
        bridge.update_state({"current_view": "settings", "current_symbol": "2330.TW"})
        bridge.complete(command["command_id"], {"ok": True, "result": {"current_view": "settings"}})
        result = await task
        return result, events, command, driver

    result, events, command, driver = asyncio.run(scenario())
    assert command["action"] == "navigate"
    assert result["status"] == "completed"
    completed = next(item for item in events if item["type"] == "tool.completed")
    assert completed["tool"] == "ui.navigate"
    assert completed["result_summary"]["schema_version"] == "open_stock_ai.ui_command_result.v1"
    # A validated UI bridge acknowledgement is now the terminal Host evidence
    # for a bounded UI task.  Do not ask the model for a redundant second turn:
    # compatible local models can otherwise repeat the same UI action until a
    # step or cost guard stops the Run.
    assert len(driver.inputs) == 1
    finalized = next(item for item in events if item["type"] == "completion.host_finalized")
    assert finalized["reason"] == "verified_ui_bridge_operation"
    assert result["summary"].startswith("Host 已完成並驗證介面操作：ui.navigate")
    assert result["tool_trace"][0]["result"]["acknowledgement"]["result"]["current_view"] == "settings"
    assert all("chain_of_thought" not in str(item) for item in events)
