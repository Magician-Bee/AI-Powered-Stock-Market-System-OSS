"""Fail-closed operational alert aggregation for runtime safety signals."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection


@dataclass(frozen=True, slots=True)
class OperationalAlertPolicy:
    feed_stale_seconds: float = 60.0
    database_pressure_ratio: float = 0.85

    def __post_init__(self) -> None:
        if self.feed_stale_seconds <= 0:
            raise ValueError("feed stale threshold must be positive")
        if not 0 < self.database_pressure_ratio <= 1:
            raise ValueError("database pressure threshold must be between zero and one")


@dataclass(frozen=True, slots=True)
class OperationalAlertReceipt:
    alert_id: str
    code: str
    severity: str
    message: str
    stop_new_orders: bool
    observed_at: str
    details: dict[str, Any]
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.operational_alert.v1",
            "alert_id": self.alert_id,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "stop_new_orders": self.stop_new_orders,
            "observed_at": self.observed_at,
            "details": self.details,
        }

    def verify(self) -> bool:
        return hashlib.sha256(_canonical(self.payload())).hexdigest() == self.receipt_sha256

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperationalAlertReceipt":
        receipt = cls(
            alert_id=str(payload["alert_id"]),
            code=str(payload["code"]),
            severity=str(payload["severity"]),
            message=str(payload["message"]),
            stop_new_orders=bool(payload["stop_new_orders"]),
            observed_at=str(payload["observed_at"]),
            details=dict(payload.get("details") or {}),
            receipt_sha256=str(payload["receipt_sha256"]),
        )
        if not receipt.verify():
            raise ValueError("invalid operational alert receipt hash")
        return receipt


@dataclass(frozen=True, slots=True)
class OperationalAlertDeliveryReceipt:
    delivery_id: str
    alert_id: str
    sink: str
    status: str
    attempted_at: str
    provider_receipt: dict[str, Any]
    reason: str | None
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.operational_alert_delivery.v1",
            "delivery_id": self.delivery_id,
            "alert_id": self.alert_id,
            "sink": self.sink,
            "status": self.status,
            "attempted_at": self.attempted_at,
            "provider_receipt": self.provider_receipt,
            "reason": self.reason,
        }

    def verify(self) -> bool:
        return hashlib.sha256(_canonical(self.payload())).hexdigest() == self.receipt_sha256

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}


class DurableOperationalAlertStore:
    """Append-only alert and delivery ledger that survives runtime restarts."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=ManagedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=FULL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists operational_alert_receipts (
                    alert_id text primary key,
                    code text not null,
                    severity text not null,
                    message text not null,
                    stop_new_orders integer not null,
                    observed_at text not null,
                    details_json text not null,
                    receipt_sha256 text not null,
                    created_at text not null
                );
                create table if not exists operational_alert_delivery_receipts (
                    delivery_id text primary key,
                    alert_id text not null,
                    sink text not null,
                    status text not null,
                    attempted_at text not null,
                    provider_receipt_json text not null,
                    reason text,
                    receipt_sha256 text not null
                );
                create index if not exists idx_operational_alert_delivery_alert
                    on operational_alert_delivery_receipts(alert_id, sink, attempted_at);
                create trigger if not exists operational_alert_receipts_immutable_update
                    before update on operational_alert_receipts
                    begin select raise(abort, 'operational alert receipts are append-only'); end;
                create trigger if not exists operational_alert_receipts_immutable_delete
                    before delete on operational_alert_receipts
                    begin select raise(abort, 'operational alert receipts are append-only'); end;
                create trigger if not exists operational_alert_delivery_immutable_update
                    before update on operational_alert_delivery_receipts
                    begin select raise(abort, 'operational alert delivery receipts are append-only'); end;
                create trigger if not exists operational_alert_delivery_immutable_delete
                    before delete on operational_alert_delivery_receipts
                    begin select raise(abort, 'operational alert delivery receipts are append-only'); end;
                """
            )
            conn.commit()

    def has_alert(self, alert_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from operational_alert_receipts where alert_id=?", (alert_id,)
            ).fetchone()
        return row is not None

    def record_alert(self, receipt: OperationalAlertReceipt) -> None:
        if not receipt.verify():
            raise ValueError("cannot persist an invalid operational alert receipt")
        with self._connect() as conn:
            conn.execute(
                """insert or ignore into operational_alert_receipts(
                    alert_id, code, severity, message, stop_new_orders, observed_at,
                    details_json, receipt_sha256, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.alert_id, receipt.code, receipt.severity, receipt.message,
                    int(receipt.stop_new_orders), receipt.observed_at,
                    _json(receipt.details), receipt.receipt_sha256, receipt.observed_at,
                ),
            )
            conn.commit()

    def alerts(self, *, limit: int = 100) -> list[OperationalAlertReceipt]:
        bounded = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                "select * from operational_alert_receipts order by created_at desc limit ?",
                (bounded,),
            ).fetchall()
        return [
            OperationalAlertReceipt(
                alert_id=row["alert_id"], code=row["code"], severity=row["severity"],
                message=row["message"], stop_new_orders=bool(row["stop_new_orders"]),
                observed_at=row["observed_at"], details=_load(row["details_json"]),
                receipt_sha256=row["receipt_sha256"],
            )
            for row in rows
        ]

    def latest_delivery(self, alert_id: str, sink: str) -> OperationalAlertDeliveryReceipt | None:
        with self._connect() as conn:
            row = conn.execute(
                """select * from operational_alert_delivery_receipts
                   where alert_id=? and sink=? order by attempted_at desc limit 1""",
                (alert_id, sink),
            ).fetchone()
        return _delivery_from_row(row) if row else None

    def record_delivery(self, receipt: OperationalAlertDeliveryReceipt) -> None:
        if not receipt.verify():
            raise ValueError("cannot persist an invalid operational alert delivery receipt")
        with self._connect() as conn:
            conn.execute(
                """insert into operational_alert_delivery_receipts(
                    delivery_id, alert_id, sink, status, attempted_at,
                    provider_receipt_json, reason, receipt_sha256
                ) values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.delivery_id, receipt.alert_id, receipt.sink, receipt.status,
                    receipt.attempted_at, _json(receipt.provider_receipt), receipt.reason,
                    receipt.receipt_sha256,
                ),
            )
            conn.commit()

    def deliveries(self, *, alert_id: str | None = None, limit: int = 100) -> list[OperationalAlertDeliveryReceipt]:
        bounded = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            if alert_id:
                rows = conn.execute(
                    """select * from operational_alert_delivery_receipts
                       where alert_id=? order by attempted_at desc limit ?""",
                    (alert_id, bounded),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select * from operational_alert_delivery_receipts order by attempted_at desc limit ?",
                    (bounded,),
                ).fetchall()
        return [_delivery_from_row(row) for row in rows]


class OperationalAlertDispatcher:
    """Attempt explicit operator delivery and persist every outcome fail-closed."""

    def __init__(
        self,
        store: DurableOperationalAlertStore,
        sinks: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any] | bool]] | None = None,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.sinks = dict(sinks or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def dispatch(self, alerts: list[OperationalAlertReceipt]) -> list[OperationalAlertDeliveryReceipt]:
        deliveries: list[OperationalAlertDeliveryReceipt] = []
        for alert in alerts:
            for sink_name, sender in self.sinks.items():
                previous = self.store.latest_delivery(alert.alert_id, sink_name)
                if previous and previous.status == "delivered":
                    continue
                attempted_at = str(self.clock())
                status = "failed"
                provider_receipt: dict[str, Any] = {}
                reason: str | None = None
                try:
                    raw = sender(alert.as_dict())
                    provider_receipt = dict(raw) if isinstance(raw, Mapping) else {"accepted": bool(raw)}
                    if bool(provider_receipt.get("accepted", True)):
                        status = "delivered"
                    else:
                        reason = str(provider_receipt.get("error") or "operator sink rejected alert")
                except Exception as exc:  # one sink must not hide other sink outcomes
                    reason = f"{type(exc).__name__}: {exc}"
                receipt = _make_delivery_receipt(
                    alert.alert_id, sink_name, status, attempted_at, provider_receipt, reason
                )
                self.store.record_delivery(receipt)
                deliveries.append(receipt)
        return deliveries


class OperationalAlertRouter:
    """Convert feed/order/reconciliation/DB/model signals into deduplicated receipts."""

    def __init__(
        self,
        *,
        policy: OperationalAlertPolicy | None = None,
        clock: Callable[[], str] | None = None,
        store: DurableOperationalAlertStore | None = None,
    ) -> None:
        self.policy = policy or OperationalAlertPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.store = store
        self._seen: set[str] = set()

    def evaluate(self, signals: Mapping[str, Any]) -> list[OperationalAlertReceipt]:
        now = str(self.clock())
        candidates: list[tuple[str, str, str, bool, dict[str, Any]]] = []
        feed = signals.get("feed") if isinstance(signals.get("feed"), Mapping) else {}
        freshness = _number(feed.get("freshness_seconds"))
        if _scope_enabled(feed) and (freshness is None or freshness > self.policy.feed_stale_seconds):
            candidates.append(("feed_stale", "CRITICAL", "Market feed is stale or has no verified freshness.", True, {"freshness_seconds": freshness}))

        reconciliation = signals.get("reconciliation") if isinstance(signals.get("reconciliation"), Mapping) else {}
        mismatch_count = _integer(reconciliation.get("mismatch_count"))
        if _scope_enabled(reconciliation) and (mismatch_count is None or mismatch_count > 0):
            candidates.append(("reconciliation_mismatch", "CRITICAL", "Reconciliation mismatch requires operator recovery.", True, {"mismatch_count": mismatch_count}))

        orders = signals.get("orders") if isinstance(signals.get("orders"), Mapping) else {}
        unknown_count = _integer(orders.get("unknown_count"))
        if _scope_enabled(orders) and (unknown_count is None or unknown_count > 0):
            candidates.append(("unknown_order", "CRITICAL", "Unknown order state blocks new submissions.", True, {"unknown_count": unknown_count}))

        database = signals.get("database") if isinstance(signals.get("database"), Mapping) else {}
        pressure = _number(database.get("pressure_ratio"))
        if _scope_enabled(database) and (pressure is None or pressure >= self.policy.database_pressure_ratio):
            candidates.append(("database_pressure", "WARNING", "Database pressure requires maintenance attention.", False, {"pressure_ratio": pressure}))

        model = signals.get("model") if isinstance(signals.get("model"), Mapping) else {}
        if _scope_enabled(model) and (model.get("status") in {"disabled", "not_configured"} or model.get("blockers")):
            candidates.append(("model_drift", "CRITICAL", "Model evidence is disabled or drift monitoring is incomplete.", True, {"status": model.get("status"), "blockers": list(model.get("blockers") or [])}))

        chaos = signals.get("chaos") if isinstance(signals.get("chaos"), Mapping) else {}
        if _scope_enabled(chaos) and chaos.get("triggered") is True:
            candidates.append(("chaos_triggered", "WARNING", "A deterministic chaos trigger requires recovery verification.", False, {"fault": chaos.get("fault")}))

        receipts: list[OperationalAlertReceipt] = []
        for code, severity, message, stop_new_orders, details in candidates:
            alert_id = hashlib.sha256(_canonical({"code": code, "details": details})).hexdigest()[:32]
            if alert_id in self._seen or (self.store is not None and self.store.has_alert(alert_id)):
                continue
            payload = {
                "schema_version": "open_stock_ai.operational_alert.v1",
                "alert_id": alert_id,
                "code": code,
                "severity": severity,
                "message": message,
                "stop_new_orders": stop_new_orders,
                "observed_at": now,
                "details": details,
            }
            receipt = OperationalAlertReceipt(
                alert_id=alert_id,
                code=code,
                severity=severity,
                message=message,
                stop_new_orders=stop_new_orders,
                observed_at=now,
                details=details,
                receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
            )
            self._seen.add(alert_id)
            if self.store is not None:
                self.store.record_alert(receipt)
            receipts.append(receipt)
        return receipts


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _scope_enabled(signal: Mapping[str, Any]) -> bool:
    """Allow a runtime to declare an unavailable production source explicitly.

    An absent value remains fail-closed for legacy callers.  A paper-only
    desktop runtime, however, must be able to distinguish "no live broker is
    configured" from "a configured broker failed to report a safety signal".
    """

    return signal.get("enabled") is not False


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _make_delivery_receipt(
    alert_id: str,
    sink: str,
    status: str,
    attempted_at: str,
    provider_receipt: Mapping[str, Any],
    reason: str | None,
) -> OperationalAlertDeliveryReceipt:
    delivery_id = f"OAD-{uuid4().hex}"
    payload = {
        "schema_version": "open_stock_ai.operational_alert_delivery.v1",
        "delivery_id": delivery_id,
        "alert_id": alert_id,
        "sink": sink,
        "status": status,
        "attempted_at": attempted_at,
        "provider_receipt": dict(provider_receipt),
        "reason": reason,
    }
    return OperationalAlertDeliveryReceipt(
        delivery_id=delivery_id,
        alert_id=alert_id,
        sink=sink,
        status=status,
        attempted_at=attempted_at,
        provider_receipt=dict(provider_receipt),
        reason=reason,
        receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
    )


def _delivery_from_row(row: sqlite3.Row) -> OperationalAlertDeliveryReceipt:
    return OperationalAlertDeliveryReceipt(
        delivery_id=row["delivery_id"],
        alert_id=row["alert_id"],
        sink=row["sink"],
        status=row["status"],
        attempted_at=row["attempted_at"],
        provider_receipt=_load(row["provider_receipt_json"]),
        reason=row["reason"],
        receipt_sha256=row["receipt_sha256"],
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    return dict(loaded) if isinstance(loaded, Mapping) else {}
