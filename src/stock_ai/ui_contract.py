from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .agent_ui_bridge import agent_ui_bridge
from .market_intelligence.service import get_market_intelligence_service


router = APIRouter(prefix="/api/ui", tags=["First-party UI Contract"])


ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "navigation": (
        "workspace.open", "workspace.tab.open", "entity.search", "entity.select",
        "route.back", "route.forward",
    ),
    "market": (
        "market.snapshot.refresh", "market.category.select", "market.candidate.select",
        "market.ranking.select", "market.event.open", "market.monitor.subscribe",
        "market.monitor.unsubscribe",
    ),
    "watchlists": (
        "watchlist.group.create", "watchlist.group.rename", "watchlist.symbol.add",
        "watchlist.symbol.remove", "watchlist.symbol.move", "alert.create", "alert.update",
        "alert.delete", "screener.condition.set", "screener.run", "screener.save",
        "screener.schedule",
    ),
    "chart": (
        "chart.type.set", "chart.timeframe.set", "chart.range.set", "chart.price_basis.set",
        "chart.indicator.toggle", "chart.indicator.configure", "chart.drawing.create",
        "chart.drawing.update", "chart.drawing.delete", "chart.annotation.undo",
        "chart.annotation.clear", "chart.viewport.reset", "chart.focus.enter",
        "chart.focus.exit", "chart.event_layer.toggle",
    ),
    "portfolio": (
        "portfolio.account.select", "portfolio.position.select", "portfolio.mark_to_market",
        "order.draft.create", "order.draft.update", "order.preview", "order.submit.paper",
        "order.cancel.paper", "simulation.account.reset",
    ),
    "research": (
        "research.compare.set", "research.compare.run", "research.deep_run.start",
        "research.strategy.save", "research.backtest.run", "research.factor.run",
        "research.relationship.run", "research.report.open", "research.report.export",
    ),
    "system": (
        "system.data.refresh", "system.cache.invalidate", "system.reconciliation.run",
        "system.broker.authorize", "system.broker.test", "system.provider.select",
        "system.provider.test", "system.provider.save", "system.tool.enable",
        "system.tool.disable", "system.tool.test", "system.interface.update",
        "system.interface.reset",
    ),
    "agent": (
        "agent.dock.open", "agent.dock.close", "agent.dock.resize", "agent.dock.maximize",
        "agent.dock.restore", "agent.tab.open", "agent.session.new", "agent.run.pause",
        "agent.run.resume", "agent.run.cancel", "agent.approval.approve",
        "agent.approval.deny", "agent.artifact.open",
    ),
}


def _target_for(action_id: str) -> str:
    prefix = action_id.split(".", 1)[0]
    return {
        "workspace": "shell.workspace.root",
        "route": "shell.workspace.root",
        "entity": "shell.entity-search",
        "market": "market.overview.root",
        "watchlist": "market.watchlists.root",
        "alert": "market.watchlists.root",
        "screener": "market.screener.root",
        "chart": "instrument.chart.root",
        "portfolio": "portfolio.overview.root",
        "order": "portfolio.orders.root",
        "simulation": "portfolio.simulation.root",
        "research": "research.compare.root",
        "system": "system.agent-models.root",
        "agent": "agent.dock.root",
    }.get(prefix, "shell.workspace.root")


def _risk_for(action_id: str) -> tuple[str, str]:
    if action_id in {"order.submit.paper", "simulation.account.reset", "system.cache.invalidate"}:
        return "high", "explicit"
    if action_id.endswith((".delete", ".remove", ".disable", ".cancel")):
        return "medium", "confirm"
    return "low", "none"


def _input_schema(action_id: str) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if action_id == "workspace.open":
        properties = {"workspace": {"type": "string", "enum": ["home", "market", "instrument", "portfolio", "research", "system"]}}
        required = ["workspace"]
    elif action_id == "workspace.tab.open":
        properties = {"workspace": {"type": "string"}, "tab": {"type": "string"}}
        required = ["workspace", "tab"]
    elif action_id in {"entity.select", "market.candidate.select", "watchlist.symbol.add", "watchlist.symbol.remove"}:
        properties = {"symbol": {"type": "string", "minLength": 1}}
        required = ["symbol"]
    elif action_id == "chart.type.set":
        properties = {"type": {"type": "string", "enum": ["candles", "bars", "line", "area", "heikin"]}}
        required = ["type"]
    elif action_id == "chart.indicator.toggle":
        properties = {"indicator": {"type": "string", "enum": ["ma5", "ma10", "ma20", "ma60", "boll", "volume", "macd"]}, "visible": {"type": "boolean"}}
        required = ["indicator"]
    elif action_id == "chart.range.set":
        properties = {"range": {"oneOf": [{"type": "integer", "minimum": 1}, {"const": "all"}]}}
        required = ["range"]
    elif action_id == "agent.dock.resize":
        properties = {"width": {"type": "integer", "minimum": 320, "maximum": 520}}
        required = ["width"]
    elif action_id == "agent.tab.open":
        properties = {"tab": {"type": "string", "enum": ["chat", "tasks", "artifacts"]}}
        required = ["tab"]
    return {"type": "object", "additionalProperties": True, "properties": properties, "required": required}


def action_contract(action_id: str, group: str) -> dict[str, Any]:
    risk, approval = _risk_for(action_id)
    return {
        "action_id": action_id,
        "group": group,
        "input_schema": _input_schema(action_id),
        "output_schema": {"type": "object", "required": ["ok", "result"]},
        "target_ui_id": _target_for(action_id),
        "context_schema": "stock_ai.workspace_context.v2",
        "risk": risk,
        "permission": "first_party_ui",
        "approval": approval,
        "timeout_seconds": 20,
        "idempotency": "caller_key_required_for_mutation",
        "precondition": "target UI exists and connected UI revision is current",
        "postcondition": "browser acknowledgement returns updated UI state",
        "rollback": "restore prior WorkspaceContext or use the corresponding undo action when available",
        "visible_summary": action_id.replace(".", " "),
    }


UI_ACTIONS = {
    action_id: action_contract(action_id, group)
    for group, action_ids in ACTION_GROUPS.items()
    for action_id in action_ids
}


class UIActionRequest(BaseModel):
    action_id: str = Field(min_length=3, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    context_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


@router.get("/contract")
def ui_contract() -> dict[str, Any]:
    return {
        "schema_version": "stock_ai.ui_contract.v1",
        "workspace_context_schema": "stock_ai.workspace_context.v2",
        "workspaces": ["home", "market", "instrument", "portfolio", "research", "system"],
        "actions": list(UI_ACTIONS.values()),
    }


@router.get("/state")
def ui_state() -> dict[str, Any]:
    snapshot = agent_ui_bridge.snapshot()
    return {
        "schema_version": "stock_ai.ui_state.v1",
        "connected": snapshot["connected"],
        "last_seen": snapshot["last_seen"],
        "workspace_context": get_market_intelligence_service().context().model_dump(mode="json"),
        "visible_ui": snapshot["state"],
    }


@router.post("/actions", status_code=202)
def execute_ui_action(request: UIActionRequest) -> dict[str, Any]:
    contract = UI_ACTIONS.get(request.action_id)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"unknown UI action: {request.action_id}")
    current = get_market_intelligence_service().context()
    if request.context_revision is not None and request.context_revision != current.revision:
        raise HTTPException(status_code=409, detail={"code": "stale_workspace_context", "current_revision": current.revision})
    if contract["risk"] != "low" and not request.idempotency_key:
        raise HTTPException(status_code=422, detail="idempotency_key is required for this action")
    try:
        command = agent_ui_bridge.enqueue(
            "execute_action",
            {
                "action_id": request.action_id,
                "input": request.input,
                "context_revision": current.revision,
                "idempotency_key": request.idempotency_key,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "schema_version": "stock_ai.ui_action_receipt.v1",
        "status": "pending",
        "command_id": command["command_id"],
        "action": contract,
    }


@router.get("/actions/{command_id}")
def ui_action_status(command_id: int) -> dict[str, Any]:
    try:
        return agent_ui_bridge.command_status(command_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown UI command") from exc
