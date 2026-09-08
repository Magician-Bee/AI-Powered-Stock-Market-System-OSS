from __future__ import annotations

"""Durable external-account reconciliation evidence.

The reconciliation engine compares values in memory.  This ledger preserves
the cryptographic identities of the broker snapshot, Host ledger and result so
a later restart cannot turn a transient "matched" response into production
proof.  It stores no raw balances, positions, orders, fills or account IDs.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .account_reconciliation import AccountReconciliationResult, HostAccountLedger
from .contracts import BrokerAccountSnapshot, BrokerId
from .integration_receipts import BrokerIntegrationReceiptStore


_SCHEMA = "stock_ai.account_reconciliation_receipt.v1"
_EXECUTION_SCHEMA = "stock_ai.account_reconciliation_execution.v1"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _digest(value: str, name: str) -> str:
    value = str(value).lower().strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class BrokerAccountReconciliationReceipt:
    receipt_id: str
    broker_id: BrokerId
    account_id_masked: str
    integration_receipt_sha256: str
    broker_snapshot_sha256: str
    host_ledger_sha256: str
    reconciliation_result_sha256: str
    status: str
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        broker: BrokerAccountSnapshot,
        host: HostAccountLedger,
        result: AccountReconciliationResult,
        integration_receipt_sha256: str,
        observed_at: datetime | None = None,
    ) -> "BrokerAccountReconciliationReceipt":
        if result.broker_id != broker.broker_id or result.account_id_masked != broker.account_id_masked:
            raise ValueError("reconciliation result must belong to broker snapshot")
        if host.broker_id != broker.broker_id or host.account_id_masked != broker.account_id_masked:
            raise ValueError("Host ledger must belong to broker snapshot")
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            broker_id=broker.broker_id,
            account_id_masked=broker.account_id_masked,
            integration_receipt_sha256=_digest(integration_receipt_sha256, "integration_receipt_sha256"),
            broker_snapshot_sha256=_hash(broker.model_dump(mode="json")),
            host_ledger_sha256=_hash(host.model_dump(mode="json")),
            reconciliation_result_sha256=_hash(result.model_dump(mode="json")),
            status=result.status,
            observed_at=_utc(observed_at or datetime.now(timezone.utc)),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_hash(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA, "receipt_id": self.receipt_id,
            "broker_id": self.broker_id, "account_id_masked": self.account_id_masked,
            "integration_receipt_sha256": self.integration_receipt_sha256,
            "broker_snapshot_sha256": self.broker_snapshot_sha256,
            "host_ledger_sha256": self.host_ledger_sha256,
            "reconciliation_result_sha256": self.reconciliation_result_sha256,
            "status": self.status, "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, value: dict[str, Any]) -> "BrokerAccountReconciliationReceipt":
        if value.get("schema_version") != _SCHEMA:
            raise ValueError("invalid account reconciliation receipt schema")
        receipt = cls(
            receipt_id=str(value.get("receipt_id") or ""),
            broker_id=str(value.get("broker_id") or ""),  # type: ignore[arg-type]
            account_id_masked=str(value.get("account_id_masked") or ""),
            integration_receipt_sha256=str(value.get("integration_receipt_sha256") or ""),
            broker_snapshot_sha256=str(value.get("broker_snapshot_sha256") or ""),
            host_ledger_sha256=str(value.get("host_ledger_sha256") or ""),
            reconciliation_result_sha256=str(value.get("reconciliation_result_sha256") or ""),
            status=str(value.get("status") or ""),
            observed_at=_utc(datetime.fromisoformat(str(value.get("observed_at") or ""))),
            receipt_sha256=str(value.get("receipt_sha256") or ""),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not any(token in self.account_id_masked for token in ("*", "•", "x", "X")):
            raise ValueError("account reconciliation receipt requires masked account identity")
        for field in (
            "integration_receipt_sha256", "broker_snapshot_sha256", "host_ledger_sha256",
            "reconciliation_result_sha256", "receipt_sha256",
        ):
            _digest(getattr(self, field), field)
        if self.status not in {"matched", "mismatch", "incomplete"}:
            raise ValueError("invalid account reconciliation status")
        if self.receipt_sha256 != _hash(self.payload()):
            raise ValueError("account reconciliation receipt hash mismatch")


@dataclass(frozen=True, slots=True)
class BrokerAccountReconciliationExecutionReceipt:
    """Durable evidence that an account reconciliation trigger completed."""

    receipt_id: str
    trigger: str
    broker_id: BrokerId
    account_id_masked: str
    reconciliation_receipt_sha256: str
    status: str
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        trigger: str,
        broker_id: BrokerId,
        account_id_masked: str,
        reconciliation_receipt_sha256: str,
        status: str,
        observed_at: datetime | None = None,
    ) -> "BrokerAccountReconciliationExecutionReceipt":
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            trigger=str(trigger).strip(),
            broker_id=broker_id,
            account_id_masked=str(account_id_masked),
            reconciliation_receipt_sha256=_digest(
                reconciliation_receipt_sha256, "reconciliation_receipt_sha256"
            ),
            status=str(status),
            observed_at=_utc(observed_at or datetime.now(timezone.utc)),
            receipt_sha256="",
        )
        return replace(receipt, receipt_sha256=_hash(receipt.payload()))

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _EXECUTION_SCHEMA,
            "receipt_id": self.receipt_id,
            "trigger": self.trigger,
            "broker_id": self.broker_id,
            "account_id_masked": self.account_id_masked,
            "reconciliation_receipt_sha256": self.reconciliation_receipt_sha256,
            "status": self.status,
            "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, value: dict[str, Any]) -> "BrokerAccountReconciliationExecutionReceipt":
        if value.get("schema_version") != _EXECUTION_SCHEMA:
            raise ValueError("invalid account reconciliation execution schema")
        receipt = cls(
            receipt_id=str(value.get("receipt_id") or ""),
            trigger=str(value.get("trigger") or ""),
            broker_id=str(value.get("broker_id") or ""),  # type: ignore[arg-type]
            account_id_masked=str(value.get("account_id_masked") or ""),
            reconciliation_receipt_sha256=str(
                value.get("reconciliation_receipt_sha256") or ""
            ),
            status=str(value.get("status") or ""),
            observed_at=_utc(datetime.fromisoformat(str(value.get("observed_at") or ""))),
            receipt_sha256=str(value.get("receipt_sha256") or ""),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.trigger:
            raise ValueError("account reconciliation execution requires an ID and trigger")
        if not any(token in self.account_id_masked for token in ("*", "•", "x", "X")):
            raise ValueError("account reconciliation execution requires masked account identity")
        _digest(self.reconciliation_receipt_sha256, "reconciliation_receipt_sha256")
        _digest(self.receipt_sha256, "receipt_sha256")
        if self.status not in {"completed", "blocked"}:
            raise ValueError("invalid account reconciliation execution status")
        if self.receipt_sha256 != _hash(self.payload()):
            raise ValueError("account reconciliation execution hash mismatch")


class BrokerAccountReconciliationReceiptStore:
    def __init__(self, path: str | Path, *, integration_store: BrokerIntegrationReceiptStore | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.integration_store = integration_store
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 5000")
        connection.execute("pragma synchronous = full")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute("""
                create table if not exists broker_account_reconciliation_receipts (
                    receipt_id text primary key, broker_id text not null, receipt_sha256 text not null unique,
                    receipt_json text not null, persisted_at text not null
                )
            """)
            connection.execute("""
                create table if not exists broker_account_reconciliation_executions (
                    receipt_id text primary key, broker_id text not null,
                    receipt_sha256 text not null unique, receipt_json text not null,
                    persisted_at text not null
                )
            """)
            connection.execute("""
                create table if not exists broker_account_reconciliation_events (
                    event_id text primary key, event_type text not null,
                    claimed_at text not null
                )
            """)
            for action in ("update", "delete"):
                connection.execute(f"""
                    create trigger if not exists broker_account_reconciliation_immutable_{action}
                    before {action} on broker_account_reconciliation_receipts
                    begin select raise(abort, 'broker account reconciliation evidence is immutable'); end
                """)
                connection.execute(f"""
                    create trigger if not exists broker_account_reconciliation_execution_immutable_{action}
                    before {action} on broker_account_reconciliation_executions
                    begin select raise(abort, 'broker account reconciliation execution evidence is immutable'); end
                """)
                connection.execute(f"""
                    create trigger if not exists broker_account_reconciliation_event_immutable_{action}
                    before {action} on broker_account_reconciliation_events
                    begin select raise(abort, 'broker account reconciliation event evidence is immutable'); end
                """)

    def record(self, receipt: BrokerAccountReconciliationReceipt) -> BrokerAccountReconciliationReceipt:
        receipt.verify()
        if self.integration_store is not None:
            linked = {item.receipt.receipt_sha256 for item in self.integration_store.receipts(receipt.broker_id)}
            if receipt.integration_receipt_sha256 not in linked:
                raise ValueError("account reconciliation is not linked to durable SDK integration evidence")
        with self._connect() as connection:
            connection.execute(
                "insert into broker_account_reconciliation_receipts values (?, ?, ?, ?, ?)",
                (receipt.receipt_id, receipt.broker_id, receipt.receipt_sha256,
                 json.dumps(receipt.model_dump(), sort_keys=True), datetime.now(timezone.utc).isoformat()),
            )
        return receipt

    def receipts(self, broker_id: BrokerId | None = None) -> list[BrokerAccountReconciliationReceipt]:
        query = "select receipt_json from broker_account_reconciliation_receipts"
        parameters: tuple[str, ...] = ()
        if broker_id:
            query += " where broker_id = ?"; parameters = (broker_id,)
        query += " order by persisted_at, receipt_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [BrokerAccountReconciliationReceipt.model_validate(json.loads(row["receipt_json"])) for row in rows]

    def record_execution(
        self, receipt: BrokerAccountReconciliationExecutionReceipt
    ) -> BrokerAccountReconciliationExecutionReceipt:
        receipt.verify()
        with self._connect() as connection:
            connection.execute(
                "insert into broker_account_reconciliation_executions values (?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.broker_id,
                    receipt.receipt_sha256,
                    json.dumps(receipt.model_dump(), sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return receipt

    def execution_receipts(
        self, broker_id: BrokerId | None = None
    ) -> list[BrokerAccountReconciliationExecutionReceipt]:
        query = "select receipt_json from broker_account_reconciliation_executions"
        parameters: tuple[str, ...] = ()
        if broker_id:
            query += " where broker_id = ?"
            parameters = (broker_id,)
        query += " order by persisted_at, receipt_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            BrokerAccountReconciliationExecutionReceipt.model_validate(
                json.loads(row["receipt_json"])
            )
            for row in rows
        ]

    def claim_event(self, event_type: str, event_id: str) -> bool:
        event_id = str(event_id).strip()
        event_type = str(event_type).strip()
        if not event_id:
            raise ValueError("account reconciliation events require an event ID")
        if not event_type:
            raise ValueError("account reconciliation events require an event type")
        with self._connect() as connection:
            cursor = connection.execute(
                "insert or ignore into broker_account_reconciliation_events values (?, ?, ?)",
                (event_id, event_type, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1
