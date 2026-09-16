from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shlex
import signal
import tempfile
from pathlib import Path
from typing import Any


_ALLOWED_COMMANDS = {
    "pwd",
    "ls",
    "rg",
    "grep",
    "find",
    "sed",
    "head",
    "tail",
    "wc",
    "sort",
    "cut",
    "tr",
    "printf",
    "echo",
    "git",
    "python",
    "python3",
    "pytest",
    "node",
}
_READ_ONLY_GIT = {
    "status",
    "diff",
    "log",
    "show",
    "grep",
    "ls-files",
    "rev-parse",
    "branch",
    "remote",
    "tag",
    "describe",
}
_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "2>", "2>>"}
_SENSITIVE_TOKENS = (".env", ".ssh", ".gnupg", "credentials", "secrets", "id_rsa", "id_ed25519", ".codex")


class SandboxExecutor:
    """No-shell project command runner with macOS seatbelt enforcement."""

    def __init__(self, project_root: Path, *, max_output: int = 80_000) -> None:
        self.project_root = project_root.resolve()
        self.max_output = max_output
        # The command worker must never need write access to the checked-out
        # project.  Keep its HOME/TMP outside the repository so a read-only
        # sandbox does not create bytecode, caches or credentials in source.
        project_digest = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()[:16]
        self.runtime_root = Path(tempfile.gettempdir()) / "stock-ai-agent-sandbox" / project_digest
        self.home = self.runtime_root / "home"
        self.tmp = self.runtime_root / "tmp"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    async def run(self, command: str, *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
        safe_cwd = cwd.resolve()
        if safe_cwd != self.project_root and self.project_root not in safe_cwd.parents:
            raise PermissionError("Terminal cwd must remain inside the project root")
        argv = self.validate(command, cwd=safe_cwd)
        executable = self._resolve_executable(argv[0])
        argv[0] = str(executable)
        os_sandbox = self._os_sandbox_argv(argv, executable=executable)
        process = await asyncio.create_subprocess_exec(
            *os_sandbox["argv"],
            cwd=str(safe_cwd),
            env=self._environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            await self._terminate_process_group(process)
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            await process.communicate()
            raise
        return {
            "schema_version": "open_stock_ai.sandbox_terminal_result.v1",
            "command": command,
            "argv": [Path(argv[0]).name, *argv[1:]],
            "cwd": str(safe_cwd.relative_to(self.project_root)) or ".",
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout.decode("utf-8", errors="replace")[: self.max_output],
            "stderr": stderr.decode("utf-8", errors="replace")[: self.max_output],
            "sandbox": {
                "shell": False,
                "project_paths_only": True,
                "sanitized_environment": True,
                "network_commands": False,
                "home_isolated": True,
                "destructive_commands": False,
                "os_enforced": os_sandbox["enforced"],
                "os_sandbox_backend": os_sandbox["backend"],
                "project_write_blocked": os_sandbox["project_write_blocked"],
            },
        }

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        *,
        grace_seconds: float = 1.0,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()

    def validate(self, command: str, *, cwd: Path | None = None) -> list[str]:
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise PermissionError(f"Invalid sandbox command syntax: {exc}") from exc
        if not argv:
            raise ValueError("terminal.run requires command")
        executable = argv[0]
        if "/" in executable or executable not in _ALLOWED_COMMANDS:
            raise PermissionError(f"Command is not in the sandbox allowlist: {executable}")
        for token in argv[1:]:
            lowered = token.casefold()
            if token in _SHELL_OPERATORS or "$(" in token or "`" in token:
                raise PermissionError("Shell pipelines, redirection and command substitution are blocked")
            if any(secret in lowered for secret in _SENSITIVE_TOKENS):
                raise PermissionError("Credential, secret and Codex account paths are blocked")
            if token.startswith("~") or token == ".." or token.startswith("../") or "/../" in token:
                raise PermissionError("Terminal paths must stay inside the project root")
            if token.startswith("/"):
                path = Path(token).resolve()
                if path != self.project_root and self.project_root not in path.parents:
                    raise PermissionError("Absolute terminal paths must stay inside the project root")
        active_cwd = cwd or self.project_root
        self._validate_command_policy(executable, argv[1:], cwd=active_cwd)
        self._reject_symlink_path_arguments(argv[1:], active_cwd)
        return argv

    def _validate_command_policy(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
    ) -> None:
        if executable == "git":
            forbidden = {
                "-C", "-c", "--config", "--config-env", "--git-dir", "--work-tree", "--exec-path",
                "--ext-diff", "--textconv",
            }
            if any(
                arg in forbidden
                or arg.startswith((
                    "-C", "-c", "--git-dir=", "--work-tree=", "--exec-path=",
                    "--config=", "--config-env=",
                ))
                for arg in arguments
            ):
                raise PermissionError("Git path, config and external execution overrides are blocked")
            subcommand = next((arg for arg in arguments if not arg.startswith("-")), "")
            if subcommand not in _READ_ONLY_GIT:
                raise PermissionError("Sandbox git is read-only; mutations require a separately approved capability")
        if executable == "find" and any(arg in {"-exec", "-execdir", "-delete", "-ok", "-okdir"} for arg in arguments):
            raise PermissionError("Destructive or executable find actions are blocked")
        if executable == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in arguments):
            raise PermissionError("In-place sed mutation is blocked; use project.replace_text")
        if executable in {"python", "python3"}:
            if "-c" in arguments or any(arg.startswith("-") and arg not in {"-m", "-B", "-q"} for arg in arguments):
                raise PermissionError("Inline Python and interpreter escape flags are blocked")
            if "-m" in arguments:
                index = arguments.index("-m")
                module = arguments[index + 1] if index + 1 < len(arguments) else ""
                if module not in {"pytest", "compileall"}:
                    raise PermissionError("Only pytest and compileall modules are allowed in the sandbox")
            elif arguments:
                script = arguments[0]
                if script.startswith("-"):
                    raise PermissionError("Interactive Python is blocked")
                self._require_project_path(script, cwd=cwd)
            else:
                raise PermissionError("Interactive Python is blocked")
        if executable == "node":
            if not arguments or arguments[0] != "--check" or len(arguments) != 2:
                raise PermissionError("Sandbox Node is limited to syntax checking one project file")
            self._require_project_path(arguments[1], cwd=cwd)

    def _require_project_path(self, value: str, *, cwd: Path | None = None) -> Path:
        base = cwd or self.project_root
        raw_path = base / value if not Path(value).is_absolute() else Path(value)
        self._reject_symlink_path(raw_path)
        path = raw_path.resolve()
        if path != self.project_root and self.project_root not in path.parents:
            raise PermissionError("Executable files must stay inside the project root")
        if any(secret in str(path).casefold() for secret in _SENSITIVE_TOKENS):
            raise PermissionError("Sensitive files cannot be executed")
        return path

    def _reject_symlink_path_arguments(self, arguments: list[str], cwd: Path) -> None:
        for value in arguments:
            if value.startswith("-") or value == "--":
                continue
            raw_path = cwd / value if not Path(value).is_absolute() else Path(value)
            # Only an existing filesystem object can redirect a command.  Text
            # patterns and ordinary command values are intentionally ignored.
            if raw_path.exists() or raw_path.is_symlink():
                self._reject_symlink_path(raw_path)
                resolved = raw_path.resolve()
                if resolved != self.project_root and self.project_root not in resolved.parents:
                    raise PermissionError("Terminal paths must stay inside the project root")

    def _reject_symlink_path(self, raw_path: Path) -> None:
        try:
            relative = raw_path.absolute().relative_to(self.project_root)
        except ValueError:
            if raw_path.exists() or raw_path.is_symlink():
                raise PermissionError("Terminal paths must stay inside the project root")
            return
        cursor = self.project_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise PermissionError("Terminal paths must not traverse symbolic links")

    def _resolve_executable(self, name: str) -> Path:
        candidates = [self.project_root / ".venv" / "bin" / name, Path("/usr/bin") / name, Path("/bin") / name]
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        raise FileNotFoundError(name)

    def _environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self.project_root / '.venv' / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "PYTHONPATH": str(self.project_root / "src"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_EXTERNAL_DIFF": "/usr/bin/false",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    def _os_sandbox_argv(self, argv: list[str], *, executable: Path) -> dict[str, Any]:
        """Wrap non-Git commands in macOS sandbox-exec when available.

        Git uses an isolated config/environment below instead: current macOS
        command-line Git invokes Xcode discovery inside sandbox-exec before it
        can read a repository.  Its read-only command set has no hook, pager,
        textconv or external-diff escape path.
        """

        if Path(argv[0]).name == "git":
            return {
                "argv": self._safe_git_argv(argv),
                "enforced": True,
                "backend": "isolated_git_config",
                "project_write_blocked": True,
            }
        sandbox_exec = Path("/usr/bin/sandbox-exec")
        if platform.system() != "Darwin" or not sandbox_exec.is_file():
            return {
                "argv": argv,
                "enforced": False,
                "backend": "process_policy_only",
                "project_write_blocked": False,
            }
        return {
            "argv": [str(sandbox_exec), "-p", self._macos_profile(executable), *argv],
            "enforced": True,
            "backend": "macos_sandbox_exec",
            "project_write_blocked": True,
        }

    def _safe_git_argv(self, argv: list[str]) -> list[str]:
        command_arguments = list(argv[1:])
        subcommand_index = next(
            (index for index, value in enumerate(command_arguments) if not value.startswith("-")),
            None,
        )
        if (
            subcommand_index is not None
            and command_arguments[subcommand_index] in {"diff", "show", "log"}
        ):
            # Repository ``diff.external`` is active even without an explicit
            # --ext-diff flag.  Force the command-specific off switch after
            # the subcommand; a global ``diff.external=false`` would instead
            # attempt to execute a program literally named "false".
            command_arguments[subcommand_index + 1 : subcommand_index + 1] = [
                "--no-ext-diff",
                "--no-textconv",
            ]
        return [
            argv[0],
            "-c", "core.hooksPath=/dev/null",
            "-c", "diff.trustExitCode=false",
            "-c", "core.attributesFile=/dev/null",
            "-c", "core.pager=cat",
            *command_arguments,
        ]

    def _macos_profile(self, executable: Path) -> str:
        del executable  # Command dispatch is already a no-shell allowlist.
        runtime_root = self.runtime_root.resolve()
        rules = [
            "(version 1)",
            # `system.sb` cannot execute the Python runtime bundled with this
            # workstation.  Start from its functional default, then apply
            # explicit denials for the security-critical capabilities below.
            "(allow default)",
            "(deny network*)",
            # No filesystem writes are allowed outside this run's isolated
            # HOME/TMP.  The project checkout is deliberately read-only and a
            # script cannot pivot to an arbitrary host temp directory.
            "(deny file-write* "
            f"(require-not (subpath {json.dumps(str(runtime_root))})))",
            # macOS maps /tmp and /var through /private.  Permit only the
            # current project and this run's isolated runtime there; otherwise
            # an approved script could read a sibling process's temp files.
            "(deny file-read* (require-all "
            f"(subpath {json.dumps('/private')}) "
            f"(require-not (subpath {json.dumps(str(self.project_root))})) "
            f"(require-not (subpath {json.dumps(str(runtime_root))}))))",
            # Treat every account home as private.  This remains effective for
            # project checkouts under ~/Desktop while allowing the checkout
            # itself to be read by the command worker.
            "(deny file-read* (require-all "
            f"(subpath {json.dumps('/Users')}) "
            f"(require-not (subpath {json.dumps(str(self.project_root))}))))",
        ]
        return "\n".join(rules)
