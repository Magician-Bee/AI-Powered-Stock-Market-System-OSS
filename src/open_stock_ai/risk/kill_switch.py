from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator


_SCHEMA = "open_stock_ai.risk_control.v2"
_SCOPE_TYPES = ("global", "account", "broker", "strategy", "symbol")


@dataclass(frozen=True)
class LossLimitPolicy:
    """Durable loss limits used by the order boundary."""

    intraday_loss_pct: float = 1.5
    daily_loss_pct: float = 3.0
    weekly_loss_pct: float = 6.0
    monthly_loss_pct: float = 10.0
    consecutive_loss_count: int = 3

    def __post_init__(self) -> None:
        if any(float(value) <= 0 for value in (
            self.intraday_loss_pct,
            self.daily_loss_pct,
            self.weekly_loss_pct,
            self.monthly_loss_pct,
        )):
            raise ValueError("Loss limits must be positive")
        if int(self.consecutive_loss_count) <= 0:
            raise ValueError("consecutive_loss_count must be positive")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DurableRiskControlStore:
    """SQLite-backed kill switches and realized-P&L loss-limit receipts.

    The store is intentionally fail-closed at the order boundary: an active
    switch for the global scope or any matching account/broker/strategy/symbol
    scope blocks a new order.  Events are idempotent by ``event_id`` so a
    broker retry cannot inflate realized losses twice.
    """

    def __init__(
        self,
        database_path: str | Path = ":memory:",
        policy: LossLimitPolicy | None = None,
    ) -> None:
        self.database_path = str(database_path)
        self.policy = policy or LossLimitPolicy()
        self._lock = RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self.database_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            yield self._memory_connection
            return
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                create table if not exists risk_control_switches (
                    scope_type text not null,
                    scope_key text not null,
                    enabled integer not null,
                    reason text not null,
                    activated_at text not null,
                    activated_by text not null,
                    primary key (scope_type, scope_key)
                );
                create table if not exists risk_pnl_events (
                    event_id text primary key,
                    occurred_at text not null,
                    pnl_pct real not null,
                    scopes_json text not null
                );
                create index if not exists idx_risk_pnl_events_occurred_at
                    on risk_pnl_events (occurred_at);
                """
            )
            connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _timestamp(value: str | datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _scope(scope_type: str, scope_key: str) -> tuple[str, str]:
        normalized_type = str(scope_type).strip().lower()
        normalized_key = str(scope_key).strip()
        if normalized_type not in _SCOPE_TYPES:
            raise ValueError(f"Unsupported risk scope: {scope_type}")
        if not normalized_key:
            raise ValueError("Risk scope key is required")
        return normalized_type, normalized_key

    @classmethod
    def _scopes(
        cls,
        scopes: dict[str, Any] | None,
        *,
        include_global: bool = True,
    ) -> dict[str, str]:
        normalized = {"global": "global"} if include_global else {}
        for scope_type, scope_key in (scopes or {}).items():
            normalized_type, normalized_key = cls._scope(scope_type, str(scope_key))
            normalized[normalized_type] = normalized_key
        return normalized

    def activate(
        self,
        scope_type: str = "global",
        scope_key: str = "global",
        reason: str = "Manual risk-control activation.",
        activated_by: str = "system",
    ) -> dict[str, Any]:
        normalized_type, normalized_key = self._scope(scope_type, scope_key)
        activated_at = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                insert into risk_control_switches
                    (scope_type, scope_key, enabled, reason, activated_at, activated_by)
                values (?, ?, 1, ?, ?, ?)
                on conflict(scope_type, scope_key) do update set
                    enabled=1, reason=excluded.reason,
                    activated_at=excluded.activated_at, activated_by=excluded.activated_by
                """,
                (normalized_type, normalized_key, str(reason), activated_at, str(activated_by)),
            )
            connection.commit()
        return self.order_gate({normalized_type: normalized_key})

    def clear(self, scope_type: str = "global", scope_key: str = "global") -> dict[str, Any]:
        normalized_type, normalized_key = self._scope(scope_type, scope_key)
        with self._lock, self._connect() as connection:
            connection.execute(
                "update risk_control_switches set enabled=0 where scope_type=? and scope_key=?",
                (normalized_type, normalized_key),
            )
            connection.commit()
        return self.order_gate({normalized_type: normalized_key})

    def active_switches(self, scopes: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        normalized = self._scopes(scopes)
        scope_clauses = ["scope_type='global'"]
        parameters: list[Any] = []
        for scope_type, scope_key in normalized.items():
            if scope_type == "global":
                continue
            scope_clauses.append("(scope_type=? and scope_key=?)")
            parameters.extend((scope_type, scope_key))
        query = (
            "select scope_type, scope_key, reason, activated_at, activated_by "
            "from risk_control_switches where enabled=1 and (" + " or ".join(scope_clauses) + ")"
        )
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def order_gate(self, scopes: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = self._scopes(scopes)
        active = self.active_switches(normalized)
        payload: dict[str, Any] = {
            "schema_version": "open_stock_ai.risk_order_gate.v1",
            "method": "durable_risk_control_order_gate",
            "allowed": not active,
            "scopes": normalized,
            "active_switches": active,
        }
        payload["receipt_sha256"] = self._hash(payload)
        return payload

    def record_realized_pnl(
        self,
        event_id: str,
        pnl_pct: float,
        occurred_at: str | datetime | None = None,
        scopes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(event_id).strip():
            raise ValueError("event_id is required")
        normalized_scopes = self._scopes(scopes, include_global=scopes is None)
        timestamp = self._timestamp(occurred_at).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "insert or ignore into risk_pnl_events (event_id, occurred_at, pnl_pct, scopes_json) values (?, ?, ?, ?)",
                (
                    str(event_id),
                    timestamp,
                    float(pnl_pct),
                    json.dumps(normalized_scopes, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.commit()
        return {
            "schema_version": "open_stock_ai.realized_pnl_event.v1",
            "event_id": str(event_id),
            "occurred_at": timestamp,
            "pnl_pct": float(pnl_pct),
            "scopes": normalized_scopes,
            "receipt_sha256": self._hash({
                "event_id": str(event_id),
                "occurred_at": timestamp,
                "pnl_pct": float(pnl_pct),
                "scopes": normalized_scopes,
            }),
        }

    def evaluate_limits(
        self,
        as_of: str | datetime | None = None,
        scopes: dict[str, Any] | None = None,
        policy: LossLimitPolicy | None = None,
    ) -> dict[str, Any]:
        effective_policy = policy or self.policy
        timestamp = self._timestamp(as_of)
        normalized_scopes = self._scopes(scopes)
        events = self._events()
        violations: list[dict[str, Any]] = []
        periods = {
            "intraday": timestamp - timedelta(hours=24),
            "daily": timestamp.replace(hour=0, minute=0, second=0, microsecond=0),
            "weekly": timestamp - timedelta(days=7),
            "monthly": timestamp - timedelta(days=31),
        }
        limits = {
            "intraday": effective_policy.intraday_loss_pct,
            "daily": effective_policy.daily_loss_pct,
            "weekly": effective_policy.weekly_loss_pct,
            "monthly": effective_policy.monthly_loss_pct,
        }
        for scope_type, scope_key in normalized_scopes.items():
            matching = [event for event in events if self._event_matches(event, scope_type, scope_key)]
            for period, start in periods.items():
                total = sum(event["pnl_pct"] for event in matching if start <= event["occurred_at"] <= timestamp)
                if total < -float(limits[period]):
                    violations.append({
                        "scope_type": scope_type,
                        "scope_key": scope_key,
                        "limit_type": f"{period}_loss_pct",
                        "observed_loss_pct": round(abs(total), 8),
                        "limit_loss_pct": float(limits[period]),
                    })
            recent = sorted(
                (event for event in matching if event["occurred_at"] <= timestamp),
                key=lambda event: event["occurred_at"],
                reverse=True,
            )
            consecutive = 0
            for event in recent:
                if event["pnl_pct"] >= 0:
                    break
                consecutive += 1
            if consecutive >= int(effective_policy.consecutive_loss_count):
                violations.append({
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "limit_type": "consecutive_loss_count",
                    "observed_loss_count": consecutive,
                    "limit_loss_count": int(effective_policy.consecutive_loss_count),
                })
        for violation in violations:
            self.activate(
                violation["scope_type"],
                violation["scope_key"],
                reason=f"Loss limit breached: {violation['limit_type']}",
                activated_by="loss_limit_monitor",
            )
        payload: dict[str, Any] = {
            "schema_version": "open_stock_ai.risk_loss_limit_receipt.v1",
            "method": "durable_realized_pnl_loss_limit_evaluation",
            "as_of": timestamp.isoformat(),
            "scopes": normalized_scopes,
            "policy": effective_policy.as_dict(),
            "violations": violations,
            "order_allowed": not self.active_switches(normalized_scopes),
        }
        payload["receipt_sha256"] = self._hash(payload)
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "select scope_type, scope_key, reason, activated_at, activated_by "
                "from risk_control_switches where enabled=1 order by scope_type, scope_key"
            ).fetchall()
        return {
            "schema_version": _SCHEMA,
            "method": "durable_risk_control_status",
            "database_path": self.database_path,
            "policy": self.policy.as_dict(),
            "active_switches": [dict(row) for row in rows],
        }

    def _events(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "select event_id, occurred_at, pnl_pct, scopes_json from risk_pnl_events"
            ).fetchall()
        events = []
        for row in rows:
            try:
                event_scopes = self._scopes(json.loads(row["scopes_json"]), include_global=False)
            except (TypeError, json.JSONDecodeError, ValueError):
                continue
            events.append({
                "event_id": row["event_id"],
                "occurred_at": self._timestamp(row["occurred_at"]),
                "pnl_pct": float(row["pnl_pct"]),
                "scopes": event_scopes,
            })
        return events

    @staticmethod
    def _event_matches(event: dict[str, Any], scope_type: str, scope_key: str) -> bool:
        event_scopes = event["scopes"]
        return event_scopes.get("global") == "global" or event_scopes.get(scope_type) == scope_key


@dataclass
class KillSwitch:
    """Compatibility facade for callers that only need live trading disabled."""

    enabled: bool = True
    reason: str = "Live trading is disabled by Open Stock AI policy."
    database_path: str | Path | None = None

    def __post_init__(self) -> None:
        self.store = DurableRiskControlStore(self.database_path or ":memory:")
        if self.enabled:
            self.store.activate("global", "global", self.reason, "kill_switch")

    def live_trading_allowed(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        status = self.store.status()
        status.update({
            "enabled": self.enabled,
            "reason": self.reason,
            "live_trading_allowed": False,
        })
        return status
