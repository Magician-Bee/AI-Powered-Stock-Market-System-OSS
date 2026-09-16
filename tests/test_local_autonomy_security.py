"""Defensive auth regressions with synthetic requests and inert local handlers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess

from fastapi import FastAPI
import httpx
import pytest
from starlette.requests import Request

from stock_ai.local_security import LocalRuntimeSecurity, requires_runtime_session


TOKEN = "isolated-runtime-session"
ROOT = Path(__file__).resolve().parents[1]


def request(path, *, method="GET", token=None, origin=None, host=None, server=("127.0.0.1", 8000)):
    headers = []
    for name, value in (("x-stock-ai-session", token), ("origin", origin), ("host", host)):
        if value is not None:
            headers.append((name.encode(), value.encode()))
    return Request({
        "type": "http", "http_version": "1.1", "scheme": "http", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": server,
    })


@pytest.mark.parametrize("asynchronous", [False, True])
def test_autonomy_and_existing_api_require_the_same_runtime_session(asynchronous):
    security = LocalRuntimeSecurity(TOKEN)
    authorize = (lambda req: asyncio.run(security.authorize_async(req))) if asynchronous else security.authorize
    for path in ("/api", "/api/agents/runs", "/agent/autonomy", "/agent/autonomy/status", "/agent/autonomy/control"):
        assert authorize(request(path)).status_code == 403
        assert authorize(request(path, token="incorrect")).status_code == 403
        assert authorize(request(path, token=TOKEN)) is None
    for path in ("/", "/static/js/core/runtime-session.js", "/agent/autonomy-not-an-api", "/api-docs"):
        assert authorize(request(path)) is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_autonomy_mutations_require_a_trusted_origin_when_provided(asynchronous):
    security = LocalRuntimeSecurity(TOKEN)
    authorize = (lambda req: asyncio.run(security.authorize_async(req))) if asynchronous else security.authorize
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for origin in (None, "http://127.0.0.1:8000", "http://localhost:8000"):
            assert authorize(request("/agent/autonomy/control", method=method, token=TOKEN, origin=origin)) is None
        for origin in ("https://untrusted.example", "http://localhost:8001", "null", "http://localhost:invalid"):
            denied = authorize(request("/agent/autonomy/control", method=method, token=TOKEN, origin=origin))
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_test_transport_exception_requires_the_synthetic_transport_address(asynchronous):
    security = LocalRuntimeSecurity(TOKEN)
    authorize = (lambda req: asyncio.run(security.authorize_async(req))) if asynchronous else security.authorize
    for path in ("/api/agents/runs", "/agent/autonomy/control"):
        assert authorize(request(path, host="testserver")).status_code == 403
        assert authorize(request(path, server=("testserver", 80))) is None


def test_every_declared_autonomy_route_is_inside_the_session_boundary():
    # Read mount/decorator definitions without starting a service or invoking
    # handlers that manage accounts, market data, controls, or model requests.
    import ast

    tree = ast.parse((ROOT / "src/stock_ai/autonomous_trading_api.py").read_text())
    paths = []
    prefix = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "APIRouter":
            prefix = next(item.value.value for item in node.keywords if item.arg == "prefix")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "router"
                and node.func.attr in {"get", "post", "put", "patch", "delete"}):
            paths.append(node.args[0].value)
    assert prefix and paths
    assert all(requires_runtime_session(prefix + path) for path in paths)


def test_middleware_blocks_before_inert_autonomy_handler_and_accepts_valid_session():
    security, invoked = LocalRuntimeSecurity(TOKEN), []
    app = FastAPI()

    @app.middleware("http")
    async def boundary(req, call_next):
        denied = await security.authorize_async(req)
        return denied if denied is not None else await call_next(req)

    @app.post("/agent/autonomy/control")
    async def inert_control():
        invoked.append("inert-handler-only")
        return {"accepted": True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            missing = await client.post("/agent/autonomy/control")
            denied = await client.post("/agent/autonomy/control", headers={
                "X-Stock-AI-Session": TOKEN, "Origin": "https://untrusted.example",
            })
            assert missing.status_code == denied.status_code == 403
            assert invoked == []
            accepted = await client.post("/agent/autonomy/control", headers={
                "X-Stock-AI-Session": TOKEN, "Origin": "http://localhost:8000",
            })
            assert accepted.status_code == 200

    asyncio.run(exercise())
    assert invoked == ["inert-handler-only"]


def test_browser_wrapper_attaches_session_only_to_same_origin_protected_paths():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const calls = [];
const nativeFetch = async (input, init) => {
  calls.push({ input, init });
  return { status: 200 };
};
const context = {
  URL, Request, Headers,
  document: { querySelector: () => ({ content: 'isolated-runtime-session' }) },
  window: { fetch: nativeFetch, location: { href: 'http://127.0.0.1:8000/', origin: 'http://127.0.0.1:8000' } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context);
(async () => {
  for (const path of ['/api', '/api/agents/runs', '/agent/autonomy', '/agent/autonomy/status', '/agent/autonomy/control']) {
    await context.window.fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    assert.equal(calls.at(-1).init.headers.get('X-Stock-AI-Session'), 'isolated-runtime-session');
    assert.equal(calls.at(-1).init.headers.get('Content-Type'), 'application/json');
  }
  const sourceRequest = new Request('http://127.0.0.1:8000/agent/autonomy/status', { headers: { 'X-Retained': 'yes' } });
  await context.window.fetch(sourceRequest, { headers: { 'X-Added': 'yes' } });
  assert.equal(calls.at(-1).init.headers.get('X-Retained'), 'yes');
  assert.equal(calls.at(-1).init.headers.get('X-Added'), 'yes');
  assert.equal(calls.at(-1).init.headers.get('X-Stock-AI-Session'), 'isolated-runtime-session');
  for (const path of ['/static/test.js', '/agent/autonomy-not-an-api', '/api-docs', 'https://untrusted.example/agent/autonomy/status', 'http://localhost:8000/agent/autonomy/status']) {
    await context.window.fetch(path);
    assert.equal(new Headers(calls.at(-1).init.headers).has('X-Stock-AI-Session'), false);
  }
  process.stdout.write('SESSION_WRAPPER_PASSED\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    source = ROOT / "src/stock_ai/ui/static/js/core/runtime-session.js"
    completed = subprocess.run(
        ["node", "-e", "const SOURCE_PATH = " + json.dumps(str(source)) + ";\n" + script],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "SESSION_WRAPPER_PASSED" in completed.stdout
