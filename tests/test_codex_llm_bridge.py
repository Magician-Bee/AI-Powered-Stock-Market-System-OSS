from __future__ import annotations

import asyncio

import httpx
import pytest

from stock_ai.codex_llm_bridge import CodexLLMBridge
from stock_ai.codex_runtime import CodexRuntime


class FakeCodexRuntime:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.closed: list[str] = []
        self.turns: list[dict] = []

    async def require_account(self):
        return {"authenticated": True}

    async def start_llm_bridge_run(self, run_id):
        self.started.append(run_id)
        return object()

    async def close_llm_bridge_run(self, run_id):
        self.closed.append(run_id)

    async def run_llm_bridge_turn(self, **kwargs):
        self.turns.append(kwargs)
        tools = kwargs.get("tools") or []
        if tools:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_codex_test",
                        "type": "function",
                        "function": {"name": "lookup_quote", "arguments": '{"symbol":"2330.TW"}'},
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {"content": "Codex bridge ready", "tool_calls": [], "finish_reason": "stop"}


def test_codex_bridge_is_authenticated_openai_compatible_and_audited():
    async def exercise():
        runtime = FakeCodexRuntime()
        events: list[dict] = []
        bridge = CodexLLMBridge(runtime, run_id="AR-test", event_sink=events.append)
        async with bridge:
            async with httpx.AsyncClient(base_url=bridge.base_url, timeout=5) as client:
                unauthorized = await client.post(
                    "/chat/completions",
                    json={"model": "codex", "messages": [{"role": "user", "content": "hello"}]},
                )
                assert unauthorized.status_code == 401

                headers = {"Authorization": f"Bearer {bridge.token}"}
                models = await client.get("/models", headers=headers)
                assert models.status_code == 200
                assert models.json()["data"][0]["id"] == "codex"

                response = await client.post(
                    "/chat/completions",
                    headers=headers,
                    json={"model": "ignored-by-host", "messages": [{"role": "user", "content": "hello"}]},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["model"] == "codex"
                assert body["choices"][0]["message"]["content"] == "Codex bridge ready"

                tool_response = await client.post(
                    "/chat/completions",
                    headers=headers,
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "quote"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "lookup_quote",
                                    "parameters": {"type": "object", "properties": {}},
                                },
                            }
                        ],
                    },
                )
                assert tool_response.status_code == 200
                choice = tool_response.json()["choices"][0]
                assert choice["finish_reason"] == "tool_calls"
                assert choice["message"]["tool_calls"][0]["function"]["name"] == "lookup_quote"

            evidence = bridge.evidence()

        assert runtime.started == ["AR-test:external-framework"]
        assert runtime.closed == ["AR-test:external-framework"]
        assert evidence["driver"] == "codex_app_server"
        assert evidence["authenticated_by"] == "existing_chatgpt_codex_account"
        assert evidence["bearer_token_exposed"] is False
        assert evidence["request_count"] == 2
        assert evidence["requests"][1]["selected_tools"] == ["lookup_quote"]
        assert any(event["type"] == "model.bridge.completed" for event in events)

    asyncio.run(exercise())


def test_codex_bridge_rejects_streaming_without_calling_runtime():
    async def exercise():
        runtime = FakeCodexRuntime()
        async with CodexLLMBridge(runtime, run_id="AR-stream") as bridge:
            async with httpx.AsyncClient(base_url=bridge.base_url, timeout=5) as client:
                response = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {bridge.token}"},
                    json={
                        "model": "codex",
                        "stream": True,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
                assert response.status_code == 400
        assert runtime.turns == []

    asyncio.run(exercise())


def test_bridge_response_and_audit_show_server_resolved_model_without_starting_listener():
    class ConfiguredRuntime(FakeCodexRuntime):
        async def run_llm_bridge_turn(self, **kwargs):
            return {
                **await super().run_llm_bridge_turn(**kwargs),
                "provider_model_metadata": {"model": "server-model", "reasoning_effort": "ultra"},
            }

    async def exercise():
        events = []
        bridge = CodexLLMBridge(ConfiguredRuntime(), run_id="AR-model", event_sink=events.append)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge._app), base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {bridge.token}"},
                json={"model": "client-alias", "messages": [{"role": "user", "content": "hello"}]},
            )
        assert response.status_code == 200
        assert response.json()["model"] == "server-model"
        completed = next(event for event in events if event["type"] == "model.bridge.completed")
        assert completed["model"] == "server-model"
        assert completed["reasoning_effort"] == "ultra"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("tool_choice", "structured_result", "message"),
    [
        (
            "none",
            {"content": None, "tool_calls": [{"name": "lookup", "arguments_json": "{}"}]},
            "tool_choice=none",
        ),
        (
            "required",
            {"content": "no tool", "tool_calls": []},
            "tool_choice=required",
        ),
        (
            {"type": "function", "function": {"name": "lookup"}},
            {"content": "no tool", "tool_calls": []},
            "required tool choice: lookup",
        ),
    ],
)
def test_codex_runtime_enforces_framework_tool_choice(tmp_path, tool_choice, structured_result, message):
    runtime = CodexRuntime(tmp_path)

    async def fake_start(run_id):
        return object()

    async def fake_turn(*args, **kwargs):
        return structured_result

    runtime.start_llm_bridge_run = fake_start
    runtime._run_embedded_structured_turn = fake_turn

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(
            runtime.run_llm_bridge_turn(
                run_id="AR-choice",
                messages=[{"role": "user", "content": "test"}],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "lookup", "parameters": {"type": "object"}},
                    }
                ],
                tool_choice=tool_choice,
            )
        )
