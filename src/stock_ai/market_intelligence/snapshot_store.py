from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from open_stock_ai.config.settings import load_settings
from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from stock_ai.config import get_settings
from stock_ai.data_quality_contracts import DecisionDataQualityReceipt

from .contracts import MarketIntelligenceSnapshot, WorkspaceContext, utc_now


def _database_path() -> Path:
    configured = Path(load_settings().sqlite_path).expanduser()
    if not configured.is_absolute():
        configured = get_settings().project_root / configured
    configured.parent.mkdir(parents=True, exist_ok=True)
    return configured


class SnapshotStore:
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
                create table if not exists market_intelligence_snapshots (
                    snapshot_id text primary key,
                    status text not null,
                    generated_at text not null,
                    data_as_of text not null,
                    model_status text not null,
                    payload_json text not null,
                    created_at text not null
                );
                create index if not exists idx_market_intelligence_snapshots_latest
                    on market_intelligence_snapshots(created_at desc);
                create index if not exists idx_market_intelligence_snapshots_model
                    on market_intelligence_snapshots(model_status, created_at desc);

                create table if not exists market_decision_quality_receipts (
                    receipt_id text primary key,
                    snapshot_id text not null
                        references market_intelligence_snapshots(snapshot_id),
                    symbol text not null,
                    receipt_sha256 text not null unique,
                    payload_json text not null,
                    persisted_at text not null,
                    unique(snapshot_id, symbol)
                );
                create index if not exists idx_market_decision_quality_receipts_snapshot
                    on market_decision_quality_receipts(snapshot_id, symbol);
                create trigger if not exists trg_market_decision_quality_receipts_immutable_update
                    before update on market_decision_quality_receipts
                    begin select raise(abort, 'market decision quality receipts are immutable'); end;
                create trigger if not exists trg_market_decision_quality_receipts_immutable_delete
                    before delete on market_decision_quality_receipts
                    begin select raise(abort, 'market decision quality receipts are immutable'); end;

                create table if not exists market_intelligence_scans (
                    scan_id text primary key,
                    status text not null,
                    requested_at text not null,
                    started_at text,
                    completed_at text,
                    snapshot_id text,
                    trigger_json text not null,
                    error_json text,
                    foreign key(snapshot_id)
                        references market_intelligence_snapshots(snapshot_id)
                );

                create table if not exists workspace_contexts (
                    context_id text primary key,
                    payload_json text not null,
                    updated_at text not null
                );

                create table if not exists workspace_alerts (
                    alert_id text primary key,
                    symbol text not null,
                    alert_type text not null,
                    rule_json text not null,
                    status text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists workspace_comparisons (
                    comparison_id text primary key,
                    symbols_json text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists user_watchlists (
                    watchlist_id text primary key,
                    user_id text not null,
                    name text not null,
                    created_at text not null,
                    updated_at text not null,
                    metadata_json text not null,
                    unique(user_id, name)
                );
                create table if not exists user_watchlist_symbols (
                    watchlist_id text not null
                        references user_watchlists(watchlist_id) on delete cascade,
                    symbol text not null,
                    added_at text not null,
                    provenance_json text not null,
                    primary key(watchlist_id, symbol)
                );
                """
            )
            conn.commit()

    def save_snapshot(
        self, snapshot: MarketIntelligenceSnapshot
    ) -> MarketIntelligenceSnapshot:
        payload = snapshot.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into market_intelligence_snapshots (
                    snapshot_id, status, generated_at, data_as_of,
                    model_status, payload_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(snapshot_id) do update set
                    status=excluded.status,
                    generated_at=excluded.generated_at,
                    data_as_of=excluded.data_as_of,
                    model_status=excluded.model_status,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.status,
                    snapshot.generated_at,
                    snapshot.data_as_of,
                    snapshot.model_overlay.status,
                    payload,
                    utc_now(),
                ),
            )
            self._record_decision_quality_receipts(conn, snapshot)
            conn.commit()
        return snapshot

    @staticmethod
    def _record_decision_quality_receipts(
        conn: sqlite3.Connection,
        snapshot: MarketIntelligenceSnapshot,
    ) -> None:
        """Persist each decision's quality evidence once and reject rewrites."""

        declared_schema = str(snapshot.data_quality.get("decision_receipt_schema") or "")
        if declared_schema:
            if declared_schema != "stock_ai.decision_data_quality_receipt.v1":
                raise ValueError(f"unsupported decision quality receipt schema: {declared_schema}")
            expected_count = len(snapshot.candidate_details)
            declared_count = snapshot.data_quality.get("decision_receipt_count")
            if declared_count != expected_count:
                raise ValueError(
                    "market decision quality receipt count mismatch:"
                    f" declared={declared_count!r} expected={expected_count}"
                )
            missing = sorted(
                symbol
                for symbol, candidate in snapshot.candidate_details.items()
                if candidate.data_quality_receipt is None
            )
            if missing:
                raise ValueError(
                    "market decision quality receipt missing for declared v1 snapshot:"
                    f" {', '.join(missing)}"
                )

        for symbol, candidate in snapshot.candidate_details.items():
            receipt = candidate.data_quality_receipt
            if receipt is None:
                # Historical snapshots predate the receipt contract.  They
                # remain readable and can receive model overlays, but new
                # snapshots issued by MarketIntelligenceService always carry
                # one receipt per decision.
                continue
            if receipt.snapshot_id != snapshot.snapshot_id or receipt.symbol != symbol:
                raise ValueError(
                    "market decision quality receipt identity mismatch:"
                    f" snapshot={snapshot.snapshot_id}:{symbol}"
                    f" receipt={receipt.snapshot_id}:{receipt.symbol}"
                )
            payload = receipt.model_dump_json()
            existing = conn.execute(
                """
                select receipt_id, receipt_sha256, payload_json
                  from market_decision_quality_receipts
                 where snapshot_id=? and symbol=?
                """,
                (snapshot.snapshot_id, symbol),
            ).fetchone()
            if existing is not None:
                if (
                    existing["receipt_id"] != receipt.receipt_id
                    or existing["receipt_sha256"] != receipt.receipt_sha256
                    or existing["payload_json"] != payload
                ):
                    raise ValueError(
                        "market decision quality receipt is already bound to different evidence:"
                        f"{snapshot.snapshot_id}:{symbol}"
                    )
                continue
            conn.execute(
                """
                insert into market_decision_quality_receipts(
                    receipt_id, snapshot_id, symbol, receipt_sha256, payload_json, persisted_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    snapshot.snapshot_id,
                    symbol,
                    receipt.receipt_sha256,
                    payload,
                    utc_now(),
                ),
            )

    def latest_snapshot(self) -> MarketIntelligenceSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload_json
                  from market_intelligence_snapshots
                 order by created_at desc
                 limit 1
                """
            ).fetchone()
        return (
            MarketIntelligenceSnapshot.model_validate_json(row["payload_json"])
            if row
            else None
        )

    def snapshot(self, snapshot_id: str) -> MarketIntelligenceSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload_json
                  from market_intelligence_snapshots
                 where snapshot_id=?
                """,
                (snapshot_id,),
            ).fetchone()
        return (
            MarketIntelligenceSnapshot.model_validate_json(row["payload_json"])
            if row
            else None
        )

    def decision_quality_receipts(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select receipt_id, symbol, receipt_sha256, payload_json
                  from market_decision_quality_receipts
                 where snapshot_id=?
                 order by symbol
                """,
                (snapshot_id,),
            ).fetchall()
        receipts: list[dict[str, Any]] = []
        for row in rows:
            receipt = DecisionDataQualityReceipt.model_validate_json(row["payload_json"])
            if (
                receipt.receipt_id != row["receipt_id"]
                or receipt.receipt_sha256 != row["receipt_sha256"]
                or receipt.snapshot_id != snapshot_id
                or receipt.symbol != row["symbol"]
            ):
                raise ValueError(
                    "stored market decision quality receipt identity or hash mismatch:"
                    f" {snapshot_id}:{row['symbol']}"
                )
            receipts.append(receipt.model_dump(mode="json"))
        return receipts

    def last_successful_model_overlay(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select payload_json
                  from market_intelligence_snapshots
                 where model_status='succeeded'
                 order by created_at desc
                 limit 1
                """
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        overlay = payload.get("model_overlay")
        return overlay if isinstance(overlay, dict) else None

    def create_scan(self, scan_id: str, trigger: dict[str, Any]) -> dict[str, Any]:
        requested = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into market_intelligence_scans (
                    scan_id, status, requested_at, trigger_json
                ) values (?, 'queued', ?, ?)
                """,
                (scan_id, requested, json.dumps(trigger, ensure_ascii=False)),
            )
            conn.commit()
        return self.scan(scan_id) or {}

    def start_scan(self, scan_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update market_intelligence_scans
                   set status='running', started_at=?
                 where scan_id=?
                """,
                (utc_now(), scan_id),
            )
            conn.commit()

    def complete_scan(self, scan_id: str, snapshot_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update market_intelligence_scans
                   set status='completed', completed_at=?, snapshot_id=?
                 where scan_id=?
                """,
                (utc_now(), snapshot_id, scan_id),
            )
            conn.commit()

    def fail_scan(self, scan_id: str, error: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                update market_intelligence_scans
                   set status='failed', completed_at=?, error_json=?
                 where scan_id=?
                """,
                (utc_now(), json.dumps(error, ensure_ascii=False), scan_id),
            )
            conn.commit()

    def scan(self, scan_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from market_intelligence_scans where scan_id=?",
                (scan_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "schema_version": "stock_ai.market_intelligence_scan.v1",
            "scan_id": row["scan_id"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "snapshot_id": row["snapshot_id"],
            "trigger": json.loads(row["trigger_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    def context(self, context_id: str = "default") -> WorkspaceContext:
        with self._connect() as conn:
            row = conn.execute(
                "select payload_json from workspace_contexts where context_id=?",
                (context_id,),
            ).fetchone()
        return (
            WorkspaceContext.model_validate_json(row["payload_json"])
            if row
            else WorkspaceContext()
        )

    def save_context(
        self,
        context: WorkspaceContext,
        context_id: str = "default",
    ) -> WorkspaceContext:
        context.updated_at = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into workspace_contexts(context_id, payload_json, updated_at)
                values (?, ?, ?)
                on conflict(context_id) do update set
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (context_id, context.model_dump_json(), context.updated_at),
            )
            conn.commit()
        return context

    def create_alert(
        self,
        *,
        alert_id: str,
        symbol: str,
        alert_type: str,
        rule: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into workspace_alerts(
                    alert_id, symbol, alert_type, rule_json,
                    status, created_at, updated_at
                ) values (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    alert_id,
                    symbol,
                    alert_type,
                    json.dumps(rule, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return {
            "schema_version": "stock_ai.workspace_alert.v1",
            "alert_id": alert_id,
            "symbol": symbol,
            "alert_type": alert_type,
            "rule": rule,
            "status": "active",
            "created_at": now,
        }

    def active_alerts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select alert_id, symbol, alert_type, rule_json,
                       status, created_at, updated_at
                  from workspace_alerts
                 where status='active'
                 order by updated_at desc
                 limit ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [
            {
                "alert_id": row["alert_id"],
                "symbol": row["symbol"],
                "alert_type": row["alert_type"],
                "rule": json.loads(row["rule_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def create_comparison(
        self,
        *,
        comparison_id: str,
        symbols: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into workspace_comparisons(
                    comparison_id, symbols_json, created_at, updated_at
                ) values (?, ?, ?, ?)
                """,
                (
                    comparison_id,
                    json.dumps(symbols, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return {
            "schema_version": "stock_ai.workspace_comparison.v1",
            "comparison_id": comparison_id,
            "symbols": symbols,
            "created_at": now,
        }

    def add_watchlist_symbol(
        self,
        *,
        watchlist_id: str,
        symbol: str,
        user_id: str = "local-user",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into user_watchlists(
                    watchlist_id, user_id, name, created_at, updated_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?)
                on conflict(watchlist_id) do update set updated_at=excluded.updated_at
                """,
                (
                    watchlist_id,
                    user_id,
                    "首頁自選",
                    now,
                    now,
                    json.dumps(
                        {"origin": "home_market_workspace"},
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute(
                """
                insert into user_watchlist_symbols(
                    watchlist_id, symbol, added_at, provenance_json
                ) values (?, ?, ?, ?)
                on conflict(watchlist_id, symbol) do nothing
                """,
                (
                    watchlist_id,
                    symbol,
                    now,
                    json.dumps(
                        {"origin": "user_action", "surface": "home"},
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.commit()
        return {
            "schema_version": "stock_ai.watchlist_mutation.v1",
            "watchlist_id": watchlist_id,
            "symbol": symbol,
            "status": "added",
            "updated_at": now,
        }

    def watchlist_symbols(
        self,
        watchlist_id: str = "home-default",
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select s.symbol, s.added_at, s.provenance_json,
                       w.watchlist_id, w.name
                  from user_watchlist_symbols s
                  join user_watchlists w on w.watchlist_id=s.watchlist_id
                 where s.watchlist_id=?
                 order by s.added_at desc
                 limit ?
                """,
                (watchlist_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "watchlist_id": row["watchlist_id"],
                "watchlist_name": row["name"],
                "symbol": row["symbol"],
                "added_at": row["added_at"],
                "provenance": json.loads(row["provenance_json"]),
            }
            for row in rows
        ]
