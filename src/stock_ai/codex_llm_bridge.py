from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
import uvicorn

if TYPE_CHECKING:
    from .codex_runtime import CodexRuntime


EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


class CodexLLMBridge:
    """Run-scoped, loopback-only OpenAI Chat Completions facade for Codex.

    External frameworks receive only a random bearer token and this ephemeral
    endpoint. They never receive the user's Codex/ChatGPT credentials, project
    permissions, terminal access, or the App Server transport itself.
    """

    def __init__(
        self,
        runtime: CodexRuntime,
        *,
        run_id: str,
        event_sink: EventSink | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_id = run_id
        self.thread_key = f"{run_id}:external-framework"
        self.event_sink = event_sink
        self.token = secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port: int | None = None
        self._socket: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[Any] | None = None
        self._requests: list[dict[str, Any]] = []
        self._app = self._build_app()

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("Codex LLM bridge has not started")
        return f"http://{self.host}:{self.port}/v1"

    async def __aenter__(self) -> CodexLLMBridge:
        await self.runtime.require_account()
        await self.runtime.start_llm_bridge_run(self.thread_key)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(128)
        listener.setblocking(False)
        self._socket = listener
        self.port = int(listener.getsockname()[1])
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="error",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve(sockets=[listener]))
        for _ in range(100):
            if self._server.started:
                break
            if self._server_task.done():
                await self._server_task
            await asyncio.sleep(0.01)
        if not self._server.started:
            await self.close()
            raise RuntimeError("Codex LLM bridge failed to start")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except TimeoutError:
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
        if self._socket is not None:
            self._socket.close()
        self._server = None
        self._server_task = None
        self._socket = None
        await self.runtime.close_llm_bridge_run(self.thread_key)

    def evidence(self) -> dict[str, Any]:
        return {
            "driver": "codex_app_server",
            "transport": "run_scoped_loopback_openai_compatible",
            "host": self.host,
            "authenticated_by": "existing_chatgpt_codex_account",
            "bearer_token_exposed": False,
            "hidden_thread": True,
            "sandbox": "read_only",
            "approval_mode": "deny_all",
            "request_count": len(self._requests),
            "tool_call_count": sum(int(item["tool_call_count"]) for item in self._requests),
            "requests": list(self._requests),
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="Run-scoped Codex LLM Bridge",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @app.get("/v1/models")
        async def models(authorization: str | None = Header(default=None)):
            self._authorize(authorization)
            return {
                "object": "list",
                "data": [
                    {"id": "codex", "object": "model", "owned_by": "openai-codex-app-server"},
                    {
                        "id": "stock-ai-agent",
                        "object": "model",
                        "owned_by": "openai-codex-app-server",
                        "effective_model": "codex",
                    },
                ],
            }

        @app.post("/v1/chat/completions")
        async def chat_completions(
            payload: dict[str, Any],
            authorization: str | None = Header(default=None),
        ):
            self._authorize(authorization)
            if payload.get("stream"):
                raise HTTPException(status_code=400, detail="Streaming is not supported by the audited bridge")
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise HTTPException(status_code=400, detail="messages must be a non-empty array")
            tools = payload.get("tools") or []
            if not isinstance(tools, list):
                raise HTTPException(status_code=400, detail="tools must be an array")
            started = time.perf_counter()
            await self._emit(
                {
                    "type": "model.bridge.requested",
                    "source": "codex_app_server",
                    "message_count": len(messages),
                    "offered_tool_count": len(tools),
                }
            )
            try:
                result = await self.runtime.run_llm_bridge_turn(
                    run_id=self.thread_key,
                    messages=messages,
                    tools=tools,
                    tool_choice=payload.get("tool_choice"),
                    event_sink=self.event_sink,
                )
            except Exception as exc:
                await self._emit(
                    {
                        "type": "model.bridge.failed",
                        "source": "codex_app_server",
                        "error": type(exc).__name__,
                    }
                )
                raise HTTPException(status_code=502, detail=f"Codex bridge turn failed: {exc}") from exc
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            tool_calls = list(result.get("tool_calls") or [])
            selected_tools = [
                str((item.get("function") or {}).get("name") or "")
                for item in tool_calls
                if isinstance(item, dict)
            ]
            selected_tools = [name for name in selected_tools if name]
            trace = {
                "message_count": len(messages),
                "offered_tool_count": len(tools),
                "tool_call_count": len(tool_calls),
                "selected_tools": selected_tools,
                "duration_ms": duration_ms,
            }
            self._requests.append(trace)
            await self._emit(
                {
                    "type": "model.bridge.completed",
                    "source": "codex_app_server",
                    **trace,
                }
            )
            message: dict[str, Any] = {
                "role": "assistant",
                "content": result.get("content"),
            }
            if tool_calls:
                message["tool_calls"] = tool_calls
            return {
                "id": f"chatcmpl_codex_{uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "codex",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": result.get("finish_reason") or ("tool_calls" if tool_calls else "stop"),
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        return app

    def _authorize(self, authorization: str | None) -> None:
        expected = f"Bearer {self.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Invalid run-scoped bridge token")

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        emitted = self.event_sink(event)
        if asyncio.iscoroutine(emitted):
            await emitted
