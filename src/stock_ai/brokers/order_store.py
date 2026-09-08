from __future__ import annotations

"""Durable write-ahead storage for Broker OMS order state."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import BrokerOrderIntent, BrokerOrderReceipt, BrokerOrderState
from .order_approval import BrokerHumanApprovalReceipt
from .risk import BrokerPreTradeRiskReceipt
from .live_activation import BrokerRestrictedLiveActivationReceipt


_TERMINAL_STATES = (
    BrokerOrderState.FILLED,
    BrokerOrderState.CANCELLED,
    BrokerOrderState.REJECTED,
)


@dataclass(frozen=True, slots=True)
class DurableOrderEntry:
    intent: BrokerOrderIntent
    state: BrokerOrderState
    receipt: BrokerOrderReceipt | None
    broker_order_id: str | None
    filled_quantity: Decimal
    remaining_quantity: Decimal
    parent_intent_id: str | None
    replacement_intent_id: str | None
    report_ids: tuple[str, ...]
    action_receipts: tuple[BrokerOrderReceipt, ...]
    human_approval_receipt: BrokerHumanApprovalReceipt | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BrokerOMSRecoveryReceipt:
    """Durable evidence of the safety barrier applied during OMS restart."""

    receipt_id: str
    recovered_at: datetime
    open_orders_before_recovery: tuple[dict[str, str], ...]
    marked_unknown_intent_ids: tuple[str, ...]
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class DurablePreTradeRiskReceipt:
    intent_id: str
    receipt: BrokerPreTradeRiskReceipt
    persisted_at: datetime


@dataclass(frozen=True, slots=True)
class DurableRestrictedLiveActivationUse:
    intent_id: str
    receipt: BrokerRestrictedLiveActivationReceipt
    notional: Decimal
    persisted_at: datetime


class BrokerOMSStore:
    """SQLite WAL source of truth for OMS idempotency and identity bindings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        # A live-order write-ahead record must survive a successful commit,
        # not merely remain in the process page cache.
        connection.execute("pragma synchronous = full")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute(
                """
                create table if not exists broker_oms_orders (
                    intent_id text primary key,
                    idempotency_key text not null unique,
                    broker_id text not null,
                    account_alias text not null,
                    broker_order_id text,
                    state text not null,
                    intent_json text not null,
                    receipt_json text,
                    filled_quantity text not null,
                    remaining_quantity text not null,
                    parent_intent_id text,
                    replacement_intent_id text,
                    report_ids_json text not null,
                    action_receipts_json text not null,
                    human_approval_receipt_json text,
                    updated_at text not null,
                    unique (broker_id, broker_order_id)
                )
                """
            )
            connection.execute(
                """
                create table if not exists broker_restricted_live_activation_receipts (
                    receipt_id text primary key,
                    broker_id text not null,
                    account_alias text not null,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists broker_restricted_live_activation_uses (
                    intent_id text primary key references broker_oms_orders(intent_id),
                    receipt_id text not null references broker_restricted_live_activation_receipts(receipt_id),
                    trading_date text not null,
                    notional text not null,
                    persisted_at text not null
                )
                """
            )
            for table in (
                "broker_restricted_live_activation_receipts",
                "broker_restricted_live_activation_uses",
            ):
                connection.execute(
                    f"""
                    create trigger if not exists {table}_immutable_update
                    before update on {table}
                    begin select raise(abort, 'restricted-live activation evidence is immutable'); end
                    """
                )
                connection.execute(
                    f"""
                    create trigger if not exists {table}_immutable_delete
                    before delete on {table}
                    begin select raise(abort, 'restricted-live activation evidence is immutable'); end
                    """
                )
            connection.execute(
                """
                create table if not exists broker_pretrade_risk_receipts (
                    intent_id text primary key references broker_oms_orders(intent_id),
                    receipt_id text not null unique,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null
                )
                """
            )
            connection.execute(
                """
                create trigger if not exists broker_pretrade_risk_receipts_immutable_update
                before update on broker_pretrade_risk_receipts
                begin select raise(abort, 'pre-trade risk receipts are immutable'); end
                """
            )
            connection.execute(
                """
                create trigger if not exists broker_pretrade_risk_receipts_immutable_delete
                before delete on broker_pretrade_risk_receipts
                begin select raise(abort, 'pre-trade risk receipts are immutable'); end
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("pragma table_info(broker_oms_orders)")
            }
            if "human_approval_receipt_json" not in columns:
                connection.execute(
                    "alter table broker_oms_orders add column human_approval_receipt_json text"
                )
            connection.execute(
                """
                create table if not exists broker_oms_recovery_receipts (
                    receipt_id text primary key,
                    recovered_at text not null,
                    open_orders_json text not null,
                    marked_unknown_intent_ids_json text not null,
                    receipt_sha256 text not null unique
                )
                """
            )

    def save(self, entry: DurableOrderEntry) -> None:
        payload = (
            entry.intent.intent_id, entry.intent.idempotency_key,
            entry.intent.broker_id, entry.intent.account_alias,
            entry.broker_order_id, entry.state.value,
            _json(entry.intent.model_dump(mode="json")),
            _json(entry.receipt.model_dump(mode="json")) if entry.receipt else None,
            str(entry.filled_quantity), str(entry.remaining_quantity),
            entry.parent_intent_id, entry.replacement_intent_id,
            _json(list(entry.report_ids)),
            _json([item.model_dump(mode="json") for item in entry.action_receipts]),
            (
                _json(entry.human_approval_receipt.model_dump(mode="json"))
                if entry.human_approval_receipt
                else None
            ),
            _timestamp(entry.updated_at),
        )
        with self._connect() as connection:
            connection.execute(
                """
                insert into broker_oms_orders (
                    intent_id,idempotency_key,broker_id,account_alias,broker_order_id,
                    state,intent_json,receipt_json,filled_quantity,remaining_quantity,
                    parent_intent_id,replacement_intent_id,report_ids_json,
                    action_receipts_json,human_approval_receipt_json,updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(intent_id) do update set
                    idempotency_key=excluded.idempotency_key,
                    broker_id=excluded.broker_id,
                    account_alias=excluded.account_alias,
                    broker_order_id=excluded.broker_order_id,
                    state=excluded.state,
                    intent_json=excluded.intent_json,
                    receipt_json=excluded.receipt_json,
                    filled_quantity=excluded.filled_quantity,
                    remaining_quantity=excluded.remaining_quantity,
                    parent_intent_id=excluded.parent_intent_id,
                    replacement_intent_id=excluded.replacement_intent_id,
                    report_ids_json=excluded.report_ids_json,
                    action_receipts_json=excluded.action_receipts_json,
                    human_approval_receipt_json=excluded.human_approval_receipt_json,
                    updated_at=excluded.updated_at
                """,
                payload,
            )

    def entries(self) -> list[DurableOrderEntry]:
        with self._connect() as connection:
            rows = connection.execute("select * from broker_oms_orders order by updated_at, intent_id").fetchall()
        return [self._entry(row) for row in rows]

    def by_intent_id(self, intent_id: str) -> DurableOrderEntry | None:
        """Load one order from the durable OMS authority."""

        with self._connect() as connection:
            row = connection.execute(
                "select * from broker_oms_orders where intent_id=?",
                (intent_id,),
            ).fetchone()
        return self._entry(row) if row is not None else None

    def by_idempotency_key(self, idempotency_key: str) -> DurableOrderEntry | None:
        """Resolve idempotency against durable state, including other processes."""

        with self._connect() as connection:
            row = connection.execute(
                "select * from broker_oms_orders where idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._entry(row) if row is not None else None

    def by_broker_order(self, broker_id: str, broker_order_id: str) -> DurableOrderEntry | None:
        """Resolve a broker order without consulting an in-memory index."""

        with self._connect() as connection:
            row = connection.execute(
                """
                select * from broker_oms_orders
                 where broker_id=? and broker_order_id=?
                """,
                (broker_id, broker_order_id),
            ).fetchone()
        return self._entry(row) if row is not None else None

    def by_report_id(self, report_id: str) -> DurableOrderEntry | None:
        """Resolve a retained report binding from the durable order ledger."""

        with self._connect() as connection:
            rows = connection.execute(
                "select * from broker_oms_orders order by intent_id"
            ).fetchall()
        for row in rows:
            if report_id in (_load(row["report_ids_json"]) or []):
                return self._entry(row)
        return None

    def mark_open_orders_unknown(self) -> list[str]:
        """Persist a restart barrier before any order can be retried."""

        receipt = self.recover_open_orders()
        return list(receipt.marked_unknown_intent_ids) if receipt else []

    def recover_open_orders(self) -> BrokerOMSRecoveryReceipt | None:
        """Atomically persist restart evidence before blocking every open intent.

        The receipt captures the exact pre-recovery state and binds it to the
        resulting UNKNOWN barrier.  This provides a crash/restart audit trail
        even when no broker adapter is available to reconcile immediately.
        """

        placeholders = ",".join("?" for _ in _TERMINAL_STATES)
        terminal = tuple(item.value for item in _TERMINAL_STATES)
        recovered_at = datetime.now(timezone.utc)
        now = _timestamp(recovered_at)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                select intent_id, state, updated_at
                  from broker_oms_orders
                 where state not in ({placeholders})
                 order by intent_id
                """,
                terminal,
            ).fetchall()
            open_orders = tuple(
                {
                    "intent_id": str(row["intent_id"]),
                    "state": str(row["state"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in rows
            )
            if not open_orders:
                return None
            marked_unknown = tuple(item["intent_id"] for item in open_orders)
            connection.execute(
                f"update broker_oms_orders set state=?, updated_at=? where state not in ({placeholders})",
                (BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED.value, now, *terminal),
            )
            receipt_payload = {
                "schema_version": "stock_ai.broker_oms_recovery_receipt.v1",
                "recovered_at": now,
                "open_orders_before_recovery": open_orders,
                "marked_unknown_intent_ids": marked_unknown,
            }
            receipt_sha256 = sha256(_json(receipt_payload).encode("utf-8")).hexdigest()
            receipt_id = f"BORR-{uuid4().hex}"
            connection.execute(
                """
                insert into broker_oms_recovery_receipts (
                    receipt_id, recovered_at, open_orders_json,
                    marked_unknown_intent_ids_json, receipt_sha256
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    now,
                    _json(list(open_orders)),
                    _json(list(marked_unknown)),
                    receipt_sha256,
                ),
            )
        return BrokerOMSRecoveryReceipt(
            receipt_id=receipt_id,
            recovered_at=recovered_at,
            open_orders_before_recovery=open_orders,
            marked_unknown_intent_ids=marked_unknown,
            receipt_sha256=receipt_sha256,
        )

    def recovery_receipts(self) -> list[BrokerOMSRecoveryReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select * from broker_oms_recovery_receipts
                 order by recovered_at, receipt_id
                """
            ).fetchall()
        return [self._recovery_receipt(row) for row in rows]

    def record_pretrade_risk_receipt(
        self,
        intent_id: str,
        receipt: BrokerPreTradeRiskReceipt,
    ) -> DurablePreTradeRiskReceipt:
        """Persist the verified pre-trade evidence once, before broker I/O."""

        persisted_at = datetime.now(timezone.utc)
        payload = _json(receipt.model_dump(mode="json"))
        with self._connect() as connection:
            existing = connection.execute(
                "select * from broker_pretrade_risk_receipts where intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["receipt_id"]) != receipt.receipt_id
                    or str(existing["receipt_sha256"]) != receipt.receipt_sha256
                    or str(existing["receipt_json"]) != payload
                ):
                    raise ValueError("a live intent cannot replace its pre-trade risk receipt")
                return self._pretrade_risk_receipt(existing)
            connection.execute(
                """
                insert into broker_pretrade_risk_receipts (
                    intent_id, receipt_id, receipt_sha256, receipt_json, persisted_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    receipt.receipt_id,
                    receipt.receipt_sha256,
                    payload,
                    _timestamp(persisted_at),
                ),
            )
        return DurablePreTradeRiskReceipt(
            intent_id=intent_id, receipt=receipt, persisted_at=persisted_at
        )

    def pretrade_risk_receipts(self) -> list[DurablePreTradeRiskReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "select * from broker_pretrade_risk_receipts order by persisted_at, intent_id"
            ).fetchall()
        return [self._pretrade_risk_receipt(row) for row in rows]

    def restricted_live_daily_notional(
        self,
        receipt: BrokerRestrictedLiveActivationReceipt,
        *,
        observed_at: datetime | None = None,
    ) -> Decimal:
        date_key = _utc_date(observed_at or datetime.now(timezone.utc))
        with self._connect() as connection:
            rows = connection.execute(
                """
                select notional
                  from broker_restricted_live_activation_uses
                 where receipt_id=? and trading_date=?
                """,
                (receipt.receipt_id, date_key),
            ).fetchall()
        return sum((Decimal(str(row["notional"])) for row in rows), Decimal("0"))

    def record_restricted_live_activation_use(
        self,
        intent_id: str,
        receipt: BrokerRestrictedLiveActivationReceipt,
        *,
        notional: Decimal,
        observed_at: datetime | None = None,
    ) -> DurableRestrictedLiveActivationUse:
        """Persist Host-verified activation and order-use evidence before I/O."""

        if notional <= 0:
            raise ValueError("restricted-live activation use requires positive notional")
        persisted_at = observed_at or datetime.now(timezone.utc)
        payload = _json(receipt.model_dump(mode="json"))
        receipt_sha256 = sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            existing_receipt = connection.execute(
                "select receipt_json, receipt_sha256 from broker_restricted_live_activation_receipts where receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing_receipt is None:
                connection.execute(
                    """
                    insert into broker_restricted_live_activation_receipts (
                        receipt_id, broker_id, account_alias, receipt_sha256, receipt_json, persisted_at
                    ) values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        receipt.broker_id,
                        receipt.account_alias,
                        receipt_sha256,
                        payload,
                        _timestamp(persisted_at),
                    ),
                )
            elif (
                str(existing_receipt["receipt_json"]) != payload
                or str(existing_receipt["receipt_sha256"]) != receipt_sha256
            ):
                raise ValueError("restricted-live activation receipt ID is bound to different evidence")
            existing_use = connection.execute(
                "select * from broker_restricted_live_activation_uses where intent_id=?", (intent_id,)
            ).fetchone()
            if existing_use is not None:
                if (
                    str(existing_use["receipt_id"]) != receipt.receipt_id
                    or Decimal(str(existing_use["notional"])) != notional
                ):
                    raise ValueError("live intent cannot replace its activation evidence")
                return self._restricted_live_activation_use(existing_use, receipt)
            connection.execute(
                """
                insert into broker_restricted_live_activation_uses (
                    intent_id, receipt_id, trading_date, notional, persisted_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    receipt.receipt_id,
                    _utc_date(persisted_at),
                    str(notional),
                    _timestamp(persisted_at),
                ),
            )
        return DurableRestrictedLiveActivationUse(
            intent_id=intent_id, receipt=receipt, notional=notional, persisted_at=_utc(persisted_at)
        )

    def restricted_live_activation_uses(self) -> list[DurableRestrictedLiveActivationUse]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select uses.*, receipts.receipt_json
                  from broker_restricted_live_activation_uses as uses
                  join broker_restricted_live_activation_receipts as receipts
                    on receipts.receipt_id=uses.receipt_id
                 order by uses.persisted_at, uses.intent_id
                """
            ).fetchall()
        return [
            self._restricted_live_activation_use(
                row,
                BrokerRestrictedLiveActivationReceipt.model_validate(_load(row["receipt_json"])),
            )
            for row in rows
        ]

    @staticmethod
    def _entry(row: sqlite3.Row) -> DurableOrderEntry:
        receipt_payload = _load(row["receipt_json"])
        return DurableOrderEntry(
            intent=BrokerOrderIntent.model_validate(_load(row["intent_json"])),
            state=BrokerOrderState(str(row["state"])),
            receipt=BrokerOrderReceipt.model_validate(receipt_payload) if receipt_payload else None,
            broker_order_id=str(row["broker_order_id"]) if row["broker_order_id"] else None,
            filled_quantity=Decimal(str(row["filled_quantity"])),
            remaining_quantity=Decimal(str(row["remaining_quantity"])),
            parent_intent_id=str(row["parent_intent_id"]) if row["parent_intent_id"] else None,
            replacement_intent_id=str(row["replacement_intent_id"]) if row["replacement_intent_id"] else None,
            report_ids=tuple(str(item) for item in _load(row["report_ids_json"]) or []),
            action_receipts=tuple(BrokerOrderReceipt.model_validate(item) for item in _load(row["action_receipts_json"]) or []),
            human_approval_receipt=(
                BrokerHumanApprovalReceipt.model_validate(
                    _load(row["human_approval_receipt_json"])
                )
                if row["human_approval_receipt_json"]
                else None
            ),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _recovery_receipt(row: sqlite3.Row) -> BrokerOMSRecoveryReceipt:
        return BrokerOMSRecoveryReceipt(
            receipt_id=str(row["receipt_id"]),
            recovered_at=datetime.fromisoformat(str(row["recovered_at"])),
            open_orders_before_recovery=tuple(
                {
                    "intent_id": str(item["intent_id"]),
                    "state": str(item["state"]),
                    "updated_at": str(item["updated_at"]),
                }
                for item in _load(row["open_orders_json"]) or []
            ),
            marked_unknown_intent_ids=tuple(
                str(item)
                for item in _load(row["marked_unknown_intent_ids_json"]) or []
            ),
            receipt_sha256=str(row["receipt_sha256"]),
        )

    @staticmethod
    def _pretrade_risk_receipt(row: sqlite3.Row) -> DurablePreTradeRiskReceipt:
        return DurablePreTradeRiskReceipt(
            intent_id=str(row["intent_id"]),
            receipt=BrokerPreTradeRiskReceipt.model_validate(_load(row["receipt_json"])),
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        )

    @staticmethod
    def _restricted_live_activation_use(
        row: sqlite3.Row,
        receipt: BrokerRestrictedLiveActivationReceipt,
    ) -> DurableRestrictedLiveActivationUse:
        return DurableRestrictedLiveActivationUse(
            intent_id=str(row["intent_id"]),
            receipt=receipt,
            notional=Decimal(str(row["notional"])),
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str | None) -> Any:
    return json.loads(value) if value else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _utc_date(value: datetime) -> str:
    return _utc(value).date().isoformat()
