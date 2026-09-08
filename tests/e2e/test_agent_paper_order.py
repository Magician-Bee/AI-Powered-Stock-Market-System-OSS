from __future__ import annotations

import asyncio

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_tools import StockAgentToolRegistry


def test_agent_paper_order_requires_exact_current_preview_and_broker_validation(monkeypatch):
    registry = StockAgentToolRegistry()
    context = AgentRunContext(
        run_id="AR-paper-e2e",
        autonomy="paper_execute",
        symbols=("2330.TW",),
        allow_paper_orders=True,
    )
    submitted = []
    preview_result = {
        "schema_version": "preview.v1",
        "status": "ok",
        "can_submit": True,
        "market": {"price": 100.0, "source_envelope": {"signature": "signed-current-source"}},
        "risk_advisory": {"approved": True},
    }
    monkeypatch.setattr("stock_ai.agent_tools.paper_training_preview", lambda request: preview_result)
    monkeypatch.setattr(
        "stock_ai.agent_tools.paper_training_order",
        lambda request: submitted.append(request) or {"schema_version": "execution.v1", "status": "filled"},
    )
    order = {"symbol": "2330.TW", "side": "buy", "quantity_shares": 10, "rationale": "e2e"}

    with pytest.raises(PermissionError, match="must be previewed"):
        asyncio.run(registry.execute("paper.submit_order", order, context))
    preview = asyncio.run(registry.execute("paper.preview_order", order, context))
    execution = asyncio.run(registry.execute("paper.submit_order", order, context))

    assert preview["market"]["source_envelope"]["signature"] == "signed-current-source"
    assert execution["status"] == "filled"
    assert submitted[0].actor == "agent"
    assert registry.describe_capabilities()["inventory_is_not_execution"] is True
