from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec


class GitToolProvider:
    """Host-owned, argv-only Git operations scoped to the current project."""

    provider_id = "git"

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="git.status",
                    description="Read the current branch and working-tree status.",
                    category="git_read",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    packages=("git",),
                ),
                AgentToolSpec(
                    name="git.diff",
                    description="Read a bounded Git diff without changing repository state.",
                    category="git_read",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "staged": {"type": "boolean"},
                            "paths": {"type": "array", "maxItems": 100, "items": {"type": "string"}},
                        },
                    },
                    packages=("git",),
                ),
                AgentToolSpec(
                    name="git.log",
                    description="Read recent commit metadata.",
                    category="git_read",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                    },
                    packages=("git",),
                ),
                AgentToolSpec(
                    name="git.create_branch",
                    description="Create and switch to a scoped local branch.",
                    category="git_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name"],
                        "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120}},
                    },
                    mutating=True,
                    requires_project_execution=True,
                    rollback_support=False,
                    packages=("git",),
                ),
                AgentToolSpec(
                    name="git.commit",
                    description="Stage only the supplied project paths and create a local commit.",
                    category="git_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["message", "paths"],
                        "properties": {
                            "message": {"type": "string", "minLength": 1, "maxLength": 500},
                            "paths": {
                                "type": "array",
                                "maxItems": 100,
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                    mutating=True,
                    requires_project_execution=True,
                    packages=("git",),
                ),
                AgentToolSpec(
                    name="git.restore",
                    description="Restore explicitly supplied working-tree paths from Git.",
                    category="git_write",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["paths"],
                        "properties": {
                            "paths": {
                                "type": "array",
                                "maxItems": 100,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "staged": {"type": "boolean"},
                        },
                    },
                    mutating=True,
                    destructive=True,
                    requires_project_execution=True,
                    packages=("git",),
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        return {
            "configured": (self.project_root / ".git").exists(),
            "runtime_ready": True,
            "health": "ready",
            "project_root": str(self.project_root),
            "remote_push_available": False,
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown Git capability: {name}")
        if spec.requires_project_execution and not context.allow_project_actions:
            raise PermissionError(f"{name} requires autonomy=project_execute or full_execute")
        if name == "git.status":
            return await self._result(name, ["status", "--porcelain=v1", "--branch"])
        if name == "git.diff":
            command = ["diff", "--no-ext-diff"]
            if arguments.get("staged"):
                command.append("--cached")
            command.extend(["--", *self._paths(arguments.get("paths") or [])])
            return await self._result(name, command, ok_codes={0})
        if name == "git.log":
            limit = max(1, min(int(arguments.get("limit") or 20), 100))
            return await self._result(
                name,
                ["log", f"-n{limit}", "--date=iso-strict", "--pretty=format:%H%x09%ad%x09%an%x09%s"],
            )
        if name == "git.create_branch":
            branch = str(arguments.get("name") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", branch) or ".." in branch:
                raise ValueError("Invalid branch name")
            before = await self._head()
            result = await self._result(name, ["switch", "-c", branch])
            result.update({"before_sha256": before, "after_sha256": await self._head(), "branch": branch})
            return result
        if name == "git.commit":
            paths = self._paths(arguments.get("paths") or [])
            if not paths:
                raise ValueError("git.commit requires at least one explicit path")
            await self._result("git.stage", ["add", "--", *paths])
            before = await self._head()
            result = await self._result(name, ["commit", "-m", str(arguments["message"]), "--", *paths])
            result.update({"before_sha256": before, "after_sha256": await self._head()})
            return result
        if name == "git.restore":
            paths = self._paths(arguments.get("paths") or [])
            if not paths:
                raise ValueError("git.restore requires at least one explicit path")
            command = ["restore"]
            if arguments.get("staged"):
                command.append("--staged")
            command.extend(["--", *paths])
            result = await self._result(name, command)
            result["confirmed"] = True
            return result
        raise RuntimeError(f"Git capability is registered but not implemented: {name}")

    def _paths(self, values: list[Any]) -> list[str]:
        paths = []
        for value in values:
            candidate = (self.project_root / str(value)).resolve()
            try:
                relative = candidate.relative_to(self.project_root)
            except ValueError as exc:
                raise PermissionError("Git path escapes project root") from exc
            paths.append(str(relative))
        return paths

    async def _head(self) -> str | None:
        result = await self._run(["rev-parse", "HEAD"])
        return result["stdout"].strip() if result["exit_code"] == 0 else None

    async def _result(
        self,
        operation: str,
        arguments: list[str],
        *,
        ok_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        result = await self._run(arguments)
        if result["exit_code"] not in (ok_codes or {0}):
            raise RuntimeError(result["stderr"] or f"git {operation} failed")
        return {
            "schema_version": "open_stock_ai.git_result.v1",
            "operation": operation,
            **result,
        }

    async def _run(self, arguments: list[str]) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        return {
            "exit_code": int(process.returncode or 0),
            "stdout": stdout.decode("utf-8", errors="replace")[:80_000],
            "stderr": stderr.decode("utf-8", errors="replace")[:20_000],
        }
