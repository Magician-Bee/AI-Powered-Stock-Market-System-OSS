from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .base import WorkerCrashedError, WorkerRequest, WorkerToolError, profile_for


WorkerHandler = Callable[[WorkerRequest], Awaitable[dict[str, Any]]]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ProcessChannel:
    process: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def execute(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if self.process.returncode is not None:
            raise WorkerCrashedError(f"Worker exited with code {self.process.returncode}")
        if self.process.stdin is None or self.process.stdout is None:
            raise WorkerCrashedError("Worker IPC pipes are unavailable")
        async with self.lock:
            self.process.stdin.write(
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode()
            )
            await self.process.stdin.drain()
            raw = await self.process.stdout.readline()
            if not raw:
                raise WorkerCrashedError(f"Worker exited before replying (code={self.process.returncode})")
            response = json.loads(raw)
            if response.get("request_id") != payload.get("request_id"):
                raise WorkerCrashedError("Worker IPC response ID did not match its request")
            if not response.get("ok"):
                error = response.get("error") or {}
                raise WorkerToolError(
                    f"{error.get('type') or 'WorkerError'}: {error.get('message') or 'failed'}"
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise WorkerCrashedError("Worker returned a non-object result")
            events = [
                dict(item)
                for item in response.get("events") or []
                if isinstance(item, dict)
            ]
            return result, events


class WorkerSupervisor:
    """Track cancellable tool workers and expose a private Unix-socket health channel."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        socket_path: Path,
        project_root: str | Path | None = None,
    ) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.socket_path = socket_path.resolve()
        if len(os.fsencode(self.socket_path)) >= 100:
            digest = hashlib.sha256(str(self.socket_path).encode("utf-8")).hexdigest()[:20]
            self.socket_path = Path("/tmp") / f"stock-ai-{digest}" / "agent.sock"
        self._tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._processes: dict[tuple[str, str], _ProcessChannel] = {}
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self._server: asyncio.AbstractServer | None = None
        with self._connect() as conn:
            apply_migrations(conn)

    async def start(self) -> dict[str, Any]:
        recovered = self.recover_crashed()
        if os.name != "nt" and self._server is None:
            try:
                await self._start_socket_server()
            except OSError as exc:
                # Some managed macOS sessions permit normal project files but
                # deny Unix-domain sockets below the project runtime folder.
                # The supervisor channel is local-only and ephemeral, so keep
                # durable data in its configured root and relocate only this
                # socket to a private temporary directory.
                digest = hashlib.sha256(str(self.socket_path).encode("utf-8")).hexdigest()[:20]
                fallback = Path("/tmp") / f"stock-ai-{digest}" / "agent.sock"
                if self.socket_path == fallback:
                    raise
                logger.warning(
                    "Worker supervisor socket unavailable at %s (%s); using %s",
                    self.socket_path,
                    exc,
                    fallback,
                )
                self.socket_path = fallback
                try:
                    await self._start_socket_server()
                except OSError as fallback_exc:
                    # The health endpoint is observability only; worker
                    # execution uses stdio with each isolated process.  Do
                    # not make an otherwise usable desktop installation fail
                    # simply because its sandbox prohibits every AF_UNIX bind.
                    logger.warning(
                        "Worker supervisor health socket is unavailable (%s); continuing without it",
                        fallback_exc,
                    )
        return {
            "schema_version": "open_stock_ai.worker_supervisor.v1",
            "started": True,
            "socket": str(self.socket_path) if self._server else None,
            "recovered_crashed_workers": recovered,
        }

    async def _start_socket_server(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.chmod(0o700)
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle_socket, path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    async def execute(
        self,
        *,
        run_id: str,
        step_id: str,
        worker_type: str,
        timeout_seconds: int,
        payload: dict[str, Any],
        handler: WorkerHandler,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        worker_id = f"AWRK-{uuid4().hex}"
        profile = profile_for(worker_type)
        request = WorkerRequest(
            worker_id=worker_id,
            run_id=run_id,
            step_id=step_id,
            worker_type=worker_type,
            timeout_seconds=profile.timeout_for(timeout_seconds),
            payload={
                "tool": payload.get("tool"),
                "arguments": payload.get("audit_arguments") or {},
                "worker_profile": profile.describe(),
            },
        )
        self._insert(request)
        isolated = worker_type in {
            "project",
            "terminal",
            "browser",
            "external",
            "broker",
        } and bool(payload.get("tool"))
        if isolated:
            channel = await self._process_channel(run_id, worker_type)
            self._set_pid(worker_id, channel.process.pid)
            process_payload = {
                **payload,
                "request_id": worker_id,
            }
            task = asyncio.create_task(
                channel.execute(process_payload),
                name=f"agent-worker-process:{worker_id}",
            )
        else:
            task = asyncio.create_task(handler(request), name=f"agent-worker:{worker_id}")
        self._tasks[worker_id] = task
        try:
            process_result = await asyncio.wait_for(task, timeout=request.timeout_seconds)
        except asyncio.CancelledError:
            if isolated:
                await self._stop_process(run_id, worker_type)
            self._finish(worker_id, "cancelled", error={"type": "CancelledError"})
            raise
        except TimeoutError as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if isolated:
                await self._stop_process(run_id, worker_type)
            self._finish(worker_id, "failed", error={"type": "TimeoutError", "message": str(exc)})
            raise
        except WorkerToolError as exc:
            self._finish(
                worker_id,
                "failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        except BaseException as exc:
            tool_error = _expected_tool_error(exc)
            if tool_error is not None:
                # An in-process capability can deliberately reject a request
                # (for example FastAPI's HTTPException for a missing verified
                # quote).  That is a healthy worker returning a domain error,
                # not a crashed worker.  Preserve the distinction in the
                # durable worker record so recovery does not restart or loop
                # a healthy executor.
                self._finish(
                    worker_id,
                    "failed",
                    error={"type": type(tool_error).__name__, "message": str(tool_error)},
                )
                raise tool_error from exc
            if isolated and isinstance(exc, WorkerCrashedError):
                await self._stop_process(run_id, worker_type)
            self._finish(
                worker_id,
                "crashed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise WorkerCrashedError(f"{worker_type} worker crashed: {exc}") from exc
        else:
            if isolated:
                result, worker_events = process_result
                if event_sink is not None:
                    for event in worker_events:
                        emitted = event_sink(event)
                        if asyncio.iscoroutine(emitted):
                            await emitted
            else:
                result = process_result
            self._finish(worker_id, "completed")
            return worker_id, result
        finally:
            self._tasks.pop(worker_id, None)

    async def cancel(self, worker_id: str) -> bool:
        task = self._tasks.get(worker_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def cancel_run(self, run_id: str) -> int:
        worker_ids = self.active_workers(run_id=run_id)
        cancelled = 0
        for worker in worker_ids:
            cancelled += int(await self.cancel(worker["worker_id"]))
        return cancelled

    async def close_run(self, run_id: str) -> None:
        await self.cancel_run(run_id)
        for key in tuple(self._processes):
            if key[0] == run_id:
                await self._stop_process(*key)

    def active_workers(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        query = "select * from agent_workers where status='running'"
        params: tuple[Any, ...] = ()
        if run_id:
            query += " and run_id=?"
            params = (run_id,)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def recover_crashed(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_workers
                   set status='crashed', completed_at=?, error_json=?
                 where status='running'
                """,
                (
                    _now(),
                    json.dumps(
                        {"type": "HostRestarted", "message": "Worker lost during host restart."},
                        separators=(",", ":"),
                    ),
                ),
            )
            conn.commit()
        return int(cursor.rowcount)

    async def close(self) -> None:
        for task in tuple(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for run_id, worker_type in tuple(self._processes):
            await self._stop_process(run_id, worker_type)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def _handle_socket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2)
            request = json.loads(raw.decode("utf-8")) if raw else {}
            response = {
                "ok": request.get("op") == "health",
                "active_worker_count": len(self._tasks),
                "isolated_process_count": len(self._processes),
                "isolated_process_pids": sorted(
                    channel.process.pid for channel in self._processes.values()
                ),
                "pid": os.getpid(),
            }
            writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as exc:
            logger.debug("Worker health IPC response failed: %s", exc, exc_info=True)
        finally:
            writer.close()
            await writer.wait_closed()

    def _insert(self, request: WorkerRequest) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_workers(
                    worker_id, run_id, step_id, worker_type, status, pid,
                    started_at, heartbeat_at, payload_json
                ) values (?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    request.worker_id,
                    request.run_id,
                    request.step_id,
                    request.worker_type,
                    None,
                    _now(),
                    _now(),
                    json.dumps(request.payload, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()

    def _set_pid(self, worker_id: str, pid: int) -> None:
        with self._connect() as conn:
            conn.execute("update agent_workers set pid=? where worker_id=?", (pid, worker_id))
            conn.commit()

    async def _process_channel(self, run_id: str, worker_type: str) -> _ProcessChannel:
        key = (run_id, worker_type)
        channel = self._processes.get(key)
        if channel is not None and channel.process.returncode is None:
            return channel
        environment = os.environ.copy()
        project_src = str(self.project_root / "src")
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            project_src
            if not existing_pythonpath
            else os.pathsep.join((project_src, existing_pythonpath))
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "stock_ai.worker_process",
            worker_type,
            str(self.project_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        channel = _ProcessChannel(process)
        self._processes[key] = channel
        return channel

    async def _stop_process(self, run_id: str, worker_type: str) -> None:
        channel = self._processes.pop((run_id, worker_type), None)
        if channel is None:
            return
        process = channel.process
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()

    def _finish(self, worker_id: str, status: str, *, error: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update agent_workers set status=?, completed_at=?, heartbeat_at=?, error_json=?
                 where worker_id=?
                """,
                (
                    status,
                    _now(),
                    _now(),
                    json.dumps(error, ensure_ascii=False) if error else None,
                    worker_id,
                ),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected_tool_error(exc: BaseException) -> WorkerToolError | None:
    """Normalize a capability rejection without coupling to an HTTP framework."""

    if isinstance(exc, (ValueError, PermissionError)):
        return WorkerToolError(str(exc))

    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int) or not 400 <= status_code < 600:
        return None
    detail = getattr(exc, "detail", None)
    if isinstance(detail, (dict, list)):
        detail_text = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
    else:
        detail_text = str(detail if detail is not None else exc)
    return WorkerToolError(f"{type(exc).__name__} {status_code}: {detail_text}")


__all__ = ["WorkerCrashedError", "WorkerSupervisor", "WorkerToolError"]
