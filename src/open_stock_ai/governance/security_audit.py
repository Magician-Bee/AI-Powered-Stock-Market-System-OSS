"""Immutable security/trading audit events and order reconstruction receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .content_retention import ContentAddressedRetentionLedger, RetentionError
from .retention_archive import verify_critical_retention_archive


AUDIT_EVENT_SCHEMA = "stock_ai.security_trading_audit_event.v1"
ORDER_RECONSTRUCTION_SCHEMA = "stock_ai.order_audit_reconstruction_receipt.v1"
_TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED"}
_ALLOWED_TRANSITIONS = {
    "CREATED": {
        "VALIDATING",
        "RISK_PENDING",
        "USER_APPROVAL_PENDING",
        "SUBMITTING",
        "UNKNOWN_RECONCILIATION_REQUIRED",
    },
    "VALIDATING": {"RISK_PENDING", "USER_APPROVAL_PENDING", "SUBMITTING", "REJECTED"},
    "RISK_PENDING": {"USER_APPROVAL_PENDING", "SUBMITTING", "REJECTED"},
    "USER_APPROVAL_PENDING": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "UNKNOWN_RECONCILIATION_REQUIRED",
    },
    "ACKNOWLEDGED": {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED",
        "REJECTED",
        "UNKNOWN_RECONCILIATION_REQUIRED",
    },
    "PARTIALLY_FILLED": {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED",
        "UNKNOWN_RECONCILIATION_REQUIRED",
    },
    "CANCEL_PENDING": {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "UNKNOWN_RECONCILIATION_REQUIRED",
    },
    "UNKNOWN_RECONCILIATION_REQUIRED": {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "REJECTED",
    },
}
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
}


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _utc(value: str | None) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or datetime.now(timezone.utc).isoformat()))
    except ValueError as exc:
        raise RetentionError("security_audit_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise RetentionError("security_audit_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or _contains_sensitive_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


class SecurityTradingAuditTrail:
    """Append secret-free decisions to the shared durable retention authority."""

    def __init__(self, ledger: ContentAddressedRetentionLedger) -> None:
        if ledger.store is None:
            raise RetentionError("security_audit_requires_durable_retention_store")
        self.ledger = ledger

    def record(
        self,
        *,
        event_id: str,
        category: str,
        actor_id: str,
        subject_type: str,
        subject_id: str,
        decision: str,
        outcome: str,
        reason: str,
        evidence_sha256: str,
        details: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        identity = str(event_id or "").strip()
        fields = (category, actor_id, subject_type, subject_id, decision, outcome, reason)
        if not identity or any(not str(item or "").strip() for item in fields):
            raise RetentionError("security_audit_identity_invalid")
        evidence_hash = str(evidence_sha256 or "").lower()
        if len(evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in evidence_hash):
            raise RetentionError("security_audit_evidence_hash_invalid")
        safe_details = dict(details or {})
        if _contains_sensitive_key(safe_details):
            raise RetentionError("security_audit_sensitive_field_rejected")
        payload = {
            "schema_version": AUDIT_EVENT_SCHEMA,
            "event_id": identity,
            "category": str(category).strip(),
            "actor_id": str(actor_id).strip(),
            "subject_type": str(subject_type).strip(),
            "subject_id": str(subject_id).strip(),
            "decision": str(decision).strip(),
            "outcome": str(outcome).strip(),
            "reason": str(reason).strip(),
            "evidence_sha256": evidence_hash,
            "details": safe_details,
            "occurred_at": _utc(occurred_at),
        }
        return self.ledger.append(
            f"security-audit-{identity}",
            payload,
            critical=True,
            kind="security_audit",
            occurred_at=payload["occurred_at"],
        )


def _analyze_order(archive: Mapping[str, Any], intent_id: str) -> dict[str, Any]:
    if not verify_critical_retention_archive(archive):
        raise RetentionError("order_reconstruction_archive_invalid")
    normalized_id = str(intent_id or "").strip()
    if not normalized_id:
        raise RetentionError("order_reconstruction_intent_id_required")
    snapshots: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    audit_events: list[dict[str, Any]] = []
    for retained in archive.get("records") or []:
        payload = retained.get("payload") or {}
        if payload.get("schema_version") == "stock_ai.broker_oms_execution_state.v1":
            if str((payload.get("intent") or {}).get("intent_id") or "") == normalized_id:
                snapshots.append((dict(retained), payload))
        elif (
            payload.get("schema_version") == AUDIT_EVENT_SCHEMA
            and payload.get("subject_type") == "broker_order"
            and payload.get("subject_id") == normalized_id
        ):
            audit_events.append(
                {
                    "record_id": retained["record_id"],
                    "content_sha256": retained["content_sha256"],
                    "event_id": payload["event_id"],
                    "category": payload["category"],
                    "decision": payload["decision"],
                    "outcome": payload["outcome"],
                    "evidence_sha256": payload["evidence_sha256"],
                    "occurred_at": payload["occurred_at"],
                }
            )
    snapshots.sort(key=lambda item: (str(item[1].get("updated_at") or ""), item[0]["record_id"]))
    blockers: list[str] = []
    if not snapshots:
        blockers.append("order_state_records_missing")
        intent: dict[str, Any] = {}
        intent_hash = ""
        timeline: list[dict[str, Any]] = []
    else:
        intent = dict(snapshots[0][1].get("intent") or {})
        intent_hash = _digest(intent)
        if any(_digest(dict(payload.get("intent") or {})) != intent_hash for _, payload in snapshots):
            blockers.append("order_intent_changed_across_audit_records")
        timeline = [
            {
                "record_id": retained["record_id"],
                "content_sha256": retained["content_sha256"],
                "state": str(payload.get("state") or ""),
                "updated_at": str(payload.get("updated_at") or ""),
                "broker_order_id": payload.get("broker_order_id"),
                "report_ids": list(payload.get("report_ids") or []),
            }
            for retained, payload in snapshots
        ]
        states = [item["state"] for item in timeline]
        if states[0] != "CREATED":
            blockers.append("order_initial_state_missing")
        for previous, current in zip(states, states[1:]):
            if current != previous and current not in _ALLOWED_TRANSITIONS.get(previous, set()):
                blockers.append("order_state_transition_invalid")
                break
        if states[-1] not in _TERMINAL_STATES:
            blockers.append("order_terminal_state_missing")
    if not audit_events:
        blockers.append("order_security_decision_missing")
    elif intent_hash and not any(item["evidence_sha256"] == intent_hash for item in audit_events):
        blockers.append("order_security_decision_not_bound_to_intent")
    return {
        "archive_sha256": str(archive.get("archive_sha256") or ""),
        "intent_id": normalized_id,
        "intent_sha256": intent_hash,
        "intent": intent,
        "state_timeline": timeline,
        "security_decisions": sorted(
            audit_events, key=lambda item: (item["occurred_at"], item["record_id"])
        ),
        "source_record_ids": sorted(
            [item["record_id"] for item, _ in snapshots]
            + [item["record_id"] for item in audit_events]
        ),
        "final_state": timeline[-1]["state"] if timeline else None,
        "blockers": blockers,
        "complete": not blockers,
    }


def reconstruct_order_audit(
    archive: Mapping[str, Any],
    intent_id: str,
    *,
    reconstructed_at: str | None = None,
) -> dict[str, Any]:
    analysis = _analyze_order(archive, intent_id)
    payload = {
        "schema_version": ORDER_RECONSTRUCTION_SCHEMA,
        "reconstructed_at": _utc(reconstructed_at),
        **analysis,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_order_audit_reconstruction(
    receipt: Mapping[str, Any], archive: Mapping[str, Any]
) -> bool:
    required = {
        "schema_version",
        "reconstructed_at",
        "archive_sha256",
        "intent_id",
        "intent_sha256",
        "intent",
        "state_timeline",
        "security_decisions",
        "source_record_ids",
        "final_state",
        "blockers",
        "complete",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != ORDER_RECONSTRUCTION_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    try:
        _utc(str(receipt.get("reconstructed_at") or ""))
        analysis = _analyze_order(archive, str(receipt.get("intent_id") or ""))
    except RetentionError:
        return False
    return all(receipt.get(key) == value for key, value in analysis.items())


__all__ = [
    "AUDIT_EVENT_SCHEMA",
    "ORDER_RECONSTRUCTION_SCHEMA",
    "SecurityTradingAuditTrail",
    "reconstruct_order_audit",
    "verify_order_audit_reconstruction",
]
