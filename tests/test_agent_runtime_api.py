from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from open_stock_ai.agent_runtime.artifact_store import ArtifactStore
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime
from stock_ai.agent_api import (
    AgentRunRequest,
    AgentScheduleEvent,
    _effective_autonomy,
    _run_context,
    trigger_agent_automation_event,
)
from stock_ai.main import _is_critical_slo_api_request, app


client = TestClient(app)


def test_slo_api_scope_excludes_high_frequency_read_only_ui_queries():
    assert _is_critical_slo_api_request("POST", "/api/agents/run") is True
    assert _is_critical_slo_api_request("POST", "/agent/paper-training/order") is True
    assert _is_critical_slo_api_request("POST", "/api/data/observability/notify") is True
    assert _is_critical_slo_api_request("GET", "/api/agents/runs") is False
    assert _is_critical_slo_api_request("GET", "/api/agents/observability") is False
    assert _is_critical_slo_api_request("GET", "/api/data/observability") is False
    assert _is_critical_slo_api_request("GET", "/api/agents/tools") is False


def test_operational_alert_api_returns_the_runtime_owned_dashboard(monkeypatch):
    expected = {
        "schema_version": "open_stock_ai.operational_alert_dashboard.v1",
        "current_alerts": [],
        "delivery": {"configured": False},
    }
    monkeypatch.setattr(
        "stock_ai.agent_api.get_operational_alert_runtime",
        lambda: SimpleNamespace(dashboard=lambda: expected),
    )

    response = client.get("/api/agents/operational-alerts")

    assert response.status_code == 200
    assert response.json() == expected


def test_chaos_recovery_api_is_read_only_projection_of_operational_dashboard(monkeypatch):
    expected = {
        "schema_version": "open_stock_ai.chaos_recovery_dashboard.v1",
        "status": "not_configured",
        "triggered": False,
    }
    monkeypatch.setattr(
        "stock_ai.agent_api.get_operational_alert_runtime",
        lambda: SimpleNamespace(dashboard=lambda: {"chaos_recovery": expected}),
    )

    response = client.get("/api/agents/chaos-recovery")

    assert response.status_code == 200
    assert response.json() == expected


class FakeAgentService:
    default_driver = "codex"

    def __init__(self):
        self.drivers = {
            "codex": SimpleNamespace(describe=lambda: {"configured": True}),
            "openai-compatible": SimpleNamespace(describe=lambda: {"configured": False}),
        }

    def describe(self):
        return {
            "schema_version": "open_stock_ai.agent_runtime.v1",
            "architecture": "embedded_model_turns_plus_host_executed_tools",
            "default_driver": "codex",
            "drivers": [{"id": "codex", "native_capabilities_preserved": True}],
            "tools": [{"name": "market.analyze_symbol"}],
            "boundaries": {"codex_native_capabilities_preserved": True},
        }

    async def run(self, **kwargs):
        event_sink = kwargs.get("event_sink")
        if event_sink:
            await event_sink(
                {
                    "sequence": 1,
                    "type": "tool.started",
                    "tool": "market.analyze_symbol",
                    "skills": ["market-analysis"],
                    "packages": ["OpenStockAIEngine"],
                    "schedules": [],
                }
            )
        result = {
            "schema_version": "open_stock_ai.agent_run.v1",
            "status": "completed",
            "driver": kwargs["driver_id"] or "codex",
            "autonomy": kwargs["autonomy"],
            "symbols": kwargs["symbols"],
            "decision": {"action": "watch"},
            "tool_trace": [{"tool": "market.analyze_symbol", "ok": True}],
            "paper_execution_count": 0,
            "live_execution_count": 0,
        }
        return result


class FakeDurableRuntime:
    def __init__(self, service=None):
        self.service = service or FakeAgentService()
        self.requests = {}
        self.runs = {}

    async def create_run(self, **kwargs):
        run_id = f"AR-fake-{len(self.requests) + 1}"
        self.requests[run_id] = kwargs
        run = {"run_id": run_id, "status": "queued", "terminal": False}
        self.runs[run_id] = run
        return run

    async def wait(self, run_id):
        request = self.requests[run_id]
        result = await self.service.run(**request, run_id=run_id)
        self.runs[run_id] = {"run_id": run_id, "status": "completed", "terminal": True, "result": result}
        return result

    async def stream(self, run_id, after_sequence=0):
        del after_sequence
        events = []

        async def sink(event):
            events.append(event)

        request = self.requests[run_id]
        result = await self.service.run(**request, run_id=run_id, event_sink=sink)
        for event in events:
            yield {"type": "activity", "event": event}
        self.runs[run_id] = {"run_id": run_id, "status": "completed", "terminal": True, "result": result}
        yield {"type": "result", "run_id": run_id, "status": "completed", "result": result}

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def list_runs(self, limit=50):
        return list(self.runs.values())[:limit]

    async def cancel(self, run_id):
        if run_id not in self.runs:
            return None
        self.runs[run_id] = {"run_id": run_id, "status": "cancelled", "terminal": True}
        return self.runs[run_id]

    def snapshot(self, run_id):
        run = self.runs.get(run_id)
        if run is None:
            return None
        return {
            "schema_version": "open_stock_ai.agent_run_snapshot.v2",
            "run": run,
            "current_plan": None,
            "plan_revisions": [],
            "steps": [],
            "tool_calls": [],
            "approvals": [],
            "artifacts": [],
            "events": [],
            "environment_snapshot": {},
            "last_sequence": 0,
            "final_result": run.get("result"),
        }

    async def pause(self, run_id):
        if run_id not in self.runs:
            return None
        self.runs[run_id] = {"run_id": run_id, "status": "suspended", "terminal": False}
        return self.runs[run_id]

    async def retry(self, run_id):
        if run_id not in self.runs:
            return None
        self.runs[run_id] = {"run_id": run_id, "status": "queued", "terminal": False}
        return self.runs[run_id]

    async def continue_after_limit(self, run_id, additional_steps=6, max_steps=None):
        if run_id not in self.runs:
            return None
        self.runs[run_id] = {
            **self.runs[run_id],
            "status": "queued",
            "terminal": False,
            "max_steps": max_steps or additional_steps,
        }
        return self.runs[run_id]

    def get_workflow(self, workflow_id):
        if workflow_id != "AWF-demo":
            return None
        return {"workflow_id": workflow_id, "namespace": "stock-ai", "name": "demo", "plan": {}}

    async def run_workflow(self, workflow_id, **kwargs):
        if self.get_workflow(workflow_id) is None:
            raise KeyError(workflow_id)
        return await self.create_run(
            objective=kwargs.get("objective") or "workflow objective",
            symbols=[],
            driver_id="codex",
            autonomy=kwargs.get("autonomy") or "advisory",
            max_steps=kwargs.get("max_steps") or 6,
            session_id=kwargs.get("session_id"),
            parent_run_id=kwargs.get("parent_run_id"),
            initial_plan={"workflow_id": workflow_id},
        )

    async def trigger_automation_event(self, event_type, payload):
        return [{"event_type": event_type, "payload": payload, "status": "completed"}]


def test_interoperable_agent_routes_are_exposed():
    paths = set(app.openapi()["paths"])
    assert "/api/agents" in paths
    assert "/api/agents/tools" in paths
    assert "/api/agents/observability" in paths
    assert "/api/agents/storage" in paths
    assert "/api/agents/settings" in paths
    assert "/api/agents/providers/{provider_id}/health" in paths
    assert "/api/agents/providers/openai-compatible/models" in paths
    assert "/api/agents/run" in paths
    assert "/api/agents/run/stream" in paths
    assert "/api/agents/runs" in paths
    assert "/api/agents/runs/{run_id}" in paths
    assert "/api/agents/runs/{run_id}/snapshot" in paths
    assert "/api/agents/runs/{run_id}/pause" in paths
    assert "/api/agents/runs/{run_id}/retry" in paths
    assert "/api/agents/runs/{run_id}/continue" in paths
    assert "/api/agents/runs/{run_id}/stream" in paths
    assert "/api/agents/runs/{run_id}/cancel" in paths
    assert "/api/agents/sessions/{session_id}/messages" in paths
    assert "/api/agents/sessions/{session_id}/forest" in paths
    assert "/api/agents/branches/{branch_id}" in paths
    assert "/api/agents/branches/{branch_id}/pause" in paths
    assert "/api/agents/interactions/{interaction_id}/respond" in paths
    assert "/api/agents/artifacts/{artifact_id}" in paths
    assert "/api/agents/artifacts/{artifact_id}/select" in paths
    assert "/api/agents/artifacts/{artifact_id}/propose-change" in paths
    assert "/api/agents/automations/preview" in paths
    assert "/api/agents/automations/{automation_id}" in paths
    assert "/api/agents/automations/{automation_id}/archive" in paths
    assert "/api/agents/automations/callback-receipts" in paths
    assert "/api/agents/automations/events" in paths
    assert "/api/agents/events/stream" in paths
    assert "/api/agents/ui/state" in paths
    assert "/api/agents/ui/commands" in paths
    assert "/api/agents/ui/commands/{command_id}/result" in paths
    assert "/api/codex/run" in paths
    assert "/api/codex/capabilities" in paths


def test_agent_runtime_and_tools_endpoints(monkeypatch):
    fake = FakeAgentService()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: fake)

    runtime = client.get("/api/agents")
    tools = client.get("/api/agents/tools")

    assert runtime.status_code == 200
    assert runtime.json()["boundaries"]["codex_native_capabilities_preserved"] is True
    assert tools.status_code == 200
    assert tools.json()["items"][0]["name"] == "market.analyze_symbol"


def test_critical_api_requests_record_runtime_slo_without_self_observing_dashboard(monkeypatch):
    class SLORecorder:
        def __init__(self):
            self.calls = []

        def record_slo_observation(self, service, **payload):
            self.calls.append((service, payload))
            return {"service": service, "status": "pass", "report_sha256": "a" * 64}

    recorder = SLORecorder()
    monkeypatch.setattr("stock_ai.main.get_agent_run_runtime", lambda: recorder)
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/run",
        json={"objective": "只做分析，不建立訂單", "autonomy": "advisory"},
    )

    assert response.status_code == 200
    assert response.headers["X-Stock-AI-SLO-API"] == "pass"
    assert recorder.calls[0][0] == "api.request"
    assert recorder.calls[0][1]["latency_ms"] >= 0
    assert recorder.calls[0][1]["success"] is True


def test_automation_event_endpoint_dispatches_to_durable_runtime(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/automations/events",
        json={"event_type": "market.tick", "payload": {"price": 125}},
    )

    assert response.status_code == 202
    assert response.json() == {
        "schema_version": "open_stock_ai.automation_event_result.v1",
        "count": 1,
        "items": [{"event_type": "market.tick", "payload": {"price": 125}, "status": "completed"}],
    }


def test_verified_automation_callback_returns_only_receipt_identifiers(monkeypatch):
    class Durable(FakeDurableRuntime):
        def record_verified_automation_callback(self, **kwargs):
            assert kwargs["authentication"]["authenticated"] is True
            return {"receipt_id": "ACR-proof", "receipt_sha256": "f" * 64}

    durable = Durable()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)
    request = SimpleNamespace(
        state=SimpleNamespace(
            automation_callback_authentication={
                "source": "n8n", "authenticated": True, "token_sha256": "not-a-secret"
            }
        )
    )

    import asyncio

    result = asyncio.run(
        trigger_agent_automation_event(
            AgentScheduleEvent(event_type="automation.n8n.trigger", payload={"source": "n8n"}),
            request,
        )
    )

    assert result["callback_receipt_id"] == "ACR-proof"
    assert result["callback_receipt_sha256"] == "f" * 64
    assert "token_sha256" not in result


def test_archive_automation_endpoint_preserves_auditable_record(monkeypatch):
    archived: list[str] = []

    class Automations:
        def archive(self, automation_id):
            archived.append(automation_id)
            return {"automation_id": automation_id, "state": "archived"}

    durable = SimpleNamespace(final_runtime=SimpleNamespace(automations=Automations()))
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post("/api/agents/automations/AUT-obsolete/archive")

    assert response.status_code == 200
    assert archived == ["AUT-obsolete"]
    assert response.json() == {"automation_id": "AUT-obsolete", "state": "archived"}


def test_agent_run_uses_selected_codex_provider_and_preserves_autonomy(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/run",
        json={
            "objective": "use the system to evaluate 2330",
            "symbols": ["2330.TW"],
            "driver": "codex",
            "autonomy": "advisory",
            "max_steps": 4,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["driver"] == "codex"
    assert payload["tool_trace"][0]["tool"] == "market.analyze_symbol"
    assert payload["live_execution_count"] == 0


def test_market_question_clears_selected_symbol_and_keeps_market_scope(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/runs",
        json={
            "objective": "現在什麼可以買？",
            "symbols": ["2330.TW"],
            "driver": "codex",
            "context_scope": "market",
        },
    )

    assert response.status_code == 202
    request = durable.requests[response.json()["run_id"]]
    assert request["driver_id"] == "codex"
    assert request["symbols"] == []
    assert request["run_metadata"]["context_scope"] == "market"
    assert request["objective"].startswith("[MARKET_SCOPE]")
    assert "2330.TW" not in request["objective"]


def test_intent_classifier_uses_selected_provider_and_does_not_bias_market_scope(monkeypatch):
    calls = []

    class Provider:
        async def start_session(self, session_id, *, project_root):
            calls.append(("start", session_id, project_root))

        async def generate_structured(self, session_id, prompt, schema):
            calls.append(("generate", session_id, prompt, schema))
            return {
                "title": "全市場可投資標的盤點",
                "category": "market_analysis",
                "scope": "market",
                "use_selected_symbol": True,
                "symbols": ["2330.TW"],
            }

        async def close_session(self, session_id):
            calls.append(("close", session_id))

        def capabilities(self):
            return {"model": "gpt-oss:20b"}

    provider = Provider()
    service = FakeAgentService()
    service.provider_registry = SimpleNamespace(get=lambda driver_id: provider)
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: service)

    response = client.post(
        "/api/agents/classify-intent",
        json={
            "objective": "現在什麼可以買？",
            "selected_symbol": "2330.TW",
            "driver": "codex",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "全市場可投資標的盤點"
    assert payload["scope"] == "market"
    assert payload["use_selected_symbol"] is False
    assert payload["symbols"] == []
    assert payload["model"] == "gpt-oss:20b"
    assert any(item[0] == "generate" and "2330.TW" in item[2] for item in calls)
    classifier_prompt = next(item[2] for item in calls if item[0] == "generate")
    assert "Fishbone" in classifier_prompt
    assert "failed branch" in classifier_prompt


def test_intent_classifier_promotes_explicit_paper_order_to_market_decision(monkeypatch):
    class Provider:
        async def start_session(self, session_id, *, project_root):
            return None

        async def generate_structured(self, session_id, prompt, schema):
            # Reproduces GPT-OSS classifying an explicit paper order as a
            # generic market-analysis question.
            return {
                "title": "全市場候選分析",
                "category": "market_analysis",
                "scope": "market",
                "use_selected_symbol": False,
                "symbols": [],
            }

        async def close_session(self, session_id):
            return None

        def capabilities(self):
            return {"model": "gpt-oss:20b"}

    service = FakeAgentService()
    service.provider_registry = SimpleNamespace(get=lambda driver_id: Provider())
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: service)

    response = client.post(
        "/api/agents/classify-intent",
        json={"objective": "模擬交易下單一個賺錢機率最高的股票", "driver": "codex"},
    )

    assert response.status_code == 200
    assert response.json()["category"] == "market_decision"
    assert response.json()["scope"] == "market"


def test_intent_classifier_uses_local_security_master_for_literal_taiwan_company_name(monkeypatch):
    class Provider:
        async def start_session(self, session_id, *, project_root):
            return None

        async def generate_structured(self, session_id, prompt, schema):
            # Reproduces the native GPT-OSS failure: 台新新光金 was incorrectly
            # emitted as the unrelated US ticker TSN.
            return {
                "title": "台新新光金紙上模擬交易",
                "category": "market_decision",
                "scope": "instrument",
                "use_selected_symbol": False,
                "symbols": ["TSN"],
            }

        async def close_session(self, session_id):
            return None

        def capabilities(self):
            return {"model": "gpt-oss:20b"}

    service = FakeAgentService()
    service.provider_registry = SimpleNamespace(get=lambda driver_id: Provider())
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: service)
    monkeypatch.setattr(
        "stock_ai.agent_api.get_market_data_platform",
        lambda: SimpleNamespace(
            securities=lambda **_kwargs: [
                {
                    "symbol": "2887.TW",
                    "name": "台新新光金",
                    "legal_name": "台新新光金融控股股份有限公司",
                },
                {
                    "symbol": "2888.TW",
                    "name": "新光金",
                    "legal_name": "台新新光金融集團關聯公司",
                }
            ]
        ),
    )

    response = client.post(
        "/api/agents/classify-intent",
        json={"objective": "請分析台新新光金，然後做一筆紙上模擬交易。", "driver": "codex"},
    )

    assert response.status_code == 200
    assert response.json()["scope"] == "instrument"
    assert response.json()["symbols"] == ["2887.TW"]


def test_intent_classifier_prevents_fishbone_evidence_recovery_from_becoming_ui_task(monkeypatch):
    class Provider:
        async def start_session(self, session_id, *, project_root):
            return None

        async def generate_structured(self, session_id, prompt, schema):
            # Model mistake reproduced from the native desktop acceptance run.
            return {
                "title": "修復 Fishbone 資料來源",
                "category": "system",
                "scope": "neutral",
                "use_selected_symbol": False,
                "symbols": [],
            }

        async def close_session(self, session_id):
            return None

        def capabilities(self):
            return {"model": "gpt-oss:20b"}

    service = FakeAgentService()
    service.provider_registry = SimpleNamespace(get=lambda driver_id: Provider())
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: service)

    response = client.post(
        "/api/agents/classify-intent",
        json={
            "objective": "請針對 Fishbone 失敗做局部修復，使用不重複的替代資料來源與證據。",
            "driver": "codex",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["category"] == "research"
    assert payload["scope"] == "neutral"


def test_model_intent_becomes_the_runtime_hint_without_keyword_routing():
    objective, symbols, scope = _run_context(
        AgentRunRequest(
            objective="幫我整理這個主題並找出下一步。",
            context_scope="neutral",
            intent={
                "title": "整理研究主題與下一步",
                "category": "research",
                "scope": "neutral",
                "source": "model",
            },
        )
    )

    assert objective.startswith("[MODEL_TASK_KIND:market_information]\n")
    assert symbols == []
    assert scope == "neutral"


def test_explicit_paper_order_upgrades_only_default_advisory_autonomy():
    request = AgentRunRequest(
        objective="請分析 2887.TW，並完成一筆 100 股買進紙上模擬交易。",
        autonomy="advisory",
    )

    objective, _, _ = _run_context(request)

    assert _effective_autonomy(request, objective) == "paper_execute"
    assert _effective_autonomy(
        request.model_copy(update={"autonomy": "full_execute"}),
        objective,
    ) == "full_execute"
    assert _effective_autonomy(
        request.model_copy(update={"objective": "僅分析 2887.TW，不建立紙上交易。"}),
        "僅分析 2887.TW，不建立紙上交易。",
    ) == "advisory"
    assert _effective_autonomy(
        request.model_copy(
            update={
                "objective": "只做 2887.TW 分析；不要建立紙上訂單、不要等待外部驗收條件。",
                "autonomy": "paper_execute",
            }
        ),
        "只做 2887.TW 分析；不要建立紙上訂單、不要等待外部驗收條件。",
    ) == "advisory"
    assert _effective_autonomy(
        request.model_copy(
            update={
                "objective": "只做研究，不建立 Artifact、紙上交易、實盤交易或自動化。",
                "autonomy": "paper_execute",
            }
        ),
        "只做研究，不建立 Artifact、紙上交易、實盤交易或自動化。",
    ) == "advisory"


def test_model_task_kind_header_is_idempotent_when_rerunning_a_durable_objective():
    objective, symbols, scope = _run_context(
        AgentRunRequest(
            objective=(
                "[MODEL_TASK_KIND:market_information]\n"
                "[MODEL_TASK_KIND:market_information]\n"
                "請完整分析 2330.TW。"
            ),
            symbols=["2330.TW"],
            context_scope="instrument",
            intent={
                "category": "instrument_analysis",
                "scope": "instrument",
                "source": "model",
            },
        )
    )

    assert objective.count("[MODEL_TASK_KIND:market_information]") == 1
    assert objective.endswith("請完整分析 2330.TW。")
    assert symbols == ["2330.TW"]
    assert scope == "instrument"


def test_model_market_intent_keeps_selected_symbol_out_of_whole_market_run(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/runs",
        json={
            "objective": "現在什麼可以買？",
            "symbols": ["2330.TW"],
            "context_scope": "market",
            "intent": {
                "title": "全市場可投資標的盤點",
                "category": "market_analysis",
                "scope": "market",
                "source": "model",
            },
            "driver": "codex",
        },
    )

    assert response.status_code == 202
    request = durable.requests[response.json()["run_id"]]
    assert request["objective"].startswith("[MODEL_TASK_KIND:market_information]\n[MARKET_SCOPE]")
    assert request["symbols"] == []


def test_unconfigured_selected_provider_returns_actionable_422(monkeypatch):
    durable = FakeDurableRuntime()
    service = FakeAgentService()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: service)

    response = client.post(
        "/api/agents/runs",
        json={"objective": "test provider", "driver": "openai-compatible"},
    )
    assert response.status_code == 422
    assert "not configured" in response.json()["detail"]


def test_run_uses_the_initialized_service_default_not_a_second_local_preference(monkeypatch):
    """Portable API requests must not reread a stale machine-local setting."""
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)
    monkeypatch.setattr("stock_ai.agent_api.get_agent_service", lambda: durable.service)
    monkeypatch.setattr(
        "stock_ai.agent_api.load_agent_driver_settings",
        lambda: (_ for _ in ()).throw(AssertionError("the initialized service owns this setting")),
    )

    created = client.post("/api/agents/runs", json={"objective": "portable default"})

    assert created.status_code == 202
    request = durable.requests[created.json()["run_id"]]
    assert request["driver_id"] == "codex"


def test_agent_run_stream_emits_activity_before_result(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    with client.stream(
        "POST",
        "/api/agents/run/stream",
        json={"objective": "analyze 2330", "symbols": ["2330.TW"], "autonomy": "advisory"},
    ) as response:
        lines = [line for line in response.iter_lines() if line]

    assert response.status_code == 200
    assert '"type":"activity"' in lines[0]
    assert '"tool":"market.analyze_symbol"' in lines[0]
    assert '"type":"result"' in lines[-1]


def test_durable_agent_run_routes_create_inspect_and_cancel(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    created = client.post(
        "/api/agents/runs",
        json={"objective": "inspect project", "autonomy": "advisory"},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    assert client.get(f"/api/agents/runs/{run_id}").status_code == 200
    assert client.get("/api/agents/runs").json()["count"] == 1

    cancelled = client.post(f"/api/agents/runs/{run_id}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"


def test_durable_agent_run_snapshot_pause_and_retry_routes(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)
    created = client.post("/api/agents/runs", json={"objective": "durable controls"})
    run_id = created.json()["run_id"]

    snapshot = client.get(f"/api/agents/runs/{run_id}/snapshot")
    paused = client.post(f"/api/agents/runs/{run_id}/pause")
    retried = client.post(f"/api/agents/runs/{run_id}/retry")

    assert snapshot.status_code == 200
    assert snapshot.json()["schema_version"] == "open_stock_ai.agent_run_snapshot.v2"
    assert paused.json()["status"] == "suspended"
    assert retried.json()["status"] == "queued"


def test_snapshot_api_flattens_real_artifact_history_and_replays_evidence(monkeypatch):
    class SnapshotRuntime:
        def snapshot(self, run_id):
            assert run_id == "AR-contract"
            return {
                "schema_version": "open_stock_ai.agent_run_snapshot.v2",
                "run": {"run_id": run_id, "session_id": "AS-contract", "status": "completed", "terminal": True},
                "artifact_versions": {
                    "ART-report": [
                        {"version": 1, "content": {"risk": 75}},
                        {"artifact_id": "ART-report", "version": 2, "content": {"risk": "dynamic"}},
                    ]
                },
                "evidence": {"EV-state": {"evidence_id": "EV-state", "claim": "State evidence"}},
                "events": [
                    {
                        "type": "research.evidence_added",
                        "run_id": run_id,
                        "session_id": "AS-contract",
                        "branch_id": "BR-research",
                        "artifact_id": "ART-report",
                        "artifact_version": 2,
                        "payload": {
                            "evidence": {
                                "evidence_id": "EV-event",
                                "claim": "Demand remains strong",
                                "source_type": "company_ir",
                            }
                        },
                    }
                ],
            }

    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: SnapshotRuntime())

    response = client.get("/api/agents/runs/AR-contract/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert [(item["artifact_id"], item["version"]) for item in payload["artifact_versions"]] == [
        ("ART-report", 1),
        ("ART-report", 2),
    ]
    assert {item["evidence_id"] for item in payload["evidence"]} == {"EV-state", "EV-event"}
    event_evidence = next(item for item in payload["evidence"] if item["evidence_id"] == "EV-event")
    assert event_evidence["branch_id"] == "BR-research"
    assert event_evidence["artifact_id"] == "ART-report"
    assert event_evidence["artifact_version"] == 2


def test_artifact_change_api_requires_versioned_patch_contract(monkeypatch):
    captured = {}

    def revise_artifact(**kwargs):
        captured.update(kwargs)
        return {"artifact_id": kwargs["artifact_id"], "version": 4, "content": kwargs["content"]}

    runtime = SimpleNamespace(final_runtime=SimpleNamespace(revise_artifact=revise_artifact))
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: runtime)

    incompatible = client.post(
        "/api/agents/artifacts/ART-risk/propose-change",
        json={"proposal": {"content": {"risk_score": "dynamic"}}, "artifact_context_selection": {}},
    )
    accepted = client.post(
        "/api/agents/artifacts/ART-risk/propose-change",
        json={
            "expected_version": 3,
            "content": {"risk_score": "dynamic"},
            "reason": "Use a volatility-aware threshold",
            "message_id": "MSG-42",
            "affected_node_ids": ["NODE-RISK"],
        },
    )

    assert incompatible.status_code == 422
    assert accepted.status_code == 201
    assert accepted.json()["version"] == 4
    assert captured == {
        "artifact_id": "ART-risk",
        "expected_version": 3,
        "content": {"risk_score": "dynamic"},
        "changed_by": "stock_ai_ui_user",
        "reason": "Use a volatility-aware threshold",
        "message_id": "MSG-42",
        "affected_node_ids": ("NODE-RISK",),
    }


def test_continue_route_accepts_an_explicit_larger_step_budget(monkeypatch):
    durable = FakeDurableRuntime()
    durable.runs["AR-limit"] = {
        "run_id": "AR-limit",
        "status": "max_steps_reached",
        "terminal": True,
    }
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    response = client.post(
        "/api/agents/runs/AR-limit/continue",
        json={"additional_steps": 12, "max_steps": 24},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["max_steps"] == 24


def test_artifact_route_opens_and_downloads_only_the_requested_run_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "artifact.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-artifact",
        namespace="stock-ai",
        title="artifact test",
    )
    store = AgentRunStore(path)
    store.create_run(
        "AR-artifact",
        {
            "objective": "artifact",
            "symbols": [],
            "driver_id": "codex",
            "autonomy": "advisory",
            "max_steps": 6,
            "session_id": "AS-artifact",
        },
    )
    artifacts = ArtifactStore(path, tmp_path / "artifacts")
    artifact = artifacts.create_text(
        session_id="AS-artifact",
        run_id="AR-artifact",
        name="report.txt",
        content="verified report",
        metadata={"step_id": "finalize", "evidence_ids": ["EV-1"]},
    )
    created_history = artifacts.versions.store.list_versions(artifact["artifact_id"])
    assert len(created_history) == 1
    assert created_history[0].version == 1
    assert created_history[0].changed_by == "artifact_store"
    assert created_history[0].content["content"] == "verified report"
    runtime = DurableAgentRuntime(
        service_provider=lambda: None,
        store=store,
        artifact_store=artifacts,
    )
    snapshot = runtime.snapshot("AR-artifact")
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: runtime)
    url = f"/api/agents/runs/AR-artifact/artifacts/{artifact['artifact_id']}"

    opened = client.get(url)
    downloaded = client.get(f"{url}?download=true")
    missing = client.get(f"/api/agents/runs/AR-other/artifacts/{artifact['artifact_id']}")

    assert opened.status_code == 200
    assert opened.text == "verified report"
    assert opened.headers["content-disposition"].startswith("inline;")
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert missing.status_code == 404
    assert snapshot["artifacts"][0]["artifact_version"] == 1
    assert snapshot["artifact_versions"][artifact["artifact_id"]][0]["content"]["content"] == "verified report"


def test_structured_artifact_reload_and_versioned_patch_update_the_file_and_event(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "structured-artifact.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-structured",
        namespace="stock-ai",
        title="structured artifact test",
    )
    store = AgentRunStore(path)
    store.create_run(
        "AR-structured",
        {
            "objective": "build a structured DAG",
            "symbols": [],
            "driver_id": "codex",
            "autonomy": "advisory",
            "max_steps": 6,
            "session_id": "AS-structured",
        },
    )
    artifacts = ArtifactStore(path, tmp_path / "artifacts")
    artifact = artifacts.create_structured(
        session_id="AS-structured",
        run_id="AR-structured",
        name="plan.json",
        renderer="dag",
        schema_version="open_stock_ai.visualization.v1",
        document={"title": "Plan", "nodes": [{"id": "N1", "label": "Research"}]},
        node_id="N1",
    )
    created_history = artifacts.versions.store.list_versions(artifact["artifact_id"])
    assert len(created_history) == 1
    assert created_history[0].content["renderer"] == "dag"
    assert created_history[0].content["document"]["nodes"][0]["label"] == "Research"
    runtime = DurableAgentRuntime(
        service_provider=lambda: None,
        store=store,
        artifact_store=artifacts,
    )
    snapshot = runtime.snapshot("AR-structured")
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: runtime)

    assert snapshot["artifacts"][0]["renderer"] == "dag"
    assert snapshot["artifacts"][0]["document"]["nodes"][0]["label"] == "Research"
    assert snapshot["artifact_versions"][artifact["artifact_id"]][0]["content"]["renderer"] == "dag"

    response = client.post(
        f"/api/agents/artifacts/{artifact['artifact_id']}/propose-change",
        json={
            "expected_version": 1,
            "content": {
                "renderer": "dag",
                "schema_version": "open_stock_ai.visualization.v1",
                "document": {"title": "Plan", "nodes": [{"id": "N1", "label": "Verified research"}]},
            },
            "reason": "Clarify the selected node",
            "affected_node_ids": ["N1"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["version"] == 2
    assert payload["events"][0]["type"] == "artifact.updated"
    reloaded = artifacts.get("AR-structured", artifact["artifact_id"])
    assert reloaded is not None
    assert reloaded["document"]["nodes"][0]["label"] == "Verified research"
    assert runtime.snapshot("AR-structured")["artifact_versions"][artifact["artifact_id"]][-1]["version"] == 2

    stale = client.post(
        f"/api/agents/artifacts/{artifact['artifact_id']}/propose-change",
        json={
            "expected_version": 1,
            "content": payload["content"],
            "reason": "Stale edit",
            "affected_node_ids": ["N1"],
        },
    )
    assert stale.status_code == 409


def test_workflow_get_and_run_routes_use_the_durable_runtime(monkeypatch):
    durable = FakeDurableRuntime()
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: durable)

    workflow = client.get("/api/agents/workflows/AWF-demo")
    created = client.post(
        "/api/agents/workflows/AWF-demo/runs",
        json={"objective": "run a copied workflow", "autonomy": "advisory", "max_steps": 4},
    )

    assert workflow.status_code == 200
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    assert durable.requests[run_id]["initial_plan"] == {"workflow_id": "AWF-demo"}


def test_agent_settings_api_activates_external_provider_without_returning_secrets(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "stock_ai.agent_api.save_agent_preferences",
        lambda values: captured.update(values) or object(),
    )
    monkeypatch.setattr("stock_ai.agent_api.clear_agent_service", lambda: captured.update(cleared=True))
    monkeypatch.setattr(
        "stock_ai.agent_api.public_agent_preferences",
        lambda settings=None: {
            "schema_version": "open_stock_ai.agent_settings.v1",
            "default_driver": "external-agent",
            "external_agent": {"token_configured": True},
            "secrets_returned": False,
        },
    )

    response = client.post(
        "/api/agents/settings",
        json={
            "default_driver": "external-agent",
            "external_endpoint": "http://127.0.0.1:9000/turn",
            "external_framework": "hermes-compatible",
            "external_token": "top-secret",
        },
    )

    assert response.status_code == 200
    assert captured["default_driver"] == "external-agent"
    assert captured["external_endpoint"] == "http://127.0.0.1:9000/turn"
    assert captured["external_token"] == "top-secret"
    assert captured["cleared"] is True
    assert "top-secret" not in response.text
