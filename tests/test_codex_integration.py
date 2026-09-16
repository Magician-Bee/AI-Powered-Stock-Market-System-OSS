from stock_ai.codex_market import _fallback_item, _merge_and_enforce
from types import SimpleNamespace

import pytest
import stock_ai.codex_runtime as codex_runtime_module

from stock_ai.codex_runtime import (
    ApprovalMode,
    CodexRuntime,
    Sandbox,
    _approval_choice,
    _elicitation_content,
    _resolve_codex_bin,
    _sdk_audit_event,
    _turn_item_trace,
)


def test_native_codex_run_still_uses_full_access_and_auto_review(monkeypatch):
    captured = {}

    class Result:
        final_response = "ok"
        status = "completed"
        items = []

    class Thread:
        id = "thread-full-access"

        async def run(self, instruction, **kwargs):
            captured.update(kwargs)
            return Result()

    runtime = CodexRuntime()

    async def account():
        return {"authenticated": True}

    async def thread():
        return Thread()

    monkeypatch.setattr(runtime, "_require_account", account)
    monkeypatch.setattr(runtime, "_project_thread", thread)

    import asyncio

    result = asyncio.run(runtime.run("inspect project", computer_use=True))

    assert result["response"] == "ok"
    assert result["model_invocation"]["status"] == "succeeded"
    assert result["model_invocation"]["model_id"] == "codex-app-server/default"
    assert result["provenance"]["model_call_id"] == result["model_invocation"]["call_id"]
    assert captured["sandbox"] == Sandbox.full_access
    assert captured["approval_mode"] == ApprovalMode.auto_review


def test_native_codex_thread_is_resumed_after_a_server_restart(tmp_path, monkeypatch):
    captured = {}

    class Thread:
        id = "native-thread-123"

    class Client:
        async def thread_resume(self, thread_id, **kwargs):
            captured["thread_id"] = thread_id
            captured.update(kwargs)
            return Thread()

    runtime = CodexRuntime(project_root=tmp_path)
    runtime._save_native_thread_id("native-thread-123")

    import asyncio

    resumed = asyncio.run(runtime._resume_native_thread(Client()))

    assert resumed.id == "native-thread-123"
    assert captured["thread_id"] == "native-thread-123"
    assert captured["sandbox"] == Sandbox.full_access
    assert captured["approval_mode"] == ApprovalMode.auto_review


def test_agent_run_reuses_one_hidden_thread_until_lifecycle_close(monkeypatch):
    class Thread:
        pass

    class Client:
        def __init__(self):
            self.starts = 0

        async def start_configured_thread(self, **kwargs):
            del kwargs
            self.starts += 1
            return Thread(), {"model": "account-model", "reasoning_effort": "ultra"}

    runtime = CodexRuntime()
    client = Client()

    async def account():
        return {"authenticated": True}

    async def get_client():
        return client

    monkeypatch.setattr(runtime, "_require_account", account)
    monkeypatch.setattr(runtime, "client", get_client)
    monkeypatch.setattr(runtime, "_start_configured_thread", client.start_configured_thread)

    import asyncio

    async def scenario():
        first = await runtime.start_agent_run("AR-one")
        second = await runtime.start_agent_run("AR-one")
        assert first is second
        await runtime.close_agent_run("AR-one")
        third = await runtime.start_agent_run("AR-one")
        assert third is not first

    asyncio.run(scenario())
    assert client.starts == 2


def test_explicit_codex_binary_override_is_used(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    executable.write_text("test", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("STOCK_AI_CODEX_BIN", str(executable))

    assert _resolve_codex_bin() == executable.resolve()


@pytest.mark.parametrize("available_apps,path_available,expected", [
    (("chatgpt", "codex"), True, "chatgpt"),
    (("codex",), True, "codex"),
    ((), True, "path"),
    ((), False, "bundled"),
])
def test_macos_codex_prefers_installed_app_before_path_and_bundled_runtime(
    tmp_path, monkeypatch, available_apps, path_available, expected,
):
    binaries = {name: tmp_path / name / "codex" for name in ("chatgpt", "codex", "path", "bundled")}
    for name in (*available_apps, "path", "bundled"):
        binaries[name].parent.mkdir(parents=True, exist_ok=True)
        binaries[name].write_text("test", encoding="utf-8")
        binaries[name].chmod(0o755)
    monkeypatch.delenv("STOCK_AI_CODEX_BIN", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(codex_runtime_module.sys, "platform", "darwin")
    monkeypatch.setattr(codex_runtime_module, "_MACOS_CODEX_APP_BINARIES", (binaries["chatgpt"], binaries["codex"]))
    monkeypatch.setattr(codex_runtime_module.shutil, "which", lambda _: str(binaries["path"]) if path_available else None)
    monkeypatch.setattr(codex_runtime_module, "bundled_codex_path", lambda: binaries["bundled"])

    assert _resolve_codex_bin() == binaries[expected].resolve()


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_codex_binary_override_does_not_silently_fall_back(tmp_path, monkeypatch, exists):
    executable = tmp_path / "invalid-codex"
    if exists:
        executable.write_text("not executable", encoding="utf-8")
        executable.chmod(0o644)
    monkeypatch.setenv("STOCK_AI_CODEX_BIN", str(executable))
    # Model the permission check explicitly so this regression also runs on Windows.
    monkeypatch.setattr(codex_runtime_module.os, "access", lambda *_: False)

    with pytest.raises(PermissionError if exists else FileNotFoundError, match="STOCK_AI_CODEX_BIN"):
        _resolve_codex_bin()


def test_computer_use_approval_is_scoped_to_an_authorized_turn():
    runtime = CodexRuntime()
    request = {
        "questions": [
            {
                "id": "approval",
                "header": "Approve app tool call?",
                "question": "Allow Computer Use to inspect Chrome?",
                "options": [
                    {"label": "Accept", "description": "Continue"},
                    {"label": "Decline", "description": "Stop"},
                ],
            }
        ]
    }

    assert runtime._handle_server_request("item/tool/requestUserInput", request) == {}
    runtime._computer_use_authorized = True
    assert runtime._handle_server_request("item/tool/requestUserInput", request) == {
        "answers": {"approval": {"answers": ["Accept"]}}
    }
    assert runtime.capability_status()["approval_events"][-1]["method"] == "item/tool/requestUserInput"


def test_native_command_and_file_approvals_are_declined_without_explicit_scoped_grant(tmp_path):
    runtime = CodexRuntime(project_root=tmp_path)

    assert runtime._handle_server_request(
        "item/commandExecution/requestApproval",
        {"cwd": str(tmp_path), "command": "pytest"},
    ) == {"decision": "decline"}
    assert runtime._handle_server_request(
        "item/fileChange/requestApproval",
        {"path": str(tmp_path / "README.md")},
    ) == {"decision": "decline"}
    assert runtime.capability_status()["approval_events"][-1]["reason"] == "no_explicit_grant"


def test_native_approval_requires_capability_project_scope_and_expiry(tmp_path):
    runtime = CodexRuntime(project_root=tmp_path)
    runtime._active_approval_grant = runtime._approval_policy.issue(
        run_id="CR-test",
        capabilities={"native.command", "native.file_change"},
        resource_root=tmp_path,
        ttl_seconds=60,
    )

    assert runtime._handle_server_request(
        "item/commandExecution/requestApproval",
        {"cwd": str(tmp_path), "command": "pytest"},
    ) == {"decision": "accept"}
    assert runtime._handle_server_request(
        "item/fileChange/requestApproval",
        {"path": str(tmp_path.parent / "outside.txt")},
    ) == {"decision": "decline"}
    assert runtime.capability_status()["approval_events"][-1]["reason"] == "resource_outside_project_scope"

    assert runtime._handle_server_request(
        "item/commandExecution/requestApproval",
        {"cwd": str(tmp_path), "command": f"cat {tmp_path.parent / 'outside.txt'}"},
    ) == {"decision": "decline"}
    assert runtime._handle_server_request(
        "item/commandExecution/requestApproval",
        {"cwd": str(tmp_path), "command": "cat ~/.ssh/config"},
    ) == {"decision": "decline"}


def test_approval_choice_never_picks_decline_or_cancel_when_allow_is_available():
    assert _approval_choice(
        [
            {"label": "Cancel"},
            {"label": "Allow for this session"},
            {"label": "Decline"},
        ]
    ) == "Allow for this session"


def test_computer_use_mcp_elicitation_is_accepted_only_during_authorized_turn():
    runtime = CodexRuntime()
    request = {
        "serverName": "computer-use",
        "mode": "form",
        "message": "Allow Chrome control?",
        "requestedSchema": {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["allow", "deny"]}},
            "required": ["decision"],
        },
    }

    assert runtime._handle_server_request("mcpServer/elicitation/request", request) == {}
    runtime._computer_use_authorized = True
    assert runtime._handle_server_request("mcpServer/elicitation/request", request) == {
        "action": "accept",
        "content": {"decision": "allow"},
    }


def test_elicitation_content_prefers_positive_values_and_required_booleans():
    assert _elicitation_content(
        {
            "properties": {
                "permission": {"oneOf": [{"const": "deny"}, {"const": "approve", "title": "Approve"}]},
                "remember": {"type": "boolean"},
            },
            "required": ["permission", "remember"],
        }
    ) == {"permission": "approve", "remember": True}


def test_turn_trace_keeps_tool_identity_without_full_payload():
    class Item:
        def model_dump(self, mode="json"):
            return {
                "type": "mcpToolCall",
                "id": "item-1",
                "status": "completed",
                "server": "node_repl",
                "tool": "js",
                "arguments": {"secret": "must not be copied"},
            }

    assert _turn_item_trace(Item()) == {
        "type": "mcpToolCall",
        "id": "item-1",
        "status": "completed",
        "server": "node_repl",
        "tool": "js",
    }


def test_sdk_audit_events_expose_item_lifecycle_but_not_reasoning_text():
    reasoning = SimpleNamespace(
        method="item/reasoning/textDelta",
        payload={"delta": "private chain of thought"},
    )
    item = SimpleNamespace(
        method="item/completed",
        payload={
            "item": {
                "type": "webSearch",
                "id": "search-1",
                "status": "completed",
                "query": "secret query body is not copied",
            }
        },
    )

    assert _sdk_audit_event(reasoning) is None
    assert _sdk_audit_event(item) == {
        "type": "model.sdk.item.completed",
        "sdk_event": "item/completed",
        "source": "codex_app_server",
        "item": {"type": "webSearch", "id": "search-1", "status": "completed"},
    }
