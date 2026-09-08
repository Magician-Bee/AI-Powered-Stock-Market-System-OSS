from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from open_stock_ai.agent_runtime.context_broker import ContextBroker
from open_stock_ai.agent_runtime.orchestrator import (
    AgentOrchestrator,
    _result_summary,
    _schema_for_task,
    _tool_plan_title,
)
from open_stock_ai.agent_runtime.provider_capabilities import (
    ProviderCapabilityProfile,
    ProviderConformanceSuite,
)
from open_stock_ai.agent_runtime.providers.codex import CodexProvider
from open_stock_ai.agent_runtime.providers.http import (
    ExternalAgentProvider,
    OpenAICompatibleProvider,
    endpoint_transport_scope,
)
from open_stock_ai.agent_runtime.providers.normalizer import (
    ProviderOutputNormalizer,
    ProviderProtocolError,
)
from open_stock_ai.agent_runtime.circuit_breaker import (
    CircuitBreakerPolicy,
    CircuitBreakerRegistry,
)
from open_stock_ai.agent_runtime.transport_guard import ExternalTransportGuard
from open_stock_ai.agent_runtime.validation_layers import (
    EvidenceValidator,
    ExecutionValidator,
    ProtocolValidator,
    SemanticClaimValidator,
)
from open_stock_ai.agent_runtime.validators import ValidatorEngine


@pytest.mark.parametrize(
    ("raw", "state", "summary"),
    [
        ('{"status":"final","answer":"完成"}', "complete", "完成"),
        ('```json\n{"status":"final","answer":"完成"}\n```', "complete", "完成"),
        ('前言 {"status":"final","answer":"完成"} 後記', "complete", "完成"),
        ('<think>private</think>{"status":"final","answer":"完成"}', "complete", "完成"),
        ('{"status":"final","answer":"完成",}', "complete", "完成"),
    ],
)
def test_provider_output_normalizer_handles_format_variants(
    raw: str,
    state: str,
    summary: str,
) -> None:
    result = ProviderOutputNormalizer().normalize(raw)

    assert result.payload["state"] == state
    assert result.payload["summary"] == summary
    assert result.raw_output == raw


def test_provider_output_protocol_error_is_not_a_semantic_answer_error() -> None:
    with pytest.raises(ProviderProtocolError) as caught:
        ProviderOutputNormalizer().normalize("not json")

    assert caught.value.code == "protocol_invalid_json"
    assert caught.value.raw_output == "not json"


def test_universal_protocol_accepts_object_and_string_tool_arguments() -> None:
    object_result = ProviderOutputNormalizer().normalize(
        {
            "status": "need_tools",
            "message": "需要資料",
            "actions": [{"tool": "market.quote", "arguments": {"symbol": "AAA"}}],
        }
    )
    string_result = ProviderOutputNormalizer().normalize(
        {
            "status": "need_tools",
            "message": "需要資料",
            "actions": [{"tool": "market.quote", "arguments": '{"symbol":"AAA"}'}],
        }
    )

    assert object_result.payload["tool_calls"][0]["arguments"] == {"symbol": "AAA"}
    assert string_result.payload["tool_calls"][0]["arguments"] == {"symbol": "AAA"}
    assert string_result.schema_mapping["tool_calls.arguments"] == "parsed_json_string"


def test_universal_protocol_preserves_a_waiting_decision_interaction() -> None:
    result = ProviderOutputNormalizer().normalize(
        {
            "status": "waiting_decision",
            "message": "我傾向採用分批停利。",
            "actions": [],
            "interaction": {
                "prompt": "你要選擇哪一種假設的停利節奏？",
                "agent_view": "分批停利較能平衡不確定性。",
                "preferred_option": "staged",
                "options": [
                    {"option_id": "staged", "label": "分批", "reason": "保留彈性"},
                    {"option_id": "single", "label": "一次", "reason": "操作簡化"},
                ],
                "unknowns": ["持有期間"],
                "important_risks": ["波動"],
            },
        }
    )

    assert result.payload["state"] == "waiting_decision"
    assert result.payload["interaction"]["preferred_option"] == "staged"
    assert result.schema_mapping["status"] == "state"


def test_universal_protocol_repairs_common_ollama_state_and_action_aliases() -> None:
    action_result = ProviderOutputNormalizer().normalize(
        {
            "actions": [
                {
                    "name": "market.search_taiwan_securities",
                    "arguments": {"query": "鴻海"},
                }
            ]
        }
    )
    final_result = ProviderOutputNormalizer().normalize(
        {"status": "completed", "message": "資料已整理完成。", "actions": []}
    )

    assert action_result.payload["state"] == "continue"
    action_call = action_result.payload["tool_calls"][0]
    assert action_call["id"].startswith("universal-1-")
    assert action_call["name"] == "market.search_taiwan_securities"
    assert action_call["arguments"] == {"query": "鴻海"}
    assert action_result.schema_mapping["tool_calls"] == "state"
    assert final_result.payload["state"] == "complete"
    assert final_result.payload["summary"] == "資料已整理完成。"


def test_provider_capability_profiles_select_universal_or_advanced_protocol() -> None:
    codex = CodexProvider(runtime=object()).capabilities()["profile"]
    ollama_like = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        model="qwen",
    ).capabilities()["profile"]
    external = ExternalAgentProvider(
        endpoint="http://localhost:9000/agent",
        framework="hermes",
        model="mock",
    ).capabilities()["profile"]

    assert ProviderCapabilityProfile.model_validate(codex).recommended_protocol == "advanced_v1"
    assert ProviderCapabilityProfile.model_validate(codex).parallel_tool_calls is True
    assert ProviderCapabilityProfile.model_validate(ollama_like).recommended_protocol == "universal_v1"
    assert ProviderCapabilityProfile.model_validate(external).json_schema is False


def test_openai_compatible_provider_bounds_remote_connect_wait_without_shrinking_generation_timeout() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://remote-ollama.test/v1",
        model="gpt-oss:20b",
        timeout_seconds=120,
    )

    timeout = provider._http_timeout()

    assert timeout.connect == 10
    assert timeout.read == 120
    assert timeout.write == 120
    assert timeout.pool == 120


def test_openai_compatible_provider_honours_shorter_user_timeout_for_connection() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://remote-ollama.test/v1",
        model="gpt-oss:20b",
        timeout_seconds=5,
    )

    timeout = provider._http_timeout()

    assert timeout.connect == 5
    assert timeout.read == 5


def test_shared_provider_guard_isolates_upstream_circuits() -> None:
    """A failing upstream must not open another configured endpoint's circuit."""

    circuits = CircuitBreakerRegistry()
    guard = ExternalTransportGuard(circuits=circuits)

    failing = OpenAICompatibleProvider(
        base_url="http://first-ollama.test:11434/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
        transport_guard=guard,
    )
    healthy = OpenAICompatibleProvider(
        base_url="http://second-ollama.test:11434/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"data": [{"id": "gpt-oss:20b"}]})
        ),
        transport_guard=guard,
    )
    circuits.breaker(
        failing.transport_scope,
        policy=CircuitBreakerPolicy(
            failure_threshold=1,
            base_backoff_seconds=60,
            maximum_backoff_seconds=60,
        ),
    )

    assert failing.transport_guard is guard
    assert healthy.transport_guard is guard
    assert failing.transport_scope != healthy.transport_scope
    external = ExternalAgentProvider(
        endpoint="http://external-agent.test:9000/run",
        framework="mock",
        transport_guard=guard,
    )
    assert external.transport_scope.startswith("provider:external-agent:")
    assert "external-agent.test" not in external.transport_scope
    with pytest.raises(RuntimeError, match="circuit blocked"):
        asyncio.run(
            failing.generate_structured(
                "session",
                "請回答",
                {"type": "object", "properties": {}},
            )
        )

    health = asyncio.run(healthy.health())
    assert health["reachable"] is True
    assert asyncio.run(failing.health())["reachable"] is False


def test_provider_transport_scope_is_safe_for_an_invalid_port() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://ollama.test:not-a-port/v1",
        model="gpt-oss:20b",
    )

    assert provider.transport_scope.startswith("provider:openai-compatible:")


def test_endpoint_transport_scope_is_endpoint_isolated_and_safe() -> None:
    first = endpoint_transport_scope("source:model-discovery:ollama", "http://first-ollama.test:11434/api/tags")
    second = endpoint_transport_scope("source:model-discovery:ollama", "http://second-ollama.test:11434/api/tags")
    same_origin = endpoint_transport_scope("source:model-discovery:ollama", "http://first-ollama.test:11434/v1/models")

    assert first.startswith("source:model-discovery:ollama:")
    assert first != second
    assert first == same_origin
    assert "first-ollama.test" not in first


def test_openai_compatible_provider_records_terminal_failure_after_bounded_connect_retries() -> None:
    attempts: list[str] = []
    events: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise httpx.ConnectTimeout("remote Ollama did not accept the connection", request=request)

    provider = OpenAICompatibleProvider(
        base_url="http://remote-ollama.test/v1",
        model="gpt-oss:20b",
        timeout_seconds=120,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(
            provider.generate_structured(
                "session",
                "請回答",
                {"type": "object", "properties": {}},
                event_sink=events.append,
            )
        )

    assert len(attempts) == 2
    assert [event["type"] for event in events] == [
        "model.provider.requested",
        "model.provider.transport_retry",
        "model.provider.failed",
    ]
    assert events[1]["reason"] == "ConnectTimeout"


@pytest.mark.parametrize("model", ["qwen", "gemma"])
def test_ollama_compatible_provider_e2e_uses_schema_fallback_and_normalizer(
    model: str,
) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": "json_schema unsupported"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '<think>hidden</think>{"status":"final","answer":"完成"}'
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model=model,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {
                "type": "object",
                "required": ["state", "summary"],
                "properties": {
                    "state": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        )
    )

    assert result["state"] == "complete"
    assert result["summary"] == "完成"
    assert len(requests) == 2
    assert all(request["max_tokens"] == 4_096 for request in requests)
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert "Required JSON schema" in requests[0]["messages"][0]["content"]
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


def test_ollama_compatible_provider_repairs_one_invalid_protocol_response() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        content = (
            "這是自然語言答案，沒有遵守 JSON 協定。"
            if len(requests) == 1
            else '{"status":"final","answer":"完成","actions":[]}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    events: list[dict] = []
    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            event_sink=events.append,
        )
    )

    assert result["state"] == "complete"
    assert result["summary"] == "完成"
    assert len(requests) == 2
    assert all(request["max_tokens"] == 4_096 for request in requests)
    assert all(request["reasoning_effort"] == "low" for request in requests)
    assert "Required JSON schema" in requests[1]["messages"][1]["content"]
    assert [event["type"] for event in events] == [
        "model.provider.requested",
        "model.provider.protocol_retry",
        "model.provider.completed",
    ]


def test_ollama_compatible_provider_retries_one_transient_500_then_completes() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(500, json={"error": "model worker temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"status":"final","answer":"完成"}'}}
                ]
            },
        )

    events: list[dict] = []
    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            event_sink=events.append,
        )
    )

    assert result["state"] == "complete"
    assert len(requests) == 2
    retry = next(event for event in events if event["type"] == "model.provider.transport_retry")
    assert retry["attempt"] == 2
    assert retry["reason"] == "HTTP 500"
    assert events[-1]["type"] == "model.provider.completed"


def test_ollama_compatible_provider_waits_for_short_conservative_admission_interval(monkeypatch) -> None:
    class AdmissionThenProceedGuard:
        calls = 0

        async def call(self, scope, operation):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    f"external transport rate limit blocked {scope}: unverified_policy_conservative_interval"
                )
            return await operation()

    async def no_op_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("open_stock_ai.agent_runtime.providers.http.asyncio.sleep", no_op_sleep)
    events: list[dict] = []
    guard = AdmissionThenProceedGuard()
    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{\"status\":\"final\",\"answer\":\"完成\"}'}}]},
            )
        ),
        transport_guard=guard,
    )

    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
            event_sink=events.append,
        )
    )

    assert result["state"] == "complete"
    assert guard.calls == 2
    wait = next(event for event in events if event["type"] == "model.provider.admission_wait")
    assert wait["reason"] == "unverified_policy_conservative_interval"


def test_ollama_compatible_provider_queues_while_another_request_owns_the_slot(monkeypatch) -> None:
    class BusyThenProceedGuard:
        calls = 0

        async def call(self, scope, operation):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    f"external transport rate limit blocked {scope}: maximum_concurrency_reached"
                )
            return await operation()

    async def no_op_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("open_stock_ai.agent_runtime.providers.http.asyncio.sleep", no_op_sleep)
    events: list[dict] = []
    guard = BusyThenProceedGuard()
    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{\"status\":\"final\",\"answer\":\"完成\"}'}}]},
            )
        ),
        transport_guard=guard,
    )

    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
            event_sink=events.append,
        )
    )

    assert result["state"] == "complete"
    assert guard.calls == 2
    wait = next(event for event in events if event["type"] == "model.provider.admission_wait")
    assert wait["reason"] == "maximum_concurrency_reached"


def test_ollama_native_tool_calls_are_preserved_as_universal_actions() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning": "Need Host evidence.",
                            "tool_calls": [
                                {
                                    "id": "call-analysis",
                                    "type": "function",
                                    "function": {
                                        "name": "market.analyze_symbol",
                                        "arguments": '{"symbol":"2330.TW"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="http://ollama.local/v1",
        model="gpt-oss:20b",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請先取得 Host 證據",
            {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
        )
    )

    assert result["state"] == "continue"
    assert result["summary"] == "Requesting Host-validated tool evidence."
    assert result["tool_calls"] == [
        {
            "id": "call-analysis",
            "name": "market.analyze_symbol",
            "arguments": {"symbol": "2330.TW"},
        }
    ]
    assert len(requests) == 1


def test_web_research_plan_titles_and_summaries_reflect_model_queries() -> None:
    finance = _tool_plan_title(
        {
            "name": "web.research",
            "arguments": {"query": "Taiwan finance sector recent performance"},
        }
    )
    shipping = _tool_plan_title(
        {
            "name": "web.research",
            "arguments": {"query": "Taiwan shipping sector recent performance"},
        }
    )
    summary = _result_summary(
        {
            "ok": True,
            "result": {
                "schema_version": "open_stock_ai.web_research.v1",
                "query": "Taiwan finance sector recent performance",
                "search_result_count": 8,
                "source_count": 3,
                "search_providers": ["bing", "duckduckgo"],
            },
        }
    )

    assert finance != shipping
    assert "finance sector" in finance
    assert summary["query"] == "Taiwan finance sector recent performance"
    assert summary["source_count"] == 3


def test_external_agent_e2e_maps_universal_protocol_output() -> None:
    requests: list[dict] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(_request.content))
        return httpx.Response(
            200,
            json={
                "output": {
                    "status": "need_tools",
                    "message": "需要行情",
                    "actions": [
                        {
                            "tool": "market.quote",
                            "arguments": '{"symbol":"AAA"}',
                        }
                    ],
                }
            },
        )

    provider = ExternalAgentProvider(
        endpoint="http://external.local/agent",
        framework="hermes",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        provider.generate_structured(
            "session",
            "請回答",
            {"type": "object", "required": ["status"]},
        )
    )

    assert result["state"] == "continue"
    assert result["summary"] == "需要行情"
    assert result["tool_calls"][0]["id"].startswith("universal-1-")
    assert result["tool_calls"][0]["name"] == "market.quote"
    assert result["tool_calls"][0]["arguments"] == {"symbol": "AAA"}
    assert requests[0]["protocol"] == "open_stock_ai.universal_provider_turn.v1"


def test_universal_generated_tool_ids_are_stable_by_call_identity() -> None:
    first = ProviderOutputNormalizer().normalize(
        {
            "status": "need_tools",
            "message": "research",
            "actions": [
                {
                    "tool": "market.research_pack",
                    "arguments": '{"symbol":"2330.TW"}',
                }
            ],
        }
    ).payload["tool_calls"][0]["id"]
    repeated = ProviderOutputNormalizer().normalize(
        {
            "status": "need_tools",
            "message": "research again",
            "actions": [
                {
                    "tool": "market.research_pack",
                    "arguments": '{"symbol":"2330.TW"}',
                }
            ],
        }
    ).payload["tool_calls"][0]["id"]
    different = ProviderOutputNormalizer().normalize(
        {
            "status": "need_tools",
            "message": "fundamentals",
            "actions": [
                {
                    "tool": "market.monthly_revenue",
                    "arguments": '{"symbol":"2330.TW"}',
                }
            ],
        }
    ).payload["tool_calls"][0]["id"]

    assert first == repeated
    assert first != different


def test_universal_schema_does_not_require_advanced_plan_bookkeeping() -> None:
    universal = _schema_for_task("market_information", protocol="universal_v1")
    advanced = _schema_for_task("market_information", protocol="advanced_v1")

    assert set(universal["required"]) == set(universal["properties"])
    assert {"decision", "routing", "result", "interaction"}.issubset(universal["required"])
    assert {"waiting_user_input", "waiting_decision"}.issubset(
        universal["properties"]["status"]["enum"]
    )
    assert "plan_patch" not in universal["properties"]
    assert "completion_evaluation" not in universal["properties"]
    assert "plan_patch" in advanced["required"]
    assert "completion_evaluation" in advanced["required"]


def test_proposal_evaluation_and_post_answer_arguments_are_strict_provider_compatible() -> None:
    marker = "[MODEL_OUTPUT_SCHEMA:proposal_evaluation]"
    advanced = _schema_for_task(
        "market_information",
        protocol="advanced_v1",
        objective=marker,
    )
    universal = _schema_for_task(
        "market_information",
        protocol="universal_v1",
        objective=marker,
    )

    advanced_result = advanced["properties"]["structured_result"]["anyOf"][1]
    universal_result = universal["properties"]["result"]
    assert advanced_result["properties"]["schema_version"]["enum"] == [
        "open_stock_ai.proposal_evaluation.v1"
    ]
    assert universal_result == advanced_result
    for schema in (advanced, universal):
        arguments = schema["properties"]["interaction_proposals"]["items"]["properties"]["arguments"]
        assert arguments["additionalProperties"] is False
        assert set(arguments["required"]) == {"objective", "intent_json"}
        assert set(arguments["properties"]) == {"objective", "intent_json"}


def test_market_radar_advanced_schema_is_strict_provider_compatible() -> None:
    schema = _schema_for_task("market_radar", protocol="advanced_v1")

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    nodes = list(walk(schema))
    object_nodes = [node for node in nodes if node.get("type") == "object"]
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all(
        set(node.get("required") or []) == set(node.get("properties") or {})
        for node in object_nodes
    )
    assert all("$ref" not in node for node in nodes)
    assert all(
        len(node.get("required") or []) == len(set(node.get("required") or []))
        for node in nodes
    )


@pytest.mark.parametrize(
    "task_kind",
    [
        "general_answer",
        "project_task",
        "market_information",
        "market_decision",
        "ui_task",
        "current_information",
    ],
)
def test_every_advanced_task_schema_is_strict_codex_compatible(task_kind: str) -> None:
    schema = _schema_for_task(task_kind, protocol="advanced_v1")

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    object_nodes = [node for node in walk(schema) if node.get("type") == "object"]
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all(
        set(node.get("required") or []) == set(node.get("properties") or {})
        for node in object_nodes
    )


def test_context_broker_filters_actual_agent_turn_tools_and_discloses_progressively() -> None:
    broker = ContextBroker()
    manifest = [
        {"name": "project.read_file", "risk_class": "read_only"},
        {"name": "project.write_file", "risk_class": "local_reversible", "mutating": True},
        {"name": "git.status", "risk_class": "read_only"},
        {"name": "market.analyze_symbol", "risk_class": "read_only"},
        {"name": "paper.submit_order", "risk_class": "financial_paper", "mutating": True},
        {"name": "system.capabilities", "risk_class": "read_only"},
    ]

    project = broker.filter_capabilities(manifest, task_kind="project_task")
    initial = broker.disclose_capabilities(manifest, task_kind="project_task", step=1)
    expanded = broker.disclose_capabilities(manifest, task_kind="project_task", step=2)

    assert {item["name"] for item in project} == {
        "project.read_file",
        "project.write_file",
        "git.status",
        "system.capabilities",
    }
    assert all(not item["name"].startswith(("market.", "paper.")) for item in initial)
    assert expanded == project


def test_context_broker_exposes_evidence_tools_not_discovery_only_search() -> None:
    broker = ContextBroker()
    manifest = [
        {"name": "web.search", "risk_class": "read_only"},
        {"name": "web.fetch", "risk_class": "read_only"},
        {"name": "web.research", "risk_class": "read_only"},
        {"name": "market.analyze_universe", "risk_class": "read_only"},
        {"name": "agent.run_subtasks", "risk_class": "read_only"},
    ]

    names = {
        item["name"]
        for item in broker.filter_capabilities(
            manifest,
            task_kind="market_information",
        )
    }

    assert "web.search" not in names
    assert {
        "web.fetch",
        "web.research",
        "market.analyze_universe",
        "agent.run_subtasks",
    } <= names


def test_context_broker_hides_recursive_subtasks_for_routine_market_decisions() -> None:
    broker = ContextBroker()
    manifest = [
        {"name": "agent.run_subtasks", "risk_class": "read_only"},
        {"name": "market.analyze_symbol", "risk_class": "read_only"},
        {"name": "paper.preview_order", "risk_class": "financial_paper"},
        {"name": "paper.submit_order", "risk_class": "financial_paper", "mutating": True},
    ]

    names = {
        item["name"]
        for item in broker.filter_capabilities(manifest, task_kind="market_decision")
    }

    assert names == {
        "market.analyze_symbol",
        "paper.preview_order",
        "paper.submit_order",
    }


def test_context_broker_keeps_paper_order_tools_after_market_intent_correction() -> None:
    broker = ContextBroker()
    manifest = [
        {"name": "agent.run_subtasks", "risk_class": "read_only"},
        {"name": "market.analyze_symbol", "risk_class": "read_only"},
        {"name": "paper.preview_order", "risk_class": "financial_paper"},
        {"name": "paper.submit_order", "risk_class": "financial_paper", "mutating": True},
    ]

    names = {
        item["name"]
        for item in broker.filter_capabilities(manifest, task_kind="market_decision")
    }

    assert "agent.run_subtasks" not in names
    assert {"paper.preview_order", "paper.submit_order"} <= names


def test_context_broker_limits_explicit_paper_execution_to_bounded_host_path() -> None:
    broker = ContextBroker()
    manifest = [
        {"name": "market.analyze_symbol"},
        {"name": "market.research_pack"},
        {"name": "market.institutional_flow"},
        {"name": "paper.preview_order"},
        {"name": "paper.submit_order"},
        {"name": "web.research"},
        {"name": "agent.run_subtasks"},
    ]

    names = {
        item["name"]
        for item in broker.filter_capabilities(
            manifest,
            task_kind="market_decision",
            task_kinds=["market_decision", "paper_execution"],
        )
    }

    assert names == {
        "market.analyze_symbol",
        "market.research_pack",
        "paper.preview_order",
        "paper.submit_order",
    }


def test_orchestrator_passes_context_broker_filtered_tools_to_turn() -> None:
    captured = []

    class Driver:
        driver_id = "mock"

        def describe(self):
            return {"model": "mock"}

        async def start_run(self, _context):
            return None

        async def close_run(self, _run_id):
            return None

        async def decide(self, turn):
            captured.append(turn)
            return {
                "status": "final",
                "message": "專案檢查需要 Host 證據。",
                "actions": [],
                "evidence_ids": [],
                "remaining_gaps": [],
            }

    class Tools:
        def manifest(self):
            return [
                {
                    "name": "project.read_file",
                    "description": "read",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "category": "project",
                    "risk_class": "read_only",
                },
                {
                    "name": "market.analyze_symbol",
                    "description": "market",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "category": "market",
                    "risk_class": "read_only",
                },
                {
                    "name": "paper.submit_order",
                    "description": "paper",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "category": "paper",
                    "risk_class": "financial_paper",
                    "mutating": True,
                },
            ]

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("No tool should execute")

    orchestrator = AgentOrchestrator(
        drivers={"mock": Driver()},
        tools=Tools(),
        default_driver="mock",
    )
    result = asyncio.run(
        orchestrator.run(
            objective="請檢查這個專案的程式碼結構",
            driver_id="mock",
            max_steps=1,
        )
    )

    assert result["task_kind"] == "project_task"
    assert [item["name"] for item in captured[0].tools] == ["project.read_file"]


def test_provider_conformance_suite_executes_behavioral_probe() -> None:
    class Provider:
        provider_id = "mock-advanced"

        def capabilities(self):
            return {
                "profile": {
                    "provider": self.provider_id,
                    "model": "mock",
                    "native_tool_calling": True,
                    "json_schema": True,
                    "parallel_tool_calls": True,
                    "streaming": True,
                    "reasoning_format": "opaque",
                    "persistent_session": True,
                    "recommended_protocol": "advanced_v1",
                }
            }

        async def generate_structured(self, _session_id, prompt, _schema):
            nonce = next(
                token.rstrip("。")
                for token in prompt.split()
                if token.startswith("CONF-")
            )
            return {
                "status": "final",
                "answer": f"能力測試通過 {nonce}",
                "actions": [
                    {"tool": "system.capabilities", "arguments": {}},
                    {
                        "tool": "market.search_taiwan_securities",
                        "arguments": {"query": ""},
                    },
                ],
            }

    report = asyncio.run(
        ProviderConformanceSuite().probe(
            Provider(),
            session_id="conformance-session",
            include_expensive=True,
        )
    )

    assert report.core_passed is True
    assert report.recommended_protocol == "advanced_v1"
    assert {
        item.capability
        for item in report.checks
        if item.measured and item.passed
    }.issuperset(
        {
            "json_object",
            "json_schema",
            "tool_calling",
            "parallel_tools",
            "traditional_chinese",
            "reasoning_format",
            "long_context",
        }
    )


def test_validation_layers_report_distinct_error_taxonomies() -> None:
    protocol = ProtocolValidator().validate(
        {"state": 2},
        {"required": ["state", "summary"], "properties": {"state": {"type": "string"}}},
    )
    execution = ExecutionValidator().validate({"ok": True}, mutation_expected=True)
    evidence = EvidenceValidator().validate(
        [{"evidence_id": "EV-1", "symbol": "BBB"}],
        symbol="AAA",
    )
    semantic = SemanticClaimValidator().validate(
        [{"text": "claim", "evidence_ids": ["EV-missing"]}],
        [{"evidence_id": "EV-1"}],
    )

    assert {item.code for item in protocol.issues} == {
        "protocol_missing_field",
        "protocol_schema_mismatch",
    }
    assert execution.issues[0].code == "execution_missing_receipt"
    assert evidence.issues[0].code == "evidence_symbol_mismatch"
    assert semantic.issues[0].code == "semantic_claim_missing_evidence"


def test_semantic_validator_detects_numeric_and_directional_contradiction() -> None:
    report = SemanticClaimValidator().validate(
        [
            {
                "text": "公司營收大幅成長",
                "metric": "revenue_yoy",
                "operator": "gt",
                "value": 0,
                "evidence_ids": ["revenue-1"],
            }
        ],
        [
            {
                "evidence_id": "revenue-1",
                "symbol": "AAA",
                "result": {"revenue_yoy": -12.5, "summary": "營收衰退 12.5%"},
            }
        ],
    )

    assert report.valid is False
    assert report.metadata["claims"][0]["status"] == "contradicted"
    assert report.issues[0].code == "semantic_claim_contradicted"


def test_host_validator_executes_all_four_validation_layers() -> None:
    validator = ValidatorEngine()
    tool_result = validator.validate_tool_result(
        tool={
            "name": "paper.submit_order",
            "mutating": True,
            "output_schema": {"type": "object"},
        },
        arguments={"symbol": "2330.TW"},
        result={"order": {"order_id": "PB-001", "status": "filled"}},
    )
    tool_layers = {
        check["layer"]
        for check in tool_result.checks
        if check["name"].endswith("_validation_layer")
    }
    assert tool_layers == {"protocol", "execution"}

    completion = validator.validate_completion(
        state="complete",
        has_pending_tool_calls=False,
        plan={
            "nodes": [],
            "completion_criteria": ["market observation is recorded"],
        },
        successful_observations=1,
        evidence_required=True,
        completion_evaluation={
            "criteria_met": True,
            "criterion_results": [
                {
                    "criterion": "market observation is recorded",
                    "met": True,
                    "evidence_ids": ["quote-1"],
                }
            ],
            "evidence_ids": ["quote-1"],
            "remaining_gaps": [],
        },
        known_evidence_ids={"quote-1"},
        evidence_catalog={
            "quote-1": {
                "tool": "market.quote",
                "ok": True,
                "validation": {"passed": True},
            }
        },
    )
    completion_layers = {
        check["layer"]
        for check in completion.checks
        if check["name"].endswith("_validation_layer")
    }
    assert completion.passed is True
    assert completion_layers == {"evidence", "semantic"}
