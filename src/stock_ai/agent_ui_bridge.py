from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


class AgentUIBridge:
    """Durable command bus shared by browser, WKWebView and Agent tools."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        event_publisher: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._lock = RLock()
        self._db_path: Path | None = None
        self._event_publisher = event_publisher
        self._state: dict[str, Any] = {}
        self._last_seen: str | None = None
        self._next_id = 1
        self._commands: deque[dict[str, Any]] = deque(maxlen=200)
        self._results: dict[int, dict[str, Any]] = {}
        if db_path is not None:
            self.configure(db_path)

    def configure(
        self,
        db_path: str | Path,
        *,
        event_publisher: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        path = Path(db_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._db_path = path
            if event_publisher is not None:
                self._event_publisher = event_publisher
            with self._connect() as conn:
                apply_migrations(conn)

    def update_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now = _now()
            changed = state != self._state
            persisted = False
            if self._db_path is not None:
                try:
                    with self._connect() as conn:
                        conn.execute(
                            """
                            insert into agent_ui_state(singleton_id, state_json, last_seen, updated_at)
                            values (1, ?, ?, ?)
                            on conflict(singleton_id) do update set
                                state_json=excluded.state_json,
                                last_seen=excluded.last_seen,
                                updated_at=excluded.updated_at
                            """,
                            (_json(state), now, now),
                        )
                        conn.commit()
                        persisted = True
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
            else:
                persisted = True
            # A browser state heartbeat is safe to coalesce. If another
            # durable runtime write holds SQLite briefly, acknowledge the
            # current state in memory so the Dock keeps working; the next
            # heartbeat persists it rather than surfacing a 500 to the UI.
            self._state = dict(state)
            self._last_seen = now
            snapshot = self.snapshot() if persisted else {
                "schema_version": "open_stock_ai.ui_state.v1",
                "connected": True,
                "last_seen": now,
                "state": dict(state),
                "last_command_id": self._next_id - 1,
                "persistence": "deferred_database_lock",
            }
        # The WebView heartbeats every 750ms to keep UI-command dispatch live.
        # Persisting every identical heartbeat turns an idle desktop into an
        # unbounded runtime-event writer. The durable singleton is still
        # refreshed above; only an actual context change enters the Agent's
        # scheduler/audit stream.
        if changed:
            self._publish("ui.state.updated", {"state": dict(state), "ui": snapshot})
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            last_seen = self._last_seen
            last_command_id = self._next_id - 1
            if self._db_path is not None:
                with self._connect() as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "select state_json, last_seen from agent_ui_state where singleton_id=1"
                    ).fetchone()
                    command_row = conn.execute(
                        "select coalesce(max(command_id), 0) from agent_ui_commands"
                    ).fetchone()
                if row is not None:
                    state = _decode(row["state_json"], {})
                    last_seen = row["last_seen"]
                last_command_id = int(command_row[0]) if command_row else 0
            connected = False
            if last_seen is not None:
                try:
                    connected = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)
                    ).total_seconds() <= 5
                except ValueError:
                    connected = False
            return {
                "schema_version": "open_stock_ai.ui_state.v1",
                "connected": connected,
                "last_seen": last_seen,
                "state": state,
                "last_command_id": last_command_id,
            }

    def commands_after(self, command_id: int) -> list[dict[str, Any]]:
        with self._lock:
            if self._db_path is not None:
                with self._connect() as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        """
                        select command_id, action, arguments_json, created_at
                          from agent_ui_commands
                         where command_id > ? and status='pending'
                         order by command_id
                         limit 200
                        """,
                        (int(command_id),),
                    ).fetchall()
                return [
                    {
                        "command_id": int(row["command_id"]),
                        "action": row["action"],
                        "arguments": _decode(row["arguments_json"], {}),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            return [
                dict(item)
                for item in self._commands
                if int(item["command_id"]) > command_id
                and int(item["command_id"]) not in self._results
            ]

    def complete(self, command_id: int, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            completed = {**result, "completed_at": _now()}
            if self._db_path is not None:
                with self._connect() as conn:
                    cursor = conn.execute(
                        """
                        update agent_ui_commands
                           set status='completed', completed_at=?, result_json=?
                         where command_id=? and status='pending'
                        """,
                        (completed["completed_at"], _json(completed), int(command_id)),
                    )
                    conn.commit()
                if not cursor.rowcount:
                    existing = self._result(int(command_id))
                    if existing is None:
                        raise KeyError(command_id)
                    return existing
                self._publish(
                    "ui.command.completed",
                    {"command_id": int(command_id), "result": completed},
                )
                return completed
            self._results[int(command_id)] = completed
            oldest_retained = max(0, self._next_id - 200)
            for completed_id in tuple(self._results):
                if completed_id < oldest_retained:
                    self._results.pop(completed_id, None)
            result = dict(self._results[int(command_id)])
        self._publish(
            "ui.command.completed",
            {"command_id": int(command_id), "result": result},
        )
        return result

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        publisher = self._event_publisher
        if publisher is not None:
            publisher(event_type, payload)

    async def dispatch(
        self,
        action: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        command = self.enqueue(action, arguments)
        command_id = int(command["command_id"])
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            result = self.command_status(command_id)
            if result["status"] == "completed":
                acknowledgement = result["acknowledgement"] or {}
                if acknowledgement.get("ok") is False:
                    raise RuntimeError(str((acknowledgement.get("error") or {}).get("message") or "UI command failed"))
                return {
                    "schema_version": "open_stock_ai.ui_command_result.v1",
                    "command": command,
                    "acknowledgement": acknowledgement,
                    "ui_state": self.snapshot(),
                }
            await asyncio.sleep(0.1)
        raise TimeoutError(f"UI command acknowledgement timed out: {action}")

    def enqueue(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.snapshot()["connected"]:
            raise RuntimeError("No Stock AI browser or native WebView is connected to the UI bridge")
        with self._lock:
            command = {
                "action": action,
                "arguments": dict(arguments),
                "created_at": _now(),
            }
            if self._db_path is not None:
                with self._connect() as conn:
                    cursor = conn.execute(
                        """
                        insert into agent_ui_commands(
                            action, arguments_json, status, created_at
                        ) values (?, ?, 'pending', ?)
                        """,
                        (action, _json(arguments), command["created_at"]),
                    )
                    command_id = int(cursor.lastrowid)
                    conn.commit()
            else:
                command_id = self._next_id
                self._next_id += 1
                self._commands.append({"command_id": command_id, **command})
            command = {"command_id": command_id, **command}
        self._publish("ui.command.queued", {"command": command})
        return command

    def command_status(self, command_id: int) -> dict[str, Any]:
        with self._lock:
            result = self._result(int(command_id))
            status = "completed" if result is not None else "pending"
            if self._db_path is not None and result is None:
                with self._connect() as conn:
                    row = conn.execute(
                        "select status from agent_ui_commands where command_id=?",
                        (int(command_id),),
                    ).fetchone()
                if row is None:
                    raise KeyError(command_id)
                status = str(row[0])
            elif self._db_path is None and not any(
                int(item["command_id"]) == int(command_id) for item in self._commands
            ):
                raise KeyError(command_id)
            return {
                "schema_version": "stock_ai.ui_action_status.v1",
                "command_id": int(command_id),
                "status": status,
                "acknowledgement": result,
            }

    async def wait_for_state(
        self,
        key: str,
        expected: Any,
        *,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            snapshot = self.snapshot()
            actual = snapshot["state"].get(key)
            if actual == expected:
                return {
                    "schema_version": "open_stock_ai.ui_wait.v1",
                    "matched": True,
                    "key": key,
                    "expected": expected,
                    "actual": actual,
                    "ui_state": snapshot,
                }
            await asyncio.sleep(0.1)
        return {
            "schema_version": "open_stock_ai.ui_wait.v1",
            "matched": False,
            "key": key,
            "expected": expected,
            "actual": self.snapshot()["state"].get(key),
            "ui_state": self.snapshot(),
        }

    def _result(self, command_id: int) -> dict[str, Any] | None:
        if self._db_path is None:
            result = self._results.get(command_id)
            return dict(result) if result is not None else None
        with self._connect() as conn:
            row = conn.execute(
                "select result_json from agent_ui_commands where command_id=? and status='completed'",
                (int(command_id),),
            ).fetchone()
        return _decode(row[0], None) if row and row[0] else None

    def _connect(self) -> sqlite3.Connection:
        if self._db_path is None:
            raise RuntimeError("Durable UI bridge storage is not configured")
        conn = sqlite3.connect(self._db_path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


agent_ui_bridge = AgentUIBridge()
