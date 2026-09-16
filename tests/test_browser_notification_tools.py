from __future__ import annotations

import asyncio

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.tool_providers.browser import BrowserToolProvider, _validate_external_url
from stock_ai.tool_providers.notifications import NotificationToolProvider


def _context(*, elevated: bool = False) -> AgentRunContext:
    return AgentRunContext(
        run_id="AR-browser-notifications",
        autonomy="external_execute" if elevated else "advisory",
        symbols=(),
        allow_external_actions=elevated,
    )


def test_browser_provider_exposes_stateful_high_level_tools_and_blocks_private_networks():
    provider = BrowserToolProvider()
    manifest = {item["name"]: item for item in provider.manifest()}

    assert set(manifest) == {
        "browser.open",
        "browser.read_page",
        "browser.click",
        "browser.fill",
        "browser.wait_for",
        "browser.close",
    }
    assert manifest["browser.click"]["requires_external_execution"] is True
    assert manifest["browser.fill"]["requires_external_execution"] is True
    assert _validate_external_url("https://example.com/path") == "https://example.com/path"
    with pytest.raises(PermissionError, match="private"):
        _validate_external_url("http://127.0.0.1:8000")
    with pytest.raises(PermissionError, match="private"):
        _validate_external_url("http://localhost:8000")
    with pytest.raises(PermissionError, match="requires autonomy"):
        asyncio.run(provider.execute("browser.click", {"selector": "button"}, _context()))


def test_notification_provider_reads_status_and_requires_elevation_for_send():
    provider = NotificationToolProvider()

    channels = asyncio.run(provider.execute("notifications.channels", {}, _context()))

    assert channels["count"] == 2
    assert all("token" not in str(item).casefold() for item in channels["items"])
    with pytest.raises(PermissionError, match="requires autonomy"):
        asyncio.run(
            provider.execute(
                "notifications.send",
                {"title": "Test", "body": "Preview", "channels": ["telegram"], "dry_run": True},
                _context(),
            )
        )

    result = asyncio.run(
        provider.execute(
            "notifications.send",
            {"title": "Test", "body": "Preview", "channels": ["telegram"], "dry_run": True},
            _context(elevated=True),
        )
    )
    assert result["dry_run"] is True
    assert result["attempted_count"] == 0


@pytest.mark.skipif(
    __import__("os").getenv("STOCK_AI_RUN_BROWSER_E2E") != "1",
    reason="release smoke only: set STOCK_AI_RUN_BROWSER_E2E=1 on a workstation with Chrome",
)
def test_real_browser_opens_and_reads_javascript_capable_page():
    async def scenario():
        provider = BrowserToolProvider()
        context = AgentRunContext(run_id="AR-real-browser", autonomy="advisory", symbols=())
        try:
            opened = await provider.execute("browser.open", {"url": "https://example.com"}, context)
            page = await provider.execute("browser.read_page", {"max_chars": 5000}, context)
            assert opened["status_code"] == 200
            assert opened["javascript_enabled"] is True
            assert page["title"] == "Example Domain"
            assert "Example Domain" in page["text"]
            assert page["secret_values_returned"] is False
        finally:
            await provider.close_run(context.run_id)

    asyncio.run(scenario())
