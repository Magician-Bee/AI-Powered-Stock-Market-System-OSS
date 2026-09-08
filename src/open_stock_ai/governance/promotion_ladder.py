"""Fail-closed capability promotion and automatic downgrade contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

from open_stock_ai.storage.migrations import ManagedSQLiteConnection


_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = "open_stock_ai.capability_promotion_receipt.v1"
PromotionLevel = Literal["research", "paper", "shadow", "broker_sandbox", "restricted_live", "production_live"]
_LEVELS: tuple[PromotionLevel, ...] = (
    "research", "paper", "shadow", "broker_sandbox", "restricted_live", "production_live"
)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def ordered_levels() -> tuple[PromotionLevel, ...]:
    return _LEVELS


def required_evidence(level: PromotionLevel) -> tuple[str, ...]:
    payload = yaml.safe_load((_ROOT / "config" / "capability_status.yaml").read_text(encoding="utf-8")) or {}
    levels = payload.get("levels") or {}
    item = levels.get(str(level).upper()) if isinstance(levels, dict) else None
    return tuple(str(value) for value in (item or {}).get("requires") or [])


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    from_level: PromotionLevel
    to_level: PromotionLevel
    action: Literal["promote", "downgrade"]
    approved_by: str
    reason: str
    evidence_hash: str
    missing_evidence: tuple[str, ...]
    occurred_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "action": self.action,
            "approved_by": self.approved_by,
            "reason": self.reason,
            "evidence_hash": self.evidence_hash,
            "missing_evidence": list(self.missing_evidence),
            "occurred_at": self.occurred_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.receipt_sha256)


class PromotionError(PermissionError):
    """Raised when a capability transition is unsafe or incomplete."""


class SQLitePromotionReceiptStore:
    """Append-only restart-safe storage for capability transitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                create table if not exists governed_capability_promotion_receipts (
                    receipt_sha256 text primary key, from_level text not null,
                    to_level text not null, action text not null,
                    occurred_at text not null, payload_json text not null
                )
            """)
            for action in ("update", "delete"):
                connection.execute(f"""
                    create trigger if not exists governed_capability_promotion_immutable_{action}
                    before {action} on governed_capability_promotion_receipts
                    begin select raise(abort, 'capability promotion receipts are immutable'); end
                """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, factory=ManagedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 5000")
        connection.execute("pragma synchronous = full")
        return connection

    def record(self, receipt: PromotionReceipt) -> PromotionReceipt:
        if not receipt.verify() or not verify_promotion_receipt(receipt.as_dict()):
            raise ValueError("invalid capability promotion receipt")
        encoded = json.dumps(receipt.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from governed_capability_promotion_receipts where receipt_sha256 = ?",
                (receipt.receipt_sha256,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != encoded:
                    raise ValueError("capability_promotion_receipt_immutable_conflict")
                return receipt
            connection.execute(
                "insert into governed_capability_promotion_receipts values (?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_sha256,
                    receipt.from_level,
                    receipt.to_level,
                    receipt.action,
                    receipt.occurred_at,
                    encoded,
                ),
            )
        return receipt

    def receipts(self) -> list[PromotionReceipt]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_capability_promotion_receipts order by occurred_at, receipt_sha256"
            ).fetchall()
        return [_receipt_from_dict(json.loads(row["payload_json"])) for row in rows]


class CapabilityPromotionLadder:
    def __init__(
        self,
        current_level: PromotionLevel = "research",
        *,
        store: SQLitePromotionReceiptStore | None = None,
    ) -> None:
        if current_level not in _LEVELS:
            raise ValueError("unknown_capability_level")
        self.store = store
        persisted = store.receipts() if store is not None else []
        if persisted:
            persisted_level = persisted[-1].to_level
            if current_level != "research" and current_level != persisted_level:
                raise ValueError("capability_promotion_store_level_conflict")
            current_level = persisted_level
        self.current_level = current_level
        self.receipts: list[PromotionReceipt] = persisted

    def promote(self, target_level: PromotionLevel, *, evidence: dict[str, Any], approved_by: str, now: datetime | None = None) -> PromotionReceipt:
        current_index = _LEVELS.index(self.current_level)
        target_index = _LEVELS.index(target_level)
        if target_index != current_index + 1:
            raise PromotionError("capability_promotion_must_advance_one_level")
        actor = str(approved_by or "").strip()
        if not actor or actor.casefold() in {"agent", "codex", "model", "ai", "system"}:
            raise PromotionError("human_approval_required_for_capability_promotion")
        required = required_evidence(target_level)
        missing = tuple(key for key in required if not evidence.get(key))
        if missing:
            raise PromotionError(f"capability_promotion_evidence_missing:{','.join(missing)}")
        receipt = self._receipt(target_level, "promote", actor, "promotion_evidence_verified", evidence, missing, now)
        if self.store is not None:
            self.store.record(receipt)
        self.current_level = target_level
        self.receipts.append(receipt)
        return receipt

    def downgrade(self, target_level: PromotionLevel, *, reason: str, approved_by: str = "host", now: datetime | None = None) -> PromotionReceipt:
        if _LEVELS.index(target_level) >= _LEVELS.index(self.current_level):
            raise PromotionError("capability_downgrade_requires_lower_level")
        if not str(reason or "").strip():
            raise PromotionError("capability_downgrade_reason_required")
        receipt = self._receipt(target_level, "downgrade", str(approved_by or "host"), str(reason), {}, (), now)
        if self.store is not None:
            self.store.record(receipt)
        self.current_level = target_level
        self.receipts.append(receipt)
        return receipt

    def _receipt(self, target_level: PromotionLevel, action: Literal["promote", "downgrade"], actor: str, reason: str, evidence: dict[str, Any], missing: tuple[str, ...], now: datetime | None) -> PromotionReceipt:
        occurred_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        evidence_hash = hashlib.sha256(_canonical(evidence)).hexdigest()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "from_level": self.current_level,
            "to_level": target_level,
            "action": action,
            "approved_by": actor,
            "reason": reason,
            "evidence_hash": evidence_hash,
            "missing_evidence": list(missing),
            "occurred_at": occurred_at,
        }
        return PromotionReceipt(
            from_level=self.current_level, to_level=target_level, action=action,
            approved_by=actor, reason=reason, evidence_hash=evidence_hash,
            missing_evidence=missing, occurred_at=occurred_at,
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )


def verify_promotion_receipt(receipt: dict[str, Any]) -> bool:
    required = {"schema_version", "from_level", "to_level", "action", "approved_by", "reason", "evidence_hash", "missing_evidence", "occurred_at", "receipt_sha256"}
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    if receipt.get("from_level") not in _LEVELS or receipt.get("to_level") not in _LEVELS:
        return False
    if receipt.get("action") not in {"promote", "downgrade"}:
        return False
    expected = hashlib.sha256(_canonical({key: receipt[key] for key in required if key != "receipt_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(receipt.get("receipt_sha256") or ""))


def _receipt_from_dict(value: dict[str, Any]) -> PromotionReceipt:
    if not verify_promotion_receipt(value):
        raise ValueError("invalid persisted capability promotion receipt")
    if value["from_level"] not in _LEVELS or value["to_level"] not in _LEVELS:
        raise ValueError("invalid persisted capability promotion level")
    if value["action"] not in {"promote", "downgrade"}:
        raise ValueError("invalid persisted capability promotion action")
    receipt = PromotionReceipt(
        from_level=value["from_level"],
        to_level=value["to_level"],
        action=value["action"],
        approved_by=str(value["approved_by"]),
        reason=str(value["reason"]),
        evidence_hash=str(value["evidence_hash"]),
        missing_evidence=tuple(str(item) for item in value["missing_evidence"]),
        occurred_at=str(value["occurred_at"]),
        receipt_sha256=str(value["receipt_sha256"]),
    )
    if not receipt.verify():
        raise ValueError("invalid persisted capability promotion receipt")
    return receipt
