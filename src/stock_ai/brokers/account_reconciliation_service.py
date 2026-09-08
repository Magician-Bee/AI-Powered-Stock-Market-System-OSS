"""Durable startup, periodic and event-driven account reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .account_reconciliation import (
    BrokerAccountReconciliationEngine,
    HostAccountLedger,
)
from .account_reconciliation_receipts import (
    BrokerAccountReconciliationExecutionReceipt,
    BrokerAccountReconciliationReceipt,
    BrokerAccountReconciliationReceiptStore,
)
from .contracts import BrokerAccountSnapshot, BrokerId


AccountScope = tuple[BrokerId, str]
AccountSnapshotLoader = Callable[
    [BrokerId, str], tuple[BrokerAccountSnapshot, HostAccountLedger, str]
]


@dataclass(frozen=True, slots=True)
class AccountReconciliationSchedule:
    periodic_seconds: int = 60
    event_types: tuple[str, ...] = (
        "broker.account.snapshot",
        "broker.connection.restored",
        "broker.session.started",
    )

    def __post_init__(self) -> None:
        if int(self.periodic_seconds) < 60:
            raise ValueError(
                "periodic account reconciliation interval must be at least 60 seconds"
            )
        if not self.event_types or any(not str(item).strip() for item in self.event_types):
            raise ValueError("account reconciliation requires named event types")

    def as_dict(self) -> dict[str, object]:
        return {
            "startup": True,
            "periodic_seconds": int(self.periodic_seconds),
            "event_types": list(self.event_types),
        }


class BrokerAccountReconciliationService:
    """Execute and durably record account-owner reconciliation runs.

    A loader must return broker truth, the Host-owned ledger, and the hash of
    the SDK integration receipt that authorized the snapshot. Missing values
    remain ``incomplete`` and therefore block trading; this service never
    repairs or infers account state.
    """

    def __init__(
        self,
        store: BrokerAccountReconciliationReceiptStore,
        snapshot_loader: AccountSnapshotLoader,
        *,
        engine: BrokerAccountReconciliationEngine | None = None,
        schedule: AccountReconciliationSchedule | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.snapshot_loader = snapshot_loader
        self.engine = engine or BrokerAccountReconciliationEngine()
        self.schedule = schedule or AccountReconciliationSchedule()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def triggers(self) -> dict[str, object]:
        return self.schedule.as_dict()

    def run_startup(
        self, scopes: Iterable[AccountScope]
    ) -> list[BrokerAccountReconciliationExecutionReceipt]:
        return [
            self.run("startup", broker_id, account_alias)
            for broker_id, account_alias in scopes
        ]

    def run_periodic(
        self, scopes: Iterable[AccountScope]
    ) -> list[BrokerAccountReconciliationExecutionReceipt]:
        return [
            self.run("periodic", broker_id, account_alias)
            for broker_id, account_alias in scopes
        ]

    def handle_event(
        self,
        event_type: str,
        event_id: str,
        scopes: Iterable[AccountScope],
    ) -> list[BrokerAccountReconciliationExecutionReceipt]:
        if event_type not in self.schedule.event_types:
            return []
        if not str(event_id).strip():
            raise ValueError("account reconciliation events require an event ID")
        if not self.store.claim_event(event_type, event_id):
            return []
        return [
            self.run(f"event:{event_type}", broker_id, account_alias)
            for broker_id, account_alias in scopes
        ]

    def run(
        self, trigger: str, broker_id: BrokerId, account_alias: str
    ) -> BrokerAccountReconciliationExecutionReceipt:
        if trigger not in {"startup", "periodic"} and not trigger.startswith("event:"):
            raise ValueError("unsupported account reconciliation trigger")
        broker, host, integration_receipt_sha256 = self.snapshot_loader(
            broker_id, str(account_alias)
        )
        result = self.engine.reconcile(broker, host)
        observed_at = self.clock()
        account_receipt = BrokerAccountReconciliationReceipt.issue(
            receipt_id=f"BAR-{uuid.uuid4().hex}",
            broker=broker,
            host=host,
            result=result,
            integration_receipt_sha256=integration_receipt_sha256,
            observed_at=observed_at,
        )
        self.store.record(account_receipt)
        execution = BrokerAccountReconciliationExecutionReceipt.issue(
            receipt_id=f"BAE-{uuid.uuid4().hex}",
            trigger=trigger,
            broker_id=broker.broker_id,
            account_id_masked=broker.account_id_masked,
            reconciliation_receipt_sha256=account_receipt.receipt_sha256,
            status="completed" if result.trading_allowed else "blocked",
            observed_at=observed_at,
        )
        return self.store.record_execution(execution)
