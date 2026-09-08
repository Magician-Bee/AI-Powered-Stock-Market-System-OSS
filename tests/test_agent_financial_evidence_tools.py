from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentRunContext
from open_stock_ai.agent_runtime.orchestrator import (
    _compact_replay_result,
    _reasoning_step_summary,
)
from stock_ai.agent_tools import StockAgentToolRegistry
from stock_ai.models import InstitutionalFlowItem, RevenueItem


def _context() -> AgentRunContext:
    return AgentRunContext(
        run_id="AR-financial-evidence",
        autonomy="advisory",
        symbols=("2330.TW",),
    )


def test_official_institutional_and_revenue_tools_are_host_executable(monkeypatch):
    institutional = InstitutionalFlowItem(
        trade_date="2026-08-07",
        symbol="2330.TW",
        name="台積電",
        foreign_buy=100,
        foreign_sell=40,
        foreign_net=60,
        foreign_dealer_buy=0,
        foreign_dealer_sell=0,
        foreign_dealer_net=0,
        trust_buy=30,
        trust_sell=10,
        trust_net=20,
        dealer_buy=12,
        dealer_sell=7,
        dealer_net=5,
        dealer_hedge_net=2,
        total_institutional_net=85,
        source="TWSE T86",
    )
    revenue = RevenueItem(
        report_date="2026-08-08",
        period="2026-07",
        symbol="2330.TW",
        name="台積電",
        industry="半導體業",
        current_revenue=1000,
        previous_revenue=900,
        last_year_revenue=800,
        mom_change_percent=11.1,
        yoy_change_percent=25.0,
        ytd_revenue=7000,
        last_ytd_revenue=6000,
        ytd_change_percent=16.7,
        source="TWSE OpenAPI",
        unit="thousand_twd",
    )
    monkeypatch.setattr(
        "stock_ai.agent_tools.list_institutional_flows",
        lambda **kwargs: [institutional],
    )
    monkeypatch.setattr(
        "stock_ai.agent_tools.list_monthly_revenues",
        lambda **kwargs: [revenue],
    )
    registry = StockAgentToolRegistry()

    flow_result = asyncio.run(
        registry.execute(
            "market.institutional_flow",
            {"symbol": "2330.TW", "limit": 10},
            _context(),
        )
    )
    revenue_result = asyncio.run(
        registry.execute(
            "market.monthly_revenue",
            {"symbol": "2330.TW", "limit": 12},
            _context(),
        )
    )

    names = {item["name"] for item in registry.manifest()}
    assert {"market.institutional_flow", "market.monthly_revenue"} <= names
    assert flow_result["data_status"] == "available"
    assert flow_result["items"][0]["total_institutional_net"] == 85
    assert revenue_result["data_status"] == "available"
    assert revenue_result["items"][0]["period"] == "2026-07"

    flow_summary = _reasoning_step_summary(
        node_title="法人",
        related_trace=[{"ok": True, "tool": "market.institutional_flow", "result": flow_result}],
        fallback="",
    )
    revenue_summary = _reasoning_step_summary(
        node_title="基本面",
        related_trace=[{"ok": True, "tool": "market.monthly_revenue", "result": revenue_result}],
        fallback="",
    )
    assert "外資淨額 60" in flow_summary
    assert "三大法人合計 85" in flow_summary
    assert "最新期間 2026-07" in revenue_summary
    assert "年增率 25.0%" in revenue_summary


def test_financial_evidence_receipts_are_bounded_for_the_next_model_turn():
    result = {
        "schema_version": "stock_ai.institutional_flow_evidence.v1",
        "symbol": "2330.TW",
        "count": 20,
        "data_status": "available",
        "items": [{"trade_date": f"2026-08-{day:02d}"} for day in range(1, 21)],
    }

    compact = _compact_replay_result({"ok": True, "result": result})

    assert compact is not None
    assert compact["count"] == 20
    assert len(compact["items"]) == 12


def test_empty_financial_source_is_explicitly_data_blocked(monkeypatch):
    monkeypatch.setattr("stock_ai.agent_tools.list_institutional_flows", lambda **kwargs: [])
    registry = StockAgentToolRegistry()

    result = asyncio.run(
        registry.execute(
            "market.institutional_flow",
            {"symbol": "2330.TW"},
            _context(),
        )
    )

    assert result["count"] == 0
    assert result["data_status"] == "no_data"
