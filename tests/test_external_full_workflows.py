from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
import stock_ai.external_project_tools as external_project_tools_module
from stock_ai.external_project_tools import ExternalProjectToolProvider, _workflow_env
from stock_ai.external_workflow_worker import _project_config_path


FULL_TOOLS = {
    "external.tradingagents.analyze_symbol",
    "external.fingpt.run_sentiment_model",
    "external.finrl.train_policy",
    "external.finrl.predict_actions",
    "external.qlib.train_factor_model",
    "external.qlib.backtest_model",
    "external.qlib.reference_risk_metrics",
    "external.finrobot.generate_financial_report",
}


def _runtime_fingerprint() -> dict:
    package_lock = [{"name": "framework", "version": "1.0"}]
    return {
        "schema_version": "open_stock_ai.external_worker_runtime_fingerprint.v1",
        "python_implementation": "CPython",
        "python_version": "3.12.0",
        "platform": "test",
        "machine": "test",
        "package_lock": package_lock,
        "package_lock_sha256": hashlib.sha256(
            json.dumps(package_lock, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def _context(*, elevated: bool) -> AgentRunContext:
    return AgentRunContext(
        run_id="AR-full-external",
        autonomy="external_execute" if elevated else "advisory",
        symbols=("2330.TW",),
        allow_external_actions=elevated,
    )


def _isolated_provider(tmp_path):
    project = tmp_path / "project"
    module = project / "external" / "FinGPT" / "upstream.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 1\n", encoding="utf-8")
    lock = project / "config" / "external_sources.lock.yaml"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        """schema_version: test\nprojects:\n  fingpt:\n    name: FinGPT\n    path: external/FinGPT\n    origin: https://example.invalid/FinGPT.git\n    branch: main\n    head: 1111111111111111111111111111111111111111\n""",
        encoding="utf-8",
    )
    return ExternalProjectToolProvider(project), module


def _isolated_bridge_provider(tmp_path, project_key, display_name):
    project = tmp_path / "project"
    module = project / "external" / display_name / "upstream.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 1\n", encoding="utf-8")
    lock = project / "config" / "external_sources.lock.yaml"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        "schema_version: test\nprojects:\n"
        f"  {project_key}:\n"
        f"    name: {display_name}\n"
        f"    path: external/{display_name}\n"
        f"    origin: https://example.invalid/{display_name}.git\n"
        "    branch: main\n"
        "    head: 2222222222222222222222222222222222222222\n",
        encoding="utf-8",
    )
    return ExternalProjectToolProvider(project), module


def test_full_workflow_tools_are_real_external_execution_capabilities():
    manifest = {item["name"]: item for item in ExternalProjectToolProvider().manifest()}

    assert FULL_TOOLS.issubset(manifest)
    for name in FULL_TOOLS:
        assert manifest[name]["mutating"] is True
        assert manifest[name]["requires_external_execution"] is True
        assert manifest[name]["skills"]
        assert manifest[name]["packages"][0].startswith("external/")


def test_full_workflow_tools_reject_advisory_mode_before_starting_worker():
    provider = ExternalProjectToolProvider()

    with pytest.raises(PermissionError, match="external_execute"):
        asyncio.run(
            provider.execute(
                "external.fingpt.run_sentiment_model",
                {"texts": ["revenue increased"]},
                _context(elevated=False),
            )
        )


def test_full_workflow_validates_source_lock_artifacts_and_records_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AI_FINGPT_LOCAL_MODEL_ENABLED", "true")
    provider, module = _isolated_provider(tmp_path)
    artifact = provider.workflow_root / "runs" / "AR-full-external" / "fingpt" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"label": "positive"}), encoding="utf-8")

    async def fake_invoke(project, action, payload, *, timeout):
        assert project == "fingpt"
        assert action == "run_sentiment_model"
        assert payload["_run_id"] == "AR-full-external"
        assert timeout == 900
        return {
            "ok": True,
            "worker_python": "/runtime/python",
            "executed_function": "FinoGridSentimentAnalyzer.load/score_batch",
            "upstream_modules": [str(module.relative_to(provider.project_root))],
            "result": {"scores": [{"label": "positive", "score": 1}]},
            "artifacts": [
                {
                    "path": str(artifact.relative_to(provider.project_root)),
                    "size": artifact.stat().st_size,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            ],
            "model_provenance": {"model_loaded": True, "inference_executed": True},
            "runtime_fingerprint": _runtime_fingerprint(),
        }

    monkeypatch.setattr(provider, "_invoke_workflow_worker", fake_invoke)
    result = asyncio.run(
        provider.execute(
            "external.fingpt.run_sentiment_model",
            {"texts": ["revenue increased"]},
            _context(elevated=True),
        )
    )

    assert result["schema_version"] == "open_stock_ai.external_full_workflow.v1"
    assert result["fallback_used"] is False
    assert result["module_under_locked_path"] is True
    assert result["upstream_modules"][0]["sha256"] == hashlib.sha256(module.read_bytes()).hexdigest()
    assert result["artifacts"][0]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert result["runtime_fingerprint"]["package_lock_sha256"] == _runtime_fingerprint()["package_lock_sha256"]
    status = json.loads((provider.workflow_root / "status" / "fingpt.json").read_text())
    assert status["model_loaded"] is True
    assert status["inference_executed"] is True


def test_full_workflow_reports_prerequisites_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AI_FINGPT_LOCAL_MODEL_ENABLED", "true")
    provider, _ = _isolated_provider(tmp_path)

    async def fake_invoke(*args, **kwargs):
        return {
            "ok": False,
            "error_type": "prerequisite_missing",
            "error": "FinGPT dependencies are missing",
            "missing_prerequisites": ["torch", "transformers", "peft"],
        }

    monkeypatch.setattr(provider, "_invoke_workflow_worker", fake_invoke)
    with pytest.raises(RuntimeError, match="torch, transformers, peft"):
        asyncio.run(
            provider.execute(
                "external.fingpt.run_sentiment_model",
                {"texts": ["evidence"]},
                _context(elevated=True),
            )
        )


def test_runtime_health_probes_actual_interpreter_dependencies():
    result = asyncio.run(
        ExternalProjectToolProvider().execute(
            "external.runtime.health",
            {},
            _context(elevated=False),
        )
    )

    assert result["schema_version"] == "open_stock_ai.external_runtime_health.v2"
    assert result["fallback_models"] is False
    for name in ("TradingAgents", "FinGPT", "FinRL", "Qlib", "FinRobot"):
        item = result["projects"][name]
        assert "workflow_worker_python" in item
        assert isinstance(item["workflow_missing_dependencies"], list)
        assert item["full_workflow_ready"] == (
            item["workflow_dependencies_ready"] and item["workflow_configuration_ready"]
        )


def test_local_fingpt_model_is_preserved_but_disabled_when_codex_is_primary():
    provider = ExternalProjectToolProvider()
    manifest = {item["name"]: item for item in provider.manifest()}

    assert manifest["external.fingpt.run_sentiment_model"]["available"] is False
    assert manifest["external.fingpt.run_sentiment_model"]["availability_reason"] == (
        "local_fingpt_disabled_codex_is_primary"
    )
    with pytest.raises(PermissionError, match="Codex is the configured primary model driver"):
        asyncio.run(
            provider.execute(
                "external.fingpt.run_sentiment_model",
                {"texts": ["do not load a local model"]},
                _context(elevated=True),
            )
        )


@pytest.mark.parametrize(
    ("project_key", "display_name", "tool_name", "arguments", "token_env"),
    [
        (
            "tradingagents",
            "TradingAgents",
            "external.tradingagents.analyze_symbol",
            {"symbol": "AAPL", "trade_date": "2025-01-10"},
            "OPENAI_COMPATIBLE_API_KEY",
        ),
        (
            "finrobot",
            "FinRobot",
            "external.finrobot.generate_financial_report",
            {"prompt": "report"},
            "STOCK_AI_CODEX_BRIDGE_TOKEN",
        ),
    ],
)
def test_llm_framework_workers_are_forced_through_run_scoped_codex_bridge(
    tmp_path,
    monkeypatch,
    project_key,
    display_name,
    tool_name,
    arguments,
    token_env,
):
    provider, module = _isolated_bridge_provider(tmp_path, project_key, display_name)
    captured = {}

    class FakeBridge:
        def __init__(self, runtime, *, run_id, event_sink=None):
            self.run_id = run_id
            self.event_sink = event_sink
            self.base_url = "http://127.0.0.1:54321/v1"
            self.token = "ephemeral-test-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def evidence(self):
            return {
                "driver": "codex_app_server",
                "host": "127.0.0.1",
                "bearer_token_exposed": False,
                "request_count": 1,
            }

    async def fake_invoke(project, action, payload, *, timeout, environment_overrides=None):
        captured.update(
            {
                "project": project,
                "action": action,
                "payload": payload,
                "timeout": timeout,
                "environment_overrides": environment_overrides,
            }
        )
        return {
            "ok": True,
            "worker_python": "/runtime/python",
            "executed_function": "upstream.workflow",
            "upstream_modules": [str(module.relative_to(provider.project_root))],
            "result": {"completed": True},
            "artifacts": [],
            "model_provenance": {"workflow_executed": True},
            "runtime_fingerprint": _runtime_fingerprint(),
        }

    monkeypatch.setattr(external_project_tools_module, "CodexLLMBridge", FakeBridge)
    monkeypatch.setattr(provider, "_invoke_workflow_worker", fake_invoke)
    result = asyncio.run(provider.execute(tool_name, arguments, _context(elevated=True)))

    assert captured["payload"]["base_url" if project_key == "finrobot" else "backend_url"] == (
        "http://127.0.0.1:54321/v1"
    )
    assert captured["environment_overrides"] == {token_env: "ephemeral-test-token"}
    assert result["model_provenance"]["llm_driver"] == "codex_app_server"
    assert result["model_provenance"]["external_llm_api_key_used"] is False
    assert result["llm_bridge"]["bearer_token_exposed"] is False
    assert "ephemeral-test-token" not in json.dumps(result)


def test_workflow_environment_does_not_forward_codex_or_github_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TOKEN", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("HF_TOKEN", "allowed-for-fingpt")

    env = _workflow_env(tmp_path, tmp_path / ".runtime", "fingpt", {})

    assert "CODEX_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["HF_TOKEN"] == "allowed-for-fingpt"
    assert env["HOME"].startswith(str(tmp_path / ".runtime"))


def test_qlib_config_path_cannot_escape_project():
    with pytest.raises(PermissionError, match="project-relative"):
        _project_config_path("../outside.yaml", "qlib")
