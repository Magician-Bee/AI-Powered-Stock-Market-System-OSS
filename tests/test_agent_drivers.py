from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import stock_ai.agent_drivers as agent_drivers
from open_stock_ai.agent_runtime.contracts import AgentTurnInput
from stock_ai.agent_drivers import (
    AgentDriverSettings,
    CodexAgentDriver,
    ExternalAgentDriver,
    OpenAICompatibleAgentDriver,
    discover_openai_compatible_models,
)


def _settings(**overrides) -> AgentDriverSettings:
    values = {
        "default_driver": "codex",
        "openai_base_url": "http://model.test/v1",
        "openai_api_key": "secret-token",
        "openai_model": "test-model",
        "openai_timeout_seconds": 30,
        "external_endpoint": "http://agent.test/turn",
        "external_token": "external-secret",
        "external_framework": "hermes-compatible",
        "external_model": "hermes-test",
    }
    values.update(overrides)
    return AgentDriverSettings(**values)


def _turn() -> AgentTurnInput:
    return AgentTurnInput(
        run_id="AR-test",
        objective="Read the real system before deciding.",
        system_prompt="Use tools and return JSON.",
        transcript=({"role": "host", "type": "run_context", "content": {}},),
        tools=(
            {
                "name": "system.capabilities",
                "description": "Read boundaries",
                "input_schema": {"type": "object", "properties": {}},
            },
        ),
        output_schema={"type": "object"},
        metadata={"step": 1, "max_steps": 6},
    )


def test_openai_compatible_driver_calls_chat_completions_with_host_context(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "state": "continue",
                                    "summary": "Inspect capabilities",
                                    "tool_calls": [
                                        {"id": "call-1", "name": "system.capabilities", "arguments": {}}
                                    ],
                                    "decision": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        agent_drivers.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = asyncio.run(OpenAICompatibleAgentDriver(_settings()).decide(_turn()))

    assert captured["url"] == "http://model.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["payload"]["model"] == "test-model"
    assert "system.capabilities" in captured["payload"]["messages"][1]["content"]
    assert result["tool_calls"][0]["name"] == "system.capabilities"


def test_ollama_root_discovers_loaded_models_and_returns_openai_base(monkeypatch):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3:8b"}, {"id": "gpt-oss:20b"}]})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3:8b",
                            "size": 5_000_000,
                            "details": {"family": "qwen3", "parameter_size": "8B", "quantization_level": "Q4_K_M"},
                        },
                        {"name": "gpt-oss:20b", "size": 12_000_000},
                    ]
                },
            )
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        agent_drivers.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = asyncio.run(discover_openai_compatible_models("http://ollama.test:11434"))

    assert result["provider_kind"] == "ollama"
    assert result["recommended_base_url"] == "http://ollama.test:11434/v1"
    assert [item["id"] for item in result["items"]] == ["gpt-oss:20b", "qwen3:8b"]
    assert result["items"][1]["details"]["parameter_size"] == "8B"
    assert "http://ollama.test:11434/v1/models" in requested
    assert "http://ollama.test:11434/api/tags" in requested
    assert result["credentials_returned"] is False


def test_model_discovery_uses_endpoint_isolated_shared_transport_guard(monkeypatch):
    scopes = []

    class SharedGuard:
        async def call(self, scope, operation):
            scopes.append(scope)
            return await operation()

    monkeypatch.setattr(
        agent_drivers,
        "default_external_transport_guard",
        lambda: SharedGuard(),
    )
    monkeypatch.setattr(
        agent_drivers,
        "ExternalTransportGuard",
        lambda: pytest.fail("model discovery must not create a private transport guard"),
    )
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    monkeypatch.setattr(
        agent_drivers.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = asyncio.run(discover_openai_compatible_models("http://model.test/v1"))

    assert result["reachable"] is False
    assert len(scopes) == 2
    assert scopes[0].startswith("source:model-discovery:openai-compatible:")
    assert scopes[1].startswith("source:model-discovery:ollama:")
    assert scopes[0] != scopes[1]
    assert "model.test" not in " ".join(scopes)


def test_provider_registry_reuses_the_process_shared_transport_guard(monkeypatch):
    shared_guard = object()
    monkeypatch.setattr(
        agent_drivers,
        "default_external_transport_guard",
        lambda: shared_guard,
    )

    registry = agent_drivers.build_provider_registry(_settings())

    assert registry.get("openai-compatible").transport_guard is shared_guard
    assert registry.get("external-agent").transport_guard is shared_guard


def test_direct_model_drivers_reuse_the_process_shared_transport_guard(monkeypatch):
    shared_guard = object()
    monkeypatch.setattr(
        agent_drivers,
        "default_external_transport_guard",
        lambda: shared_guard,
    )

    openai = OpenAICompatibleAgentDriver(_settings())
    external = ExternalAgentDriver(_settings())

    assert openai.provider.transport_guard is shared_guard
    assert external.provider.transport_guard is shared_guard


def test_external_agent_driver_uses_interoperable_turn_protocol(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "decision": {
                    "state": "complete",
                    "summary": "Observed host data",
                    "tool_calls": [],
                    "decision": {
                        "action": "watch",
                        "symbol": "2330.TW",
                        "confidence": 70,
                        "rationale": "Host observation is authoritative.",
                        "next_check": "next session",
                    },
                }
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        agent_drivers.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = asyncio.run(ExternalAgentDriver(_settings()).decide(_turn()))

    assert captured["authorization"] == "Bearer external-secret"
    assert captured["payload"]["protocol"] == "open_stock_ai.advanced_provider_turn.v1"
    assert "system.capabilities" in captured["payload"]["prompt"]
    assert captured["payload"]["output_schema"] == {"type": "object"}
    assert captured["payload"]["framework"] == "hermes-compatible"
    assert captured["payload"]["model"] == "hermes-test"
    assert result["state"] == "complete"


def test_codex_driver_uses_embedded_app_server_turn_not_native_chat_relay(monkeypatch):
    captured = {}

    async def embedded_turn(prompt, output_schema, **kwargs):
        captured["prompt"] = prompt
        captured["schema"] = output_schema
        captured.update(kwargs)
        return {"state": "complete", "summary": "UI 內完成", "tool_calls": [], "decision": None}

    monkeypatch.setattr(agent_drivers.codex_runtime, "run_agent_turn", embedded_turn)

    result = asyncio.run(CodexAgentDriver().decide(_turn()))

    assert "system.capabilities" in captured["prompt"]
    assert captured["schema"] == {"type": "object"}
    assert captured["run_id"] == "AR-test"
    assert result["summary"] == "UI 內完成"
    description = CodexAgentDriver().describe()
    assert description["execution_mode"] == "embedded_direct"
    assert description["account_auth"] == "ChatGPT account session"


def test_external_agent_driver_requires_endpoint():
    driver = ExternalAgentDriver(_settings(external_endpoint=""))

    with pytest.raises(RuntimeError, match="requires an endpoint"):
        asyncio.run(driver.decide(_turn()))
