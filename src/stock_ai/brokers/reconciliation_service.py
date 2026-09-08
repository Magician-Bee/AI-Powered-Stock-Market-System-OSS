"""Triggerable broker-order reconciliation execution service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .contracts import BrokerOrderReconciliationSnapshot, BrokerRawEvent
from .oms import BrokerOrderManagementGateway


SnapshotLoader = Callable[[str, str], tuple[BrokerOrderReconciliationSnapshot, BrokerRawEvent]]
Scope = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ReconciliationSchedule:
    periodic_seconds: int = 60
    event_types: tuple[str, ...] = (
        "broker.order.report",
        "broker.connection.restored",
        "broker.session.started",
    )

    def __post_init__(self) -> None:
        if int(self.periodic_seconds) < 60:
            raise ValueError("periodic reconciliation interval must be at least 60 seconds")
        if not self.event_types or any(not str(item).strip() for item in self.event_types):
            raise ValueError("event-driven reconciliation requires named event types")

    def as_dict(self) -> dict[str, Any]:
        return {
            "startup": True,
            "periodic_seconds": int(self.periodic_seconds),
            "event_types": list(self.event_types),
        }


@dataclass(frozen=True, slots=True)
class ReconciliationExecutionReceipt:
    receipt_id: str
    trigger: str
    broker_id: str
    account_alias: str
    status: str
    result: dict[str, Any]
    observed_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "stock_ai.broker_reconciliation_execution.v1",
            "receipt_id": self.receipt_id,
            "trigger": self.trigger,
            "broker_id": self.broker_id,
            "account_alias": self.account_alias,
            "status": self.status,
            "result": self.result,
            "observed_at": self.observed_at,
        }

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return expected == self.receipt_sha256

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReconciliationExecutionReceipt":
        if payload.get("schema_version") != "stock_ai.broker_reconciliation_execution.v1":
            raise ValueError("invalid broker reconciliation execution schema")
        receipt = cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            trigger=str(payload.get("trigger") or ""),
            broker_id=str(payload.get("broker_id") or ""),
            account_alias=str(payload.get("account_alias") or ""),
            status=str(payload.get("status") or ""),
            result=dict(payload.get("result") or {}),
            observed_at=str(payload.get("observed_at") or ""),
            receipt_sha256=str(payload.get("receipt_sha256") or ""),
        )
        if not receipt.verify():
            raise ValueError("broker reconciliation execution receipt hash mismatch")
        return receipt


class ReconciliationReceiptStore:
    """Durable immutable execution receipts and event claims."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute("pragma synchronous = full")
            connection.executescript(
                """
                create table if not exists broker_reconciliation_execution_receipts (
                    receipt_id text primary key,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null
                );
                create table if not exists broker_reconciliation_event_claims (
                    event_id text primary key,
                    event_type text not null,
                    claimed_at text not null
                );
                create trigger if not exists broker_reconciliation_receipts_immutable_update
                    before update on broker_reconciliation_execution_receipts
                    begin select raise(abort, 'broker reconciliation receipts are immutable'); end;
                create trigger if not exists broker_reconciliation_receipts_immutable_delete
                    before delete on broker_reconciliation_execution_receipts
                    begin select raise(abort, 'broker reconciliation receipts are immutable'); end;
                create trigger if not exists broker_reconciliation_events_immutable_update
                    before update on broker_reconciliation_event_claims
                    begin select raise(abort, 'broker reconciliation event claims are immutable'); end;
                create trigger if not exists broker_reconciliation_events_immutable_delete
                    before delete on broker_reconciliation_event_claims
                    begin select raise(abort, 'broker reconciliation event claims are immutable'); end;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 5000")
        return connection

    def record(self, receipt: ReconciliationExecutionReceipt) -> ReconciliationExecutionReceipt:
        if not receipt.verify():
            raise ValueError("broker reconciliation execution receipt hash mismatch")
        serialized = json.dumps(receipt.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute(
                "select receipt_json from broker_reconciliation_execution_receipts where receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["receipt_json"]) != serialized:
                    raise ValueError("broker reconciliation receipt identity is bound to different evidence")
                return receipt
            connection.execute(
                "insert into broker_reconciliation_execution_receipts values (?, ?, ?, ?)",
                (receipt.receipt_id, receipt.receipt_sha256, serialized, datetime.now(timezone.utc).isoformat()),
            )
        return receipt

    def receipts(self) -> tuple[ReconciliationExecutionReceipt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "select receipt_json from broker_reconciliation_execution_receipts order by persisted_at, receipt_id"
            ).fetchall()
        return tuple(
            ReconciliationExecutionReceipt.from_dict(json.loads(str(row["receipt_json"])))
            for row in rows
        )

    def claim_event(self, event_type: str, event_id: str) -> bool:
        normalized_type = str(event_type).strip()
        normalized_id = str(event_id).strip()
        if not normalized_type or not normalized_id:
            raise ValueError("broker reconciliation events require event type and ID")
        with self._connect() as connection:
            cursor = connection.execute(
                "insert or ignore into broker_reconciliation_event_claims values (?, ?, ?)",
                (normalized_id, normalized_type, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1


class BrokerReconciliationService:
    """Run the same OMS reconciliation boundary from three trigger classes.

    The service owns trigger routing and event de-duplication.  Broker adapters
    provide the snapshot/raw-event pair through ``snapshot_loader``; no order
    can be retried from this service until the OMS has reconciled the pair.
    """

    def __init__(
        self,
        oms: BrokerOrderManagementGateway,
        snapshot_loader: SnapshotLoader,
        *,
        schedule: ReconciliationSchedule | None = None,
        clock: Callable[[], str] | None = None,
        receipt_store: ReconciliationReceiptStore | None = None,
    ) -> None:
        self.oms = oms
        self.snapshot_loader = snapshot_loader
        self.schedule = schedule or ReconciliationSchedule()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.receipt_store = receipt_store
        self._receipts: list[ReconciliationExecutionReceipt] = (
            list(receipt_store.receipts()) if receipt_store else []
        )
        self._handled_event_ids: set[str] = set()

    def triggers(self) -> dict[str, Any]:
        return self.schedule.as_dict()

    def run_startup(self, scopes: Iterable[Scope]) -> list[ReconciliationExecutionReceipt]:
        return [self.run("startup", broker_id, account_alias) for broker_id, account_alias in scopes]

    def run_periodic(self, scopes: Iterable[Scope]) -> list[ReconciliationExecutionReceipt]:
        return [self.run("periodic", broker_id, account_alias) for broker_id, account_alias in scopes]

    def handle_event(
        self,
        event_type: str,
        event_id: str,
        scopes: Iterable[Scope],
    ) -> list[ReconciliationExecutionReceipt]:
        if event_type not in self.schedule.event_types:
            return []
        if not str(event_id).strip():
            raise ValueError("event-driven reconciliation requires an event ID")
        if self.receipt_store is not None:
            if not self.receipt_store.claim_event(event_type, event_id):
                return []
        else:
            if event_id in self._handled_event_ids:
                return []
            self._handled_event_ids.add(event_id)
        return [
            self.run(f"event:{event_type}", broker_id, account_alias)
            for broker_id, account_alias in scopes
        ]

    def run(self, trigger: str, broker_id: str, account_alias: str) -> ReconciliationExecutionReceipt:
        if trigger not in {"startup", "periodic"} and not trigger.startswith("event:"):
            raise ValueError("unsupported reconciliation trigger")
        snapshot, raw_event = self.snapshot_loader(str(broker_id), str(account_alias))
        result = dict(self.oms.reconcile_snapshot(snapshot, raw_event=raw_event))
        status = "completed" if result.get("trading_allowed") else "blocked"
        observed_at = str(self.clock())
        payload = {
            "schema_version": "stock_ai.broker_reconciliation_execution.v1",
            "receipt_id": f"BRE-{len(self._receipts) + 1:08d}",
            "trigger": trigger,
            "broker_id": str(broker_id),
            "account_alias": str(account_alias),
            "status": status,
            "result": result,
            "observed_at": observed_at,
        }
        receipt = ReconciliationExecutionReceipt(
            receipt_id=payload["receipt_id"],
            trigger=trigger,
            broker_id=str(broker_id),
            account_alias=str(account_alias),
            status=status,
            result=result,
            observed_at=observed_at,
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        if self.receipt_store is not None:
            self.receipt_store.record(receipt)
        self._receipts.append(receipt)
        return receipt

    def receipts(self) -> tuple[ReconciliationExecutionReceipt, ...]:
        return tuple(self._receipts)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
