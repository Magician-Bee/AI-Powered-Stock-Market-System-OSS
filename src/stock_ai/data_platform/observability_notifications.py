from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


NotificationSender = Callable[[Mapping[str, Any]], Mapping[str, Any] | bool]


class ObservabilityNotificationLedger:
    """Durable, append-only delivery attempts for data-operations alerts.

    This ledger deliberately separates an alert being detected from an
    operator channel accepting a message.  A missing sender therefore creates
    a replayable ``not_configured`` receipt instead of silently dropping the
    alert or claiming that email/Slack delivery occurred.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists data_observability_notifications (
                    notification_id text primary key,
                    alert_id text not null,
                    attempt integer not null,
                    created_at text not null,
                    source_id text not null,
                    dataset text not null,
                    severity text not null,
                    code text not null,
                    status text not null check(status in ('delivered', 'failed', 'not_configured')),
                    reason text,
                    payload_json text not null,
                    provider_receipt_json text not null,
                    receipt_sha256 text not null unique,
                    unique(alert_id, attempt)
                );
                create trigger if not exists trg_data_observability_notifications_immutable_update
                before update on data_observability_notifications
                begin
                    select raise(abort, 'data observability notification receipts are immutable');
                end;
                create trigger if not exists trg_data_observability_notifications_immutable_delete
                before delete on data_observability_notifications
                begin
                    select raise(abort, 'data observability notification receipts are immutable');
                end;
                """
            )

    def dispatch(
        self,
        alerts: list[Mapping[str, Any]],
        *,
        sender: NotificationSender | None = None,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        created_at = now or datetime.now(timezone.utc).isoformat()
        receipts: list[dict[str, Any]] = []
        for alert in alerts:
            normalized = self._normalize_alert(alert)
            existing = self._latest(normalized["alert_id"])
            if existing is not None and existing["status"] == "delivered":
                receipts.append(existing)
                continue
            attempt = int(existing["attempt"] or 0) + 1 if existing else 1
            payload = {
                "schema_version": "stock_ai.data_observability_notification.v1",
                "alert": normalized,
                "attempt": attempt,
                "created_at": created_at,
            }
            status = "not_configured"
            reason = "operator_notification_sender_not_configured"
            provider_receipt: dict[str, Any] = {}
            if sender is not None:
                try:
                    raw = sender(payload)
                    provider_receipt = (
                        dict(raw) if isinstance(raw, Mapping) else {"accepted": bool(raw)}
                    )
                    if bool(provider_receipt.get("accepted", True)):
                        status = "delivered"
                        reason = None
                    else:
                        status = "failed"
                        reason = str(provider_receipt.get("error") or "operator channel rejected alert")
                except Exception as exc:  # provider isolation; receipt remains durable
                    status = "failed"
                    reason = f"{type(exc).__name__}: {exc}"
            receipt = self._insert(
                normalized,
                attempt=attempt,
                created_at=created_at,
                status=status,
                reason=reason,
                payload=payload,
                provider_receipt=provider_receipt,
            )
            receipts.append(receipt)
        return receipts

    def list(self, *, alert_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            if alert_id:
                rows = conn.execute(
                    "select * from data_observability_notifications where alert_id = ? order by attempt desc limit ?",
                    (str(alert_id), bounded),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select * from data_observability_notifications order by created_at desc, attempt desc limit ?",
                    (bounded,),
                ).fetchall()
            return [self._row(row) for row in rows]

    def _latest(self, alert_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from data_observability_notifications where alert_id = ? order by attempt desc limit 1",
                (alert_id,),
            ).fetchone()
            return self._row(row) if row is not None else None

    def _insert(
        self,
        alert: dict[str, Any],
        *,
        attempt: int,
        created_at: str,
        status: str,
        reason: str | None,
        payload: dict[str, Any],
        provider_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        identity = {
            "alert_id": alert["alert_id"],
            "attempt": attempt,
            "created_at": created_at,
            "status": status,
            "reason": reason,
            "payload": payload,
            "provider_receipt": provider_receipt,
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        notification_id = f"DSNO-{digest[:32]}"
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_observability_notifications (
                    notification_id, alert_id, attempt, created_at, source_id,
                    dataset, severity, code, status, reason, payload_json,
                    provider_receipt_json, receipt_sha256
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    alert["alert_id"],
                    attempt,
                    created_at,
                    alert["source_id"],
                    alert["dataset"],
                    alert["severity"],
                    alert["code"],
                    status,
                    reason,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(provider_receipt, ensure_ascii=False, sort_keys=True),
                    digest,
                ),
            )
            conn.commit()
        return {
            "schema_version": "stock_ai.data_observability_notification_receipt.v1",
            "notification_id": notification_id,
            "alert_id": alert["alert_id"],
            "attempt": attempt,
            "created_at": created_at,
            "source_id": alert["source_id"],
            "dataset": alert["dataset"],
            "severity": alert["severity"],
            "code": alert["code"],
            "status": status,
            "reason": reason,
            "provider_receipt": provider_receipt,
            "receipt_sha256": digest,
        }

    @staticmethod
    def _normalize_alert(alert: Mapping[str, Any]) -> dict[str, Any]:
        required = ("alert_id", "source_id", "dataset", "severity", "code")
        if any(not str(alert.get(key) or "").strip() for key in required):
            raise ValueError("observability alert requires alert_id, source_id, dataset, severity and code")
        return {
            "alert_id": str(alert["alert_id"]),
            "source_id": str(alert["source_id"]),
            "dataset": str(alert["dataset"]),
            "severity": str(alert["severity"]),
            "code": str(alert["code"]),
            "details": dict(alert.get("details") or {}),
        }

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "stock_ai.data_observability_notification_receipt.v1",
            "notification_id": row["notification_id"],
            "alert_id": row["alert_id"],
            "attempt": int(row["attempt"]),
            "created_at": row["created_at"],
            "source_id": row["source_id"],
            "dataset": row["dataset"],
            "severity": row["severity"],
            "code": row["code"],
            "status": row["status"],
            "reason": row["reason"],
            "provider_receipt": json.loads(row["provider_receipt_json"]),
            "receipt_sha256": row["receipt_sha256"],
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn
