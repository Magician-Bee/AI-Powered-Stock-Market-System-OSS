from __future__ import annotations

"""Immutable evidence for account-specific Taiwan transaction-cost schedules.

The public tax rulebook is useful for all research.  A broker's negotiated
commission and any customer pass-through fee are different: a replay must not
turn a user-provided number plus a friendly schedule ID into broker evidence.
This module records the exact account-bound schedule document hash and its
effective range before it can be used to certify a cost quote.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


_SCHEMA = "open_stock_ai.broker_cost_schedule_receipt.v1"
_DOCUMENT_SCHEMA = "open_stock_ai.cost_schedule_document_receipt.v1"
_REVIEW_SCHEMA = "open_stock_ai.reviewed_broker_cost_schedule_receipt.v1"
_DOCUMENT_KINDS = {"broker_account_schedule", "exchange_fee_schedule"}
_SHA256_HEX = set("0123456789abcdef")


def _require_sha256(value: str, field: str) -> str:
    normalized = str(value or "").lower().strip()
    if len(normalized) != 64 or any(char not in _SHA256_HEX for char in normalized):
        raise ValueError(f"{field} must be a sha256")
    return normalized


@dataclass(frozen=True, slots=True)
class BrokerCostScheduleReceipt:
    receipt_id: str
    broker_id: str
    account_alias: str
    venues: tuple[str, ...]
    product_types: tuple[str, ...]
    lot_types: tuple[str, ...]
    sides: tuple[str, ...]
    valid_from: date
    valid_to: date | None
    broker_commission_bps: float
    broker_minimum_commission_twd: float
    broker_fee_schedule_id: str
    exchange_fee_bps: float
    exchange_fee_schedule_id: str
    source_sha256: str
    source_published_at: datetime
    observed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        broker_id: str,
        account_alias: str,
        venues: tuple[str, ...] | list[str],
        product_types: tuple[str, ...] | list[str],
        lot_types: tuple[str, ...] | list[str],
        sides: tuple[str, ...] | list[str],
        valid_from: date,
        valid_to: date | None,
        broker_commission_bps: float,
        broker_minimum_commission_twd: float,
        broker_fee_schedule_id: str,
        exchange_fee_bps: float,
        exchange_fee_schedule_id: str,
        source_sha256: str,
        source_published_at: datetime,
        observed_at: datetime | None = None,
    ) -> "BrokerCostScheduleReceipt":
        received = _utc(observed_at or datetime.now(timezone.utc))
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            broker_id=str(broker_id).strip(),
            account_alias=str(account_alias).strip(),
            venues=tuple(sorted({str(item).upper().strip() for item in venues})),
            product_types=tuple(sorted({str(item).lower().strip() for item in product_types})),
            lot_types=tuple(sorted({str(item).lower().strip() for item in lot_types})),
            sides=tuple(sorted({str(item).lower().strip() for item in sides})),
            valid_from=valid_from,
            valid_to=valid_to,
            broker_commission_bps=float(broker_commission_bps),
            broker_minimum_commission_twd=float(broker_minimum_commission_twd),
            broker_fee_schedule_id=str(broker_fee_schedule_id).strip(),
            exchange_fee_bps=float(exchange_fee_bps),
            exchange_fee_schedule_id=str(exchange_fee_schedule_id).strip(),
            source_sha256=str(source_sha256).lower().strip(),
            source_published_at=_utc(source_published_at),
            observed_at=received,
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_sha(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "receipt_id": self.receipt_id,
            "broker_id": self.broker_id,
            "account_alias": self.account_alias,
            "venues": list(self.venues),
            "product_types": list(self.product_types),
            "lot_types": list(self.lot_types),
            "sides": list(self.sides),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "broker_commission_bps": self.broker_commission_bps,
            "broker_minimum_commission_twd": self.broker_minimum_commission_twd,
            "broker_fee_schedule_id": self.broker_fee_schedule_id,
            "exchange_fee_bps": self.exchange_fee_bps,
            "exchange_fee_schedule_id": self.exchange_fee_schedule_id,
            "source_sha256": self.source_sha256,
            "source_published_at": _utc(self.source_published_at).isoformat(),
            "observed_at": _utc(self.observed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "BrokerCostScheduleReceipt":
        if payload.get("schema_version") != _SCHEMA:
            raise ValueError("invalid broker cost schedule receipt schema")
        receipt = cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            broker_id=str(payload.get("broker_id") or ""),
            account_alias=str(payload.get("account_alias") or ""),
            venues=tuple(str(item).upper() for item in payload.get("venues") or ()),
            product_types=tuple(str(item).lower() for item in payload.get("product_types") or ()),
            lot_types=tuple(str(item).lower() for item in payload.get("lot_types") or ()),
            sides=tuple(str(item).lower() for item in payload.get("sides") or ()),
            valid_from=date.fromisoformat(str(payload.get("valid_from"))),
            valid_to=(date.fromisoformat(str(payload["valid_to"])) if payload.get("valid_to") else None),
            broker_commission_bps=float(payload.get("broker_commission_bps")),
            broker_minimum_commission_twd=float(payload.get("broker_minimum_commission_twd")),
            broker_fee_schedule_id=str(payload.get("broker_fee_schedule_id") or ""),
            exchange_fee_bps=float(payload.get("exchange_fee_bps")),
            exchange_fee_schedule_id=str(payload.get("exchange_fee_schedule_id") or ""),
            source_sha256=str(payload.get("source_sha256") or "").lower(),
            source_published_at=_utc(datetime.fromisoformat(str(payload.get("source_published_at")))),
            observed_at=_utc(datetime.fromisoformat(str(payload.get("observed_at")))),
            receipt_sha256=str(payload.get("receipt_sha256") or "").lower(),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.broker_id or not self.account_alias:
            raise ValueError("cost schedule receipt requires receipt, broker and account IDs")
        if not all((self.venues, self.product_types, self.lot_types, self.sides)):
            raise ValueError("cost schedule receipt requires non-empty scope")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("cost schedule receipt effective range is invalid")
        if any(value < 0 for value in (self.broker_commission_bps, self.broker_minimum_commission_twd, self.exchange_fee_bps)):
            raise ValueError("cost schedule receipt fees must be non-negative")
        if not self.broker_fee_schedule_id or not self.exchange_fee_schedule_id:
            raise ValueError("cost schedule receipt requires broker and exchange schedule IDs")
        if len(self.source_sha256) != 64 or len(self.receipt_sha256) != 64:
            raise ValueError("cost schedule receipt hashes must be sha256")
        if self.source_sha256 != self.source_sha256.lower() or self.receipt_sha256 != _sha(self.payload()):
            raise ValueError("cost schedule receipt hash mismatch")

    def matches(
        self,
        *,
        broker_id: str,
        account_alias: str,
        venue: str,
        product_type: str,
        lot_type: str,
        side: str,
        trade_date: date,
    ) -> bool:
        self.verify()
        return (
            self.broker_id == str(broker_id).strip()
            and self.account_alias == str(account_alias).strip()
            and str(venue).upper().strip() in self.venues
            and str(product_type).lower().strip() in self.product_types
            and str(lot_type).lower().strip() in self.lot_types
            and str(side).lower().strip() in self.sides
            and self.valid_from <= trade_date
            and (self.valid_to is None or trade_date <= self.valid_to)
        )


@dataclass(frozen=True, slots=True)
class CostScheduleDocumentReceipt:
    """A reviewed, secret-free hash of one fee-schedule source document.

    The document itself is intentionally not copied into the application
    database.  Its content hash, safe locator and review identity are enough
    to bind an account quote to the exact document an owner reviewed without
    putting credentials or private statements into the repository.
    """

    receipt_id: str
    document_kind: str
    broker_id: str
    account_alias: str
    source_locator: str
    source_sha256: str
    source_published_at: datetime
    observed_at: datetime
    reviewed_by: str
    reviewed_at: datetime
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        document_kind: str,
        broker_id: str,
        account_alias: str,
        source_locator: str,
        source_sha256: str,
        source_published_at: datetime,
        reviewed_by: str,
        reviewed_at: datetime,
        observed_at: datetime | None = None,
    ) -> "CostScheduleDocumentReceipt":
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            document_kind=str(document_kind).strip(),
            broker_id=str(broker_id).strip(),
            account_alias=str(account_alias).strip(),
            source_locator=str(source_locator).strip(),
            source_sha256=_require_sha256(source_sha256, "source_sha256"),
            source_published_at=_utc(source_published_at),
            observed_at=_utc(observed_at or datetime.now(timezone.utc)),
            reviewed_by=str(reviewed_by).strip(),
            reviewed_at=_utc(reviewed_at),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_sha(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _DOCUMENT_SCHEMA,
            "receipt_id": self.receipt_id,
            "document_kind": self.document_kind,
            "broker_id": self.broker_id,
            "account_alias": self.account_alias,
            "source_locator": self.source_locator,
            "source_sha256": self.source_sha256,
            "source_published_at": _utc(self.source_published_at).isoformat(),
            "observed_at": _utc(self.observed_at).isoformat(),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": _utc(self.reviewed_at).isoformat(),
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "CostScheduleDocumentReceipt":
        if payload.get("schema_version") != _DOCUMENT_SCHEMA:
            raise ValueError("invalid cost schedule document receipt schema")
        receipt = cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            document_kind=str(payload.get("document_kind") or ""),
            broker_id=str(payload.get("broker_id") or ""),
            account_alias=str(payload.get("account_alias") or ""),
            source_locator=str(payload.get("source_locator") or ""),
            source_sha256=str(payload.get("source_sha256") or "").lower(),
            source_published_at=_utc(datetime.fromisoformat(str(payload.get("source_published_at")))),
            observed_at=_utc(datetime.fromisoformat(str(payload.get("observed_at")))),
            reviewed_by=str(payload.get("reviewed_by") or ""),
            reviewed_at=_utc(datetime.fromisoformat(str(payload.get("reviewed_at")))),
            receipt_sha256=str(payload.get("receipt_sha256") or "").lower(),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or self.document_kind not in _DOCUMENT_KINDS:
            raise ValueError("cost schedule document receipt identity is invalid")
        if not self.broker_id or not self.account_alias or not self.source_locator:
            raise ValueError("cost schedule document receipt scope is incomplete")
        if not self.reviewed_by:
            raise ValueError("cost schedule document receipt reviewer is required")
        _require_sha256(self.source_sha256, "source_sha256")
        if len(self.receipt_sha256) != 64 or self.receipt_sha256 != _sha(self.payload()):
            raise ValueError("cost schedule document receipt hash mismatch")


@dataclass(frozen=True, slots=True)
class DurableCostScheduleDocumentReceipt:
    """A document receipt after insertion into the immutable local ledger."""

    receipt: CostScheduleDocumentReceipt
    persisted_at: datetime
    persistence_sha256: str

    @classmethod
    def issue(
        cls,
        receipt: CostScheduleDocumentReceipt,
        *,
        persisted_at: datetime,
    ) -> "DurableCostScheduleDocumentReceipt":
        receipt.verify()
        durable = cls(receipt=receipt, persisted_at=_utc(persisted_at), persistence_sha256="")
        durable = replace(durable, persistence_sha256=_sha(durable.payload()))
        durable.verify()
        return durable

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.durable_cost_schedule_document_receipt.v1",
            "receipt_id": self.receipt.receipt_id,
            "receipt_sha256": self.receipt.receipt_sha256,
            "persisted_at": _utc(self.persisted_at).isoformat(),
        }

    def verify(self) -> None:
        self.receipt.verify()
        if len(self.persistence_sha256) != 64 or self.persistence_sha256 != _sha(self.payload()):
            raise ValueError("durable cost schedule document receipt hash mismatch")


@dataclass(frozen=True, slots=True)
class ReviewedBrokerCostScheduleReceipt:
    """Account cost evidence bound to both broker and exchange documents."""

    schedule: "DurableBrokerCostScheduleReceipt"
    documents: tuple[DurableCostScheduleDocumentReceipt, ...]
    reviewed_by: str
    reviewed_at: datetime
    review_receipt_sha256: str

    @classmethod
    def issue(
        cls,
        schedule: "DurableBrokerCostScheduleReceipt",
        documents: tuple[DurableCostScheduleDocumentReceipt, ...] | list[DurableCostScheduleDocumentReceipt],
        *,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> "ReviewedBrokerCostScheduleReceipt":
        result = cls(
            schedule=schedule,
            documents=tuple(documents),
            reviewed_by=str(reviewed_by).strip(),
            reviewed_at=_utc(reviewed_at),
            review_receipt_sha256="",
        )
        result = replace(result, review_receipt_sha256=_sha(result.payload()))
        result.verify()
        return result

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _REVIEW_SCHEMA,
            "schedule_receipt_id": self.schedule.receipt.receipt_id,
            "schedule_receipt_sha256": self.schedule.receipt.receipt_sha256,
            "schedule_persistence_sha256": self.schedule.persistence_sha256,
            "document_receipt_ids": [item.receipt.receipt_id for item in self.documents],
            "document_receipt_sha256": [item.receipt.receipt_sha256 for item in self.documents],
            "document_source_sha256": [item.receipt.source_sha256 for item in self.documents],
            "reviewed_by": self.reviewed_by,
            "reviewed_at": _utc(self.reviewed_at).isoformat(),
        }

    def verify(self) -> None:
        self.schedule.verify()
        if not self.reviewed_by:
            raise ValueError("reviewed cost schedule reviewer is required")
        self._validate_components(self.schedule, self.documents)
        if len(self.review_receipt_sha256) != 64 or self.review_receipt_sha256 != _sha(self.payload()):
            raise ValueError("reviewed cost schedule receipt hash mismatch")

    @staticmethod
    def _validate_components(
        schedule: "DurableBrokerCostScheduleReceipt",
        documents: tuple[DurableCostScheduleDocumentReceipt, ...] | list[DurableCostScheduleDocumentReceipt],
    ) -> None:
        documents = tuple(documents)
        if len(documents) != 2:
            raise ValueError("reviewed cost schedule requires broker and exchange documents")
        kinds = {item.receipt.document_kind for item in documents}
        if kinds != _DOCUMENT_KINDS:
            raise ValueError("reviewed cost schedule document kinds are incomplete")
        if len({item.receipt.receipt_id for item in documents}) != len(documents):
            raise ValueError("reviewed cost schedule document receipts must be unique")
        account = schedule.receipt
        for item in documents:
            item.verify()
            document = item.receipt
            if document.broker_id != account.broker_id or document.account_alias != account.account_alias:
                raise ValueError("reviewed cost schedule document account scope mismatch")
        broker_document = next(item.receipt for item in documents if item.receipt.document_kind == "broker_account_schedule")
        if broker_document.source_sha256 != account.source_sha256:
            raise ValueError("reviewed broker cost schedule source hash mismatch")


@dataclass(frozen=True, slots=True)
class DurableBrokerCostScheduleReceipt:
    """One receipt after it has entered the immutable local evidence ledger."""

    receipt: BrokerCostScheduleReceipt
    persisted_at: datetime
    persistence_sha256: str

    @classmethod
    def issue(
        cls,
        receipt: BrokerCostScheduleReceipt,
        *,
        persisted_at: datetime,
    ) -> "DurableBrokerCostScheduleReceipt":
        receipt.verify()
        durable = cls(receipt=receipt, persisted_at=_utc(persisted_at), persistence_sha256="")
        durable = replace(durable, persistence_sha256=_sha(durable.payload()))
        durable.verify()
        return durable

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.durable_broker_cost_schedule_receipt.v1",
            "receipt_id": self.receipt.receipt_id,
            "receipt_sha256": self.receipt.receipt_sha256,
            "persisted_at": _utc(self.persisted_at).isoformat(),
        }

    def verify(self) -> None:
        self.receipt.verify()
        if len(self.persistence_sha256) != 64 or self.persistence_sha256 != _sha(self.payload()):
            raise ValueError("durable cost schedule receipt hash mismatch")


class BrokerCostScheduleReceiptStore:
    """SQLite ledger whose schedule and source-document receipts are immutable."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                create table if not exists cost_schedule_document_receipts (
                    receipt_id text primary key,
                    document_kind text not null,
                    broker_id text not null,
                    account_alias text not null,
                    source_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null
                )
                """
            )
            connection.execute(
                """
                create table if not exists broker_cost_schedule_receipts (
                    receipt_id text primary key,
                    broker_id text not null,
                    account_alias text not null,
                    valid_from text not null,
                    valid_to text,
                    receipt_sha256 text not null unique,
                    receipt_json text not null,
                    persisted_at text not null
                )
                """
            )
            for action in ("update", "delete"):
                connection.execute(
                    f"""
                    create trigger if not exists cost_schedule_document_receipts_immutable_{action}
                    before {action} on cost_schedule_document_receipts
                    begin select raise(abort, 'cost schedule document receipts are immutable'); end
                    """
                )
                connection.execute(
                    f"""
                    create trigger if not exists broker_cost_schedule_receipts_immutable_{action}
                    before {action} on broker_cost_schedule_receipts
                    begin select raise(abort, 'broker cost schedule receipts are immutable'); end
                    """
                )

    def record_document(
        self,
        receipt: CostScheduleDocumentReceipt,
    ) -> DurableCostScheduleDocumentReceipt:
        receipt.verify()
        payload = _json(receipt.model_dump())
        with self._connect() as connection:
            existing = connection.execute(
                "select receipt_json, persisted_at from cost_schedule_document_receipts where receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["receipt_json"]) != payload:
                    raise ValueError("cost schedule document ID is bound to different evidence")
                return self._durable_document(existing)
            persisted_at = datetime.now(timezone.utc)
            connection.execute(
                """
                insert into cost_schedule_document_receipts (
                    receipt_id, document_kind, broker_id, account_alias,
                    source_sha256, receipt_json, persisted_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.document_kind,
                    receipt.broker_id,
                    receipt.account_alias,
                    receipt.source_sha256,
                    payload,
                    persisted_at.isoformat(),
                ),
            )
        return DurableCostScheduleDocumentReceipt.issue(receipt, persisted_at=persisted_at)

    def record(self, receipt: BrokerCostScheduleReceipt) -> DurableBrokerCostScheduleReceipt:
        receipt.verify()
        payload = _json(receipt.model_dump())
        with self._connect() as connection:
            existing = connection.execute(
                "select receipt_json, persisted_at from broker_cost_schedule_receipts where receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["receipt_json"]) != payload:
                    raise ValueError("cost schedule receipt ID is bound to different evidence")
                return self._durable(existing)
            persisted_at = datetime.now(timezone.utc)
            connection.execute(
                """
                insert into broker_cost_schedule_receipts (
                    receipt_id, broker_id, account_alias, valid_from, valid_to,
                    receipt_sha256, receipt_json, persisted_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id, receipt.broker_id, receipt.account_alias,
                    receipt.valid_from.isoformat(),
                    receipt.valid_to.isoformat() if receipt.valid_to else None,
                    receipt.receipt_sha256, payload, persisted_at.isoformat(),
                ),
            )
        return DurableBrokerCostScheduleReceipt.issue(receipt, persisted_at=persisted_at)

    def record_reviewed(
        self,
        receipt: BrokerCostScheduleReceipt,
        documents: tuple[CostScheduleDocumentReceipt, ...] | list[CostScheduleDocumentReceipt],
        *,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> ReviewedBrokerCostScheduleReceipt:
        """Persist the complete broker/exchange review chain atomically.

        The aggregate is the only receipt accepted by callers that require
        reviewed account evidence.  ``record`` remains available for legacy
        sensitivity tests, but it deliberately does not manufacture this
        reviewed proof.
        """

        raw_documents = tuple(documents)
        receipt.verify()
        for document in raw_documents:
            document.verify()
        # Validate the complete relationship before writing anything so a
        # mismatched source cannot leave an apparently valid partial ledger.
        provisional_documents = tuple(
            DurableCostScheduleDocumentReceipt.issue(
                document,
                persisted_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
            for document in raw_documents
        )
        provisional_schedule = DurableBrokerCostScheduleReceipt.issue(
            receipt,
            persisted_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        ReviewedBrokerCostScheduleReceipt._validate_components(
            provisional_schedule,
            provisional_documents,
        )
        durable_documents = tuple(self.record_document(item) for item in raw_documents)
        durable_schedule = self.record(receipt)
        return ReviewedBrokerCostScheduleReceipt.issue(
            durable_schedule,
            durable_documents,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
        )

    def effective_receipt(self, **scope: Any) -> DurableBrokerCostScheduleReceipt:
        trade_date = scope.pop("trade_date")
        matches = [
            item for item in self.receipts()
            if item.receipt.matches(trade_date=trade_date, **scope)
        ]
        if len(matches) != 1:
            raise ValueError("cost schedule receipt is missing or ambiguous for account scope")
        return matches[0]

    def receipts(self) -> list[DurableBrokerCostScheduleReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "select receipt_json, persisted_at from broker_cost_schedule_receipts order by valid_from, receipt_id"
            ).fetchall()
        return [self._durable(row) for row in rows]

    def documents(self) -> list[DurableCostScheduleDocumentReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "select receipt_json, persisted_at from cost_schedule_document_receipts order by receipt_id"
            ).fetchall()
        return [self._durable_document(row) for row in rows]

    @staticmethod
    def _durable(row: sqlite3.Row) -> DurableBrokerCostScheduleReceipt:
        receipt = BrokerCostScheduleReceipt.model_validate(json.loads(str(row["receipt_json"])))
        return DurableBrokerCostScheduleReceipt.issue(
            receipt,
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        )

    @staticmethod
    def _durable_document(row: sqlite3.Row) -> DurableCostScheduleDocumentReceipt:
        receipt = CostScheduleDocumentReceipt.model_validate(json.loads(str(row["receipt_json"])))
        return DurableCostScheduleDocumentReceipt.issue(
            receipt,
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: dict[str, Any]) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = [
    "BrokerCostScheduleReceipt",
    "BrokerCostScheduleReceiptStore",
    "CostScheduleDocumentReceipt",
    "DurableBrokerCostScheduleReceipt",
    "DurableCostScheduleDocumentReceipt",
    "ReviewedBrokerCostScheduleReceipt",
]
