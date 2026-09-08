from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext


class WorkerProcess:
    def __init__(self, worker_type: str, project_root: Path) -> None:
        self.worker_type = worker_type
        self.project_root = project_root.resolve()
        if worker_type in {"project", "terminal"}:
            from .agent_general_tools import GeneralAgentToolProvider
            from .tool_providers.git import GitToolProvider

            self.providers = (
                GeneralAgentToolProvider(self.project_root),
                GitToolProvider(self.project_root),
            )
        elif worker_type == "browser":
            from .tool_providers.browser import BrowserToolProvider

            self.providers = (BrowserToolProvider(self.project_root),)
        elif worker_type == "external":
            from .external_project_tools import ExternalProjectToolProvider

            self.providers = (ExternalProjectToolProvider(),)
        elif worker_type == "broker":
            from .broker_tools import BrokerAgentToolProvider

            self.providers = (BrokerAgentToolProvider(),)
        else:
            raise ValueError(f"Unsupported isolated worker type: {worker_type}")

    async def execute(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tool = str(payload.get("tool") or "")
        arguments = payload.get("arguments") or {}
        events: list[dict[str, Any]] = []
        context = _context(payload.get("context") or {}, events)
        for provider in self.providers:
            if provider.has_tool(tool):
                return await provider.execute(tool, arguments, context), events
        raise ValueError(f"{self.worker_type} worker cannot execute {tool}")

    async def close(self) -> None:
        for provider in self.providers:
            close_run = getattr(provider, "close_run", None)
            if callable(close_run):
                # Every session in this process is scoped by its run ID; the OS
                # still provides the final containment boundary on shutdown.
                sessions = tuple(getattr(provider, "_sessions", {}).keys())
                for run_id in sessions:
                    await close_run(run_id)


def _context(payload: dict[str, Any], events: list[dict[str, Any]]) -> AgentRunContext:
    state = dict(payload.get("state")) if isinstance(payload.get("state"), dict) else {}

    async def record_event(event: dict[str, Any]) -> None:
        if isinstance(event, dict):
            events.append(dict(event))

    state["record_event"] = record_event
    return AgentRunContext(
        run_id=str(payload.get("run_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        parent_run_id=payload.get("parent_run_id"),
        autonomy=str(payload.get("autonomy") or "advisory"),
        symbols=tuple(str(item) for item in payload.get("symbols") or []),
        allow_paper_orders=bool(payload.get("allow_paper_orders")),
        allow_project_actions=bool(payload.get("allow_project_actions")),
        allow_external_actions=bool(payload.get("allow_external_actions")),
        previewed_orders=set(str(item) for item in payload.get("previewed_orders") or []),
        state=state,
    )


async def _main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m stock_ai.worker_process WORKER_TYPE PROJECT_ROOT")
    worker = WorkerProcess(sys.argv[1], Path(sys.argv[2]))
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                return 0
            request: dict[str, Any] = {}
            try:
                request = json.loads(line)
                result, events = await worker.execute(request)
                response = {
                    "request_id": request.get("request_id"),
                    "ok": True,
                    "result": result,
                    "events": events,
                }
            except BaseException as exc:
                response = {
                    "request_id": request.get("request_id"),
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str)
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()
    finally:
        await worker.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
