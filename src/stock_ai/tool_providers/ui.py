from __future__ import annotations

from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec

from ..agent_ui_bridge import AgentUIBridge, agent_ui_bridge


class UIToolProvider:
    provider_id = "ui"

    def __init__(self, bridge: AgentUIBridge = agent_ui_bridge) -> None:
        self.bridge = bridge
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="ui.get_state",
                    description="Read the current browser or native WebView state from the built-in UI bridge.",
                    category="ui",
                    packages=("UI command bridge",),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
                AgentToolSpec(
                    name="ui.navigate",
                    description="Navigate the connected Stock AI UI to a named application view.",
                    category="ui",
                    mutating=True,
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["view"],
                        "properties": {"view": {"type": "string"}},
                    },
                ),
                AgentToolSpec(
                    name="ui.select_symbol",
                    description="Select a symbol in the connected Stock AI UI without browser automation.",
                    category="ui",
                    mutating=True,
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol"],
                        "properties": {"symbol": {"type": "string"}},
                    },
                ),
                AgentToolSpec(
                    name="ui.open_panel",
                    description="Open an allowed UI panel such as Agent activity or Agent settings.",
                    category="ui",
                    mutating=True,
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["panel"],
                        "properties": {
                            "panel": {"type": "string", "enum": ["agent_activity", "agent_settings", "paper_training"]}
                        },
                    },
                ),
                AgentToolSpec(
                    name="ui.fill_order",
                    description=(
                        "Fill paper-order form fields only. This never submits an order; paper.submit_order remains the "
                        "only Agent execution path and still requires preview plus RiskEngine evidence."
                    ),
                    category="ui",
                    mutating=True,
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol", "side"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "side": {"type": "string", "enum": ["buy", "sell", "add", "reduce"]},
                            "amount": {"type": "number", "exclusiveMinimum": 0},
                            "rationale": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="ui.submit_action",
                    description="Submit one non-financial UI action. Order submission is deliberately not allowed here.",
                    category="ui",
                    mutating=True,
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["action"],
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["refresh_view", "open_agent_activity", "close_agent_activity"],
                            }
                        },
                    },
                ),
                AgentToolSpec(
                    name="ui.wait_for_state",
                    description="Wait until an exact top-level UI state value is observed or timeout expires.",
                    category="ui",
                    packages=("UI command bridge",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["key", "equals"],
                        "properties": {
                            "key": {"type": "string"},
                            "equals": {},
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
                        },
                    },
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        del context
        if name == "ui.get_state":
            return self.bridge.snapshot()
        if name == "ui.wait_for_state":
            return await self.bridge.wait_for_state(
                str(arguments.get("key") or ""),
                arguments.get("equals"),
                timeout_seconds=float(arguments.get("timeout_seconds") or 20),
            )
        if name not in self._specs:
            raise ValueError(f"Unknown UI capability: {name}")
        return await self.bridge.dispatch(name.removeprefix("ui."), arguments)

    def describe(self) -> dict[str, Any]:
        snapshot = self.bridge.snapshot()
        return {
            "configured": True,
            "runtime_ready": snapshot["connected"],
            "health": "connected" if snapshot["connected"] else "waiting_for_ui",
            "transport": "backend_action_bus",
            "fixed_port_required": False,
            "browser_automation_required": False,
            "last_seen": snapshot["last_seen"],
        }
