from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from open_stock_ai.agent_runtime.provider_capabilities import ProviderConformanceSuite
from open_stock_ai.agent_runtime.providers.codex import CodexProvider
from open_stock_ai.agent_runtime.providers.http import (
    ExternalAgentProvider,
    OpenAICompatibleProvider,
)


def _conformance_payload(prompt: str) -> dict:
    nonce = re.search(r"CONF-[0-9a-f]+", prompt)
    assert nonce is not None
    return {
        "status": "final",
        "answer": f"能力測試通過 {nonce.group(0)}",
        "actions": [
            {"tool": "system.capabilities", "arguments": {}},
            {
                "tool": "market.search_taiwan_securities",
                "arguments": {"query": ""},
            },
        ],
    }


class FakeCodexRuntime:
    async def start_agent_run(self, _run_id: str, *, project_root: str, model: str = "", reasoning_effort: str = "") -> None:
        assert project_root
        assert model == reasoning_effort == ""

    async def close_agent_run(self, _run_id: str) -> None:
        return None

    async def run_agent_turn(
        self,
        prompt: str,
        _output_schema: dict,
        *,
        run_id: str,
        event_sink=None,
        model: str = "",
        reasoning_effort: str = "",
    ) -> dict:
        assert run_id
        assert event_sink is None
        assert model == reasoning_effort == ""
        return _conformance_payload(prompt)

    async def account_status(self, refresh: bool = False) -> dict:
        return {"authenticated": True, "refresh": refresh}


def _openai_provider() -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                _conformance_payload(prompt),
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    return OpenAICompatibleProvider(
        base_url="http://openai-compatible.local/v1",
        model="mock-openai",
        transport=httpx.MockTransport(handler),
    )


def _external_provider() -> ExternalAgentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"output": _conformance_payload(payload["prompt"])},
        )

    return ExternalAgentProvider(
        endpoint="http://external-agent.local/run",
        framework="mock-framework",
        model="mock-external",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "provider",
    [
        CodexProvider(FakeCodexRuntime()),
        _openai_provider(),
        _external_provider(),
    ],
    ids=["codex", "openai-compatible", "external-agent"],
)
def test_provider_behavioral_conformance_e2e(provider) -> None:
    async def scenario():
        session_id = f"e2e-{provider.provider_id}"
        await provider.start_session(session_id, project_root="/tmp/provider-e2e")
        try:
            return await ProviderConformanceSuite().probe(
                provider,
                session_id=session_id,
                include_expensive=True,
            )
        finally:
            await provider.close_session(session_id)

    report = asyncio.run(scenario())

    assert report.provider == provider.provider_id
    assert report.core_passed is True
    passed = {item.capability for item in report.checks if item.measured and item.passed}
    assert {
        "json_object",
        "json_schema",
        "tool_calling",
        "parallel_tools",
        "traditional_chinese",
        "reasoning_format",
        "long_context",
    }.issubset(passed)
