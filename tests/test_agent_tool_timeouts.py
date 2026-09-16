from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_tools import StockAgentToolRegistry


def test_security_master_agent_tool_reports_a_bounded_timeout(monkeypatch) -> None:
    async def timeout(awaitable, *, timeout):
        assert timeout > 0
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr("stock_ai.agent_tools.asyncio.wait_for", timeout)
    registry = StockAgentToolRegistry()
    context = AgentRunContext(run_id="master-timeout", autonomy="advisory", symbols=())

    try:
        asyncio.run(
            registry.execute(
                "market.search_taiwan_securities",
                {"query": "6603", "exchange": "tpex", "refresh": True},
                context,
            )
        )
    except TimeoutError as error:
        assert "逾時" in str(error)
    else:
        raise AssertionError("The master lookup must report a bounded timeout")
