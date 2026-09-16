from __future__ import annotations

import re
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec

from ..codex_runtime import CodexRuntime, codex_runtime


class MCPToolProvider:
    """Discover and execute live MCP schemas through the local Codex App Server."""

    provider_id = "mcp"

    def __init__(self, runtime: CodexRuntime = codex_runtime) -> None:
        self.runtime = runtime
        self._specs: dict[str, AgentToolSpec] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._servers: list[dict[str, Any]] = []
        self._error: str | None = None
        self._prepared = False

    async def prepare(self, context: AgentRunContext) -> None:
        del context
        try:
            inventory = await self.runtime.mcp_inventory()
            self._bind_inventory(inventory)
        except Exception as exc:
            self._specs = {}
            self._routes = {}
            self._servers = []
            self._error = str(exc)
            self._prepared = True

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._routes

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        route = self._routes.get(name)
        if route is None:
            raise ValueError(f"MCP tool is not healthy or was not discovered for this run: {name}")
        spec = self._specs[name]
        if spec.requires_full_execution and context.autonomy != "full_execute":
            raise PermissionError(f"{name} is destructive and requires autonomy=full_execute")
        if spec.requires_external_execution and not context.allow_external_actions:
            raise PermissionError(f"{name} requires autonomy=external_execute or full_execute")
        server, tool = route
        result = await self.runtime.mcp_call(
            run_id=context.run_id,
            server=server,
            tool=tool,
            arguments=arguments,
        )
        return {
            "schema_version": "open_stock_ai.mcp_execution.v1",
            "server": server,
            "tool": tool,
            "host_tool": name,
            "result": result,
            "execution_owner": "codex_app_server_mcp_bridge",
        }

    def describe(self) -> dict[str, Any]:
        specs = list(self._specs.values())
        return {
            "configured": bool(self._servers) or self._prepared,
            "runtime_ready": bool(self._specs),
            "health": "ready" if self._specs else ("error" if self._error else "not_prepared"),
            "server_count": len(self._servers),
            "servers": self._servers,
            "schema_discovery": self._prepared,
            "host_execution_binding": bool(self._specs),
            "read_only_tool_count": sum(1 for spec in specs if not spec.mutating),
            "external_mutation_tool_count": sum(1 for spec in specs if spec.requires_external_execution),
            "destructive_full_execute_tool_count": sum(1 for spec in specs if spec.requires_full_execution),
            "arbitrary_code_servers_blocked": ["node_repl", "computer-use"],
            "error": self._error,
        }

    def _bind_inventory(self, inventory: dict[str, Any]) -> None:
        specs: dict[str, AgentToolSpec] = {}
        routes: dict[str, tuple[str, str]] = {}
        servers: list[dict[str, Any]] = []
        for server in inventory.get("data") or []:
            if not isinstance(server, dict):
                continue
            server_name = str(server.get("name") or "").strip()
            auth_status = str(server.get("authStatus") or server.get("auth_status") or "unknown")
            tools = server.get("tools") or {}
            bound = 0
            blocked = 0
            if server_name and isinstance(tools, dict):
                for key, raw_tool in tools.items():
                    tool = raw_tool if isinstance(raw_tool, dict) else {}
                    tool_name = str(tool.get("name") or key).strip()
                    if not tool_name:
                        continue
                    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
                    if not self._bindable(server_name, tool_name, annotations):
                        blocked += 1
                        continue
                    host_name = _host_tool_name(server_name, tool_name)
                    if host_name in specs:
                        continue
                    schema = tool.get("inputSchema") or tool.get("input_schema") or {
                        "type": "object",
                        "additionalProperties": True,
                    }
                    if not isinstance(schema, dict):
                        schema = {"type": "object", "additionalProperties": True}
                    read_only = annotations.get("readOnlyHint") is True
                    destructive = annotations.get("destructiveHint") is True
                    specs[host_name] = AgentToolSpec(
                        name=host_name,
                        description=str(tool.get("description") or tool.get("title") or f"MCP {server_name}/{tool_name}"),
                        category="mcp",
                        mutating=not read_only,
                        destructive=destructive,
                        requires_external_execution=not read_only and not destructive,
                        requires_full_execution=destructive,
                        packages=(f"MCP:{server_name}",),
                        input_schema=schema,
                    )
                    routes[host_name] = (server_name, tool_name)
                    bound += 1
            servers.append(
                {
                    "name": server_name,
                    "auth_status": auth_status,
                    "discovered_tool_count": len(tools) if isinstance(tools, dict) else 0,
                    "bound_tool_count": bound,
                    "blocked_tool_count": blocked,
                }
            )
        self._specs = specs
        self._routes = routes
        self._servers = servers
        self._error = None
        self._prepared = True

    def _bindable(self, server: str, tool: str, annotations: dict[str, Any]) -> bool:
        if server == "node_repl" or server == "computer-use":
            return False
        if server == "codex_apps":
            return True
        return server in {"openaiDeveloperDocs", "sites-design-picker"} and annotations.get("readOnlyHint") is True


def _host_tool_name(server: str, tool: str) -> str:
    normalize = lambda value: re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").casefold()
    return f"mcp.{normalize(server)}.{normalize(tool)}"
