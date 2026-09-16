from __future__ import annotations

"""Authoritative durable store for receipt-backed research experiments."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, configure_connection


class ExperimentStore:
    """Persist immutable experiment entities independently from model state.

    The experiment table remains in the shared authoritative SQLite database,
    but experiment-specific validation and writes live behind this store.
    ``SQLiteStore`` keeps a compatibility facade for older callers and routes
    it here instead of maintaining a second writer implementation.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = (
            "experiment_id", "request_symbol", "request_market", "request_horizon",
            "dataset_manifest_hash", "dataset_sha256", "runtime_receipt_sha256", "created_at",
        )
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError(f"research_experiment_missing:{','.join(missing)}")
        model_version_ids = payload.get("model_version_ids")
        reproducibility = payload.get("reproducibility")
        if not isinstance(model_version_ids, list) or not model_version_ids:
            raise ValueError("research_experiment_model_versions_required")
        if not isinstance(reproducibility, dict):
            raise ValueError("research_experiment_reproducibility_required")
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        with self._connect() as conn:
            existing = conn.execute(
                "select payload_json from research_experiment_receipts where experiment_id=?",
                (payload["experiment_id"],),
            ).fetchone()
            if existing is not None:
                prior = json.loads(existing[0])
                identity_keys = (
                    "schema_version", "experiment_id", "request_symbol", "request_market",
                    "request_horizon", "dataset_manifest_hash", "dataset_sha256",
                    "runtime_receipt_sha256", "model_version_ids", "reproducibility", "runtime_receipt",
                )
                if any(prior.get(key) != payload.get(key) for key in identity_keys):
                    raise ValueError("research_experiment_immutable_conflict")
                return {"saved": False, "already_exists": True, "experiment_id": payload["experiment_id"]}
            conn.execute(
                """
                insert into research_experiment_receipts (
                    experiment_id, request_symbol, request_market, request_horizon,
                    dataset_manifest_hash, dataset_sha256, runtime_receipt_sha256,
                    model_version_ids_json, reproducibility_json, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *(payload[key] for key in required[:7]),
                    json.dumps(model_version_ids, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(reproducibility, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    payload["created_at"],
                    encoded,
                ),
            )
            conn.commit()
        return {"saved": True, "already_exists": False, "experiment_id": payload["experiment_id"]}

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select payload_json from research_experiment_receipts where experiment_id=?",
                (str(experiment_id),),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "select payload_json from research_experiment_receipts order by created_at desc limit ?",
                (bounded,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


__all__ = ["ExperimentStore"]
