from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.capability_registry import CapabilityRegistry
from stock_ai.agent_tools import StockAgentToolRegistry
from stock_ai.tool_providers.mcp import MCPToolProvider
from stock_ai.tool_providers.skills import SkillToolProvider


def _context() -> AgentRunContext:
    return AgentRunContext(run_id="AR-capabilities", autonomy="advisory", symbols=())


def test_skill_provider_loads_real_skill_instructions_into_run_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    skill_dir = tmp_path / "skills" / "market-review"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: market-review\ndescription: Verify market evidence.\n---\nRead official data first.\n",
        encoding="utf-8",
    )
    provider = SkillToolProvider(tmp_path)
    context = _context()

    result = asyncio.run(provider.execute("skills.activate", {"name": "market-review"}, context))

    assert "Read official data first" in result["instructions"]
    assert result["execution_boundary"] == "instructions_only_host_capability_permissions_still_apply"
    assert context.state["active_skills"] == ["market-review"]
    assert provider.describe()["runtime_ready"] is True


def test_mcp_provider_binds_live_schemas_and_executes_through_host_bridge():
    class Runtime:
        async def mcp_inventory(self):
            return {
                "data": [
                    {
                        "name": "openaiDeveloperDocs",
                        "authStatus": "authenticated",
                        "tools": {
                            "lookup": {
                                "name": "lookup",
                                "description": "Look up a verified record.",
                                "annotations": {"readOnlyHint": True, "destructiveHint": False},
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {"query": {"type": "string"}},
                                },
                            }
                        },
                    }
                ]
            }

        async def mcp_call(self, **kwargs):
            return {"structuredContent": {"echo": kwargs}}

    provider = MCPToolProvider(runtime=Runtime())
    context = _context()

    async def scenario():
        await provider.prepare(context)
        manifest = provider.manifest()
        assert manifest[0]["name"] == "mcp.openaideveloperdocs.lookup"
        assert manifest[0]["input_schema"]["required"] == ["query"]
        result = await provider.execute(
            "mcp.openaideveloperdocs.lookup",
            {"query": "2330"},
            context,
        )
        assert result["server"] == "openaiDeveloperDocs"
        assert result["result"]["structuredContent"]["echo"]["run_id"] == "AR-capabilities"

    asyncio.run(scenario())
    status = provider.describe()
    assert status["schema_discovery"] is True
    assert status["host_execution_binding"] is True
    assert status["servers"][0]["bound_tool_count"] == 1


def test_mcp_provider_binds_connector_mutations_with_explicit_risk_tiers():
    class Runtime:
        async def mcp_inventory(self):
            return {
                "data": [
                    {
                        "name": "codex_apps",
                        "authStatus": "authenticated",
                        "tools": {
                            "drive.read": {
                                "name": "drive.read",
                                "annotations": {"readOnlyHint": True, "destructiveHint": False},
                                "inputSchema": {"type": "object"},
                            },
                            "gmail.send": {
                                "name": "gmail.send",
                                "annotations": {"readOnlyHint": False, "destructiveHint": False},
                                "inputSchema": {"type": "object"},
                            },
                            "drive.delete": {
                                "name": "drive.delete",
                                "annotations": {"readOnlyHint": False, "destructiveHint": True},
                                "inputSchema": {"type": "object"},
                            },
                        },
                    }
                ]
            }

        async def mcp_call(self, **kwargs):
            return {"ok": True, "tool": kwargs["tool"]}

    provider = MCPToolProvider(runtime=Runtime())

    async def scenario():
        advisory = AgentRunContext(run_id="AR-mcp", autonomy="advisory", symbols=())
        await provider.prepare(advisory)
        specs = {item["name"]: item for item in provider.manifest()}
        assert specs["mcp.codex_apps.drive_read"]["mutating"] is False
        assert specs["mcp.codex_apps.gmail_send"]["requires_external_execution"] is True
        assert specs["mcp.codex_apps.drive_delete"]["requires_full_execution"] is True

        with __import__("pytest").raises(PermissionError, match="external_execute"):
            await provider.execute("mcp.codex_apps.gmail_send", {}, advisory)
        external = AgentRunContext(
            run_id="AR-mcp",
            autonomy="external_execute",
            symbols=(),
            allow_external_actions=True,
        )
        assert (await provider.execute("mcp.codex_apps.gmail_send", {}, external))["result"]["ok"] is True
        with __import__("pytest").raises(PermissionError, match="full_execute"):
            await provider.execute("mcp.codex_apps.drive_delete", {}, external)
        full = AgentRunContext(
            run_id="AR-mcp",
            autonomy="full_execute",
            symbols=(),
            allow_external_actions=True,
            allow_project_actions=True,
            allow_paper_orders=True,
        )
        assert (await provider.execute("mcp.codex_apps.drive_delete", {}, full))["result"]["ok"] is True

    asyncio.run(scenario())


def test_capability_registry_routes_tools_and_reports_inventory_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))
    provider = SkillToolProvider(tmp_path)
    registry = CapabilityRegistry((provider,))

    description = registry.describe()

    assert description["execution_owner"] == "host"
    assert description["inventory_is_not_execution"] is True
    assert description["providers"][0]["id"] == "skills"


def test_stock_registry_includes_browser_notifications_and_scheduler():
    registry = StockAgentToolRegistry()
    description = registry.describe_capabilities()
    providers = {item["id"]: item for item in description["providers"]}

    assert {"browser", "notifications", "scheduler"}.issubset(providers)
    assert "browser.open" in providers["browser"]["tools"]
    assert "notifications.send" in providers["notifications"]["tools"]
    assert "schedule.create" in providers["scheduler"]["tools"]
