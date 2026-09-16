from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import stock_ai.agent_drivers as agent_drivers


def test_saved_agent_settings_activate_real_provider_without_persisting_secrets(monkeypatch, tmp_path):
    settings_path = tmp_path / "agent-settings.json"
    monkeypatch.setattr(agent_drivers, "agent_preferences_path", lambda: settings_path)
    secrets = {}
    monkeypatch.setattr(
        agent_drivers,
        "agent_secret_store",
        SimpleNamespace(
            get=lambda account: secrets.get(account, ""),
            set=lambda account, value: secrets.__setitem__(account, value),
            delete=lambda account: secrets.pop(account, None),
        ),
    )
    for key in (
        "STOCK_AI_AGENT_DRIVER",
        "STOCK_AI_AGENT_OPENAI_BASE_URL",
        "STOCK_AI_AGENT_OPENAI_API_KEY",
        "STOCK_AI_AGENT_OPENAI_MODEL",
        "STOCK_AI_EXTERNAL_AGENT_URL",
        "STOCK_AI_EXTERNAL_AGENT_TOKEN",
        "STOCK_AI_EXTERNAL_AGENT_FRAMEWORK",
        "STOCK_AI_EXTERNAL_AGENT_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = agent_drivers.save_agent_preferences(
        {
            "default_driver": "openai-compatible",
            "openai_base_url": "http://127.0.0.1:11434/v1",
            "openai_model": "local-model",
            "openai_api_key": "secret-value",
        }
    )
    public = agent_drivers.public_agent_preferences(settings)

    assert settings.default_driver == "openai-compatible"
    assert public["available_drivers"] == ["codex", "external-agent", "openai-compatible"]
    assert public["external_agent"]["enabled"] is True
    assert public["openai_compatible"]["configured"] is True
    assert public["openai_compatible"]["api_key_configured"] is True
    persisted = json.loads(settings_path.read_text(encoding="utf-8"))
    assert persisted["default_driver"] == "openai-compatible"
    assert persisted["openai_base_url"] == "http://127.0.0.1:11434/v1"
    assert "openai_api_key" not in persisted
    assert "secret-value" not in settings_path.read_text(encoding="utf-8")
    assert secrets["openai-compatible-api-key"] == "secret-value"
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600

    registry = agent_drivers.build_provider_registry(settings)
    assert registry.primary == "openai-compatible"
    assert registry.get("openai-compatible").capabilities()["configured"] is True


def test_saved_provider_secret_can_be_removed_without_plaintext_fallback(monkeypatch, tmp_path):
    settings_path = tmp_path / "agent-settings.json"
    monkeypatch.setattr(agent_drivers, "agent_preferences_path", lambda: settings_path)
    secrets = {
        "openai-compatible-api-key": "old-key",
        "external-agent-token": "old-token",
    }
    monkeypatch.setattr(
        agent_drivers,
        "agent_secret_store",
        SimpleNamespace(
            get=lambda account: secrets.get(account, ""),
            set=lambda account, value: secrets.__setitem__(account, value),
            delete=lambda account: secrets.pop(account, None),
        ),
    )

    settings = agent_drivers.save_agent_preferences(
        {
            "default_driver": "codex",
            "clear_openai_api_key": True,
            "clear_external_token": True,
        }
    )

    assert settings.default_driver == "codex"
    assert secrets == {}
    assert "old-key" not in settings_path.read_text(encoding="utf-8")
    assert "old-token" not in settings_path.read_text(encoding="utf-8")


def test_untracked_local_llm_contract_configures_default_openai_compatible_agent(monkeypatch, tmp_path):
    settings_path = tmp_path / "agent-settings.json"
    config_path = tmp_path / "agent-runtime.yaml"
    config_path.write_text(
        """
runtime:
  default_driver: openai-compatible
providers:
  openai_compatible:
    base_url: ""
    model: ""
    no_key: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_drivers, "agent_preferences_path", lambda: settings_path)
    monkeypatch.setattr(
        agent_drivers,
        "agent_secret_store",
        SimpleNamespace(get=lambda account: "", set=lambda account, value: None, delete=lambda account: None),
    )
    monkeypatch.setenv("STOCK_AI_AGENT_CONFIG", str(config_path))
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://private-ollama.test:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "gpt-oss:20b")
    monkeypatch.delenv("STOCK_AI_AGENT_DRIVER", raising=False)
    monkeypatch.delenv("STOCK_AI_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("STOCK_AI_OPENAI_MODEL", raising=False)

    settings = agent_drivers.load_agent_driver_settings()

    assert settings.default_driver == "openai-compatible"
    assert settings.openai_base_url == "http://private-ollama.test:11434/v1"
    assert settings.openai_model == "gpt-oss:20b"
    assert settings.openai_no_key is True
