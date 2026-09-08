from __future__ import annotations

"""Restart-safe, evidence-bound broker session lifecycle ledger.

This is intentionally not a credential store or a fake SDK session.  It
records only hashes emitted after an account owner has actually driven an
official SDK worker through certificate, read-only login, refresh, reconnect,
snapshot recovery and logout.  Invalid ordering fails closed.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from .contracts import BrokerId
from .integration_receipts import BrokerIntegrationReceiptStore


_SCHEMA = "stock_ai.broker_session_lifecycle_receipt.v1"
SessionEvent = Literal[
    "certificate_verified",
    "login_readonly",
    "session_refreshed",
    "reconnect_transport",
    "reconnect_snapshot_recovered",
    "logout",
]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value: dict[str, Any]) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(value: str, *, name: str) -> str:
    value = str(value).lower().strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class BrokerSessionLifecycleReceipt:
    receipt_id: str
    broker_id: BrokerId
    session_id: str
    event: SessionEvent
    integration_receipt_sha256: str
    session_reference_sha256: str
    external_evidence_sha256: str
    account_owner_confirmed: bool
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        broker_id: BrokerId,
        session_id: str,
        event: SessionEvent,
        integration_receipt_sha256: str,
        session_reference_sha256: str,
        external_evidence_sha256: str,
        account_owner_confirmed: bool,
        observed_at: datetime | None = None,
    ) -> "BrokerSessionLifecycleReceipt":
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            broker_id=broker_id,
            session_id=str(session_id).strip(),
            event=event,
            integration_receipt_sha256=_digest(integration_receipt_sha256, name="integration_receipt_sha256"),
            session_reference_sha256=_digest(session_reference_sha256, name="session_reference_sha256"),
            external_evidence_sha256=_digest(external_evidence_sha256, name="external_evidence_sha256"),
            account_owner_confirmed=bool(account_owner_confirmed),
            observed_at=_utc(observed_at or datetime.now(timezone.utc)),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_hash(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "receipt_id": self.receipt_id,
            "broker_id": self.broker_id,
            "session_id": self.session_id,
            "event": self.event,
            "integration_receipt_sha256": self.integration_receipt_sha256,
            "session_reference_sha256": self.session_reference_sha256,
            "external_evidence_sha256": self.external_evidence_sha256,
            "account_owner_confirmed": self.account_owner_confirmed,
            "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "BrokerSessionLifecycleReceipt":
        if payload.get("schema_version") != _SCHEMA:
            raise ValueError("invalid broker session lifecycle receipt schema")
        receipt = cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            broker_id=str(payload.get("broker_id") or ""),  # type: ignore[arg-type]
            session_id=str(payload.get("session_id") or ""),
            event=str(payload.get("event") or ""),  # type: ignore[arg-type]
            integration_receipt_sha256=str(payload.get("integration_receipt_sha256") or ""),
            session_reference_sha256=str(payload.get("session_reference_sha256") or ""),
            external_evidence_sha256=str(payload.get("external_evidence_sha256") or ""),
            account_owner_confirmed=bool(payload.get("account_owner_confirmed")),
            observed_at=_utc(datetime.fromisoformat(str(payload.get("observed_at") or ""))),
            receipt_sha256=str(payload.get("receipt_sha256") or ""),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.session_id:
            raise ValueError("session lifecycle receipt requires receipt and session IDs")
        if self.event not in {
            "certificate_verified", "login_readonly", "session_refreshed",
            "reconnect_transport", "reconnect_snapshot_recovered", "logout",
        }:
            raise ValueError("invalid broker session lifecycle event")
        for field in (
            "integration_receipt_sha256", "session_reference_sha256",
            "external_evidence_sha256", "receipt_sha256",
        ):
            _digest(getattr(self, field), name=field)
        if not self.account_owner_confirmed:
            raise ValueError("session lifecycle receipt requires account-owner confirmation")
        if self.receipt_sha256 != _hash(self.payload()):
            raise ValueError("broker session lifecycle receipt hash mismatch")


class BrokerSessionLifecycleStore:
    """Immutable ordered lifecycle evidence, optionally linked to E-006 receipts."""

    def __init__(
        self,
        path: str | Path,
        *,
        integration_store: BrokerIntegrationReceiptStore | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.integration_store = integration_store
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        connection.execute("pragma synchronous = full")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute(
                """
                create table if not exists broker_session_lifecycle_receipts (
                    receipt_id text primary key,
                    broker_id text not null,
                    session_id text not null,
                    event text not null,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null,
                    unique (session_id, event, receipt_sha256)
                )
                """
            )
            for action in ("update", "delete"):
                connection.execute(
                    f"""
                    create trigger if not exists broker_session_lifecycle_immutable_{action}
                    before {action} on broker_session_lifecycle_receipts
                    begin select raise(abort, 'broker session lifecycle evidence is immutable'); end
                    """
                )

    def _records(self, broker_id: BrokerId, session_id: str) -> list[BrokerSessionLifecycleReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "select receipt_json from broker_session_lifecycle_receipts where broker_id = ? and session_id = ? order by persisted_at, receipt_id",
                (broker_id, session_id),
            ).fetchall()
        return [BrokerSessionLifecycleReceipt.model_validate(json.loads(row["receipt_json"])) for row in rows]

    def _verify_integration_link(self, receipt: BrokerSessionLifecycleReceipt) -> None:
        if self.integration_store is None:
            return
        valid = {
            durable.receipt.receipt_sha256
            for durable in self.integration_store.receipts(receipt.broker_id)
        }
        if receipt.integration_receipt_sha256 not in valid:
            raise ValueError("session lifecycle receipt is not linked to durable SDK integration evidence")

    @staticmethod
    def _validate_transition(previous: SessionEvent | None, current: SessionEvent) -> None:
        allowed = {
            None: {"certificate_verified"},
            "certificate_verified": {"login_readonly"},
            "login_readonly": {"session_refreshed", "reconnect_transport", "logout"},
            "session_refreshed": {"session_refreshed", "reconnect_transport", "logout"},
            "reconnect_transport": {"reconnect_snapshot_recovered", "logout"},
            "reconnect_snapshot_recovered": {"session_refreshed", "reconnect_transport", "logout"},
            "logout": set(),
        }
        if current not in allowed[previous]:
            raise ValueError(f"invalid broker session transition: {previous or 'new'} -> {current}")

    def record(self, receipt: BrokerSessionLifecycleReceipt) -> BrokerSessionLifecycleReceipt:
        receipt.verify()
        self._verify_integration_link(receipt)
        previous = self._records(receipt.broker_id, receipt.session_id)
        self._validate_transition(previous[-1].event if previous else None, receipt.event)
        with self._connect() as connection:
            connection.execute(
                "insert into broker_session_lifecycle_receipts values (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id, receipt.broker_id, receipt.session_id, receipt.event,
                    receipt.receipt_sha256, json.dumps(receipt.model_dump(), sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return receipt

    def status(self, broker_id: BrokerId, session_id: str) -> dict[str, Any]:
        records = self._records(broker_id, session_id)
        current = records[-1].event if records else None
        return {
            "schema_version": "stock_ai.broker_session_lifecycle_status.v1",
            "broker_id": broker_id,
            "session_id": session_id,
            "events": [record.event for record in records],
            "state": "logged_out" if current == "logout" else "not_started" if current is None else "active",
            "requires_snapshot_recovery": current == "reconnect_transport",
            "external_session_proven": current in {"login_readonly", "session_refreshed", "reconnect_snapshot_recovered", "logout"},
        }
