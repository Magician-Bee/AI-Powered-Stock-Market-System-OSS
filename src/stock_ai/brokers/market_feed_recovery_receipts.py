from __future__ import annotations

"""Durable market-feed ordering, recovery and failover evidence.

The live tracker deliberately stays memory-only so it can make a prompt safety
decision.  This store is the separate restart-safe audit trail: it keeps only
event identities, sequence boundaries and hashes, never raw quote payloads.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from .contracts import BrokerId
from .feed_quality import BrokerSourceSwitchRecord
from .integration_receipts import BrokerIntegrationReceiptStore
from .sequence_tracker import BrokerSequenceObservation


_SCHEMA = "stock_ai.market_feed_recovery_receipt.v1"
_SEQUENCE_KINDS = {"initial", "contiguous", "gap", "duplicate", "out_of_order", "unsequenced"}


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
class BrokerMarketFeedRecoveryReceipt:
    receipt_id: str
    broker_id: BrokerId
    instrument_id: str
    market_session: str
    channel: str
    kind: Literal["initial", "contiguous", "gap", "duplicate", "out_of_order", "unsequenced", "snapshot_recovered", "failover_selected"]
    integration_receipt_sha256: str
    observation_sha256: str | None
    observed_sequence: int | None
    missing_from: int | None
    missing_to: int | None
    related_receipt_sha256: str | None
    source_switch_sha256: str | None
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def from_sequence_observation(
        cls,
        *,
        receipt_id: str,
        observation: BrokerSequenceObservation,
        integration_receipt_sha256: str,
    ) -> "BrokerMarketFeedRecoveryReceipt":
        return cls._issue(
            receipt_id=receipt_id,
            broker_id=observation.broker_id,
            instrument_id=observation.instrument_id,
            market_session=observation.market_session,
            channel=observation.channel,
            kind=observation.classification,
            integration_receipt_sha256=integration_receipt_sha256,
            observation_sha256=_hash(observation.model_dump(mode="json")),
            observed_sequence=observation.observed_sequence,
            missing_from=observation.missing_from,
            missing_to=observation.missing_to,
            observed_at=observation.observed_at,
        )

    @classmethod
    def snapshot_recovered(
        cls,
        *,
        receipt_id: str,
        gap_receipt: "BrokerMarketFeedRecoveryReceipt",
        snapshot_sequence: int,
        observed_at: datetime | None = None,
    ) -> "BrokerMarketFeedRecoveryReceipt":
        if gap_receipt.kind != "gap":
            raise ValueError("snapshot recovery must reference a gap receipt")
        if gap_receipt.observed_sequence is not None and snapshot_sequence < gap_receipt.observed_sequence:
            raise ValueError("recovery snapshot cannot move sequence state backwards")
        return cls._issue(
            receipt_id=receipt_id,
            broker_id=gap_receipt.broker_id,
            instrument_id=gap_receipt.instrument_id,
            market_session=gap_receipt.market_session,
            channel=gap_receipt.channel,
            kind="snapshot_recovered",
            integration_receipt_sha256=gap_receipt.integration_receipt_sha256,
            observed_sequence=snapshot_sequence,
            related_receipt_sha256=gap_receipt.receipt_sha256,
            observed_at=observed_at or datetime.now(timezone.utc),
        )

    @classmethod
    def failover_selected(
        cls,
        *,
        receipt_id: str,
        switch: BrokerSourceSwitchRecord,
        integration_receipt_sha256: str,
        channel: str,
    ) -> "BrokerMarketFeedRecoveryReceipt":
        if switch.selected_broker_id is None or not switch.selected_event_id:
            raise ValueError("failover receipt requires a selected broker event")
        return cls._issue(
            receipt_id=receipt_id,
            broker_id=switch.selected_broker_id,
            instrument_id=switch.instrument_id,
            market_session=switch.market_session,
            channel=channel,
            kind="failover_selected",
            integration_receipt_sha256=integration_receipt_sha256,
            source_switch_sha256=_hash(switch.model_dump(mode="json")),
            observed_at=switch.switched_at,
        )

    @classmethod
    def _issue(cls, **values: Any) -> "BrokerMarketFeedRecoveryReceipt":
        receipt = cls(
            receipt_id=str(values["receipt_id"]).strip(),
            broker_id=values["broker_id"],
            instrument_id=str(values["instrument_id"]),
            market_session=str(values["market_session"]),
            channel=str(values["channel"]),
            kind=values["kind"],
            integration_receipt_sha256=_digest(values["integration_receipt_sha256"], "integration_receipt_sha256"),
            observation_sha256=values.get("observation_sha256"),
            observed_sequence=values.get("observed_sequence"),
            missing_from=values.get("missing_from"),
            missing_to=values.get("missing_to"),
            related_receipt_sha256=values.get("related_receipt_sha256"),
            source_switch_sha256=values.get("source_switch_sha256"),
            observed_at=_utc(values["observed_at"]),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_hash(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA, "receipt_id": self.receipt_id, "broker_id": self.broker_id,
            "instrument_id": self.instrument_id, "market_session": self.market_session, "channel": self.channel,
            "kind": self.kind, "integration_receipt_sha256": self.integration_receipt_sha256,
            "observation_sha256": self.observation_sha256, "observed_sequence": self.observed_sequence,
            "missing_from": self.missing_from, "missing_to": self.missing_to,
            "related_receipt_sha256": self.related_receipt_sha256,
            "source_switch_sha256": self.source_switch_sha256,
            "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, value: dict[str, Any]) -> "BrokerMarketFeedRecoveryReceipt":
        if value.get("schema_version") != _SCHEMA:
            raise ValueError("invalid market feed recovery receipt schema")
        return cls._from_dump(value)

    @classmethod
    def _from_dump(cls, value: dict[str, Any]) -> "BrokerMarketFeedRecoveryReceipt":
        receipt = cls(
            receipt_id=str(value.get("receipt_id") or ""), broker_id=str(value.get("broker_id") or ""),
            instrument_id=str(value.get("instrument_id") or ""), market_session=str(value.get("market_session") or ""),
            channel=str(value.get("channel") or ""), kind=str(value.get("kind") or ""),
            integration_receipt_sha256=str(value.get("integration_receipt_sha256") or ""),
            observation_sha256=value.get("observation_sha256"), observed_sequence=value.get("observed_sequence"),
            missing_from=value.get("missing_from"), missing_to=value.get("missing_to"),
            related_receipt_sha256=value.get("related_receipt_sha256"), source_switch_sha256=value.get("source_switch_sha256"),
            observed_at=_utc(datetime.fromisoformat(str(value.get("observed_at") or ""))),
            receipt_sha256=str(value.get("receipt_sha256") or ""),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.instrument_id or not self.market_session or not self.channel:
            raise ValueError("market feed receipt requires a complete stream identity")
        if self.kind not in _SEQUENCE_KINDS | {"snapshot_recovered", "failover_selected"}:
            raise ValueError("invalid market feed receipt kind")
        for field in ("integration_receipt_sha256", "receipt_sha256"):
            _digest(getattr(self, field), field)
        for field in ("observation_sha256", "related_receipt_sha256", "source_switch_sha256"):
            if getattr(self, field) is not None:
                _digest(getattr(self, field), field)
        if self.kind == "gap" and (self.missing_from is None or self.missing_to is None or self.missing_from > self.missing_to):
            raise ValueError("gap receipt requires a valid missing sequence range")
        if self.kind == "snapshot_recovered" and not self.related_receipt_sha256:
            raise ValueError("snapshot recovery requires its gap receipt hash")
        if self.kind == "failover_selected" and not self.source_switch_sha256:
            raise ValueError("failover receipt requires its source-switch hash")
        if self.receipt_sha256 != _hash(self.payload()):
            raise ValueError("market feed receipt hash mismatch")


class BrokerMarketFeedRecoveryReceiptStore:
    """Immutable restart-safe evidence for a single broker market feed."""

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
                create table if not exists broker_market_feed_recovery_receipts (
                    receipt_id text primary key, broker_id text not null, stream_key text not null,
                    receipt_sha256 text not null unique, receipt_json text not null, persisted_at text not null
                )
            """)
            for action in ("update", "delete"):
                connection.execute(f"""
                    create trigger if not exists broker_market_feed_recovery_immutable_{action}
                    before {action} on broker_market_feed_recovery_receipts
                    begin select raise(abort, 'broker market feed recovery evidence is immutable'); end
                """)

    @staticmethod
    def _stream_key(receipt: BrokerMarketFeedRecoveryReceipt) -> str:
        return "|".join((receipt.broker_id, receipt.instrument_id, receipt.market_session, receipt.channel))

    def _assert_integration(self, receipt: BrokerMarketFeedRecoveryReceipt) -> None:
        if self.integration_store is None:
            return
        hashes = {item.receipt.receipt_sha256 for item in self.integration_store.receipts(receipt.broker_id)}
        if receipt.integration_receipt_sha256 not in hashes:
            raise ValueError("market feed receipt is not linked to durable SDK integration evidence")

    def record(self, receipt: BrokerMarketFeedRecoveryReceipt) -> BrokerMarketFeedRecoveryReceipt:
        receipt.verify()
        self._assert_integration(receipt)
        existing = self.receipts()
        if receipt.kind == "snapshot_recovered":
            gap = next((item for item in existing if item.receipt_sha256 == receipt.related_receipt_sha256), None)
            if gap is None or gap.kind != "gap" or self._stream_key(gap) != self._stream_key(receipt):
                raise ValueError("snapshot recovery must link to a durable gap on the same stream")
            if any(item.kind == "snapshot_recovered" and item.related_receipt_sha256 == gap.receipt_sha256 for item in existing):
                raise ValueError("gap has already been recovered")
        with self._connect() as connection:
            connection.execute(
                "insert into broker_market_feed_recovery_receipts values (?, ?, ?, ?, ?, ?)",
                (receipt.receipt_id, receipt.broker_id, self._stream_key(receipt), receipt.receipt_sha256,
                 json.dumps(receipt.model_dump(), sort_keys=True), datetime.now(timezone.utc).isoformat()),
            )
        return receipt

    def receipts(self, broker_id: BrokerId | None = None) -> list[BrokerMarketFeedRecoveryReceipt]:
        query = "select receipt_json from broker_market_feed_recovery_receipts"
        parameters: tuple[str, ...] = ()
        if broker_id:
            query += " where broker_id = ?"; parameters = (broker_id,)
        query += " order by persisted_at, receipt_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [BrokerMarketFeedRecoveryReceipt.model_validate(json.loads(row["receipt_json"])) for row in rows]
