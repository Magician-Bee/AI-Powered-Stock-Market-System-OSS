from __future__ import annotations

"""Immutable evidence required before a broker adapter may claim SDK readiness.

An adapter name, a documentation URL, or a locally installed package is not a
broker integration.  The account owner must first attest an official SDK
artifact and then preserve a read-only probe receipt from an isolated worker.
This ledger deliberately stores hashes and masked account aliases only; it
never stores a certificate, API token, raw account number, or SDK binary.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from .contracts import BrokerId
from .provisioning import BrokerSdkArtifactReceipt


_SCHEMA = "stock_ai.broker_integration_receipt.v1"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value: dict[str, Any]) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(value: str, *, name: str) -> str:
    normalized = str(value).lower().strip()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return normalized


@dataclass(frozen=True, slots=True)
class BrokerIntegrationReceipt:
    """A completed official-SDK, isolated read-only probe evidence record."""

    receipt_id: str
    broker_id: BrokerId
    account_alias_masked: str
    official_sdk_url: str
    sdk_version: str
    sdk_artifact_sha256: str
    official_checksum_sha256: str
    worker_release_sha256: str
    readonly_probe_sha256: str
    environment: Literal["sandbox", "production_readonly"]
    account_owner_confirmed: bool
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        broker_id: BrokerId,
        account_alias_masked: str,
        official_sdk_url: str,
        sdk_version: str,
        sdk_artifact_sha256: str,
        official_checksum_sha256: str,
        worker_release_sha256: str,
        readonly_probe_sha256: str,
        environment: Literal["sandbox", "production_readonly"],
        account_owner_confirmed: bool,
        observed_at: datetime | None = None,
    ) -> "BrokerIntegrationReceipt":
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            broker_id=broker_id,
            account_alias_masked=str(account_alias_masked).strip(),
            official_sdk_url=str(official_sdk_url).strip(),
            sdk_version=str(sdk_version).strip(),
            sdk_artifact_sha256=_sha256(sdk_artifact_sha256, name="sdk_artifact_sha256"),
            official_checksum_sha256=_sha256(official_checksum_sha256, name="official_checksum_sha256"),
            worker_release_sha256=_sha256(worker_release_sha256, name="worker_release_sha256"),
            readonly_probe_sha256=_sha256(readonly_probe_sha256, name="readonly_probe_sha256"),
            environment=environment,
            account_owner_confirmed=bool(account_owner_confirmed),
            observed_at=_utc(observed_at or datetime.now(timezone.utc)),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_hash(receipt.payload()))
        receipt.verify()
        return receipt

    @classmethod
    def from_sdk_artifact(
        cls,
        artifact: BrokerSdkArtifactReceipt,
        *,
        receipt_id: str,
        account_alias_masked: str,
        worker_release_sha256: str,
        readonly_probe_sha256: str,
        environment: Literal["sandbox", "production_readonly"],
        observed_at: datetime | None = None,
    ) -> "BrokerIntegrationReceipt":
        """Bind a worker probe to a checksum-verified official SDK receipt."""

        if not artifact.account_owner_confirmed_official_download:
            raise ValueError("official SDK artifact lacks account-owner confirmation")
        if not artifact.official_checksum_verified or not artifact.install_allowed:
            raise ValueError("official SDK artifact checksum is not verified for installation")
        return cls.issue(
            receipt_id=receipt_id,
            broker_id=artifact.broker_id,
            account_alias_masked=account_alias_masked,
            official_sdk_url=artifact.source_url,
            sdk_version=artifact.sdk_version,
            sdk_artifact_sha256=artifact.artifact_sha256,
            official_checksum_sha256=artifact.artifact_sha256,
            worker_release_sha256=worker_release_sha256,
            readonly_probe_sha256=readonly_probe_sha256,
            environment=environment,
            account_owner_confirmed=True,
            observed_at=observed_at,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "receipt_id": self.receipt_id,
            "broker_id": self.broker_id,
            "account_alias_masked": self.account_alias_masked,
            "official_sdk_url": self.official_sdk_url,
            "sdk_version": self.sdk_version,
            "sdk_artifact_sha256": self.sdk_artifact_sha256,
            "official_checksum_sha256": self.official_checksum_sha256,
            "worker_release_sha256": self.worker_release_sha256,
            "readonly_probe_sha256": self.readonly_probe_sha256,
            "environment": self.environment,
            "account_owner_confirmed": self.account_owner_confirmed,
            "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "BrokerIntegrationReceipt":
        if payload.get("schema_version") != _SCHEMA:
            raise ValueError("invalid broker integration receipt schema")
        receipt = cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            broker_id=str(payload.get("broker_id") or ""),  # type: ignore[arg-type]
            account_alias_masked=str(payload.get("account_alias_masked") or ""),
            official_sdk_url=str(payload.get("official_sdk_url") or ""),
            sdk_version=str(payload.get("sdk_version") or ""),
            sdk_artifact_sha256=str(payload.get("sdk_artifact_sha256") or ""),
            official_checksum_sha256=str(payload.get("official_checksum_sha256") or ""),
            worker_release_sha256=str(payload.get("worker_release_sha256") or ""),
            readonly_probe_sha256=str(payload.get("readonly_probe_sha256") or ""),
            environment=str(payload.get("environment") or ""),  # type: ignore[arg-type]
            account_owner_confirmed=bool(payload.get("account_owner_confirmed")),
            observed_at=_utc(datetime.fromisoformat(str(payload.get("observed_at") or ""))),
            receipt_sha256=str(payload.get("receipt_sha256") or ""),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.account_alias_masked or not self.sdk_version:
            raise ValueError("broker integration receipt requires identifiers and SDK version")
        if not self.official_sdk_url.startswith("https://"):
            raise ValueError("broker integration receipt requires an HTTPS official SDK URL")
        if not any(mark in self.account_alias_masked for mark in ("*", "•", "x", "X")):
            raise ValueError("broker integration receipt account alias must be masked")
        for field_name in (
            "sdk_artifact_sha256",
            "official_checksum_sha256",
            "worker_release_sha256",
            "readonly_probe_sha256",
            "receipt_sha256",
        ):
            _sha256(getattr(self, field_name), name=field_name)
        if self.sdk_artifact_sha256 != self.official_checksum_sha256:
            raise ValueError("official SDK checksum must match the installed artifact")
        if not self.account_owner_confirmed:
            raise ValueError("broker integration receipt requires account-owner confirmation")
        if self.environment not in {"sandbox", "production_readonly"}:
            raise ValueError("broker integration receipt environment is invalid")
        if self.receipt_sha256 != _hash(self.payload()):
            raise ValueError("broker integration receipt hash mismatch")


@dataclass(frozen=True, slots=True)
class DurableBrokerIntegrationReceipt:
    receipt: BrokerIntegrationReceipt
    persisted_at: datetime
    persistence_sha256: str

    @classmethod
    def issue(
        cls, receipt: BrokerIntegrationReceipt, *, persisted_at: datetime
    ) -> "DurableBrokerIntegrationReceipt":
        receipt.verify()
        durable = cls(receipt=receipt, persisted_at=_utc(persisted_at), persistence_sha256="")
        durable = replace(durable, persistence_sha256=_hash(durable.payload()))
        durable.verify()
        return durable

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "stock_ai.durable_broker_integration_receipt.v1",
            "receipt_id": self.receipt.receipt_id,
            "receipt_sha256": self.receipt.receipt_sha256,
            "persisted_at": _utc(self.persisted_at).isoformat(),
        }

    def verify(self) -> None:
        self.receipt.verify()
        if self.persistence_sha256 != _hash(self.payload()):
            raise ValueError("durable broker integration receipt hash mismatch")


class BrokerIntegrationReceiptStore:
    """SQLite WAL append-only ledger for real official-SDK probe evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                create table if not exists broker_integration_receipts (
                    receipt_id text primary key,
                    broker_id text not null,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null,
                    persistence_sha256 text not null unique
                )
                """
            )
            for action in ("update", "delete"):
                connection.execute(
                    f"""
                    create trigger if not exists broker_integration_receipts_immutable_{action}
                    before {action} on broker_integration_receipts
                    begin select raise(abort, 'broker integration evidence is immutable'); end
                    """
                )

    def record(self, receipt: BrokerIntegrationReceipt) -> DurableBrokerIntegrationReceipt:
        receipt.verify()
        durable = DurableBrokerIntegrationReceipt.issue(
            receipt, persisted_at=datetime.now(timezone.utc)
        )
        with self._connect() as connection:
            existing = connection.execute(
                "select receipt_json, persisted_at, persistence_sha256 from broker_integration_receipts where receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                stored = BrokerIntegrationReceipt.model_validate(json.loads(existing["receipt_json"]))
                result = DurableBrokerIntegrationReceipt(
                    receipt=stored,
                    persisted_at=_utc(datetime.fromisoformat(existing["persisted_at"])),
                    persistence_sha256=str(existing["persistence_sha256"]),
                )
                result.verify()
                if result.receipt.receipt_sha256 != receipt.receipt_sha256:
                    raise ValueError("receipt_id already belongs to different immutable evidence")
                return result
            connection.execute(
                "insert into broker_integration_receipts values (?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.broker_id,
                    receipt.receipt_sha256,
                    json.dumps(receipt.model_dump(), ensure_ascii=False, sort_keys=True),
                    durable.persisted_at.isoformat(),
                    durable.persistence_sha256,
                ),
            )
        return durable

    def receipts(self, broker_id: BrokerId | None = None) -> list[DurableBrokerIntegrationReceipt]:
        query = "select receipt_json, persisted_at, persistence_sha256 from broker_integration_receipts"
        parameters: tuple[str, ...] = ()
        if broker_id:
            query += " where broker_id = ?"
            parameters = (broker_id,)
        query += " order by persisted_at, receipt_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = [
            DurableBrokerIntegrationReceipt(
                receipt=BrokerIntegrationReceipt.model_validate(json.loads(row["receipt_json"])),
                persisted_at=_utc(datetime.fromisoformat(row["persisted_at"])),
                persistence_sha256=str(row["persistence_sha256"]),
            )
            for row in rows
        ]
        for item in result:
            item.verify()
        return result

    def readiness(self, broker_id: BrokerId) -> dict[str, Any]:
        receipts = self.receipts(broker_id)
        latest = receipts[-1] if receipts else None
        return {
            "schema_version": "stock_ai.broker_integration_readiness.v1",
            "broker_id": broker_id,
            "official_sdk_verified": latest is not None,
            "readonly_probe_recorded": latest is not None,
            "environment": latest.receipt.environment if latest else None,
            "latest_receipt_id": latest.receipt.receipt_id if latest else None,
            "latest_receipt_sha256": latest.receipt.receipt_sha256 if latest else None,
            "adapter_claim": "evidence_recorded_not_live_session" if latest else "not_verified",
        }
