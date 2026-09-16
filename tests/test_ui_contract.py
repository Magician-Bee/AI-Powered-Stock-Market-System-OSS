from __future__ import annotations

from pathlib import Path

from stock_ai.agent_ui_bridge import AgentUIBridge
from stock_ai.ui_contract import ACTION_GROUPS, UI_ACTIONS, ui_contract


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"


def test_ui_contract_exposes_every_frozen_action_with_operational_metadata() -> None:
    payload = ui_contract()
    expected = {action for actions in ACTION_GROUPS.values() for action in actions}

    assert payload["schema_version"] == "stock_ai.ui_contract.v1"
    assert payload["workspace_context_schema"] == "stock_ai.workspace_context.v2"
    assert set(UI_ACTIONS) == expected
    for action_id, action in UI_ACTIONS.items():
        assert action["action_id"] == action_id
        for field in (
            "input_schema", "output_schema", "target_ui_id", "context_schema",
            "risk", "permission", "approval", "timeout_seconds", "idempotency",
            "precondition", "postcondition", "rollback", "visible_summary",
        ):
            assert action[field]


def test_ui_bridge_exposes_nonblocking_command_status() -> None:
    bridge = AgentUIBridge()
    bridge.update_state({"current_view": "home"})

    command = bridge.enqueue("execute_action", {"action_id": "agent.dock.open", "input": {}})
    pending = bridge.command_status(command["command_id"])
    assert pending["status"] == "pending"

    bridge.complete(command["command_id"], {"ok": True, "result": {"dock_open": True}})
    completed = bridge.command_status(command["command_id"])
    assert completed["status"] == "completed"
    assert completed["acknowledgement"]["result"]["dock_open"] is True


def test_all_32_fixed_pages_and_home_have_one_static_root() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    navigation = (STATIC / "js" / "shell" / "navigation.js").read_text(encoding="utf-8")
    expected = {"home.root"}
    for workspace, tabs in {
        "market": "overview rankings watchlists screener monitor events",
        "instrument": "overview chart technical ownership financials valuation events evidence",
        "portfolio": "overview positions orders simulation performance risk",
        "research": "compare deep-research strategy-lab factor-lab relationships reports",
        "system": "securities data-platform brokers agent-models tools interface",
    }.items():
        expected.update(f"{workspace}.{tab}.root" for tab in tabs.split())

    for ui_id in expected:
        assert html.count(f'data-ui-id="{ui_id}"') == 1
    assert 'data-workspace-tab="overview chart"' not in html
    assert 'id="instrumentOverviewChartHost"' in html
    for forbidden in (
        "LEGACY_VIEW_ROUTES", "ROUTE_COPY", "routePanel(", "renderRoute(",
        "CONTEXT_SYMBOL_INPUT_IDS", "syncContextSymbolInputs",
    ):
        assert forbidden not in navigation
    assert "window.addEventListener('DOMContentLoaded', applyLocationRoute, { once: true })" in navigation
    assert "window.queueMicrotask(() => applyLocationRoute())" not in navigation
