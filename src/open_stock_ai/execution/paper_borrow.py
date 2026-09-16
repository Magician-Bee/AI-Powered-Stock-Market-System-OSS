"""Durable, fail-closed borrow inventory for local paper short simulation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.execution.taiwan_settlement import parse_timestamp
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.data_platform.borrow_history_receipts import verify_short_borrow_history_receipt


@dataclass
class PaperBorrowLedger:
    store: SQLiteStore
    account_id: str
    retention_ledger: ContentAddressedRetentionLedger | None = None

    def __post_init__(self) -> None:
        if self.retention_ledger is None:
            return
        retention_path = getattr(self.retention_ledger.store, "path", None)
        if retention_path is None or Path(retention_path).resolve() != Path(self.store.path).resolve():
            raise ValueError("paper borrow retention must share its durable database")

    def preview(self, *, symbol: str, quantity: float, receipt: dict[str, Any] | None, as_of: Any = None) -> dict[str, Any]:
        if not isinstance(receipt, dict):
            return {"allowed": False, "reason": "borrow_locate_required", "receipt": None}
        try:
            normalized = self._normalize_receipt(receipt, symbol=symbol)
        except ValueError as exc:
            return {"allowed": False, "reason": str(exc), "receipt": None}
        observed = self._timestamp(as_of)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            stored = conn.execute(
                "select * from paper_borrow_locates where receipt_id = ?", (normalized["receipt_id"],)
            ).fetchone()
        if stored is not None:
            row = dict(stored)
            if row["account_id"] != self.account_id or row["symbol"] != normalized["symbol"] or row["receipt_json"] != normalized["receipt_json"]:
                return {"allowed": False, "reason": "borrow_locate_receipt_contract_mismatch", "receipt": None}
            normalized = {**normalized, "reserved_quantity": float(row["reserved_quantity"]), "status": row["status"]}
            if row["status"] == "recalled":
                return {"allowed": False, "reason": "borrow_locate_recalled", "receipt": self._public_receipt(normalized), "available_quantity": max(0.0, normalized["available_quantity"] - normalized["reserved_quantity"])}
            if row["status"] == "expired":
                return {"allowed": False, "reason": "borrow_locate_expired", "receipt": self._public_receipt(normalized), "available_quantity": max(0.0, normalized["available_quantity"] - normalized["reserved_quantity"])}
        reason = self._receipt_unavailable_reason(normalized, observed)
        remaining = normalized["available_quantity"] - normalized.get("reserved_quantity", 0.0)
        if reason:
            return {"allowed": False, "reason": reason, "receipt": self._public_receipt(normalized), "available_quantity": max(0.0, remaining)}
        if quantity > remaining + 1e-9:
            return {"allowed": False, "reason": "borrow_locate_quantity_exhausted", "receipt": self._public_receipt(normalized), "available_quantity": max(0.0, remaining)}
        return {"allowed": True, "reason": None, "receipt": self._public_receipt(normalized), "available_quantity": max(0.0, remaining)}

    def import_receipt(self, conn: sqlite3.Connection, receipt: dict[str, Any], *, symbol: str, now: str) -> dict[str, Any]:
        normalized = self._normalize_receipt(receipt, symbol=symbol)
        existing = conn.execute("select * from paper_borrow_locates where receipt_id = ?", (normalized["receipt_id"],)).fetchone()
        if existing is not None:
            row = dict(existing)
            if row["account_id"] != self.account_id or row["symbol"] != normalized["symbol"] or row["receipt_json"] != normalized["receipt_json"]:
                raise ValueError("borrow_locate_receipt_contract_mismatch")
            return row
        conn.execute(
            """
            insert into paper_borrow_locates (
                receipt_id, account_id, symbol, created_at, verified_at, expires_at, recall_at,
                available_quantity, reserved_quantity, annual_fee_bps, source, status, receipt_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'active', ?)
            """,
            (normalized["receipt_id"], self.account_id, normalized["symbol"], now, normalized["verified_at"], normalized["expires_at"], normalized["recall_at"], normalized["available_quantity"], normalized["annual_fee_bps"], normalized["source"], normalized["receipt_json"]),
        )
        self._event(conn, receipt_id=normalized["receipt_id"], short_borrow_id=None, created_at=now, event_type="locate_imported", quantity=normalized["available_quantity"], amount=None, payload={"source": normalized["source"], "verified_at": normalized["verified_at"], "expires_at": normalized["expires_at"], "recall_at": normalized["recall_at"]})
        return dict(conn.execute("select * from paper_borrow_locates where receipt_id = ?", (normalized["receipt_id"],)).fetchone())

    def reserve_short(self, conn: sqlite3.Connection, *, receipt: dict[str, Any] | None, symbol: str, quantity: float, fill_id: str, entry_price: float, now: str, as_of: Any = None) -> dict[str, Any]:
        if not isinstance(receipt, dict):
            raise ValueError("borrow_locate_required")
        row = self.import_receipt(conn, receipt, symbol=symbol, now=now)
        observed = self._timestamp(as_of or now)
        self.refresh_statuses(conn, observed_at=observed, now=now)
        row = dict(conn.execute("select * from paper_borrow_locates where receipt_id = ?", (row["receipt_id"],)).fetchone())
        if row["status"] == "recalled":
            raise ValueError("borrow_locate_recalled")
        if row["status"] == "expired":
            raise ValueError("borrow_locate_expired")
        remaining = float(row["available_quantity"]) - float(row["reserved_quantity"])
        if quantity > remaining + 1e-9:
            raise ValueError("borrow_locate_quantity_exhausted")
        short_borrow_id = f"PSB-{uuid4().hex}"
        conn.execute("update paper_borrow_locates set reserved_quantity = reserved_quantity + ?, status = case when reserved_quantity + ? >= available_quantity then 'exhausted' else status end where receipt_id = ?", (quantity, quantity, row["receipt_id"]))
        conn.execute(
            """
            insert into paper_short_borrows (
                short_borrow_id, receipt_id, account_id, fill_id, symbol, opened_at,
                original_quantity, open_quantity, entry_price, annual_fee_bps,
                last_fee_accrual_date, status, closed_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', null)
            """,
            (short_borrow_id, row["receipt_id"], self.account_id, fill_id, symbol.upper(), now, quantity, quantity, entry_price, float(row["annual_fee_bps"]), observed.date().isoformat()),
        )
        self._event(conn, receipt_id=row["receipt_id"], short_borrow_id=short_borrow_id, created_at=now, event_type="short_opened", quantity=quantity, amount=None, payload={"fill_id": fill_id, "entry_price": entry_price})
        return {"short_borrow_id": short_borrow_id, "receipt_id": row["receipt_id"], "annual_fee_bps": float(row["annual_fee_bps"])}

    def cover_short(self, conn: sqlite3.Connection, *, symbol: str, quantity: float, now: str) -> list[dict[str, Any]]:
        rows = conn.execute("select * from paper_short_borrows where account_id = ? and symbol = ? and open_quantity > 0 order by opened_at asc, short_borrow_id asc", (self.account_id, symbol.upper())).fetchall()
        remaining = quantity
        releases: list[dict[str, Any]] = []
        for raw in rows:
            if remaining <= 1e-9:
                break
            row = dict(raw)
            released = min(remaining, float(row["open_quantity"]))
            next_open = float(row["open_quantity"]) - released
            closed = next_open <= 1e-9
            conn.execute("update paper_short_borrows set open_quantity = ?, status = ?, closed_at = ? where short_borrow_id = ?", (max(0.0, next_open), "closed" if closed else row["status"], now if closed else None, row["short_borrow_id"]))
            conn.execute("update paper_borrow_locates set reserved_quantity = max(0, reserved_quantity - ?), status = case when status = 'exhausted' then 'active' else status end where receipt_id = ?", (released, row["receipt_id"]))
            self._event(conn, receipt_id=row["receipt_id"], short_borrow_id=row["short_borrow_id"], created_at=now, event_type="short_covered", quantity=released, amount=None, payload={"remaining_open_quantity": max(0.0, next_open)})
            releases.append({"short_borrow_id": row["short_borrow_id"], "receipt_id": row["receipt_id"], "quantity": released})
            remaining -= released
        if remaining > 1e-9:
            raise ValueError("short_borrow_coverage_mismatch")
        return releases

    def refresh_statuses(self, conn: sqlite3.Connection, *, observed_at: datetime, now: str) -> None:
        rows = conn.execute("select * from paper_borrow_locates where account_id = ? and status in ('active', 'exhausted')", (self.account_id,)).fetchall()
        for raw in rows:
            row = dict(raw)
            next_status = None
            event_type = None
            if row.get("recall_at") and parse_timestamp(row["recall_at"]) <= observed_at:
                next_status, event_type = "recalled", "recall_due"
            elif parse_timestamp(row["expires_at"]) <= observed_at:
                next_status, event_type = "expired", "locate_expired"
            if next_status and row["status"] != next_status:
                conn.execute("update paper_borrow_locates set status = ? where receipt_id = ?", (next_status, row["receipt_id"]))
                conn.execute("update paper_short_borrows set status = 'recall_due' where receipt_id = ? and status = 'open' and open_quantity > 0", (row["receipt_id"],))
                self._event(conn, receipt_id=row["receipt_id"], short_borrow_id=None, created_at=now, event_type=event_type, quantity=None, amount=None, payload={"observed_at": observed_at.isoformat()})

    def accrue_fees(self, *, as_of: Any = None, market_prices: dict[str, float] | None = None) -> dict[str, Any]:
        observed = self._timestamp(as_of)
        now = observed.isoformat()
        total = 0.0
        events: list[dict[str, Any]] = []
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            self.refresh_statuses(conn, observed_at=observed, now=now)
            rows = conn.execute("select * from paper_short_borrows where account_id = ? and open_quantity > 0 and status in ('open', 'recall_due')", (self.account_id,)).fetchall()
            for raw in rows:
                row = dict(raw)
                days = max(0, (observed.date() - date.fromisoformat(row["last_fee_accrual_date"])).days)
                if not days:
                    continue
                mark_row = conn.execute("select last_price from paper_positions where account_id = ? and symbol = ?", (self.account_id, row["symbol"])).fetchone()
                mark_price = float((market_prices or {}).get(row["symbol"], mark_row[0] if mark_row is not None else row["entry_price"]))
                if mark_price <= 0:
                    mark_price = float(row["entry_price"])
                amount = float(row["open_quantity"]) * mark_price * float(row["annual_fee_bps"]) / 10_000.0 / 365.0 * days
                if amount <= 0:
                    conn.execute("update paper_short_borrows set last_fee_accrual_date = ? where short_borrow_id = ?", (observed.date().isoformat(), row["short_borrow_id"]))
                    continue
                account = conn.execute("select cash_balance from paper_accounts where account_id = ?", (self.account_id,)).fetchone()
                balance_after = float(account[0]) - amount
                conn.execute("update paper_accounts set cash_balance = ?, realized_pnl = realized_pnl - ?, updated_at = ? where account_id = ?", (balance_after, amount, now, self.account_id))
                conn.execute("insert into cash_ledger (account_id, order_id, created_at, entry_type, amount, balance_after, currency, metadata_json) values (?, null, ?, 'paper_borrow_fee', ?, ?, 'TWD', ?)", (self.account_id, now, -amount, balance_after, json.dumps({"short_borrow_id": row["short_borrow_id"], "days": days}, sort_keys=True)))
                conn.execute("update paper_short_borrows set last_fee_accrual_date = ? where short_borrow_id = ?", (observed.date().isoformat(), row["short_borrow_id"]))
                self._event(conn, receipt_id=row["receipt_id"], short_borrow_id=row["short_borrow_id"], created_at=now, event_type="fee_accrued", quantity=float(row["open_quantity"]), amount=amount, payload={"days": days, "annual_fee_bps": float(row["annual_fee_bps"]), "mark_price": mark_price})
                total += amount
                events.append({"short_borrow_id": row["short_borrow_id"], "amount": round(amount, 6), "days": days})
            conn.commit()
        result = {
            "schema_version": "open_stock_ai.paper_borrow_fee_batch.v1",
            "account_id": self.account_id,
            "as_of": observed.isoformat(),
            "accrued_fee": round(total, 6),
            "events": events,
        }
        if events and self.retention_ledger is not None:
            encoded = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.retention_ledger.append(
                f"paper-borrow-fee-{sha256(encoded).hexdigest()}",
                result,
                critical=True,
                kind="execution_borrow_fee",
                occurred_at=observed.isoformat(),
            )
        return result

    def summary(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            locates = [dict(row) for row in conn.execute("select * from paper_borrow_locates where account_id = ? order by created_at desc", (self.account_id,)).fetchall()]
            shorts = [dict(row) for row in conn.execute("select * from paper_short_borrows where account_id = ? and open_quantity > 0 order by opened_at desc", (self.account_id,)).fetchall()]
        return {"locates": [self._public_locate(row) for row in locates], "open_short_borrows": shorts, "open_short_quantity": round(sum(float(row["open_quantity"]) for row in shorts), 8), "recall_due_count": sum(1 for row in shorts if row["status"] == "recall_due")}

    def _normalize_receipt(self, receipt: dict[str, Any], *, symbol: str) -> dict[str, Any]:
        receipt_id = str(receipt.get("receipt_id") or receipt.get("locate_id") or "").strip()
        source = str(receipt.get("source") or "").strip()
        normalized_symbol = str(receipt.get("symbol") or symbol).strip().upper()
        if not receipt_id:
            raise ValueError("borrow_locate_receipt_id_required")
        if not source:
            raise ValueError("borrow_locate_source_required")
        if normalized_symbol != symbol.upper():
            raise ValueError("borrow_locate_symbol_mismatch")
        try:
            available = float(receipt.get("available_quantity"))
            fee = float(receipt.get("annual_fee_bps"))
        except (TypeError, ValueError) as exc:
            raise ValueError("borrow_locate_quantity_and_fee_required") from exc
        if available <= 0 or fee < 0 or fee > 1600:
            raise ValueError("borrow_locate_quantity_or_fee_invalid")
        verified = self._timestamp(receipt.get("verified_at"))
        expires = self._timestamp(receipt.get("expires_at"))
        recall_raw = receipt.get("recall_at")
        recall = self._timestamp(recall_raw) if recall_raw not in {None, ""} else None
        if expires <= verified:
            raise ValueError("borrow_locate_expiry_must_follow_verification")
        execution_mode = str(receipt.get("execution_mode") or "paper").strip().lower()
        if execution_mode not in {"paper", "historical"}:
            raise ValueError("borrow_locate_execution_mode_invalid")
        historical_coverage = receipt.get("historical_borrow_history")
        if execution_mode == "historical":
            if not isinstance(historical_coverage, dict):
                raise ValueError("borrow_history_execution_replay_receipt_required")
            verify_short_borrow_history_receipt(historical_coverage)
            if historical_coverage.get("execution_replay_eligible") is not True:
                raise ValueError("borrow_history_execution_replay_not_eligible")
            if normalized_symbol not in set(historical_coverage.get("symbols") or []):
                raise ValueError("borrow_history_symbol_not_covered")
        elif historical_coverage is not None:
            raise ValueError("borrow_history_requires_historical_execution_mode")
        canonical = {
            "receipt_id": receipt_id,
            "symbol": normalized_symbol,
            "source": source,
            "verified_at": verified.isoformat(),
            "expires_at": expires.isoformat(),
            "recall_at": recall.isoformat() if recall else None,
            "available_quantity": available,
            "annual_fee_bps": fee,
            "execution_mode": execution_mode,
            "historical_borrow_history": historical_coverage,
        }
        return {**canonical, "reserved_quantity": 0.0, "receipt_json": json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        return parse_timestamp(value or datetime.now(timezone.utc))

    @staticmethod
    def _receipt_unavailable_reason(receipt: dict[str, Any], observed: datetime) -> str | None:
        if receipt.get("execution_mode") == "historical":
            coverage = receipt.get("historical_borrow_history") or {}
            first = coverage.get("first_as_of")
            last = coverage.get("last_as_of")
            if not first or not last:
                return "borrow_history_pit_window_missing"
            try:
                if observed < parse_timestamp(first) or observed > parse_timestamp(last):
                    return "borrow_history_pit_date_outside_coverage"
            except (TypeError, ValueError):
                return "borrow_history_pit_window_invalid"
        if receipt.get("recall_at") and parse_timestamp(receipt["recall_at"]) <= observed:
            return "borrow_locate_recalled"
        if parse_timestamp(receipt["expires_at"]) <= observed:
            return "borrow_locate_expired"
        return None

    @staticmethod
    def _public_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        return {key: receipt.get(key) for key in ("receipt_id", "symbol", "source", "verified_at", "expires_at", "recall_at", "available_quantity", "annual_fee_bps")}

    @staticmethod
    def _public_locate(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in ("receipt_id", "symbol", "verified_at", "expires_at", "recall_at", "available_quantity", "reserved_quantity", "annual_fee_bps", "source", "status")}

    def _event(self, conn: sqlite3.Connection, *, receipt_id: str | None, short_borrow_id: str | None, created_at: str, event_type: str, quantity: float | None, amount: float | None, payload: dict[str, Any]) -> None:
        conn.execute("insert into paper_borrow_events (account_id, receipt_id, short_borrow_id, created_at, event_type, quantity, amount, payload_json) values (?, ?, ?, ?, ?, ?, ?, ?)", (self.account_id, receipt_id, short_borrow_id, created_at, event_type, quantity, amount, json.dumps(payload, ensure_ascii=False, sort_keys=True)))
