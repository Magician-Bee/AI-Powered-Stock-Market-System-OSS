"""Durable leader-lease and fencing contract for HA service ownership."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


HA_SERVICES = ("api", "agent_runtime", "market_data", "broker_gateway", "scheduler")


class LeaseNotAcquired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceTopology:
    services: tuple[str, ...] = HA_SERVICES

    def __post_init__(self) -> None:
        if not self.services or len(set(self.services)) != len(self.services):
            raise ValueError("HA topology requires unique service names")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.ha_topology.v1",
            "services": list(self.services),
            "leader_lease": True,
            "fencing_token": True,
        }


@dataclass(frozen=True, slots=True)
class LeaseReceipt:
    action: str
    lease_name: str
    owner_id: str
    epoch: int
    expires_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.ha_lease_receipt.v1",
            "action": self.action,
            "lease_name": self.lease_name,
            "owner_id": self.owner_id,
            "epoch": self.epoch,
            "expires_at": self.expires_at,
        }

    def verify(self) -> bool:
        return hashlib.sha256(_canonical(self.payload())).hexdigest() == self.receipt_sha256


class SQLiteLeaderLease:
    """SQLite-backed lease with monotonic fencing epochs."""

    def __init__(self, database: str | Path, *, ttl_seconds: int = 30, clock: Callable[[], datetime] | None = None) -> None:
        if ttl_seconds < 1:
            raise ValueError("leader lease TTL must be positive")
        self.path = Path(database).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(seconds=ttl_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as conn:
            conn.execute(
                "create table if not exists ha_leases (lease_name text primary key, owner_id text not null, epoch integer not null, expires_at text not null)"
            )

    def claim(self, lease_name: str, owner_id: str) -> LeaseReceipt:
        now = _utc(self.clock())
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select owner_id, epoch, expires_at from ha_leases where lease_name=?", (lease_name,)).fetchone()
            if row is not None and _parse(row[2]) > now and str(row[0]) != owner_id:
                raise LeaseNotAcquired(f"lease {lease_name} is owned by another live owner")
            epoch = int(row[1]) if row is not None and str(row[0]) == owner_id and _parse(row[2]) > now else int(row[1] if row else 0) + 1
            expires = now + self.ttl
            conn.execute(
                "insert into ha_leases(lease_name, owner_id, epoch, expires_at) values(?, ?, ?, ?) on conflict(lease_name) do update set owner_id=excluded.owner_id, epoch=excluded.epoch, expires_at=excluded.expires_at",
                (lease_name, owner_id, epoch, expires.isoformat()),
            )
        return _receipt("claim", lease_name, owner_id, epoch, expires.isoformat())

    def renew(self, lease_name: str, owner_id: str, epoch: int) -> LeaseReceipt:
        now = _utc(self.clock())
        with self._connect() as conn:
            row = conn.execute("select owner_id, epoch, expires_at from ha_leases where lease_name=?", (lease_name,)).fetchone()
            if row is None or str(row[0]) != owner_id or int(row[1]) != int(epoch) or _parse(row[2]) <= now:
                raise LeaseNotAcquired("only the current unexpired owner can renew")
            expires = now + self.ttl
            conn.execute("update ha_leases set expires_at=? where lease_name=?", (expires.isoformat(), lease_name))
        return _receipt("renew", lease_name, owner_id, epoch, expires.isoformat())

    def release(self, lease_name: str, owner_id: str, epoch: int) -> LeaseReceipt:
        with self._connect() as conn:
            row = conn.execute("select owner_id, epoch, expires_at from ha_leases where lease_name=?", (lease_name,)).fetchone()
            if row is None or str(row[0]) != owner_id or int(row[1]) != int(epoch):
                raise LeaseNotAcquired("only the current owner can release")
            conn.execute("delete from ha_leases where lease_name=?", (lease_name,))
        return _receipt("release", lease_name, owner_id, epoch, str(row[2]))

    def current(self, lease_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("select owner_id, epoch, expires_at from ha_leases where lease_name=?", (lease_name,)).fetchone()
        return None if row is None else {"owner_id": str(row[0]), "epoch": int(row[1]), "expires_at": str(row[2])}

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)


def _receipt(action: str, lease_name: str, owner_id: str, epoch: int, expires_at: str) -> LeaseReceipt:
    payload = {
        "schema_version": "open_stock_ai.ha_lease_receipt.v1",
        "action": action,
        "lease_name": lease_name,
        "owner_id": owner_id,
        "epoch": epoch,
        "expires_at": expires_at,
    }
    return LeaseReceipt(action, lease_name, owner_id, epoch, expires_at, hashlib.sha256(_canonical(payload)).hexdigest())


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _parse(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
