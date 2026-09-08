"""Durable, append-only persistence for release-governance receipts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import ManagedSQLiteConnection
from open_stock_ai.storage.sqlite_store import SQLiteStore


class SQLiteGovernanceStore:
    """Persist governance state in the same migrated SQLite authority as runtime data.

    The registry classes keep their in-memory mode for small deterministic unit
    tests, while production callers can provide this adapter.  All writes are
    insert-only and the schema triggers reject updates and deletes.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        SQLiteStore(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, factory=ManagedSQLiteConnection)
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        return connection

    @staticmethod
    def _decode(rows: list[tuple[str]]) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in rows]

    def load_approved_artifacts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_approved_artifacts order by approved_at, artifact_id"
            ).fetchall()
        return self._decode(rows)

    def load_activation_history(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "select artifact_id from governed_artifact_activations order by activation_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def load_rollback_receipts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_artifact_rollback_receipts order by occurred_at, receipt_sha256"
            ).fetchall()
        return self._decode(rows)

    def save_approved_artifact(
        self,
        payload: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        def write(active_connection: sqlite3.Connection) -> None:
            existing = active_connection.execute(
                "select payload_json from governed_approved_artifacts where artifact_id=?",
                (payload["artifact_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("approved_artifact_durable_immutable_conflict")
                return
            active_connection.execute(
                """
                insert into governed_approved_artifacts(
                    artifact_id, artifact_sha256, approved_by, approved_at,
                    metadata_json, payload_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["artifact_id"],
                    payload["artifact_sha256"],
                    payload["approved_by"],
                    payload["approved_at"],
                    json.dumps(payload.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                    encoded,
                ),
            )

        if connection is not None:
            write(connection)
            return
        with self._connect() as active_connection:
            write(active_connection)

    def save_activation(
        self,
        artifact_id: str,
        activated_at: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def write(active_connection: sqlite3.Connection) -> None:
            active_connection.execute(
                "insert into governed_artifact_activations(artifact_id, activated_at) values (?, ?)",
                (artifact_id, activated_at),
            )

        if connection is not None:
            write(connection)
            return
        with self._connect() as active_connection:
            write(active_connection)

    def save_rollback(
        self,
        receipt: dict[str, Any],
        target_artifact_id: str,
        activated_at: str,
    ) -> None:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "insert into governed_artifact_rollback_receipts(receipt_sha256, occurred_at, payload_json) values (?, ?, ?)",
                (receipt["receipt_sha256"], receipt["occurred_at"], encoded),
            )
            connection.execute(
                "insert into governed_artifact_activations(artifact_id, activated_at) values (?, ?)",
                (target_artifact_id, activated_at),
            )

    def load_change_sets(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_change_sets order by approved_at, change_id"
            ).fetchall()
        return self._decode(rows)

    def load_order_bindings(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_order_version_bindings order by bound_at, order_id"
            ).fetchall()
        return self._decode(rows)

    def save_change_set(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from governed_change_sets where change_id=?",
                (payload["change_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("change_set_durable_immutable_conflict")
                return
            connection.execute(
                """
                insert into governed_change_sets(
                    change_id, code_sha256, model_sha256, data_sha256,
                    risk_policy_sha256, approved_by, approved_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["change_id"], payload["code_sha256"], payload["model_sha256"],
                    payload["data_sha256"], payload["risk_policy_sha256"],
                    payload["approved_by"], payload["approved_at"], encoded,
                ),
            )

    def save_order_binding(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from governed_order_version_bindings where order_id=?",
                (payload["order_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("order_version_binding_durable_immutable_conflict")
                return
            connection.execute(
                """
                insert into governed_order_version_bindings(
                    order_id, change_id, change_sha256, code_sha256, model_sha256,
                    data_sha256, risk_policy_sha256, bound_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["order_id"], payload["change_id"], payload["change_sha256"],
                    payload["code_sha256"], payload["model_sha256"], payload["data_sha256"],
                    payload["risk_policy_sha256"], payload["bound_at"], encoded,
                ),
            )


class SQLiteRetentionStore:
    """Persist bounded projections and retention receipts in the runtime DB."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        SQLiteStore(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, factory=ManagedSQLiteConnection)
        connection.execute("pragma foreign_keys = on")
        connection.execute("pragma busy_timeout = 5000")
        return connection

    @staticmethod
    def _decode(rows: list[tuple[str]]) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in rows]

    def load_records(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_retained_content order by occurred_at, record_id"
            ).fetchall()
        return self._decode(rows)

    def load_receipts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from governed_retention_receipts order by occurred_at, receipt_sha256"
            ).fetchall()
        return self._decode(rows)

    def save_record(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from governed_retained_content where record_id=?",
                (payload["record_id"],),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("retention_record_durable_immutable_conflict")
                return
            connection.execute(
                """
                insert into governed_retained_content(
                    record_id, kind, critical, content_sha256, occurred_at, payload_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["record_id"], payload["kind"], 1 if payload["critical"] else 0,
                    payload["content_sha256"], payload["occurred_at"], encoded,
                ),
            )

    def prune_and_save_receipt(
        self,
        record_ids: list[str],
        receipt: dict[str, Any],
    ) -> None:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in record_ids)
            if record_ids:
                connection.execute(
                    f"delete from governed_retained_content where record_id in ({placeholders}) and critical=0",
                    tuple(record_ids),
                )
            connection.execute(
                "insert into governed_retention_receipts(receipt_sha256, occurred_at, payload_json) values (?, ?, ?)",
                (receipt["receipt_sha256"], receipt["occurred_at"], encoded),
            )
