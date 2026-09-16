from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

import stock_ai.agent_general_tools as general_tools_module
import stock_ai.network_security as network_security_module
from open_stock_ai.agent_runtime.approval_manager import ApprovalManager, ApprovalRequiredError
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_general_tools import GeneralAgentToolProvider
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.main import app
from stock_ai.local_security import (
    AUTOMATION_NONCE_HEADER,
    AUTOMATION_SIGNATURE_HEADER,
    AUTOMATION_TIMESTAMP_HEADER,
    LocalRuntimeSecurity,
    automation_callback_signature,
    derive_automation_callback_token,
)
from stock_ai.sandbox_executor import SandboxExecutor


def _request(
    *,
    method: str = "POST",
    token: str | None = None,
    origin: str | None = None,
    path: str = "/api/agents/runs",
    headers_extra: dict[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    headers = []
    if token:
        headers.append((b"x-stock-ai-session", token.encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    for name, value in (headers_extra or {}).items():
        headers.append((name.casefold().encode(), value.encode()))
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
        receive=receive,
    )


def _run_store(path: Path) -> None:
    session = AgentSessionStore(path).create(session_id="AS-secure", namespace="test", title="secure")
    AgentRunStore(path).create_run(
        "AR-secure",
        {
            "objective": "secure mutation",
            "symbols": [],
            "driver_id": "codex",
            "autonomy": "project_execute",
            "max_steps": 2,
            "session_id": session["session_id"],
        },
    )


def test_local_api_requires_runtime_token_and_same_origin_for_mutations():
    security = LocalRuntimeSecurity("s" * 43)
    assert security.authorize(_request()) is not None
    # Native WKWebView can omit Origin for a same-document loopback fetch;
    # the per-process session header remains the mandatory CSRF boundary.
    assert security.authorize(_request(token="s" * 43)) is None
    assert security.authorize(_request(token="s" * 43, origin="https://evil.example")) is not None
    assert (
        security.authorize(
            _request(token="s" * 43, origin="http://127.0.0.1:8000")
        )
        is None
    )
    assert (
        security.authorize(
            _request(token="s" * 43, origin="http://localhost:8000")
        )
        is None
    )


def test_n8n_callback_requires_signed_body_timestamp_and_one_time_nonce(monkeypatch):
    security = LocalRuntimeSecurity("s" * 43)
    monkeypatch.setenv("N8N_AUTOMATION_GATEWAY_TOKEN", "gateway-api-secret")
    monkeypatch.setenv("N8N_AUTOMATION_CALLBACK_SECRET", "callback-secret-v1")
    path = "/api/agents/automations/events"
    base_headers = {"X-Stock-AI-Automation-Source": "n8n"}
    assert asyncio.run(security.authorize_async(_request(path=path, headers_extra=base_headers))) is not None
    assert (
        asyncio.run(
            security.authorize_async(
            _request(
                path=path,
                headers_extra={
                    **base_headers,
                    "X-Stock-AI-Automation-Token": "wrong",
                },
            )
            )
        )
        is not None
    )
    callback_token = derive_automation_callback_token("callback-secret-v1")
    assert callback_token != "gateway-api-secret"
    assert callback_token != derive_automation_callback_token("callback-secret-v2")
    body = b'{"event_type":"automation.n8n.trigger","payload":{"source":"n8n"}}'
    timestamp = str(int(__import__("time").time()))
    nonce = "execution-42:1700000000000"
    signature = automation_callback_signature(callback_token, timestamp, nonce, body)
    signed_headers = {
        **base_headers,
        "X-Stock-AI-Automation-Token": callback_token,
        AUTOMATION_TIMESTAMP_HEADER: timestamp,
        AUTOMATION_NONCE_HEADER: nonce,
        AUTOMATION_SIGNATURE_HEADER: signature,
    }
    assert (
        asyncio.run(
            security.authorize_async(
            _request(
                path=path,
                headers_extra=signed_headers,
                body=body,
            )
            )
        )
        is None
    )
    verified_request = _request(path=path, headers_extra=signed_headers, body=body)
    assert asyncio.run(security.authorize_async(verified_request)) is not None
    # The preceding nonce has been consumed; use a fresh signed request to
    # assert that only irreversible proof hashes survive authentication.
    fresh_nonce = "execution-43:1700000000000"
    fresh_headers = {
        **signed_headers,
        AUTOMATION_NONCE_HEADER: fresh_nonce,
        AUTOMATION_SIGNATURE_HEADER: automation_callback_signature(callback_token, timestamp, fresh_nonce, body),
    }
    verified_request = _request(path=path, headers_extra=fresh_headers, body=body)
    assert asyncio.run(security.authorize_async(verified_request)) is None
    authentication = verified_request.state.automation_callback_authentication
    assert authentication["authenticated"] is True
    assert authentication["source"] == "n8n"
    assert authentication["body_sha256"] == __import__("hashlib").sha256(body).hexdigest()
    serialized = repr(authentication)
    assert "callback-secret-v1" not in serialized
    assert fresh_nonce not in serialized
    assert callback_token not in serialized
    assert fresh_headers[AUTOMATION_SIGNATURE_HEADER] not in serialized
    assert (
        asyncio.run(
            security.authorize_async(
                _request(path=path, headers_extra=signed_headers, body=body)
            )
        )
        is not None
    )
    tampered_headers = {
        **signed_headers,
        AUTOMATION_NONCE_HEADER: "execution-42:1700000000001",
    }
    assert (
        asyncio.run(
            security.authorize_async(
                _request(path=path, headers_extra=tampered_headers, body=body)
            )
        )
        is not None
    )
    stale_headers = {
        **signed_headers,
        AUTOMATION_NONCE_HEADER: "execution-42:stale",
        AUTOMATION_TIMESTAMP_HEADER: str(int(__import__("time").time()) - 301),
    }
    stale_headers[AUTOMATION_SIGNATURE_HEADER] = automation_callback_signature(
        callback_token,
        stale_headers[AUTOMATION_TIMESTAMP_HEADER],
        stale_headers[AUTOMATION_NONCE_HEADER],
        body,
    )
    assert (
        asyncio.run(
            security.authorize_async(
                _request(path=path, headers_extra=stale_headers, body=body)
            )
        )
        is not None
    )


def test_n8n_signed_callback_body_reaches_the_async_api_middleware(monkeypatch):
    monkeypatch.setenv("N8N_AUTOMATION_GATEWAY_TOKEN", "gateway-api-secret")
    monkeypatch.setenv("N8N_AUTOMATION_CALLBACK_SECRET", "callback-secret-v1")
    body = b'{"event_type":"automation.n8n.trigger","payload":{"source":"n8n"}}'
    timestamp = str(int(__import__("time").time()))
    nonce = "middleware-execution:1700000000000"
    token = derive_automation_callback_token("callback-secret-v1")
    headers = {
        "Content-Type": "application/json",
        "X-Stock-AI-Automation-Source": "n8n",
        "X-Stock-AI-Automation-Token": token,
        AUTOMATION_TIMESTAMP_HEADER: timestamp,
        AUTOMATION_NONCE_HEADER: nonce,
        AUTOMATION_SIGNATURE_HEADER: automation_callback_signature(token, timestamp, nonce, body),
    }

    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            timeout=5,
        ) as client:
            return await client.post(
                "/api/agents/automations/events",
                content=body,
                headers=headers,
            )

    response = asyncio.run(send())
    assert response.status_code == 202


def test_approval_requires_one_time_ui_challenge(tmp_path):
    path = tmp_path / "runtime.sqlite"
    _run_store(path)
    manager = ApprovalManager(path)
    with pytest.raises(ApprovalRequiredError) as requested:
        manager.require(
            run_id="AR-secure",
            step_id="stable-node",
            tool_name="project.delete_file",
            arguments={"path": "target.txt"},
            resource_scope={"path": "target.txt"},
            risk_class="local_destructive",
        )
    approval_id = requested.value.approval["approval_id"]
    with pytest.raises(PermissionError, match="one-time"):
        manager.resolve(
            approval_id,
            approved=True,
            decided_by="stock_ai_ui_user",
            challenge="forged",
        )
    challenge = manager.issue_challenge(approval_id)["challenge"]
    resolved = manager.resolve(
        approval_id,
        approved=True,
        decided_by="stock_ai_ui_user",
        challenge=challenge,
    )
    assert resolved["status"] == "approved"
    with pytest.raises(ValueError, match="no longer pending"):
        manager.resolve(
            approval_id,
            approved=True,
            decided_by="stock_ai_ui_user",
            challenge=challenge,
        )


def test_web_fetch_blocks_private_targets_and_redirects(monkeypatch, tmp_path):
    monkeypatch.setattr(
        network_security_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "public.example"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        general_tools_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = GeneralAgentToolProvider(tmp_path)
    context = AgentRunContext(run_id="AR-web", autonomy="advisory", symbols=())
    with pytest.raises(PermissionError, match="private"):
        asyncio.run(provider.execute("web.fetch", {"url": "http://127.0.0.1/"}, context))
    with pytest.raises(PermissionError, match="private"):
        asyncio.run(provider.execute("web.fetch", {"url": "https://public.example/"}, context))


def test_project_tools_exclude_pattern_based_secret_files(tmp_path):
    (tmp_path / ".env.development").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("//registry/:_authToken=value\n", encoding="utf-8")
    (tmp_path / "public.txt").write_text("safe value\n", encoding="utf-8")
    provider = GeneralAgentToolProvider(tmp_path)
    context = AgentRunContext(run_id="AR-files", autonomy="advisory", symbols=())
    listed = asyncio.run(provider.execute("project.list_files", {}, context))
    assert listed["items"] == ["public.txt"]
    with pytest.raises(PermissionError, match="secret"):
        asyncio.run(provider.execute("project.read_file", {"path": ".env.development"}, context))


def test_terminal_cancellation_terminates_process_group(tmp_path):
    (tmp_path / "child.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TMPDIR']).joinpath('child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    (tmp_path / "runner.py").write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TMPDIR']).joinpath('runner.pid').write_text(str(os.getpid()))\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    async def exercise() -> tuple[int, int]:
        executor = SandboxExecutor(tmp_path)
        runner_pid_path = executor.tmp / "runner.pid"
        child_pid_path = executor.tmp / "child.pid"
        task = asyncio.create_task(
            executor.run("python3 runner.py", cwd=tmp_path, timeout_seconds=60)
        )
        for _ in range(200):
            if runner_pid_path.exists() and child_pid_path.exists():
                break
            await asyncio.sleep(0.01)
        runner_pid = int(runner_pid_path.read_text())
        child_pid = int(child_pid_path.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return runner_pid, child_pid

    runner_pid, child_pid = asyncio.run(exercise())

    async def assert_terminated(pid: int) -> None:
        for _ in range(200):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            proc_stat = Path(f"/proc/{pid}/stat")
            if proc_stat.exists():
                try:
                    fields = proc_stat.read_text(encoding="utf-8").split()
                except (FileNotFoundError, ProcessLookupError):
                    # Linux may reap the process between exists() and read_text().
                    return
                if len(fields) > 2 and fields[2] == "Z":
                    return
            await asyncio.sleep(0.01)
        pytest.fail(f"Process {pid} remained alive after cancellation")

    asyncio.run(assert_terminated(runner_pid))
    asyncio.run(assert_terminated(child_pid))
