from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import fnmatch
import json
import os
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from uuid import uuid4

import httpx
import yaml

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_runtime.runtime_paths import AgentRuntimePaths
from open_stock_ai.agent_runtime.transport_guard import default_external_transport_guard

from .sandbox_executor import SandboxExecutor
from .network_security import validate_public_http_url


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IGNORED_PARTS = {".git", ".runtime", ".venv", "__pycache__", "node_modules"}
_SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".token", ".credentials")
_MAX_FILE_CHARS = 120_000
_MAX_WRITE_CHARS = 500_000
_MAX_COMMAND_OUTPUT = 80_000


class GeneralAgentToolProvider:
    """Real project, terminal, web and native-Codex tools shared by replaceable Agents."""

    provider_id = "project_terminal_web"

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or PROJECT_ROOT).resolve()
        self.sandbox = SandboxExecutor(self.project_root, max_output=_MAX_COMMAND_OUTPUT)
        self.rollback_root = AgentRuntimePaths.discover().checkpoints / "project-rollbacks"
        self.rollback_root.mkdir(parents=True, exist_ok=True)
        self.rollback_root.chmod(0o700)
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="project.list_files",
                    description="List real project files while excluding generated runtimes and Git internals.",
                    category="project_read",
                    packages=("ripgrep", "python-fallback"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "glob": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                        },
                    },
                ),
                AgentToolSpec(
                    name="project.search_text",
                    description="Search the real project with ripgrep and return matching file locations and lines.",
                    category="project_read",
                    packages=("ripgrep", "python-fallback"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string"},
                            "path": {"type": "string"},
                            "glob": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        },
                    },
                ),
                AgentToolSpec(
                    name="project.read_file",
                    description="Read a UTF-8 project file with line numbers; local secret files are excluded from model context.",
                    category="project_read",
                    packages=("pathlib",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                        },
                    },
                ),
                AgentToolSpec(
                    name="project.preview_patch",
                    description=(
                        "Preview an exact project file replacement against its current SHA-256. "
                        "Use the returned expected_before_sha256 in project.write_file."
                    ),
                    category="project_read",
                    packages=("pathlib", "difflib"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="project.write_file",
                    description="Create or overwrite a project text file. Requires explicit project execution mode.",
                    category="project_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content", "expected_before_sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "expected_before_sha256": {
                                "anyOf": [
                                    {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                    {"type": "null"},
                                ]
                            },
                        },
                    },
                    mutating=True,
                    requires_project_execution=True,
                    rollback_support=True,
                    packages=("pathlib",),
                ),
                AgentToolSpec(
                    name="project.replace_text",
                    description="Replace one exact unique text block in a project file. Requires explicit project execution mode.",
                    category="project_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "old_text", "new_text", "expected_before_sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "expected_before_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                        },
                    },
                    mutating=True,
                    requires_project_execution=True,
                    rollback_support=True,
                    packages=("pathlib",),
                ),
                AgentToolSpec(
                    name="project.move_file",
                    description="Move one project file to a new project path with an exact rollback manifest.",
                    category="project_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "source",
                            "destination",
                            "expected_source_sha256",
                            "expected_destination_sha256",
                        ],
                        "properties": {
                            "source": {"type": "string"},
                            "destination": {"type": "string"},
                            "expected_source_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "expected_destination_sha256": {
                                "anyOf": [
                                    {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                    {"type": "null"},
                                ]
                            },
                        },
                    },
                    mutating=True,
                    destructive=True,
                    requires_project_execution=True,
                    rollback_support=True,
                    packages=("pathlib",),
                ),
                AgentToolSpec(
                    name="project.delete_file",
                    description="Delete one project file after recording an exact run-scoped rollback copy.",
                    category="project_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "expected_before_sha256"],
                        "properties": {
                            "path": {"type": "string"},
                            "expected_before_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                        },
                    },
                    mutating=True,
                    destructive=True,
                    requires_project_execution=True,
                    rollback_support=True,
                    packages=("pathlib",),
                ),
                AgentToolSpec(
                    name="project.rollback_change",
                    description="Restore one exact project mutation from its host-issued rollback token.",
                    category="project_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["rollback_token"],
                        "properties": {"rollback_token": {"type": "string", "minLength": 10}},
                    },
                    mutating=True,
                    requires_project_execution=True,
                    rollback_support=False,
                    packages=("pathlib",),
                ),
                AgentToolSpec(
                    name="terminal.run",
                    description=(
                        "Run an allowlisted argv command in an isolated project environment without a shell, "
                        "network commands, Home access or destructive operations."
                    ),
                    category="terminal",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["command"],
                        "properties": {
                            "command": {"type": "string"},
                            "cwd": {"type": "string"},
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                        },
                    },
                    mutating=True,
                    requires_project_execution=True,
                    packages=("zsh",),
                ),
                AgentToolSpec(
                    name="web.fetch",
                    description="Fetch and read a real external HTTP/HTTPS page, JSON API or text resource.",
                    category="web",
                    packages=("httpx",),
                    retry_policy={"max_attempts": 2, "backoff": "exponential"},
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["url"],
                        "properties": {
                            "url": {"type": "string"},
                            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
                        },
                    },
                ),
                AgentToolSpec(
                    name="web.search",
                    description="Discover public web sources through multiple search providers; returned URLs are direct fetchable links.",
                    category="web",
                    packages=("httpx", "DuckDuckGo HTML", "Bing RSS"),
                    retry_policy={"max_attempts": 2, "backoff": "exponential"},
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                    },
                ),
                AgentToolSpec(
                    name="web.research",
                    description=(
                        "Research any current or niche question end-to-end: search multiple providers, open real source "
                        "pages, and return their readable content for an evidence-based answer."
                    ),
                    category="web",
                    skills=("multi-source-web-research",),
                    packages=("httpx", "DuckDuckGo HTML", "Bing RSS"),
                    retry_policy={"max_attempts": 2, "backoff": "exponential"},
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "source_count": {"type": "integer", "minimum": 1, "maximum": 5},
                            # ``limit`` is the conventional name emitted by
                            # both generic OpenAI-compatible models and our
                            # coverage planner.  Keep it as a bounded alias
                            # for source_count so an otherwise safe read-only
                            # research call cannot stall plan compilation.
                            "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                            "preferred_domains": {
                                "type": "array",
                                "maxItems": 10,
                                "items": {"type": "string"},
                            },
                        },
                    },
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        return {
            "configured": True,
            "runtime_ready": True,
            "health": "ready",
            "project_root": str(self.project_root),
            "network_tools": ["web.fetch", "web.search", "web.research"],
            "terminal_sandboxed": True,
        }

    async def execute(self, name: str, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown general Agent tool: {name}")
        if spec.requires_project_execution and not context.allow_project_actions:
            raise PermissionError(f"{name} requires autonomy=project_execute or full_execute")
        if name == "project.list_files":
            return await self._list_files(arguments)
        if name == "project.search_text":
            return await self._search_text(arguments)
        if name == "project.read_file":
            return self._read_file(arguments)
        if name == "project.preview_patch":
            return self._preview_patch(arguments)
        if name == "project.write_file":
            return self._write_file(arguments, context)
        if name == "project.replace_text":
            return self._replace_text(arguments, context)
        if name == "project.move_file":
            return self._move_file(arguments, context)
        if name == "project.delete_file":
            return self._delete_file(arguments, context)
        if name == "project.rollback_change":
            return self._rollback_change(arguments, context)
        if name == "terminal.run":
            return await self._terminal_run(arguments)
        if name == "web.fetch":
            return await self._web_fetch(arguments)
        if name == "web.search":
            return await self._web_search(arguments)
        if name == "web.research":
            return await self._web_research(arguments)
        raise RuntimeError(f"General Agent tool is registered but not implemented: {name}")

    async def _list_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        base = self._resolve(arguments.get("path") or ".", allow_sensitive=False)
        limit = max(1, min(int(arguments.get("limit") or 500), 2000))
        glob = str(arguments.get("glob") or "").strip()
        command = ["rg", "--files"]
        if glob:
            command += ["-g", glob]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return self._list_files_python(base=base, glob=glob, limit=limit)
        stdout, stderr = await process.communicate()
        files = []
        for value in stdout.decode("utf-8", errors="replace").splitlines():
            path = (base / value).resolve()
            if self._allowed_path(path, allow_sensitive=False):
                files.append(str(path.relative_to(self.project_root)))
            if len(files) >= limit:
                break
        return {
            "schema_version": "open_stock_ai.project_files.v1",
            "path": str(base.relative_to(self.project_root)) or ".",
            "count": len(files),
            "truncated": len(files) >= limit,
            "items": files,
            "stderr": stderr.decode("utf-8", errors="replace")[:2000],
        }

    async def _search_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        if not query:
            raise ValueError("project.search_text requires query")
        base = self._resolve(arguments.get("path") or ".", allow_sensitive=False)
        limit = max(1, min(int(arguments.get("limit") or 100), 500))
        command = ["rg", "-n", "--no-heading", "--color", "never", "--", query]
        glob = str(arguments.get("glob") or "").strip()
        if glob:
            command = ["rg", "-n", "--no-heading", "--color", "never", "-g", glob, "--", query]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return self._search_text_python(base=base, query=query, glob=glob, limit=limit)
        stdout, stderr = await process.communicate()
        matches = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            relative = line.split(":", 1)[0]
            path = (base / relative).resolve()
            if not self._allowed_path(path, allow_sensitive=False):
                continue
            matches.append(line)
            if len(matches) >= limit:
                break
        return {
            "schema_version": "open_stock_ai.project_search.v1",
            "query": query,
            "count": len(matches),
            "truncated": len(matches) >= limit,
            "matches": matches,
            "exit_code": process.returncode,
            "stderr": stderr.decode("utf-8", errors="replace")[:2000],
        }

    def _list_files_python(self, *, base: Path, glob: str, limit: int) -> dict[str, Any]:
        files = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or not self._allowed_path(path, allow_sensitive=False):
                continue
            relative_to_base = str(path.relative_to(base))
            if glob and not fnmatch.fnmatch(relative_to_base, glob):
                continue
            files.append(str(path.relative_to(self.project_root)))
            if len(files) >= limit:
                break
        return {
            "schema_version": "open_stock_ai.project_files.v1",
            "path": str(base.relative_to(self.project_root)) or ".",
            "count": len(files),
            "truncated": len(files) >= limit,
            "items": files,
            "provider": "python_fallback",
            "stderr": "ripgrep unavailable; used built-in project walker",
        }

    def _search_text_python(self, *, base: Path, query: str, glob: str, limit: int) -> dict[str, Any]:
        try:
            pattern = re.compile(query)
        except re.error as exc:
            raise ValueError(f"Invalid search regular expression: {exc}") from exc
        matches = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or not self._allowed_path(path, allow_sensitive=False):
                continue
            relative_to_base = str(path.relative_to(base))
            if glob and not fnmatch.fnmatch(relative_to_base, glob):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            relative_to_project = str(path.relative_to(self.project_root))
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    matches.append(f"{relative_to_project}:{line_number}:{line[:1000]}")
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        return {
            "schema_version": "open_stock_ai.project_search.v1",
            "query": query,
            "count": len(matches),
            "truncated": len(matches) >= limit,
            "matches": matches,
            "exit_code": 0 if matches else 1,
            "provider": "python_fallback",
            "stderr": "ripgrep unavailable; used built-in project search",
        }

    def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(arguments.get("path"), allow_sensitive=False)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(arguments.get("start_line") or 1))
        end = min(len(lines), int(arguments.get("end_line") or min(len(lines), start + 499)))
        selected = lines[start - 1 : end]
        content = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start))
        return {
            "schema_version": "open_stock_ai.project_file.v1",
            "path": str(path.relative_to(self.project_root)),
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": content[:_MAX_FILE_CHARS],
            "truncated": len(content) > _MAX_FILE_CHARS,
            "sha256": _text_hash(text),
        }

    def _preview_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve(arguments.get("path"), allow_sensitive=False)
        content = str(arguments.get("content") or "")
        if len(content) > _MAX_WRITE_CHARS:
            raise ValueError(f"project.preview_patch content exceeds {_MAX_WRITE_CHARS} characters")
        before = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        return {
            **self._write_result(path, before, content, "preview", ""),
            "rollback_token": None,
            "expected_before_sha256": _text_hash(before),
            "mutation_performed": False,
        }

    def _write_file(self, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        path = self._resolve(arguments.get("path"), allow_sensitive=False)
        content = str(arguments.get("content") or "")
        if len(content) > _MAX_WRITE_CHARS:
            raise ValueError(f"project.write_file content exceeds {_MAX_WRITE_CHARS} characters")
        before = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        _require_expected_hash(
            actual=_text_hash(before),
            expected=arguments.get("expected_before_sha256"),
            label=str(path.relative_to(self.project_root)),
        )
        _require_valid_candidate(path, content)
        rollback_token = self._capture_before(path, context.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.agent-{os.getpid()}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return self._write_result(path, before, content, "write", rollback_token)

    def _replace_text(self, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        path = self._resolve(arguments.get("path"), allow_sensitive=False)
        old = str(arguments.get("old_text") or "")
        new = str(arguments.get("new_text") or "")
        if not old:
            raise ValueError("project.replace_text requires non-empty old_text")
        content = path.read_text(encoding="utf-8")
        _require_expected_hash(
            actual=_text_hash(content),
            expected=arguments.get("expected_before_sha256"),
            label=str(path.relative_to(self.project_root)),
        )
        count = content.count(old)
        if count != 1:
            raise ValueError(f"old_text must match exactly once; found {count}")
        updated = content.replace(old, new, 1)
        if len(updated) > _MAX_WRITE_CHARS:
            raise ValueError(f"updated file exceeds {_MAX_WRITE_CHARS} characters")
        _require_valid_candidate(path, updated)
        rollback_token = self._capture_before(path, context.run_id)
        temporary = path.with_name(f".{path.name}.agent-{os.getpid()}.tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(path)
        return self._write_result(path, content, updated, "replace", rollback_token)

    def _move_file(self, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        source = self._resolve(arguments.get("source"), allow_sensitive=False)
        destination = self._resolve(arguments.get("destination"), allow_sensitive=False)
        if not source.is_file():
            raise FileNotFoundError(str(source))
        if destination.exists() and not destination.is_file():
            raise ValueError("project.move_file destination must be a file path")
        source_hash = _file_hash(source)
        replaced_hash = _file_hash(destination) if destination.exists() else None
        _require_expected_hash(
            actual=source_hash,
            expected=arguments.get("expected_source_sha256"),
            label=str(source.relative_to(self.project_root)),
        )
        _require_expected_hash(
            actual=replaced_hash,
            expected=arguments.get("expected_destination_sha256"),
            label=str(destination.relative_to(self.project_root)),
        )
        rollback_token = self._capture_paths((source, destination), context.run_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        return {
            "schema_version": "open_stock_ai.project_mutation.v1",
            "operation": "move",
            "source": str(source.relative_to(self.project_root)),
            "destination": str(destination.relative_to(self.project_root)),
            "before_sha256": source_hash,
            "replaced_sha256": replaced_hash,
            "after_sha256": _file_hash(destination),
            "rollback_token": rollback_token,
            "confirmed": True,
        }

    def _delete_file(self, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        path = self._resolve(arguments.get("path"), allow_sensitive=False)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        digest = _file_hash(path)
        _require_expected_hash(
            actual=digest,
            expected=arguments.get("expected_before_sha256"),
            label=str(path.relative_to(self.project_root)),
        )
        rollback_token = self._capture_paths((path,), context.run_id)
        path.unlink()
        return {
            "schema_version": "open_stock_ai.project_mutation.v1",
            "operation": "delete",
            "path": str(path.relative_to(self.project_root)),
            "before_sha256": digest,
            "after_sha256": None,
            "rollback_token": rollback_token,
            "confirmed": not path.exists(),
        }

    def _rollback_change(
        self,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        token = str(arguments.get("rollback_token") or "")
        if not re.fullmatch(r"RB-[0-9a-f]{32}", token):
            raise ValueError("Invalid rollback token")
        metadata_path = self.rollback_root / f"{token}.json"
        if not metadata_path.is_file():
            raise FileNotFoundError("Rollback token does not exist")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("run_id") != context.run_id:
            raise PermissionError("Rollback token belongs to a different Agent run")
        if isinstance(metadata.get("entries"), list):
            restored_paths: list[str] = []
            for index, entry in enumerate(metadata["entries"]):
                path = self._resolve(entry.get("path"), allow_sensitive=False)
                backup_path = self.rollback_root / f"{token}.{index}.backup"
                if entry.get("existed"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    backup_path.replace(path)
                else:
                    path.unlink(missing_ok=True)
                restored_paths.append(str(path.relative_to(self.project_root)))
                backup_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            return {
                "schema_version": "open_stock_ai.project_rollback.v1",
                "operation": "rollback",
                "paths": restored_paths,
                "confirmed": True,
            }
        path = self._resolve(metadata.get("path"), allow_sensitive=False)
        current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        backup_path = self.rollback_root / f"{token}.backup"
        if metadata.get("existed"):
            restored = backup_path.read_text(encoding="utf-8", errors="replace")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(restored, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
            restored = None
        metadata_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        return {
            "schema_version": "open_stock_ai.project_rollback.v1",
            "operation": "rollback",
            "path": str(path.relative_to(self.project_root)),
            "before_sha256": _text_hash(current),
            "after_sha256": _text_hash(restored),
            "confirmed": True,
        }

    async def _terminal_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = str(arguments.get("command") or "").strip()
        if not command:
            raise ValueError("terminal.run requires command")
        cwd = self._resolve(arguments.get("cwd") or ".")
        timeout = max(1, min(int(arguments.get("timeout_seconds") or 30), 120))
        return await self.sandbox.run(command, cwd=cwd, timeout_seconds=timeout)

    async def _web_fetch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        url = self._validate_url(arguments.get("url"))
        max_chars = max(1000, min(int(arguments.get("max_chars") or 30_000), 100_000))
        guard = default_external_transport_guard()
        async with httpx.AsyncClient(follow_redirects=False, timeout=30, headers={"User-Agent": "OpenStockAI-Agent/1.0"}) as client:
            response = None
            current_url = url
            for _ in range(6):
                current_url = self._validate_url(current_url)
                host = (urlparse(current_url).hostname or "unknown").casefold()
                response = await guard.call(
                    f"tool:web.fetch:host:{host}",
                    lambda: client.get(current_url),
                )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Web redirect omitted its Location header")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                break
            else:
                raise RuntimeError("Web fetch exceeded the five-redirect safety limit")
        if response is None:
            raise RuntimeError("Web fetch produced no response")
        content_type = response.headers.get("content-type", "")
        body = response.text
        if "html" in content_type.casefold():
            body = _html_to_text(body)
        return {
            "schema_version": "open_stock_ai.web_resource.v1",
            "requested_url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "title": _html_title(response.text) if "html" in content_type.casefold() else None,
            "content": body[:max_chars],
            "truncated": len(body) > max_chars,
        }

    async def _web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("web.search requires query")
        limit = max(1, min(int(arguments.get("limit") or 8), 20))
        duckduckgo_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        bing_url = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}"
        results: list[dict[str, str]] = []
        providers: list[str] = []
        failures: list[dict[str, str]] = []
        guard = default_external_transport_guard()
        async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0 OpenStockAI"}) as client:
            try:
                response = await guard.call(
                    "tool:web.search:provider:duckduckgo_html",
                    lambda: client.get(duckduckgo_url),
                )
                response.raise_for_status()
                results.extend(_duckduckgo_results(response.text, limit=limit))
                providers.append("DuckDuckGo HTML")
            except Exception as exc:
                failures.append({"provider": "DuckDuckGo HTML", "error": str(exc)})
            if len(results) < limit:
                try:
                    response = await guard.call(
                        "tool:web.search:provider:bing_rss",
                        lambda: client.get(bing_url),
                    )
                    response.raise_for_status()
                    results.extend(_bing_rss_results(response.text, limit=limit))
                    providers.append("Bing RSS")
                except Exception as exc:
                    failures.append({"provider": "Bing RSS", "error": str(exc)})
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in results:
            direct_url = _direct_search_url(item.get("url"))
            if not direct_url or direct_url in seen:
                continue
            seen.add(direct_url)
            unique.append({**item, "url": direct_url})
            if len(unique) >= limit:
                break
        if not unique and failures:
            raise RuntimeError(f"All web search providers failed: {failures}")
        return {
            "schema_version": "open_stock_ai.web_search.v1",
            "query": query,
            "provider": "+".join(providers) or "none",
            "providers": providers,
            "count": len(unique),
            "items": unique,
            "provider_failures": failures,
        }

    async def _web_research(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("web.research requires query")
        source_count = max(
            1,
            min(int(arguments.get("source_count") or arguments.get("limit") or 3), 5),
        )
        preferred_domains = [
            str(value).strip().casefold().removeprefix("www.")
            for value in arguments.get("preferred_domains") or []
            if str(value).strip()
        ]
        search = await self._web_search({"query": query, "limit": min(20, max(8, source_count * 4))})
        candidates = list(search.get("items") or [])
        if preferred_domains:
            candidates.sort(
                key=lambda item: 0
                if _host_matches(str(item.get("url") or ""), preferred_domains)
                else 1
            )
        sources: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for item in candidates:
            if len(sources) >= source_count:
                break
            try:
                fetched = await self._web_fetch({"url": item["url"], "max_chars": 12_000})
            except Exception as exc:
                failures.append({"url": str(item.get("url") or ""), "error": str(exc)})
                continue
            content = str(fetched.get("content") or "").strip()
            if not content:
                failures.append({"url": str(item.get("url") or ""), "error": "empty content"})
                continue
            sources.append(
                {
                    "title": fetched.get("title") or item.get("title"),
                    "url": fetched.get("final_url") or item.get("url"),
                    "search_snippet": item.get("snippet"),
                    "content_type": fetched.get("content_type"),
                    "content": content,
                    "truncated": fetched.get("truncated"),
                }
            )
        if not sources:
            raise RuntimeError(
                "Web research found no readable source pages; revise the query or use a domain-specific official tool"
            )
        return {
            "schema_version": "open_stock_ai.web_research.v1",
            "query": query,
            "search_providers": search.get("providers") or [],
            "search_result_count": search.get("count") or 0,
            "source_count": len(sources),
            "sources": sources,
            "fetch_failures": failures,
        }

    def _resolve(self, value: Any, *, allow_sensitive: bool = True) -> Path:
        raw = str(value or ".")
        raw_path = self.project_root / raw if not Path(raw).is_absolute() else Path(raw)
        self._reject_symlink_traversal(raw_path)
        candidate = raw_path.resolve()
        if candidate != self.project_root and self.project_root not in candidate.parents:
            raise PermissionError("Agent path must remain inside the project root")
        if not self._allowed_path(candidate, allow_sensitive=allow_sensitive):
            raise PermissionError("Generated runtime, Git internals and local secret files are excluded")
        return candidate

    def _reject_symlink_traversal(self, raw_path: Path) -> None:
        """Reject every symlink component before resolution can hide it.

        Resolving first is insufficient: an in-project symlink that points to
        another in-project file would look safe after ``Path.resolve()`` while
        still allowing an Agent to traverse a mutable indirection.  Project
        tools use the lexical path for this guard and only resolve afterwards
        to enforce the project-root boundary.
        """

        try:
            relative = raw_path.absolute().relative_to(self.project_root)
        except ValueError:
            return
        cursor = self.project_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PermissionError("Agent paths must not traverse symbolic links")

    def _allowed_path(self, path: Path, *, allow_sensitive: bool) -> bool:
        relative = path.relative_to(self.project_root) if path != self.project_root else Path(".")
        if any(part in _IGNORED_PARTS for part in relative.parts):
            return False
        if not allow_sensitive and _is_sensitive_name(path.name):
            return False
        return True

    def _validate_url(self, value: Any) -> str:
        return validate_public_http_url(value, purpose="Web tool")

    def _capture_before(self, path: Path, run_id: str) -> str:
        token = f"RB-{uuid4().hex}"
        metadata = {
            "run_id": run_id,
            "path": str(path.relative_to(self.project_root)),
            "existed": path.is_file(),
        }
        metadata_path = self.rollback_root / f"{token}.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        metadata_path.chmod(0o600)
        if path.is_file():
            backup_path = self.rollback_root / f"{token}.backup"
            backup_path.write_bytes(path.read_bytes())
            backup_path.chmod(0o600)
        return token

    def _capture_paths(self, paths: tuple[Path, ...], run_id: str) -> str:
        token = f"RB-{uuid4().hex}"
        entries: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            existed = path.is_file()
            entries.append(
                {
                    "path": str(path.relative_to(self.project_root)),
                    "existed": existed,
                    "sha256": _file_hash(path) if existed else None,
                }
            )
            if existed:
                backup_path = self.rollback_root / f"{token}.{index}.backup"
                backup_path.write_bytes(path.read_bytes())
                backup_path.chmod(0o600)
        metadata_path = self.rollback_root / f"{token}.json"
        metadata_path.write_text(
            json.dumps({"run_id": run_id, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        metadata_path.chmod(0o600)
        return token

    def _write_result(
        self,
        path: Path,
        before: str | None,
        content: str,
        operation: str,
        rollback_token: str,
    ) -> dict[str, Any]:
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = content.splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{path.relative_to(self.project_root)}",
                tofile=f"b/{path.relative_to(self.project_root)}",
                n=3,
            )
        )
        return {
            "schema_version": "open_stock_ai.project_write.v1",
            "operation": operation,
            "path": str(path.relative_to(self.project_root)),
            "characters": len(content),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "before_sha256": _text_hash(before),
            "after_sha256": _text_hash(content),
            "diff": diff[:80_000],
            "diff_truncated": len(diff) > 80_000,
            "rollback_token": rollback_token,
            "syntax_validation": _validate_candidate(path, content),
        }


def _text_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _require_expected_hash(*, actual: str | None, expected: Any, label: str) -> None:
    normalized = str(expected).casefold() if expected is not None else None
    if normalized != actual:
        raise RuntimeError(
            f"stale_state: {label} changed after it was read; "
            f"expected {normalized or 'missing'}, actual {actual or 'missing'}"
        )


def _require_valid_candidate(path: Path, content: str) -> None:
    validation = _validate_candidate(path, content)
    if validation["checked"] and not validation["passed"]:
        raise ValueError(
            f"invalid_{validation['parser']}_syntax: {path.name}: {validation['error']}"
        )


def _validate_candidate(path: Path, content: str) -> dict[str, Any]:
    suffix = path.suffix.casefold()
    parser = {
        ".py": "python",
        ".json": "json",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(suffix)
    if parser is None:
        return {"checked": False, "passed": True, "parser": None, "error": None}
    try:
        if parser == "python":
            compile(content, str(path), "exec")
        elif parser == "json":
            json.loads(content)
        elif parser == "toml":
            tomllib.loads(content)
        else:
            yaml.safe_load(content)
    except (SyntaxError, ValueError, TypeError, yaml.YAMLError) as exc:
        return {
            "checked": True,
            "passed": False,
            "parser": parser,
            "error": str(exc)[:2000],
        }
    return {"checked": True, "passed": True, "parser": parser, "error": None}


def _is_sensitive_name(value: str) -> bool:
    name = value.casefold()
    if name == ".env.example":
        return False
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.endswith(_SENSITIVE_SUFFIXES)
        or name.startswith(("token-cache", "credential-cache", "auth-cache"))
    )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _html_title(value: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", value, re.IGNORECASE | re.DOTALL)
    return _html_to_text(match.group(1)) if match else None


def _duckduckgo_results(value: str, *, limit: int) -> list[dict[str, str]]:
    anchor_pattern = re.compile(
        r'<a(?=[^>]*\bclass="[^"]*result__a[^"]*")(?=[^>]*\bhref="([^"]+)")[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    snippet_pattern = re.compile(
        r'<(?:a|div)[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
        re.IGNORECASE | re.DOTALL,
    )
    snippets = [_html_to_text(match) for match in snippet_pattern.findall(value)]
    results = []
    for index, match in enumerate(anchor_pattern.finditer(value)):
        results.append(
            {
                "title": _html_to_text(match.group(2)),
                "url": html.unescape(match.group(1)),
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
        if len(results) >= limit:
            break
    return results


def _bing_rss_results(value: str, *, limit: int) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return []
    results = []
    for item in root.findall(".//item"):
        link = str(item.findtext("link") or "").strip()
        title = str(item.findtext("title") or "").strip()
        if not link or not title:
            continue
        results.append(
            {
                "title": title,
                "url": link,
                "snippet": _html_to_text(str(item.findtext("description") or "")),
            }
        )
        if len(results) >= limit:
            break
    return results


def _direct_search_url(value: Any) -> str | None:
    url = html.unescape(str(value or "").strip())
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.casefold().endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = (parse_qs(parsed.query).get("uddg") or [""])[0]
        url = str(target).strip()
        parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return url


def _host_matches(url: str, domains: list[str]) -> bool:
    host = str(urlparse(url).hostname or "").casefold().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _html_to_text(value: str) -> str:
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
