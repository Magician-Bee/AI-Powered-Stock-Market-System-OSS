from __future__ import annotations

from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from open_stock_ai.config.settings import load_settings
from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from .config import get_settings
from .data_quality_contracts import DecisionDataQualityReceipt


def _database_path() -> Path:
    configured = Path(load_settings().sqlite_path).expanduser()
    if not configured.is_absolute():
        configured = get_settings().project_root / configured
    configured.parent.mkdir(parents=True, exist_ok=True)
    return configured


class SharedDecisionQualityStore:
    """Immutable persistence for quality receipts outside scan snapshots."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else _database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20, factory=ManagedSQLiteConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout=20000")
        conn.execute("pragma foreign_keys=on")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                create table if not exists decision_quality_receipts (
                    receipt_id text primary key,
                    surface text not null,
                    symbol text not null,
                    receipt_sha256 text not null unique,
                    payload_json text not null,
                    persisted_at text not null
                );
                create index if not exists idx_decision_quality_receipts_surface_symbol
                    on decision_quality_receipts(surface, symbol, persisted_at desc);
                create trigger if not exists trg_decision_quality_receipts_immutable_update
                    before update on decision_quality_receipts
                    begin select raise(abort, 'decision quality receipts are immutable'); end;
                create trigger if not exists trg_decision_quality_receipts_immutable_delete
                    before delete on decision_quality_receipts
                    begin select raise(abort, 'decision quality receipts are immutable'); end;
                """
            )
            conn.commit()

    def save(
        self,
        receipt: DecisionDataQualityReceipt,
        *,
        surface: str,
    ) -> DecisionDataQualityReceipt:
        normalized_surface = str(surface).strip()
        if not normalized_surface:
            raise ValueError("decision quality receipt surface is required")
        payload = receipt.model_dump_json()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                """
                select surface, symbol, receipt_sha256, payload_json
                  from decision_quality_receipts
                 where receipt_id=?
                """,
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["surface"] != normalized_surface
                    or existing["symbol"] != receipt.symbol
                    or existing["receipt_sha256"] != receipt.receipt_sha256
                    or existing["payload_json"] != payload
                ):
                    raise ValueError(
                        "decision quality receipt is already bound to different evidence:"
                        f"{receipt.receipt_id}"
                    )
                return receipt
            conn.execute(
                """
                insert into decision_quality_receipts(
                    receipt_id, surface, symbol, receipt_sha256, payload_json, persisted_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    normalized_surface,
                    receipt.symbol,
                    receipt.receipt_sha256,
                    payload,
                    _utc_now(),
                ),
            )
            conn.commit()
        return receipt

    def list(
        self,
        *,
        surface: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if surface:
            clauses.append("surface=?")
            params.append(surface)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select receipt_id, surface, symbol, receipt_sha256, persisted_at, payload_json
                  from decision_quality_receipts
                  {where}
                 order by persisted_at desc
                """,
                tuple(params),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            receipt = DecisionDataQualityReceipt.model_validate_json(row["payload_json"])
            if (
                receipt.receipt_id != row["receipt_id"]
                or receipt.receipt_sha256 != row["receipt_sha256"]
                or receipt.symbol != row["symbol"]
            ):
                raise ValueError(
                    "stored decision quality receipt identity or hash mismatch:"
                    f" {row['receipt_id']}"
                )
            result.append(
                {
                    "surface": row["surface"],
                    "symbol": row["symbol"],
                    "persisted_at": row["persisted_at"],
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
        return result


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
