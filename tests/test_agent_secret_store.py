from __future__ import annotations

from types import SimpleNamespace

from stock_ai.agent_secret_store import AgentSecretStore


def test_macos_keychain_write_keeps_secret_out_of_process_arguments(monkeypatch):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("stock_ai.agent_secret_store.platform.system", lambda: "Darwin")
    monkeypatch.setattr("stock_ai.agent_secret_store.subprocess.run", fake_run)

    AgentSecretStore().set("openai-compatible-api-key", "secret-value")

    arguments, options = calls[0]
    assert "secret-value" not in arguments
    assert options["input"] == "secret-value\n"
    assert options["capture_output"] is True


def test_non_macos_secret_store_uses_environment_and_refuses_plaintext_file_fallback(monkeypatch):
    monkeypatch.setattr("stock_ai.agent_secret_store.platform.system", lambda: "Linux")
    monkeypatch.setenv("STOCK_AI_EXTERNAL_AGENT_TOKEN", "environment-secret")
    store = AgentSecretStore()

    assert store.get("external-agent-token") == "environment-secret"

    monkeypatch.delenv("STOCK_AI_EXTERNAL_AGENT_TOKEN")
    try:
        store.set("external-agent-token", "do-not-write")
    except RuntimeError as exc:
        assert "environment variable" in str(exc)
    else:
        raise AssertionError("Non-macOS secret persistence must not fall back to a plaintext project file")
