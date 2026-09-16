from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .agent_run_store import AgentRunStore


class AgentEventBus:
    """Publish host application events into the durable Agent scheduler inbox."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._store: AgentRunStore | None = None

    def configure(self, store: AgentRunStore) -> None:
        with self._lock:
            self._store = store

    def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedup_key: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_type = str(event_type or "").strip()
        if not normalized_type:
            raise ValueError("Runtime event_type is required")
        with self._lock:
            store = self._store
        if store is None:
            return None
        normalized_payload = _json_safe(payload)
        timestamp = occurred_at or datetime.now(timezone.utc).isoformat()
        if dedup_key is None:
            minute_bucket = timestamp[:16]
            digest = hashlib.sha256(
                json.dumps(
                    normalized_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            dedup_key = f"{normalized_type}:{minute_bucket}:{digest}"
        try:
            return store.publish_runtime_event(
                normalized_type,
                normalized_payload,
                dedup_key=dedup_key,
                occurred_at=timestamp,
            )
        except sqlite3.OperationalError as exc:
            # Publishing telemetry must never turn a read-only UI endpoint
            # into a 500 while another runtime transaction owns SQLite.
            if "locked" in str(exc).lower():
                return None
            raise

    def describe(self) -> dict[str, Any]:
        with self._lock:
            configured = self._store is not None
        return {
            "schema_version": "open_stock_ai.agent_event_bus.v1",
            "configured": configured,
            "transport": "sqlite_durable_inbox",
        }


def _json_safe(value: Any) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    decoded = json.loads(encoded)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


agent_event_bus = AgentEventBus()


__all__ = ["AgentEventBus", "agent_event_bus"]
