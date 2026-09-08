from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_tools import StockAgentToolRegistry


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return [
            {
                "Date": "20260715",
                "ContractCode": "臺股期貨",
                "Item": "外資及陸資",
                "OpenInterest(Long)": "7319",
                "OpenInterest(Short)": "86876",
                "OpenInterest(Net)": "-79557",
            },
            {
                "Date": "20260715",
                "ContractCode": "小型臺指期貨",
                "Item": "外資及陸資",
                "OpenInterest(Long)": "2522",
                "OpenInterest(Short)": "1961",
                "OpenInterest(Net)": "561",
            },
        ]


def test_taifex_agent_tool_reads_official_latest_foreign_open_interest(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr("stock_ai.official_derivatives.httpx.get", fake_get)
    registry = StockAgentToolRegistry()
    context = AgentRunContext(run_id="TAIFEX-test", autonomy="advisory", symbols=())

    result = asyncio.run(
        registry.execute("market.taifex_foreign_open_interest", {"contract": "臺股期貨"}, context)
    )

    assert captured["url"].startswith("https://openapi.taifex.com.tw/v1/")
    assert result["source"]["official"] is True
    assert result["latest_trade_date"] == "2026-07-15"
    assert result["position"]["foreign_long"] == 7319
    assert result["position"]["foreign_short"] == 86876
    assert result["position"]["foreign_net"] == -79557
    assert result["position"]["net_short_contracts"] == 79557
    assert "79,557" in result["interpretation"]


def test_taifex_official_tool_is_visible_to_agents():
    item = next(
        tool for tool in StockAgentToolRegistry().manifest()
        if tool["name"] == "market.taifex_foreign_open_interest"
    )

    assert item["category"] == "market_research"
    assert item["skills"] == ["taifex-official-data"]
    assert item["packages"] == ["TAIFEX OpenAPI", "httpx"]
    assert item["mutating"] is False
