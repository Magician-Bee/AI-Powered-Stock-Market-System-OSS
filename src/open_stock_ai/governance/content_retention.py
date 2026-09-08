"""Content-addressed retention rules for signals and execution-critical evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "open_stock_ai.content_retention_receipt.v1"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RetentionReceipt:
    operation: str
    removed_content_sha256: tuple[str, ...]
    retained_critical_ids: tuple[str, ...]
    occurred_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "removed_content_sha256": list(self.removed_content_sha256),
            "retained_critical_ids": list(self.retained_critical_ids),
            "occurred_at": self.occurred_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.receipt_sha256)


class RetentionError(ValueError):
    """Raised when retention would overwrite or discard protected evidence."""


class ContentAddressedRetentionLedger:
    """Keep a bounded signal projection while preserving critical evidence."""

    def __init__(self, *, max_signal_entries: int = 500, store: Any | None = None) -> None:
        if int(max_signal_entries) < 1:
            raise ValueError("max_signal_entries_must_be_positive")
        self.max_signal_entries = int(max_signal_entries)
        self.store = store
        self._records: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self.receipts: list[RetentionReceipt] = []
        if store is not None:
            for record in store.load_records():
                record_id = str(record.get("record_id") or "")
                if not record_id or set(record) != {
                    "schema_version", "record_id", "kind", "critical", "content_sha256", "occurred_at", "payload"
                }:
                    raise RetentionError("retention_durable_record_invalid")
                expected_hash = hashlib.sha256(_canonical(record.get("payload") or {})).hexdigest()
                if expected_hash != record.get("content_sha256"):
                    raise RetentionError("retention_durable_content_hash_invalid")
                self._records[record_id] = dict(record)
                self._order.append(record_id)
            for payload in store.load_receipts():
                receipt = RetentionReceipt(
                    operation=str(payload["operation"]),
                    removed_content_sha256=tuple(payload["removed_content_sha256"]),
                    retained_critical_ids=tuple(payload["retained_critical_ids"]),
                    occurred_at=str(payload["occurred_at"]),
                    receipt_sha256=str(payload["receipt_sha256"]),
                )
                if not receipt.verify():
                    raise RetentionError("retention_durable_receipt_invalid")
                self.receipts.append(receipt)

    def append(
        self,
        record_id: str,
        payload: dict[str, Any],
        *,
        critical: bool,
        kind: str = "signal",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        record_id = str(record_id or "").strip()
        if not record_id or not isinstance(payload, dict) or not str(kind or "").strip():
            raise RetentionError("retention_record_identity_invalid")
        content_sha256 = hashlib.sha256(_canonical(payload)).hexdigest()
        record = {
            "schema_version": "open_stock_ai.retained_content.v1",
            "record_id": record_id,
            "kind": str(kind).strip(),
            "critical": bool(critical),
            "content_sha256": content_sha256,
            "occurred_at": str(occurred_at or datetime.now(timezone.utc).isoformat()),
            "payload": dict(payload),
        }
        existing = self._records.get(record_id)
        if existing is not None:
            if existing != record:
                raise RetentionError("retention_record_immutable_conflict")
            return dict(existing)
        if self.store is not None:
            try:
                self.store.save_record(record)
            except ValueError as exc:
                raise RetentionError(str(exc)) from exc
        self._records[record_id] = record
        self._order.append(record_id)
        return dict(record)

    def prune_signals(self, *, now: datetime | None = None) -> RetentionReceipt:
        signal_ids = [
            record_id for record_id in self._order
            if self._records[record_id]["kind"] == "signal" and not self._records[record_id]["critical"]
        ]
        remove_ids = signal_ids[: max(0, len(signal_ids) - self.max_signal_entries)]
        removed_hashes = [self._records[record_id]["content_sha256"] for record_id in remove_ids]
        occurred_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "operation": "prune_signals",
            "removed_content_sha256": removed_hashes,
            "retained_critical_ids": sorted(
                record_id for record_id in self._order if self._records[record_id]["critical"]
            ),
            "occurred_at": occurred_at,
        }
        receipt = RetentionReceipt(
            operation="prune_signals",
            removed_content_sha256=tuple(payload["removed_content_sha256"]),
            retained_critical_ids=tuple(payload["retained_critical_ids"]),
            occurred_at=occurred_at,
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        if self.store is not None:
            try:
                self.store.prune_and_save_receipt(remove_ids, receipt.as_dict())
            except (ValueError, KeyError) as exc:
                raise RetentionError("retention_durable_write_failed") from exc
        for record_id in remove_ids:
            del self._records[record_id]
            self._order.remove(record_id)
        self.receipts.append(receipt)
        return receipt

    def get(self, record_id: str) -> dict[str, Any] | None:
        record = self._records.get(str(record_id or "").strip())
        return dict(record) if record is not None else None

    def retained_counts(self) -> dict[str, int]:
        return {
            "total": len(self._records),
            "signals": sum(self._records[item]["kind"] == "signal" for item in self._order),
            "critical": sum(self._records[item]["critical"] for item in self._order),
        }


class RetentionMaintenanceScheduler:
    """Run bounded-signal retention on a durable, restart-safe cadence.

    A maintenance pass always produces the existing immutable retention
    receipt, even when there is nothing to remove.  Consequently the last
    receipt is both auditable evidence of the maintenance pass and durable
    cadence state after a desktop restart; no mutable scheduler checkpoint is
    required.
    """

    schema_version = "open_stock_ai.retention_maintenance.v1"

    def __init__(
        self,
        ledger: ContentAddressedRetentionLedger,
        *,
        interval_seconds: int = 3600,
    ) -> None:
        if int(interval_seconds) < 1:
            raise ValueError("retention_maintenance_interval_must_be_positive")
        self.ledger = ledger
        self.interval_seconds = int(interval_seconds)

    def run_due(self, *, now: datetime | None = None) -> RetentionReceipt | None:
        """Prune once when due, returning ``None`` when the cadence is current."""

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        last_completed = self._last_completed_at()
        if last_completed is not None:
            elapsed = (current - last_completed).total_seconds()
            if elapsed < self.interval_seconds:
                return None
        return self.ledger.prune_signals(now=current)

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        last_completed = self._last_completed_at()
        next_due = (
            None
            if last_completed is None
            else last_completed.timestamp() + self.interval_seconds
        )
        return {
            "schema_version": self.schema_version,
            "interval_seconds": self.interval_seconds,
            "durable": self.ledger.store is not None,
            "last_completed_at": (
                last_completed.isoformat() if last_completed is not None else None
            ),
            "next_due_at": (
                datetime.fromtimestamp(next_due, tz=timezone.utc).isoformat()
                if next_due is not None
                else None
            ),
            "due": last_completed is None or current.timestamp() >= next_due,
        }

    def _last_completed_at(self) -> datetime | None:
        completed = [
            _parse_occurred_at(receipt.occurred_at)
            for receipt in self.ledger.receipts
            if receipt.operation == "prune_signals"
        ]
        return max((item for item in completed if item is not None), default=None)


def _parse_occurred_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)

def verify_retention_receipt(receipt: dict[str, Any]) -> bool:
    required = {
        "schema_version", "operation", "removed_content_sha256", "retained_critical_ids",
        "occurred_at", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = hashlib.sha256(_canonical({key: receipt[key] for key in required if key != "receipt_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(receipt.get("receipt_sha256") or ""))
