from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from codex_cli_bin import bundled_codex_path
from open_stock_ai.analysis_contracts import AnalysisProvenance, ModelInvocationReceipt
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox
from openai_codex._run import _collect_async_turn_result
from openai_codex._approval_mode import _approval_mode_settings
from openai_codex._sandbox import _sandbox_mode
from openai_codex.generated.v2_all import (
    ConfigReadParams, ConfigReadResponse, ListMcpServerStatusResponse,
    McpServerToolCallResponse, ReasoningEffort, ThreadStartParams,
)

from .approval_policy import ApprovalPolicy, ScopedApprovalGrant


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MACOS_CODEX_APP_BINARIES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)


class CodexAuthenticationRequired(RuntimeError):
    pass


@dataclass(slots=True)
class PendingLogin:
    login_id: str
    flow: str
    status: str = "pending"
    error: str | None = None


class CodexRuntime:
    """One local Codex app-server shared by the FastAPI process."""

    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root.resolve()
        self._client: AsyncCodex | None = None
        self._client_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._thread: AsyncThread | None = None
        self._agent_threads: dict[str, AsyncThread] = {}
        self._agent_session_metadata: dict[str, dict[str, Any]] = {}
        self._llm_bridge_threads: dict[str, AsyncThread] = {}
        self._llm_bridge_metadata: dict[str, dict[str, Any]] = {}
        self._agent_threads_lock = asyncio.Lock()
        self._pending_logins: dict[str, PendingLogin] = {}
        self._login_tasks: set[asyncio.Task[Any]] = set()
        self._approval_events: deque[dict[str, Any]] = deque(maxlen=40)
        self._computer_use_authorized = False
        self._approval_policy = ApprovalPolicy()
        self._active_approval_grant: ScopedApprovalGrant | None = None
        self._last_turn_trace: list[dict[str, Any]] = []

    async def client(self) -> AsyncCodex:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                config = CodexConfig(
                    codex_bin=str(_resolve_codex_bin()),
                    cwd=str(self.project_root),
                    config_overrides=(
                        "features.computer_use=true",
                        "features.browser_use=true",
                        "features.browser_use_external=true",
                        "features.browser_use_full_cdp_access=true",
                        "features.in_app_browser=true",
                        "features.plugins=true",
                    ),
                    client_name="stock_ai_system",
                    client_title="Stock AI System",
                    client_version="0.2.0",
                    experimental_api=True,
                )
                self._client = AsyncCodex(config)
                # The beta Python SDK only handles command/file approvals. App tools such
                # as Computer Use ask through item/tool/requestUserInput, so the host must
                # provide the response while that UI operation is explicitly authorized.
                self._client._client._sync._approval_handler = self._handle_server_request
                await self._client.account(refresh_token=False)
        return self._client

    async def close(self) -> None:
        for task in tuple(self._login_tasks):
            task.cancel()
        if self._login_tasks:
            await asyncio.gather(*self._login_tasks, return_exceptions=True)
        self._login_tasks.clear()
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._thread = None
        self._agent_threads.clear()
        self._agent_session_metadata.clear()
        self._llm_bridge_threads.clear()
        self._llm_bridge_metadata.clear()

    async def account_status(self, refresh: bool = False) -> dict[str, Any]:
        client = await self.client()
        response = await client.account(refresh_token=refresh)
        account = response.account
        payload = _to_dict(account) if account is not None else {}
        return {
            "authenticated": account is not None,
            "account_type": payload.get("type"),
            "email": payload.get("email"),
            "plan_type": _enum_value(payload.get("plan_type")),
            "requires_openai_auth": response.requires_openai_auth,
            "runtime": "codex-app-server",
            "project_root": str(self.project_root),
            "computer_use_ready": True,
        }

    def capability_status(self) -> dict[str, Any]:
        return {
            "computer_use_ready": True,
            "authorized_now": self._computer_use_authorized,
            "approval_events": list(self._approval_events),
            "last_turn_trace": self._last_turn_trace,
        }

    async def start_browser_login(self) -> dict[str, Any]:
        handle = await (await self.client()).login_chatgpt()
        pending = PendingLogin(login_id=handle.login_id, flow="browser")
        self._pending_logins[pending.login_id] = pending
        self._track_login(handle, pending)
        return {
            "login_id": handle.login_id,
            "flow": pending.flow,
            "auth_url": handle.auth_url,
            "status": pending.status,
        }

    async def start_device_login(self) -> dict[str, Any]:
        handle = await (await self.client()).login_chatgpt_device_code()
        pending = PendingLogin(login_id=handle.login_id, flow="device_code")
        self._pending_logins[pending.login_id] = pending
        self._track_login(handle, pending)
        return {
            "login_id": handle.login_id,
            "flow": pending.flow,
            "verification_url": handle.verification_url,
            "user_code": handle.user_code,
            "status": pending.status,
        }

    def login_status(self, login_id: str) -> dict[str, Any]:
        pending = self._pending_logins.get(login_id)
        if pending is None:
            return {"login_id": login_id, "status": "unknown", "error": "找不到登入工作階段。"}
        return {
            "login_id": pending.login_id,
            "flow": pending.flow,
            "status": pending.status,
            "error": pending.error,
        }

    async def logout(self) -> None:
        await (await self.client()).logout()
        self._thread = None
        self._agent_threads.clear()
        self._agent_session_metadata.clear()
        self._llm_bridge_threads.clear()
        self._llm_bridge_metadata.clear()
        self._native_thread_state_path().unlink(missing_ok=True)

    async def run(
        self,
        prompt: str,
        *,
        view: str | None = None,
        symbol: str | None = None,
        explain: bool = False,
        computer_use: bool = False,
        allow_project_changes: bool = False,
    ) -> dict[str, Any]:
        await self._require_account()
        model_call_id = f"codex-native-{uuid4().hex}"
        model_started_at = datetime.now(timezone.utc).isoformat()
        context = {
            "current_view": view or "home",
            "current_symbol": str(symbol).strip().upper() if str(symbol or "").strip() else None,
            "symbol_source": "user_explicit" if str(symbol or "").strip() else "none",
            "answer_mode": "include_reason" if explain else "direct_only",
            "computer_use_requested": computer_use,
            "project_changes_authorized": allow_project_changes,
        }
        instruction = (
            f"目前軟體狀態：{json.dumps(context, ensure_ascii=False)}\n"
            f"使用者要求：{prompt.strip()}"
        )
        if computer_use:
            instruction += (
                "\n這是開發者除錯路徑。股市系統自己的 UI 應由 /api/agents 的 ui.* command bridge 操作，"
                "不得假設 Chrome、固定 8000 port 或特定 WebView；只有外部應用程式才使用 Computer Use。"
            )
        async with self._turn_lock:
            thread = await self._project_thread()
            self._computer_use_authorized = computer_use
            self._active_approval_grant = (
                self._approval_policy.issue(
                    run_id=f"codex-native:{thread.id}",
                    capabilities={"native.command", "native.file_change"},
                    resource_root=self.project_root,
                )
                if allow_project_changes
                else None
            )
            try:
                result = await thread.run(
                    instruction,
                    cwd=str(self.project_root),
                    sandbox=Sandbox.full_access,
                    approval_mode=ApprovalMode.auto_review,
                )
            finally:
                self._computer_use_authorized = False
                self._active_approval_grant = None
        traces = [_turn_item_trace(item) for item in result.items]
        tool_item_types = {
            "mcpToolCall",
            "commandExecution",
            "fileChange",
            "webSearch",
            "imageView",
            "dynamicToolCall",
        }
        self._last_turn_trace = [trace for trace in traces if trace["type"] in tool_item_types]
        runtime_status = _enum_value(getattr(result, "status", None)) or "completed"
        succeeded = runtime_status.casefold() in {"completed", "complete", "succeeded", "success"}
        model_receipt = ModelInvocationReceipt(
            call_id=model_call_id,
            provider="codex",
            model_id="codex-app-server/default",
            status="succeeded" if succeeded else "failed",
            started_at=model_started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_type=None if succeeded else "CodexTurnStatus",
            error_message=None if succeeded else runtime_status,
            raw_output_preserved=True,
        )
        provenance = AnalysisProvenance(
            origin="model",
            provider=model_receipt.provider,
            model_id=model_receipt.model_id,
            model_call_id=model_receipt.call_id,
            model_call_succeeded=succeeded,
            universe_source="explicit_symbols" if context["current_symbol"] else "none",
            symbols_considered=[context["current_symbol"]] if context["current_symbol"] else [],
            data_sources=["codex_native_context"],
            data_ready=True,
        )
        return {
            "thread_id": thread.id,
            "response": result.final_response,
            "status": runtime_status,
            "view": context["current_view"],
            "symbol": context["current_symbol"],
            "tools_used": self._last_turn_trace,
            "model_invocation": model_receipt.model_dump(),
            "provenance": provenance.model_dump(),
        }

    async def run_structured(self, prompt: str, output_schema: dict[str, Any]) -> dict[str, Any]:
        """Legacy structured request used by the dedicated market-radar endpoint."""
        return await self._run_embedded_structured_turn(
            prompt,
            output_schema,
            service_name="stock_ai_market_radar",
            developer_instructions=(
                "只根據使用者提供的市場資料輸出符合 JSON Schema 的結果。"
                "不得把未通過風控的訊號標為可立即買進；不得加入 schema 以外的說明。"
            ),
        )

    async def model_catalog(self) -> dict[str, Any]:
        """Read the signed-in account's current model catalog, without a model turn."""
        await self._require_account()
        client = await self.client()
        response = await client.models()
        items = []
        for model in response.data:
            payload = _to_dict(model)
            items.append({
                "id": payload["id"],
                "model": payload["model"],
                "display_name": payload["display_name"],
                "description": payload.get("description", ""),
                "default_reasoning_effort": _enum_value(payload.get("default_reasoning_effort")),
                "supported_reasoning_efforts": [
                    {
                        "reasoning_effort": _enum_value(_to_dict(option).get("reasoning_effort")),
                        "description": _to_dict(option).get("description", ""),
                    }
                    for option in payload.get("supported_reasoning_efforts", [])
                ],
                "is_default": bool(payload.get("is_default")),
            })
        return {
            "schema_version": "open_stock_ai.codex_models.v1",
            "provider": "codex",
            "items": items,
            "count": len(items),
            "next_cursor": response.next_cursor,
            "defaults": await self._configured_model_defaults(),
        }

    async def _configured_model_defaults(self) -> dict[str, Any]:
        client = await self.client()
        await client._ensure_initialized()
        response = await client._client.request(
            "config/read",
            ConfigReadParams(cwd=str(self.project_root), include_layers=False).model_dump(mode="json", by_alias=True, exclude_none=True),
            response_model=ConfigReadResponse,
        )
        # Only these two non-secret fields leave the effective Codex config.
        return {
            "model": response.config.model,
            "reasoning_effort": _enum_value(response.config.model_reasoning_effort),
        }

    async def validate_model_selection(self, model: str = "", reasoning_effort: str = "") -> dict[str, str]:
        model, reasoning_effort = model.strip(), reasoning_effort.strip()
        if not model and not reasoning_effort:
            return {"model": "", "reasoning_effort": ""}
        catalog = await self.model_catalog()
        defaults = catalog["defaults"]
        resolved_model = model or defaults.get("model")
        candidate = next(
            (item for item in catalog["items"] if item["model"] == resolved_model), None
        ) if resolved_model else next(
            (item for item in catalog["items"] if item["is_default"]), None
        )
        if candidate is None:
            raise ValueError(f"Codex 模型不在目前帳號的可用清單：{resolved_model or '(default)'}")
        effort = reasoning_effort or (
            candidate["default_reasoning_effort"] if model
            else defaults.get("reasoning_effort") or candidate["default_reasoning_effort"]
        )
        supported = [option["reasoning_effort"] for option in candidate["supported_reasoning_efforts"]]
        if effort and effort not in supported:
            raise ValueError(
                f"Codex 模型 {candidate['model']} 不支援推理強度 {effort}；可用值：{', '.join(supported)}"
            )
        return {"model": candidate["model"], "reasoning_effort": effort or ""}

    @staticmethod
    def _saved_model_selection() -> dict[str, str]:
        # Agent settings imports this runtime; defer the reverse lookup until use.
        from .agent_drivers import load_agent_driver_settings

        settings = load_agent_driver_settings()
        return {"model": settings.codex_model, "reasoning_effort": settings.codex_reasoning_effort}

    async def _start_configured_thread(
        self, *, model: str = "", reasoning_effort: str = "", **kwargs: Any,
    ) -> tuple[AsyncThread, dict[str, Any]]:
        resolved = await self.validate_model_selection(model, reasoning_effort)
        client = await self.client()
        await client._ensure_initialized()
        approval_policy, approvals_reviewer = _approval_mode_settings(
            kwargs.pop("approval_mode", ApprovalMode.deny_all)
        )
        # SDK's public thread_start discards ThreadStartResponse. Use its typed
        # lower-level method to retain the server-resolved model and effort.
        response = await client._client.thread_start(ThreadStartParams(
            **kwargs,
            model=resolved["model"] or None,
            config={"model_reasoning_effort": resolved["reasoning_effort"]} if resolved["reasoning_effort"] else None,
            sandbox=_sandbox_mode(Sandbox.read_only),
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
        ))
        actual_effort = _enum_value(response.reasoning_effort)
        if resolved["model"] and response.model != resolved["model"]:
            raise ValueError(f"Codex 未採用指定模型：要求 {resolved['model']}，回傳 {response.model}")
        if resolved["reasoning_effort"] and actual_effort != resolved["reasoning_effort"]:
            raise ValueError(f"Codex 未採用指定推理強度：要求 {resolved['reasoning_effort']}，回傳 {actual_effort}")
        metadata = {
            "provider": "codex",
            "model": response.model,
            "reasoning_effort": actual_effort,
            "model_provider": response.model_provider,
            "thread_id": response.thread.id,
            "resolution_source": "sdk_thread_start",
            "selected_model": model,
            "selected_reasoning_effort": reasoning_effort,
        }
        return AsyncThread(client, response.thread.id), metadata

    def agent_session_metadata(self, run_id: str) -> dict[str, Any]:
        return dict(self._agent_session_metadata.get(run_id) or {})

    async def start_agent_run(
        self,
        run_id: str,
        *,
        project_root: str | Path | None = None,
        model: str = "",
        reasoning_effort: str = "",
    ) -> AsyncThread:
        """Create one hidden planning thread and retain it for the complete host run."""
        bound_root = Path(project_root or self.project_root).expanduser().resolve()
        if bound_root != self.project_root:
            raise PermissionError(
                f"Codex Agent run project root mismatch: {bound_root} != {self.project_root}"
            )
        existing = self._agent_threads.get(run_id)
        if existing is not None:
            return existing
        await self._require_account()
        async with self._agent_threads_lock:
            existing = self._agent_threads.get(run_id)
            if existing is not None:
                return existing
            thread, metadata = await self._start_configured_thread(
                model=model,
                reasoning_effort=reasoning_effort,
                cwd=str(bound_root),
                approval_mode=ApprovalMode.deny_all,
                ephemeral=True,
                developer_instructions=(
                    "你是股市AI系統 UI 內的 Codex Agent 規劃驅動器，不是外部聊天轉送器。"
                    "你必須只回傳符合主系統 JSON Schema 的工具計畫或最終回答。所有專案、網站、MCP、"
                    "Skills、UI 與執行能力，都必須由主系統 Capability Registry 授權並實際執行；"
                    "沒有工具結果就不得聲稱已執行。禁止呼叫 /api/codex/run，禁止真實券商下單。"
                ),
                service_name="stock_ai_durable_agent_runtime",
            )
            self._agent_threads[run_id] = thread
            self._agent_session_metadata[run_id] = metadata
            return thread

    async def close_agent_run(self, run_id: str) -> None:
        """Release run-local model state only after the durable host run is terminal."""
        async with self._agent_threads_lock:
            self._agent_threads.pop(run_id, None)
            self._agent_session_metadata.pop(run_id, None)

    async def start_llm_bridge_run(self, run_id: str) -> AsyncThread:
        """Create one hidden text/tool planning thread for an external framework run."""
        existing = self._llm_bridge_threads.get(run_id)
        if existing is not None:
            return existing
        await self._require_account()
        async with self._agent_threads_lock:
            existing = self._llm_bridge_threads.get(run_id)
            if existing is not None:
                return existing
            # CodexLLMBridge keys are <host-run-id>:external-framework. Keep
            # frameworks first invoked after a settings change on that run's model.
            host_run_id = run_id.removesuffix(":external-framework")
            host_metadata = self._agent_session_metadata.get(host_run_id) or {}
            selection = {
                "model": str(host_metadata["model"]),
                "reasoning_effort": str(host_metadata.get("reasoning_effort") or ""),
            } if host_metadata.get("model") else self._saved_model_selection()
            thread, metadata = await self._start_configured_thread(
                **selection,
                cwd=str(self.project_root),
                approval_mode=ApprovalMode.deny_all,
                ephemeral=True,
                developer_instructions=(
                    "You are the Codex model driver embedded inside an audited external financial framework. "
                    "Return only the requested structured assistant content or tool-call plan. Never claim a tool ran "
                    "until its result appears in the supplied messages. Do not access the project, terminal, browser, "
                    "MCP, Computer Use, or live brokerage from this thread; the Stock AI host owns execution. "
                    "Never reveal private chain-of-thought."
                ),
                service_name="stock_ai_external_framework_codex_bridge",
            )
            self._llm_bridge_threads[run_id] = thread
            self._llm_bridge_metadata[run_id] = metadata
            return thread

    async def close_llm_bridge_run(self, run_id: str) -> None:
        async with self._agent_threads_lock:
            self._llm_bridge_threads.pop(run_id, None)
            self._llm_bridge_metadata.pop(run_id, None)

    async def run_llm_bridge_turn(
        self,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        """Drive one OpenAI-compatible framework request through a hidden Codex turn."""
        thread = await self.start_llm_bridge_run(run_id)
        offered_tools = list(tools or [])
        tool_names = [
            str((item.get("function") or {}).get("name") or "").strip()
            for item in offered_tools
            if isinstance(item, dict)
        ]
        tool_names = [name for name in tool_names if name]
        name_schema: dict[str, Any] = {"type": "string"}
        if tool_names:
            name_schema["enum"] = sorted(set(tool_names))
        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["content", "tool_calls"],
            "properties": {
                "content": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "tool_calls": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "arguments_json"],
                        "properties": {
                            "name": name_schema,
                            "arguments_json": {
                                "type": "string",
                                "description": "A JSON-encoded object matching the selected tool input schema.",
                            },
                        },
                    },
                },
            },
        }
        prompt = json.dumps(
            {
                "protocol": "open_stock_ai.codex_llm_bridge.v1",
                "messages": messages,
                "tools": offered_tools,
                "tool_choice": tool_choice,
                "requirements": {
                    "content_only_when_no_tool_is_needed": True,
                    "tool_calls_only_from_offered_tools": True,
                    "no_private_reasoning": True,
                },
            },
            ensure_ascii=False,
        )
        result = await self._run_embedded_structured_turn(
            prompt,
            output_schema,
            service_name="stock_ai_external_framework_codex_bridge",
            developer_instructions="",
            thread=thread,
            session_metadata=self._llm_bridge_metadata.get(run_id),
            event_sink=event_sink,
        )
        normalized_calls = []
        for item in result.get("tool_calls") or []:
            name = str(item.get("name") or "")
            if name not in tool_names:
                raise RuntimeError(f"Codex bridge selected an unavailable framework tool: {name}")
            try:
                arguments = json.loads(str(item.get("arguments_json") or "{}"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Codex bridge returned invalid arguments for {name}") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError(f"Codex bridge arguments must be an object: {name}")
            normalized_calls.append(
                {
                    "id": f"call_codex_{uuid4().hex[:20]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            )
        if tool_choice == "none" and normalized_calls:
            raise RuntimeError("Codex bridge returned a tool call while tool_choice=none")
        if tool_choice == "required" and offered_tools and not normalized_calls:
            raise RuntimeError("Codex bridge returned no tool call while tool_choice=required")
        if isinstance(tool_choice, dict):
            required_name = str((tool_choice.get("function") or {}).get("name") or "")
            if required_name and (
                not normalized_calls
                or any(call["function"]["name"] != required_name for call in normalized_calls)
            ):
                raise RuntimeError(f"Codex bridge did not honor required tool choice: {required_name}")
        return {
            "content": result.get("content"),
            "tool_calls": normalized_calls,
            "finish_reason": "tool_calls" if normalized_calls else "stop",
            "provider_model_metadata": dict(self._llm_bridge_metadata.get(run_id) or {}),
        }

    async def mcp_inventory(self) -> dict[str, Any]:
        """Read live MCP server health and tool schemas from the shared App Server."""
        await self._require_account()
        client = await self.client()
        await client._ensure_initialized()
        response = await client._client.request(
            "mcpServerStatus/list",
            {"detail": "toolsAndAuthOnly", "limit": 200},
            response_model=ListMcpServerStatusResponse,
        )
        return response.model_dump(mode="json", by_alias=True)

    async def mcp_call(
        self,
        *,
        run_id: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one discovered MCP tool through the host App Server bridge."""
        thread = await self.start_agent_run(run_id)
        client = await self.client()
        await client._ensure_initialized()
        response = await client._client.request(
            "mcpServer/tool/call",
            {
                "threadId": thread.id,
                "server": server,
                "tool": tool,
                "arguments": arguments,
            },
            response_model=McpServerToolCallResponse,
        )
        return response.model_dump(mode="json", by_alias=True)

    async def run_agent_turn(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        run_id: str | None = None,
        model: str = "",
        reasoning_effort: str = "",
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        """Run one hidden App Server turn for the UI-owned Agent tool loop.

        This deliberately does not call :meth:`run`: no visible/persistent Codex chat
        thread is created and no user task is copied into the native workspace.  Codex
        returns only a validated tool plan; the Stock AI host executes every requested
        shared tool and streams that activity back to the browser.
        """
        thread = await self.start_agent_run(run_id, model=model, reasoning_effort=reasoning_effort) if run_id else None
        return await self._run_embedded_structured_turn(
            prompt,
            output_schema,
            service_name="stock_ai_embedded_agent_runtime",
            developer_instructions=(
                "你是股市AI系統 UI 內的 Codex Agent 驅動器，不是外部聊天轉送器。"
                "只能回傳符合 JSON Schema 的工具計畫或最終回答。所有 Stock AI、專案、網站與執行工具"
                "均由同一個 UI 內的主系統執行，禁止要求或使用 /api/codex/run，也不得產生一般聊天內容。"
                "絕不把未通過 RiskEngine 的訊號標為可立即買進，也不執行真實券商下單。"
            ),
            thread=thread,
            session_metadata=self._agent_session_metadata.get(run_id or ""),
            model_selection={"model": model, "reasoning_effort": reasoning_effort},
            event_sink=event_sink,
        )

    async def _run_embedded_structured_turn(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        service_name: str,
        developer_instructions: str,
        thread: AsyncThread | None = None,
        session_metadata: dict[str, Any] | None = None,
        model_selection: dict[str, str] | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        await self._require_account()
        async with self._turn_lock:
            active_thread = thread
            if active_thread is None:
                active_thread, session_metadata = await self._start_configured_thread(
                    **(model_selection if model_selection is not None else self._saved_model_selection()),
                    cwd=str(self.project_root),
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=True,
                    developer_instructions=developer_instructions,
                    service_name=service_name,
                )
            if session_metadata and event_sink is not None:
                emitted = event_sink({"type": "model.session.configured", **session_metadata})
                if asyncio.iscoroutine(emitted):
                    await emitted
            turn_options: dict[str, Any] = {}
            if session_metadata:
                if session_metadata.get("model"):
                    turn_options["model"] = session_metadata["model"]
                if session_metadata.get("reasoning_effort"):
                    turn_options["effort"] = ReasoningEffort(session_metadata["reasoning_effort"])
            turn = await active_thread.turn(
                prompt,
                cwd=str(self.project_root),
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all,
                output_schema=output_schema,
                **turn_options,
            )

            async def audited_stream():
                stream = turn.stream()
                try:
                    async for notification in stream:
                        audit_event = _sdk_audit_event(notification)
                        if audit_event is not None and session_metadata:
                            audit_event.update({
                                "model": session_metadata.get("model"),
                                "reasoning_effort": session_metadata.get("reasoning_effort"),
                            })
                        if audit_event is not None and event_sink is not None:
                            emitted = event_sink(audit_event)
                            if asyncio.iscoroutine(emitted):
                                await emitted
                        yield notification
                finally:
                    await stream.aclose()

            result = await _collect_async_turn_result(audited_stream(), turn_id=turn.id)
        try:
            return json.loads(result.final_response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Codex 未回傳有效的結構化市場判斷。") from exc

    async def sync_project(self) -> dict[str, Any]:
        return await self.run(
            "完整讀取目前專案的 README、pyproject、config、src、tests、docs/integration 與外部專案鎖定清單。"
            "建立一份不超過六點的專案理解摘要，包含主入口、資料中樞、策略、研究、風控、執行與 UI。"
            "不要修改檔案。",
            view="home",
            explain=True,
        )

    async def require_account(self) -> dict[str, Any]:
        account = await self.account_status(refresh=False)
        if not account["authenticated"]:
            raise CodexAuthenticationRequired("請先登入 ChatGPT 帳號再使用 Codex。")
        return account

    _require_account = require_account

    def _handle_server_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        payload = params or {}
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            approved, reason = self._approval_policy.review(
                self._active_approval_grant,
                method=method,
                payload=payload,
            )
            self._approval_events.append(
                {
                    "method": method,
                    "decision": "accept" if approved else "decline",
                    "reason": reason,
                    "approval_id": getattr(self._active_approval_grant, "approval_id", None),
                    "computer_use_authorized": self._computer_use_authorized,
                    "questions": _approval_question_summary(payload),
                    "elicitation": _elicitation_summary(payload),
                }
            )
            return {"decision": "accept" if approved else "decline"}
        self._approval_events.append(
            {
                "method": method,
                "computer_use_authorized": self._computer_use_authorized,
                "questions": _approval_question_summary(payload),
                "elicitation": _elicitation_summary(payload),
            }
        )
        if method == "item/permissions/requestApproval" and self._computer_use_authorized:
            requested = payload.get("permissions") or payload.get("requestedPermissions") or {}
            return {"permissions": requested, "scope": "session"}
        if method == "item/tool/requestUserInput" and self._computer_use_authorized:
            answers: dict[str, dict[str, list[str]]] = {}
            for question in payload.get("questions") or []:
                question_id = str(question.get("id") or "")
                if not question_id:
                    continue
                choice = _approval_choice(question.get("options") or [])
                answers[question_id] = {"answers": [choice] if choice else []}
            return {"answers": answers}
        if method == "mcpServer/elicitation/request" and self._computer_use_authorized:
            if str(payload.get("mode") or "").casefold() == "url":
                return {"action": "accept", "content": None}
            return {
                "action": "accept",
                "content": _elicitation_content(payload.get("requestedSchema") or {}),
            }
        return {}

    async def _project_thread(self) -> AsyncThread:
        if self._thread is not None:
            return self._thread
        client = await self.client()
        resumed = await self._resume_native_thread(client)
        if resumed is not None:
            self._thread = resumed
            return resumed
        self._thread = await client.thread_start(
            cwd=str(self.project_root),
            sandbox=Sandbox.full_access,
            approval_mode=ApprovalMode.auto_review,
            base_instructions=(
                "你是直接運行在股市AI系統內的 Codex 核心，不是外掛聊天機器人。"
                "目前工作目錄就是完整專案；需要時直接讀取程式、設定、測試、資料與外部專案。"
                "此路徑只供開發者除錯；一般任務與本系統 UI 操作必須經 Durable Agent Runtime 和 ui.* bridge。"
            ),
            developer_instructions=(
                "預設使用繁體中文，先給結論，保持極短。"
                "投資判斷必須引用本專案即時資料與 OpenStockAIEngine 結果；"
                "只有使用者追問原因或要求說明時才展開理由。"
                "絕不把未通過 RiskEngine 的訊號稱為可立即買進，也不執行真實券商下單。"
                "不得假設固定 localhost port、Chrome 或特定原生 WebView。"
            ),
            service_name="stock_ai_embedded_codex",
        )
        await self._thread.set_name("股市AI系統 · 原生 Codex 工作階段")
        self._save_native_thread_id(self._thread.id)
        return self._thread

    def _native_thread_state_path(self) -> Path:
        return self.project_root / "output" / "codex_native_thread.json"

    async def _resume_native_thread(self, client: AsyncCodex) -> AsyncThread | None:
        path = self._native_thread_state_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            thread_id = str(payload.get("thread_id") or "").strip()
        except (OSError, json.JSONDecodeError):
            return None
        if not thread_id:
            return None
        try:
            return await client.thread_resume(
                thread_id,
                cwd=str(self.project_root),
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.auto_review,
            )
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _save_native_thread_id(self, thread_id: str) -> None:
        path = self._native_thread_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps({"thread_id": thread_id}) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)

    def _track_login(self, handle: Any, pending: PendingLogin) -> None:
        async def wait_for_completion() -> None:
            try:
                event = await handle.wait()
                event_payload = _to_dict(event)
                success = bool(event_payload.get("success"))
                pending.status = "completed" if success else "failed"
                pending.error = event_payload.get("error")
            except asyncio.CancelledError:
                pending.status = "cancelled"
                raise
            except Exception as exc:  # pragma: no cover - runtime transport failure
                pending.status = "failed"
                pending.error = str(exc)

        task = asyncio.create_task(wait_for_completion())
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _sdk_audit_event(notification: Any) -> dict[str, Any] | None:
    """Expose lifecycle/item metadata, never private reasoning text or text deltas."""
    method = str(getattr(notification, "method", "") or "")
    if method not in {
        "turn/started",
        "turn/completed",
        "item/started",
        "item/completed",
        "thread/tokenUsage/updated",
        "thread/compacted",
    }:
        return None
    payload = _to_dict(getattr(notification, "payload", None))
    event: dict[str, Any] = {
        "type": "model.sdk." + method.replace("/", "."),
        "sdk_event": method,
        "source": "codex_app_server",
    }
    item = payload.get("item")
    if item is not None:
        event["item"] = _turn_item_trace(item)
    turn = payload.get("turn")
    if turn is not None:
        turn_payload = _to_dict(turn)
        event["turn"] = {
            "id": turn_payload.get("id"),
            "status": _enum_value(turn_payload.get("status")),
            "duration_ms": turn_payload.get("duration_ms") or turn_payload.get("durationMs"),
        }
    usage = payload.get("token_usage") or payload.get("tokenUsage")
    if usage is not None:
        usage_payload = _to_dict(usage)
        allowed = {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        }
        event["usage"] = {key: value for key, value in usage_payload.items() if key in allowed}
    return event


def _turn_item_trace(item: Any) -> dict[str, Any]:
    payload = _to_dict(getattr(item, "root", item))
    trace = {
        "type": payload.get("type"),
        "id": payload.get("id"),
        "status": _enum_value(payload.get("status")),
    }
    for key in ("server", "tool", "command", "error"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            trace[key] = str(value)[:500]
    return trace


def _approval_choice(options: list[dict[str, Any]]) -> str | None:
    positive = ("accept", "approve", "allow", "continue", "允許", "同意", "核准", "繼續")
    negative = ("decline", "deny", "cancel", "拒絕", "取消")
    for option in options:
        label = str(option.get("label") or "").strip()
        if any(token in label.casefold() for token in positive):
            return label
    for option in options:
        label = str(option.get("label") or "").strip()
        if label and not any(token in label.casefold() for token in negative):
            return label
    return None


def _approval_question_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for question in payload.get("questions") or []:
        summary.append(
            {
                "id": question.get("id"),
                "header": question.get("header"),
                "question": question.get("question"),
                "options": [item.get("label") for item in question.get("options") or []],
            }
        )
    return summary


def _elicitation_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not payload.get("mode") and not payload.get("serverName"):
        return None
    return {
        "server_name": payload.get("serverName"),
        "mode": payload.get("mode"),
        "message": payload.get("message"),
        "properties": list((payload.get("requestedSchema") or {}).get("properties") or {}),
    }


def _elicitation_content(schema: dict[str, Any]) -> dict[str, Any]:
    required = set(schema.get("required") or [])
    content: dict[str, Any] = {}
    for key, definition in (schema.get("properties") or {}).items():
        value = _elicitation_value(definition)
        if value is not None or key in required:
            content[key] = value
    return content


def _elicitation_value(definition: dict[str, Any]) -> Any:
    if "default" in definition:
        return definition["default"]
    options = definition.get("enum") or []
    if options:
        positive = ("accept", "approve", "allow", "continue", "yes", "允許", "同意", "核准")
        for option in options:
            if any(token in str(option).casefold() for token in positive):
                return option
        return options[0]
    one_of = definition.get("oneOf") or definition.get("anyOf") or []
    if one_of:
        positive = ("accept", "approve", "allow", "continue", "yes", "允許", "同意", "核准")
        for option in one_of:
            value = option.get("const")
            label = f"{value} {option.get('title') or ''}".casefold()
            if value is not None and any(token in label for token in positive):
                return value
        return one_of[0].get("const")
    value_type = definition.get("type")
    if value_type == "boolean":
        return True
    if value_type in {"number", "integer"}:
        return definition.get("minimum", 1)
    if value_type == "array":
        item_value = _elicitation_value(definition.get("items") or {})
        return [] if item_value is None else [item_value]
    return "accept"


def _resolve_codex_bin() -> Path:
    configured = os.getenv("STOCK_AI_CODEX_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError("STOCK_AI_CODEX_BIN must point to an existing Codex executable")
        if not os.access(candidate, os.X_OK):
            raise PermissionError("STOCK_AI_CODEX_BIN must point to an executable file")
        return candidate.resolve()

    if sys.platform == "darwin":
        # The desktop app ships the runtime matching its current cloud models.
        # An older standalone installation can remain ahead of it on PATH.
        mac_candidates = (
            *_MACOS_CODEX_APP_BINARIES,
            shutil.which("codex"),
        )
        for value in mac_candidates:
            if value:
                candidate = Path(value).expanduser()
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate.resolve()

    appdata = os.getenv("APPDATA")
    if appdata:
        npm_root = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex"
        candidates = sorted(npm_root.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        if candidates:
            return candidates[-1].resolve()

    return bundled_codex_path().resolve()


codex_runtime = CodexRuntime()
