from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import stock_ai.codex_runtime as runtime_module
from open_stock_ai.agent_runtime.providers.codex import CodexProvider
from stock_ai.codex_runtime import CodexRuntime


def _model(name, efforts, default, *, is_default=False):
    return {
        "id": name, "model": name, "display_name": name,
        "description": "Account model", "is_default": is_default,
        "default_reasoning_effort": default,
        "supported_reasoning_efforts": [
            {"reasoning_effort": effort, "description": effort} for effort in efforts
        ],
    }


@pytest.fixture
def runtime_and_sdk(monkeypatch, tmp_path):
    state = SimpleNamespace(
        starts=[], turns=[], catalog_reads=0,
        default_model="large-v1", default_effort="ultra",
        models=[
            _model("large-v1", ["high", "ultra"], "high"),
            _model("small-v2", ["medium", "high"], "medium", is_default=True),
        ],
    )

    class Wire:
        async def request(self, method, params, **kwargs):
            assert method == "config/read"
            assert params["cwd"] == str(tmp_path)
            assert params["includeLayers"] is False
            return SimpleNamespace(config=SimpleNamespace(
                model=state.default_model, model_reasoning_effort=state.default_effort,
            ))

        async def thread_start(self, params):
            state.starts.append(params)
            return SimpleNamespace(
                model=params.model or state.default_model,
                reasoning_effort=(params.config or {}).get("model_reasoning_effort") or state.default_effort,
                model_provider="openai", thread=SimpleNamespace(id=f"thread-{len(state.starts)}"),
            )

    class Client:
        _client = Wire()

        async def _ensure_initialized(self):
            pass

        async def models(self):
            state.catalog_reads += 1
            return SimpleNamespace(data=state.models, next_cursor=None)

    class Turn:
        id = "turn-1"

        async def stream(self):
            if False:
                yield None

    class Thread:
        def __init__(self, client, thread_id):
            self.id = thread_id

        async def turn(self, prompt, **kwargs):
            state.turns.append({"thread_id": self.id, **kwargs})
            return Turn()

    async def collect(stream, *, turn_id):
        async for _ in stream:
            pass
        return SimpleNamespace(final_response='{"answer":"ok","content":"ok","tool_calls":[]}')

    runtime = CodexRuntime(project_root=tmp_path)
    client = Client()

    async def account():
        return {"authenticated": True}

    async def get_client():
        return client

    monkeypatch.setattr(runtime, "client", get_client)
    monkeypatch.setattr(runtime, "_require_account", account)
    monkeypatch.setattr(runtime_module, "AsyncThread", Thread)
    monkeypatch.setattr(runtime_module, "_collect_async_turn_result", collect)
    monkeypatch.setattr(runtime, "_saved_model_selection", lambda: {"model": "small-v2", "reasoning_effort": "high"})
    return runtime, state


def test_catalog_is_dynamic_and_defaults_come_from_effective_config(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk
    first = asyncio.run(runtime.model_catalog())
    assert first["defaults"] == {"model": "large-v1", "reasoning_effort": "ultra"}
    assert first["items"][1]["is_default"] is True
    sdk.models.append(_model("future-model", ["new-effort"], "new-effort"))
    second = asyncio.run(runtime.model_catalog())
    assert second["count"] == 3
    assert second["items"][-1]["supported_reasoning_efforts"][0]["reasoning_effort"] == "new-effort"


@pytest.mark.parametrize("model,effort,expected", [
    ("small-v2", "", "medium"),
    ("small-v2", "high", "high"),
    ("", "ultra", "ultra"),
])
def test_selection_uses_model_default_or_effective_global_model(runtime_and_sdk, model, effort, expected):
    runtime, _ = runtime_and_sdk
    resolved = asyncio.run(runtime.validate_model_selection(model, effort))
    assert resolved == {"model": model or "large-v1", "reasoning_effort": expected}


@pytest.mark.parametrize("model,effort", [("unavailable", "high"), ("small-v2", "ultra"), ("", "medium")])
def test_unsupported_selection_is_rejected_before_thread_start(runtime_and_sdk, model, effort):
    runtime, sdk = runtime_and_sdk
    with pytest.raises(ValueError, match="Codex"):
        asyncio.run(runtime.start_agent_run("bad", model=model, reasoning_effort=effort))
    assert sdk.starts == []


def test_blank_settings_inherit_and_record_actual_sdk_configuration(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk
    asyncio.run(runtime.start_agent_run("defaults"))
    params = sdk.starts[0]
    assert params.model is None and params.config is None
    assert params.ephemeral is True
    assert str(params.sandbox.value) == "read-only"
    assert sdk.catalog_reads == 0
    metadata = runtime.agent_session_metadata("defaults")
    assert metadata["model"] == "large-v1"
    assert metadata["reasoning_effort"] == "ultra"
    assert metadata["selected_model"] == metadata["selected_reasoning_effort"] == ""
    assert metadata["resolution_source"] == "sdk_thread_start"


def test_new_provider_settings_do_not_change_existing_run_and_turns_are_pinned(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk
    old = CodexProvider(runtime)
    new = CodexProvider(runtime, model="small-v2", reasoning_effort="high")
    events = []

    async def scenario():
        await old.start_session("old", project_root=str(runtime.project_root))
        await new.start_session("old", project_root=str(runtime.project_root))
        await new.start_session("new", project_root=str(runtime.project_root))
        await new.generate_structured("old", "hello", {}, event_sink=events.append)
        await new.generate_structured("new", "hello", {}, event_sink=events.append)

    asyncio.run(scenario())
    assert len(sdk.starts) == 2
    assert [turn["model"] for turn in sdk.turns] == ["large-v1", "small-v2"]
    assert [turn["effort"].value for turn in sdk.turns] == ["ultra", "high"]
    assert [event["type"] for event in events] == ["model.session.configured"] * 2
    assert events[0]["model"] == "large-v1"
    assert events[1]["reasoning_effort"] == "high"


def test_compaction_and_resumed_checkpoint_keep_original_resolved_model(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk
    provider = CodexProvider(runtime)

    async def scenario():
        await provider.start_session("paused", project_root=str(runtime.project_root))
        metadata = provider.session_metadata("paused")
        sdk.default_model, sdk.default_effort = "small-v2", "medium"
        await provider.compact_context("paused")
        assert provider.session_metadata("paused")["model"] == "large-v1"
        assert provider.session_metadata("paused")["reasoning_effort"] == "ultra"
        await provider.close_session("paused")
        replacement = CodexProvider(runtime, model="small-v2", reasoning_effort="high")
        replacement.restore_session_selection("paused", metadata)
        await replacement.start_session("paused", project_root=str(runtime.project_root))
        assert replacement.session_metadata("paused")["model"] == "large-v1"

    asyncio.run(scenario())


def test_market_radar_and_new_bridge_threads_use_saved_selection(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk

    async def scenario():
        await runtime.run_structured("radar", {})
        await runtime.run_llm_bridge_turn(run_id="bridge", messages=[])
        await runtime.run_llm_bridge_turn(run_id="bridge", messages=[])

    asyncio.run(scenario())
    assert len(sdk.starts) == 2
    assert all(params.model == "small-v2" for params in sdk.starts)
    assert all(params.config == {"model_reasoning_effort": "high"} for params in sdk.starts)
    assert all(turn["model"] == "small-v2" and turn["effort"].value == "high" for turn in sdk.turns)


def test_bridge_first_started_after_setting_change_inherits_its_host_run(runtime_and_sdk):
    runtime, sdk = runtime_and_sdk

    async def scenario():
        await runtime.start_agent_run("host-run")
        await runtime.run_llm_bridge_turn(run_id="host-run:external-framework", messages=[])

    asyncio.run(scenario())
    assert sdk.starts[-1].model == "large-v1"
    assert sdk.starts[-1].config == {"model_reasoning_effort": "ultra"}
    assert sdk.turns[-1]["model"] == "large-v1"


@pytest.fixture
def api_client(monkeypatch, tmp_path, runtime_and_sdk):
    import stock_ai.agent_api as api
    import stock_ai.agent_drivers as drivers

    runtime, sdk = runtime_and_sdk
    monkeypatch.setenv("STOCK_AI_AGENT_SETTINGS_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("STOCK_AI_AGENT_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.delenv("STOCK_AI_AGENT_DRIVER", raising=False)
    monkeypatch.setattr(drivers, "agent_secret_store", SimpleNamespace(get=lambda _: ""))
    monkeypatch.setattr(api, "codex_runtime", runtime)
    monkeypatch.setattr(api, "clear_agent_service", lambda: None)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), sdk, tmp_path / "preferences.json"


def test_models_endpoint_and_settings_round_trip_and_clear(api_client):
    client, sdk, _ = api_client
    assert client.get("/api/agents/providers/codex/models").json()["count"] == 2
    result = client.post("/api/agents/settings", json={"codex_model": "small-v2", "codex_reasoning_effort": "high"})
    assert result.status_code == 200
    assert result.json()["codex"] == {"model": "small-v2", "reasoning_effort": "high"}
    assert client.get("/api/agents/settings").json()["codex"] == result.json()["codex"]
    reads = sdk.catalog_reads
    cleared = client.post("/api/agents/settings", json={"codex_model": "", "codex_reasoning_effort": ""})
    assert cleared.status_code == 200
    assert cleared.json()["codex"] == {"model": "", "reasoning_effort": ""}
    assert sdk.catalog_reads == reads
    assert sdk.starts == []


def test_invalid_selection_does_not_persist_partial_settings(api_client):
    client, _, path = api_client
    accepted = client.post("/api/agents/settings", json={"codex_model": "small-v2", "codex_reasoning_effort": "high"})
    assert accepted.status_code == 200
    before = path.read_bytes()
    rejected = client.post("/api/agents/settings", json={"codex_reasoning_effort": "ultra"})
    assert rejected.status_code == 422
    assert "small-v2" in rejected.json()["detail"]
    assert path.read_bytes() == before


def test_switching_provider_does_not_require_auth_for_unchanged_codex_settings(api_client, monkeypatch):
    client, sdk, _ = api_client
    accepted = client.post("/api/agents/settings", json={"codex_model": "small-v2", "codex_reasoning_effort": "high"})
    assert accepted.status_code == 200
    import stock_ai.agent_api as api
    from stock_ai.codex_runtime import CodexAuthenticationRequired

    async def logged_out():
        raise CodexAuthenticationRequired("請先登入 ChatGPT")

    monkeypatch.setattr(api.codex_runtime, "_require_account", logged_out)
    reads = sdk.catalog_reads
    result = client.post("/api/agents/settings", json={
        "default_driver": "openai-compatible", "openai_base_url": "http://127.0.0.1:11434/v1",
        "openai_model": "local-test", "openai_no_key": True,
        "codex_model": "small-v2", "codex_reasoning_effort": "high",
    })
    assert result.status_code == 200
    assert result.json()["default_driver"] == "openai-compatible"
    assert sdk.catalog_reads == reads
    assert client.get("/api/agents/providers/codex/models").status_code == 401


def test_settings_rebuild_registry_for_new_dispatch_without_mutating_old_provider(api_client, monkeypatch):
    import stock_ai.agent_service as service
    import stock_ai.agent_drivers as drivers

    client, _, _ = api_client
    monkeypatch.setattr(service, "_SERVICE", None)
    component_names = (
        "tools", "plan_manager", "checkpoint_manager", "approval_manager", "memory_manager",
        "snapshot_builder", "worker_supervisor", "validator", "policy_engine", "rollback_manager",
    )
    components = {name: object() for name in component_names}
    components["run_store"] = SimpleNamespace(consume_control_messages=lambda _: [])
    monkeypatch.setattr(service, "_runtime_components", lambda: components)
    monkeypatch.setattr(service, "AgentOrchestrator", lambda **kwargs: SimpleNamespace(**kwargs))
    before = service.get_agent_service()
    before_provider = before.provider_registry.get("codex")
    assert before_provider.model == ""
    response = client.post("/api/agents/settings", json={"codex_model": "small-v2", "codex_reasoning_effort": "high"})
    assert response.status_code == 200
    service.clear_agent_service()
    after = service.get_agent_service()
    assert after is not before
    assert after.provider_registry.get("codex").model == "small-v2"
    assert after.provider_registry.get("codex").reasoning_effort == "high"
    assert before_provider.model == ""
    assert drivers.load_agent_driver_settings().codex_model == "small-v2"
