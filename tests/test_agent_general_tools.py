from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import stock_ai.agent_general_tools as general_tools_module
import stock_ai.network_security as network_security_module
from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_general_tools import GeneralAgentToolProvider
from stock_ai.agent_tools import StockAgentToolRegistry


class _PermitGuard:
    async def call(self, _scope, operation):
        return await operation()


@pytest.fixture(autouse=True)
def _permit_web_transport_for_unit_tests(monkeypatch):
    monkeypatch.setattr(
        general_tools_module,
        "default_external_transport_guard",
        lambda: _PermitGuard(),
    )


def _context(*, project: bool = False) -> AgentRunContext:
    return AgentRunContext(
        run_id="AR-general",
        autonomy="project_execute" if project else "advisory",
        symbols=("2330.TW",),
        allow_project_actions=project,
    )


def test_registry_exposes_real_project_terminal_web_and_native_agent_tools():
    names = {item["name"] for item in StockAgentToolRegistry().manifest()}

    assert {
        "project.list_files",
        "project.search_text",
        "project.read_file",
        "project.write_file",
        "project.replace_text",
        "terminal.run",
        "web.fetch",
        "web.search",
        "web.research",
        "market.search_taiwan_securities",
    }.issubset(names)
    # Full-access Codex remains available through /api/codex/run, but must never
    # be selected as a relay tool by the shared UI Agent Runtime.
    assert "codex.native_run" not in names


def test_project_tools_read_search_write_and_replace_real_files(tmp_path: Path):
    provider = GeneralAgentToolProvider(tmp_path)
    context = _context(project=True)

    written = asyncio.run(
        provider.execute(
            "project.write_file",
            {
                "path": "src/example.txt",
                "content": "alpha\nbeta\n",
                "expected_before_sha256": None,
            },
            context,
        )
    )
    read = asyncio.run(
        provider.execute("project.read_file", {"path": "src/example.txt", "start_line": 2}, context)
    )
    searched = asyncio.run(
        provider.execute("project.search_text", {"query": "beta", "path": "src"}, context)
    )
    replaced = asyncio.run(
        provider.execute(
            "project.replace_text",
            {
                "path": "src/example.txt",
                "old_text": "beta",
                "new_text": "gamma",
                "expected_before_sha256": hashlib.sha256(b"alpha\nbeta\n").hexdigest(),
            },
            context,
        )
    )

    assert written["operation"] == "write"
    assert "2: beta" in read["content"]
    assert searched["count"] == 1
    assert replaced["operation"] == "replace"
    assert (tmp_path / "src/example.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_project_write_rejects_invalid_structured_content_before_mutation(tmp_path: Path):
    provider = GeneralAgentToolProvider(tmp_path)
    context = _context(project=True)
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="invalid_python_syntax"):
        asyncio.run(
            provider.execute(
                "project.write_file",
                {
                    "path": "module.py",
                    "content": "def broken(:\n",
                    "expected_before_sha256": before_hash,
                },
                context,
            )
        )

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    valid = asyncio.run(
        provider.execute(
            "project.write_file",
            {
                "path": "module.py",
                "content": "value = 2\n",
                "expected_before_sha256": before_hash,
            },
            context,
        )
    )
    assert valid["syntax_validation"] == {
        "checked": True,
        "passed": True,
        "parser": "python",
        "error": None,
    }


def test_advisory_mode_blocks_terminal_and_project_mutation(tmp_path: Path):
    provider = GeneralAgentToolProvider(tmp_path)

    with pytest.raises(PermissionError, match="project_execute"):
        asyncio.run(provider.execute("terminal.run", {"command": "pwd"}, _context()))
    with pytest.raises(PermissionError, match="project_execute"):
        asyncio.run(
            provider.execute(
                "project.write_file",
                {"path": "blocked.txt", "content": "blocked", "expected_before_sha256": None},
                _context(),
            )
        )


def test_terminal_tool_runs_real_command_inside_project(tmp_path: Path):
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute(
            "terminal.run",
            {"command": "printf 'terminal-ready'", "timeout_seconds": 5},
            _context(project=True),
        )
    )

    assert result["exit_code"] == 0
    assert result["stdout"] == "terminal-ready"
    assert result["cwd"] == "."
    assert result["sandbox"]["shell"] is False
    assert result["sandbox"]["home_isolated"] is True
    if sys.platform == "darwin":
        assert result["sandbox"]["os_enforced"] is True
        assert result["sandbox"]["os_sandbox_backend"] == "macos_sandbox_exec"
        assert result["sandbox"]["project_write_blocked"] is True


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.ssh/id_rsa",
        "rm -rf ~",
        "curl https://example.com",
        "printf ok | tee /tmp/leak",
        "python -c 'import os; print(os.environ)'",
        "git push origin main",
    ],
)
def test_terminal_sandbox_blocks_home_network_shell_and_destructive_commands(tmp_path: Path, command: str):
    provider = GeneralAgentToolProvider(tmp_path)

    with pytest.raises(PermissionError):
        asyncio.run(provider.execute("terminal.run", {"command": command}, _context(project=True)))


def test_terminal_sandbox_rejects_symbolic_link_escape_and_git_config_injection(tmp_path: Path):
    outside = tmp_path.parent / "outside-private.txt"
    outside.write_text("must not be read", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    provider = GeneralAgentToolProvider(tmp_path)

    with pytest.raises(PermissionError, match="symbolic links"):
        asyncio.run(
            provider.execute(
                "terminal.run",
                {"command": "sed escape.txt"},
                _context(project=True),
            )
        )
    with pytest.raises(PermissionError, match="Git path, config and external execution overrides"):
        asyncio.run(
            provider.execute(
                "terminal.run",
                {"command": "git -c diff.external=/tmp/evil status"},
                _context(project=True),
            )
        )


def test_project_tools_reject_every_symbolic_link_component_before_resolution(tmp_path: Path):
    internal = tmp_path / "internal.txt"
    internal.write_text("safe but indirect", encoding="utf-8")
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)
    linked_file = tmp_path / "linked-file.txt"
    linked_file.symlink_to(internal)
    provider = GeneralAgentToolProvider(tmp_path)

    for path in ("linked-file.txt", "linked-directory/internal.txt"):
        with pytest.raises(PermissionError, match="symbolic links"):
            asyncio.run(provider.execute("project.read_file", {"path": path}, _context()))
        with pytest.raises(PermissionError, match="symbolic links"):
            asyncio.run(
                provider.execute(
                    "project.write_file",
                    {
                        "path": path,
                        "content": "must not reach target",
                        "expected_before_sha256": None,
                    },
                    _context(project=True),
                )
            )

    assert internal.read_text(encoding="utf-8") == "safe but indirect"


def test_terminal_git_rejects_malicious_repository_diff_and_textconv_configuration(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Test"], cwd=tmp_path, check=True)
    target = tmp_path / "tracked.txt"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    target.write_text("after\n", encoding="utf-8")
    marker = tmp_path / "malicious-git-config-ran"
    payload = f"#!/bin/sh\ntouch {marker}\n"
    external = tmp_path / "malicious-diff.sh"
    external.write_text(payload, encoding="utf-8")
    external.chmod(0o700)
    subprocess.run(["git", "config", "diff.external", str(external)], cwd=tmp_path, check=True)
    (tmp_path / ".gitattributes").write_text("tracked.txt diff=malicious\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.malicious.textconv", str(external)],
        cwd=tmp_path,
        check=True,
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("terminal.run", {"command": "git diff"}, _context(project=True))
    )
    assert result["exit_code"] == 0
    assert marker.exists() is False
    for command in ("git diff --ext-diff", "git diff --textconv"):
        with pytest.raises(PermissionError, match="external execution overrides"):
            asyncio.run(provider.execute("terminal.run", {"command": command}, _context(project=True)))
    assert marker.exists() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_terminal_sandbox_blocks_project_file_writes_at_os_boundary(tmp_path: Path):
    script = tmp_path / "writer.py"
    target = tmp_path / "should-not-exist.txt"
    script.write_text(
        "from pathlib import Path\nPath('should-not-exist.txt').write_text('blocked')\n",
        encoding="utf-8",
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute(
            "terminal.run",
            {"command": "python3 writer.py", "timeout_seconds": 5},
            _context(project=True),
        )
    )

    assert result["exit_code"] != 0
    assert target.exists() is False
    assert result["sandbox"]["os_sandbox_backend"] == "macos_sandbox_exec"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_terminal_sandbox_blocks_foreign_temp_reads_and_writes_at_os_boundary(tmp_path: Path):
    outside = tmp_path.parent / "host-private-sandbox-secret.txt"
    outside.write_text("host-private-secret", encoding="utf-8")
    escaped_write = tmp_path.parent / "host-private-sandbox-write.txt"
    script = tmp_path / "foreign_temp_escape.py"
    script.write_text(
        "from pathlib import Path\n"
        f"outside = Path({str(outside)!r})\n"
        f"escaped_write = Path({str(escaped_write)!r})\n"
        "print(outside.read_text(encoding='utf-8'))\n"
        "escaped_write.write_text('escaped', encoding='utf-8')\n",
        encoding="utf-8",
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute(
            "terminal.run",
            {"command": "python3 foreign_temp_escape.py", "timeout_seconds": 5},
            _context(project=True),
        )
    )

    assert result["exit_code"] != 0
    assert "host-private-secret" not in result["stdout"]
    assert escaped_write.exists() is False
    assert result["sandbox"]["os_sandbox_backend"] == "macos_sandbox_exec"


def test_web_fetch_reads_real_http_response_through_provider(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        network_security_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/page"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>Example</title></head><body><h1>External content</h1></body></html>",
            request=request,
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        general_tools_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("web.fetch", {"url": "https://example.test/page"}, _context())
    )

    assert result["status_code"] == 200
    assert result["title"] == "Example"
    assert "External content" in result["content"]


def test_web_search_returns_direct_urls_snippets_and_uses_provider_fallback(monkeypatch, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                200,
                text=(
                    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fofficial">'
                    'Official result</a><div class="result__snippet">Current official evidence</div>'
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        general_tools_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("web.search", {"query": "current official fact", "limit": 1}, _context())
    )

    assert result["count"] == 1
    assert result["items"][0]["url"] == "https://example.test/official"
    assert result["items"][0]["snippet"] == "Current official evidence"


def test_web_search_falls_back_to_bing_rss_when_duckduckgo_has_no_results(monkeypatch, tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text="<html><body>No results</body></html>", request=request)
        if request.url.host == "www.bing.com":
            return httpx.Response(
                200,
                text=(
                    "<rss><channel><item><title>Official source</title>"
                    "<link>https://example.test/current</link>"
                    "<description>Latest verified fact</description></item></channel></rss>"
                ),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        general_tools_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("web.search", {"query": "current official fact", "limit": 1}, _context())
    )

    assert result["providers"] == ["DuckDuckGo HTML", "Bing RSS"]
    assert result["items"] == [
        {
            "title": "Official source",
            "url": "https://example.test/current",
            "snippet": "Latest verified fact",
        }
    ]


def test_web_research_searches_and_opens_readable_sources(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        network_security_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                200,
                text=(
                    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fsource">'
                    'Primary source</a><div class="result__snippet">Evidence snippet</div>'
                ),
                request=request,
            )
        if request.url.host == "www.bing.com":
            return httpx.Response(200, text="<rss><channel></channel></rss>", request=request)
        if request.url.host == "example.test":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><head><title>Primary source</title></head><body>Verified current answer.</body></html>",
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        general_tools_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("web.research", {"query": "current fact", "source_count": 1}, _context())
    )

    assert result["schema_version"] == "open_stock_ai.web_research.v1"
    assert result["source_count"] == 1
    assert result["sources"][0]["url"] == "https://example.test/source"
    assert "Verified current answer" in result["sources"][0]["content"]


def test_web_research_accepts_bounded_limit_alias(monkeypatch, tmp_path: Path):
    """Provider-standard ``limit`` must not make a read-only plan invalid."""

    monkeypatch.setattr(
        network_security_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    async def research(self, arguments):
        assert arguments["query"] == "current fact"
        return {
            "schema_version": "open_stock_ai.web_research.v1",
            "source_count": max(1, min(int(arguments.get("source_count") or arguments.get("limit") or 3), 5)),
            "sources": [{"url": "https://example.test/source", "content": "verified"}],
        }

    provider = GeneralAgentToolProvider(tmp_path)
    monkeypatch.setattr(GeneralAgentToolProvider, "_web_research", research)
    result = asyncio.run(provider.execute("web.research", {"query": "current fact", "limit": 1}, _context()))

    assert result["source_count"] == 1
    spec = next(item for item in provider.manifest() if item["name"] == "web.research")
    assert spec["input_schema"]["properties"]["limit"]["maximum"] == 5


def test_project_read_blocks_local_secret_file(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    provider = GeneralAgentToolProvider(tmp_path)

    with pytest.raises(PermissionError, match="secret"):
        asyncio.run(provider.execute("project.read_file", {"path": ".env"}, _context()))


def test_project_search_uses_builtin_fallback_when_ripgrep_is_missing(monkeypatch, tmp_path: Path):
    (tmp_path / "runtime.py").write_text("class AgentOrchestrator:\n    pass\n", encoding="utf-8")

    async def missing_ripgrep(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(general_tools_module.asyncio, "create_subprocess_exec", missing_ripgrep)
    provider = GeneralAgentToolProvider(tmp_path)

    result = asyncio.run(
        provider.execute("project.search_text", {"query": "class AgentOrchestrator"}, _context())
    )

    assert result["provider"] == "python_fallback"
    assert result["count"] == 1
    assert "runtime.py:1" in result["matches"][0]
