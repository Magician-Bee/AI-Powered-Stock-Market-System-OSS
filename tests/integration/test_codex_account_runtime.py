from __future__ import annotations

import asyncio
import os

import pytest

from stock_ai.codex_runtime import CodexRuntime


@pytest.mark.skipif(
    os.getenv("STOCK_AI_RUN_CODEX_E2E") != "1",
    reason="release smoke only: requires the workstation's authenticated ChatGPT/Codex account",
)
def test_real_codex_account_can_open_hidden_turn_and_read_live_mcp_inventory():
    async def scenario():
        runtime = CodexRuntime()
        try:
            account = await runtime.account_status(refresh=True)
            assert account["authenticated"] is True
            inventory = await runtime.mcp_inventory()
            assert isinstance(inventory.get("data"), list)
            result = await runtime.run_agent_turn(
                "Return JSON with ok=true. Do not call tools.",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean", "const": True}},
                },
                run_id="AR-live-account-smoke",
            )
            assert result == {"ok": True}
            await runtime.close_agent_run("AR-live-account-smoke")
        finally:
            await runtime.close()

    asyncio.run(scenario())
