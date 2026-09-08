"""Fail-closed rollback to the previous human-approved strategy/model artifact."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "open_stock_ai.approved_artifact_rollback_receipt.v1"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value.casefold())


@dataclass(frozen=True, slots=True)
class RollbackReceipt:
    from_artifact_id: str
    to_artifact_id: str
    from_artifact_sha256: str
    to_artifact_sha256: str
    reason: str
    approved_by: str
    occurred_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "rollback",
            "from_artifact_id": self.from_artifact_id,
            "to_artifact_id": self.to_artifact_id,
            "from_artifact_sha256": self.from_artifact_sha256,
            "to_artifact_sha256": self.to_artifact_sha256,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "occurred_at": self.occurred_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.receipt_sha256)


class ArtifactRollbackError(PermissionError):
    """Raised when an approved artifact cannot be activated or restored safely."""


class ApprovedArtifactRollbackRegistry:
    """Keep an append-only approved artifact history and restore its prior entry.

    The optional store makes approvals, activation history and rollback
    receipts survive process restarts.  In-memory mode remains available for
    isolated tests and callers that have not yet migrated their writers.
    """

    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        self._approved: dict[str, dict[str, Any]] = {}
        self._activation_history: list[str] = []
        self.receipts: list[RollbackReceipt] = []
        if store is not None:
            for record in store.load_approved_artifacts():
                artifact_id = str(record.get("artifact_id") or "")
                if not artifact_id or set(record) != {
                    "schema_version", "artifact_id", "artifact_sha256", "approved_by", "approved_at", "metadata"
                }:
                    raise ArtifactRollbackError("approved_artifact_durable_record_invalid")
                self._approved[artifact_id] = dict(record)
            self._activation_history = list(store.load_activation_history())
            for payload in store.load_rollback_receipts():
                receipt = RollbackReceipt(
                    from_artifact_id=str(payload["from_artifact_id"]),
                    to_artifact_id=str(payload["to_artifact_id"]),
                    from_artifact_sha256=str(payload["from_artifact_sha256"]),
                    to_artifact_sha256=str(payload["to_artifact_sha256"]),
                    reason=str(payload["reason"]),
                    approved_by=str(payload["approved_by"]),
                    occurred_at=str(payload["occurred_at"]),
                    receipt_sha256=str(payload["receipt_sha256"]),
                )
                if not receipt.verify():
                    raise ArtifactRollbackError("artifact_rollback_durable_receipt_invalid")
                self.receipts.append(receipt)

    @property
    def current_artifact_id(self) -> str | None:
        return self._activation_history[-1] if self._activation_history else None

    def current_artifact_id_for(self, artifact_scope: str) -> str | None:
        """Return the current artifact for one deployment lane.

        Strategy and model deployments share the same durable audit database,
        but they must not roll one another back.  The legacy unscoped property
        remains available for compatibility; new runtime controls must name a
        lane explicitly.
        """

        history = self._history_for_scope(artifact_scope)
        return history[-1] if history else None

    def status(self) -> dict[str, Any]:
        """Expose a non-secret, durable projection for operator controls."""

        return {
            "schema_version": "open_stock_ai.approved_artifact_rollback_status.v1",
            "current_artifact_id": self.current_artifact_id,
            "activation_count": len(self._activation_history),
            "receipt_count": len(self.receipts),
            "scopes": {
                scope: {
                    "current_artifact_id": self.current_artifact_id_for(scope),
                    "activation_count": len(self._history_for_scope(scope)),
                }
                for scope in ("strategy", "model")
            },
        }

    def approve(
        self,
        artifact_id: str,
        artifact_sha256: str,
        *,
        approved_by: str,
        approved_at: str,
        metadata: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        artifact_id = str(artifact_id or "").strip()
        actor = str(approved_by or "").strip()
        if not artifact_id or not _is_sha256(artifact_sha256):
            raise ArtifactRollbackError("approved_artifact_identity_or_hash_invalid")
        if not actor or actor.casefold() in {"agent", "codex", "model", "ai", "system"}:
            raise ArtifactRollbackError("human_approval_required_for_artifact")
        if not str(approved_at or "").strip():
            raise ArtifactRollbackError("approved_artifact_timestamp_required")
        record = {
            "schema_version": "open_stock_ai.approved_artifact.v1",
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_sha256.casefold(),
            "approved_by": actor,
            "approved_at": approved_at,
            "metadata": dict(metadata or {}),
        }
        existing = self._approved.get(artifact_id)
        if existing is not None and existing != record:
            raise ArtifactRollbackError("approved_artifact_immutable_conflict")
        if self.store is not None:
            try:
                self.store.save_approved_artifact(record, connection=connection)
            except ValueError as exc:
                raise ArtifactRollbackError(str(exc)) from exc
        self._approved[artifact_id] = record
        return dict(record)

    def activate(
        self,
        artifact_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        artifact = self._approved.get(str(artifact_id or "").strip())
        if artifact is None:
            raise ArtifactRollbackError("approved_artifact_not_found")
        if not self._activation_history or self._activation_history[-1] != artifact["artifact_id"]:
            if self.store is not None:
                self.store.save_activation(
                    artifact["artifact_id"],
                    datetime.now(timezone.utc).isoformat(),
                    connection=connection,
                )
            self._activation_history.append(artifact["artifact_id"])
        return dict(artifact)

    def rollback(
        self,
        *,
        reason: str,
        approved_by: str = "host",
        artifact_scope: str | None = None,
        now: datetime | None = None,
    ) -> RollbackReceipt:
        """Restore the prior approved artifact, optionally within one lane."""

        history = (
            self._history_for_scope(artifact_scope)
            if artifact_scope is not None
            else list(self._activation_history)
        )
        if len(history) < 2:
            raise ArtifactRollbackError("previous_approved_artifact_not_available")
        reason = str(reason or "").strip()
        if not reason:
            raise ArtifactRollbackError("artifact_rollback_reason_required")
        actor = str(approved_by or "").strip()
        if not actor or actor.casefold() in {"agent", "codex", "model", "ai", "system"}:
            raise ArtifactRollbackError("human_approval_required_for_artifact_rollback")
        from_artifact = self._approved[history[-1]]
        target_id = history[-2]
        target_artifact = self._approved[target_id]
        occurred_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "action": "rollback",
            "from_artifact_id": from_artifact["artifact_id"],
            "to_artifact_id": target_artifact["artifact_id"],
            "from_artifact_sha256": from_artifact["artifact_sha256"],
            "to_artifact_sha256": target_artifact["artifact_sha256"],
            "reason": reason,
            "approved_by": actor,
            "occurred_at": occurred_at,
        }
        receipt = RollbackReceipt(
            from_artifact_id=from_artifact["artifact_id"],
            to_artifact_id=target_artifact["artifact_id"],
            from_artifact_sha256=from_artifact["artifact_sha256"],
            to_artifact_sha256=target_artifact["artifact_sha256"],
            reason=reason,
            approved_by=actor,
            occurred_at=occurred_at,
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        if self.store is not None:
            try:
                self.store.save_rollback(receipt.as_dict(), target_artifact["artifact_id"], occurred_at)
            except (sqlite3.IntegrityError, ValueError) as exc:
                raise ArtifactRollbackError("artifact_rollback_durable_write_failed") from exc
        self._activation_history.append(target_artifact["artifact_id"])
        self.receipts.append(receipt)
        return receipt

    def _history_for_scope(self, artifact_scope: str) -> list[str]:
        normalized_scope = str(artifact_scope or "").strip().casefold()
        if normalized_scope not in {"strategy", "model"}:
            raise ArtifactRollbackError("artifact_rollback_scope_invalid")
        return [
            artifact_id
            for artifact_id in self._activation_history
            if self._scope_for(self._approved[artifact_id]) == normalized_scope
        ]

    @staticmethod
    def _scope_for(artifact: dict[str, Any]) -> str:
        metadata = artifact.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        explicit = str(metadata.get("artifact_scope") or "").strip().casefold()
        if explicit in {"strategy", "model"}:
            return explicit
        if str(metadata.get("strategy_id") or "").strip():
            return "strategy"
        if str(metadata.get("framework") or "").strip():
            return "model"
        return "generic"


def verify_rollback_receipt(receipt: dict[str, Any]) -> bool:
    required = {
        "schema_version", "action", "from_artifact_id", "to_artifact_id",
        "from_artifact_sha256", "to_artifact_sha256", "reason", "approved_by",
        "occurred_at", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("action") != "rollback":
        return False
    expected = hashlib.sha256(_canonical({key: receipt[key] for key in required if key != "receipt_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(receipt.get("receipt_sha256") or ""))
