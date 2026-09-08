from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AgentRuntimePaths:
    root: Path
    logs: Path
    cache: Path
    database: Path
    artifacts: Path
    checkpoints: Path
    workspaces: Path
    browser: Path
    external_runtimes: Path
    workers: Path
    socket: Path

    @classmethod
    def discover(cls) -> AgentRuntimePaths:
        override = os.getenv("STOCK_AI_AGENT_DATA_ROOT")
        if override:
            root = Path(override).expanduser()
            logs = root / "logs"
            cache = root / "cache"
        elif platform.system() == "Darwin":
            root = Path.home() / "Library" / "Application Support" / "Stock AI"
            logs = Path.home() / "Library" / "Logs" / "Stock AI"
            cache = Path.home() / "Library" / "Caches" / "Stock AI"
        else:
            root = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "Stock AI"
            logs = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "Stock AI"
            cache = Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "Stock AI"
        root = root.resolve()
        paths = cls(
            root=root,
            logs=logs.resolve(),
            cache=cache.resolve(),
            database=root / "agent-runtime.db",
            artifacts=root / "artifacts",
            checkpoints=root / "checkpoints",
            workspaces=root / "workspaces",
            browser=root / "browser",
            external_runtimes=root / "external-runtimes",
            workers=root / ".runtime" / "workers",
            socket=root / ".runtime" / "agent-supervisor.sock",
        )
        paths.ensure()
        return paths

    def ensure(self) -> None:
        for path in (
            self.root,
            self.logs,
            self.cache,
            self.artifacts,
            self.checkpoints,
            self.workspaces,
            self.browser,
            self.external_runtimes,
            self.workers,
            self.socket.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
