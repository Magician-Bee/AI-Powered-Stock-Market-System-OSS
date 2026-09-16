from __future__ import annotations

"""Bridge authoritative settlement receipts into durable loss-limit control.

The order boundary already knows how to evaluate realized-P&L limits, but a
limit is only as trustworthy as the event that feeds it.  This module keeps
that boundary explicit: every event needs a timezone-aware settlement time,
an account scope, a finite percentage supplied by the source, and a
content-addressed source receipt.  Paper OMS rows are adapted as a local
source; broker reconciliation callers must provide their broker receipt.
"""

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from open_stock_ai.execution.taiwan_settlement import parse_timestamp
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger

from .kill_switch import DurableRiskControlStore


SCHEMA_VERSION = "open_stock_ai.settlement_pnl_feed.v1"
SOURCE_RECEIPT_SCHEMA_VERSION = "open_stock_ai.settlement_pnl_source.v1"


def _hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_source_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create a content-addressed source receipt for a feed event."""

    body = {
        "schema_version": SOURCE_RECEIPT_SCHEMA_VERSION,
        **dict(payload),
    }
    body["receipt_sha256"] = _hash(body)
    return body


def verify_source_receipt(receipt: Mapping[str, Any]) -> bool:
    """Verify a source receipt without consulting a live provider."""

    if receipt.get("schema_version") != SOURCE_RECEIPT_SCHEMA_VERSION:
        raise ValueError("settlement_source_receipt_schema_invalid")
    body = dict(receipt)
    actual = str(body.pop("receipt_sha256", ""))
    if len(actual) != 64 or actual != _hash(body):
        raise ValueError("settlement_source_receipt_hash_mismatch")
    if not str(body.get("source") or "").strip():
        raise ValueError("settlement_source_receipt_source_required")
    return True


class SettlementPnLFeed:
    """Ingest settlement P&L once, then evaluate scoped durable limits."""

    def __init__(
        self,
        risk_control: DurableRiskControlStore,
        *,
        retention_ledger: ContentAddressedRetentionLedger | None = None,
        require_critical_retention: bool = False,
    ):
        self.risk_control = risk_control
        self.retention_ledger = retention_ledger
        if require_critical_retention and retention_ledger is None:
            raise ValueError("settlement PnL feed requires a critical retention ledger")
        if retention_ledger is not None:
            retention_path = getattr(retention_ledger.store, "path", None)
            risk_path = Path(risk_control.database_path).expanduser().resolve()
            if retention_path is None or Path(retention_path).resolve() != risk_path:
                raise ValueError("settlement PnL retention must share its durable database")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.risk_control._lock, self.risk_control._connect() as connection:
            connection.execute(
                """
                create table if not exists risk_settlement_pnl_receipts (
                    event_id text primary key,
                    payload_json text not null,
                    receipt_sha256 text not null,
                    created_at text not null
                )
                """
            )
            connection.commit()

    def ingest(self, event: Mapping[str, Any], *, as_of: str | datetime | None = None) -> dict[str, Any]:
        normalized = self._normalize_event(event)
        event_id = normalized["event_id"]
        payload_json = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_hash = _hash(normalized)
        idempotent = False
        with self.risk_control._lock, self.risk_control._connect() as connection:
            row = connection.execute(
                "select payload_json, receipt_sha256 from risk_settlement_pnl_receipts where event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                if str(row[1]) != event_hash or str(row[0]) != payload_json:
                    raise ValueError("settlement_pnl_event_conflict")
                idempotent = True
            else:
                connection.execute(
                    """
                    insert into risk_settlement_pnl_receipts
                        (event_id, payload_json, receipt_sha256, created_at)
                    values (?, ?, ?, ?)
                    """,
                    (event_id, payload_json, event_hash, datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()

        risk_event = self.risk_control.record_realized_pnl(
            event_id=event_id,
            pnl_pct=normalized["realized_pnl_pct"],
            occurred_at=normalized["settled_at"],
            scopes=normalized["scopes"],
        )
        evaluation = self.risk_control.evaluate_limits(
            as_of=as_of or normalized["settled_at"],
            scopes=normalized["scopes"],
        )
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "method": "settlement_receipt_to_durable_loss_limits",
            "event": normalized,
            "risk_event": risk_event,
            "limit_evaluation": evaluation,
            "idempotent": idempotent,
            "order_allowed": bool(evaluation["order_allowed"]),
        }
        result["receipt_sha256"] = _hash(result)
        if self.retention_ledger is not None:
            self.retention_ledger.append(
                f"settlement-pnl-{result['receipt_sha256']}",
                result,
                critical=True,
                kind="execution_settlement_pnl",
                occurred_at=normalized["settled_at"],
            )
        return result

    def ingest_paper_oms(
        self,
        oms: Any,
        *,
        as_of: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Adapt durable PaperOMS realized deltas into the same feed.

        The paper denominator is the account's initial cash, explicitly
        disclosed in the returned batch receipt.  Enforced T+2 rows are only
        ingested after they are settled; legacy immediate paper fills use the
        fill timestamp and are labelled accordingly.
        """

        as_of_value = parse_timestamp(as_of).isoformat() if as_of is not None else None
        with oms.store._connect() as connection:
            connection.row_factory = sqlite3.Row
            account = connection.execute(
                "select initial_cash from paper_accounts where account_id = ?",
                (oms.account_id,),
            ).fetchone()
            if account is None or float(account["initial_cash"] or 0.0) <= 0:
                raise ValueError("paper_pnl_denominator_required")
            denominator = float(account["initial_cash"])
            fills = connection.execute(
                """
                select f.*, o.market, o.payload_json,
                       s.settlement_id, s.status as settlement_status,
                       s.settled_at, s.receipt_json
                  from paper_fills f
                  join paper_orders o on o.order_id = f.order_id
                  left join paper_settlements s on s.fill_id = f.fill_id
                 where f.account_id = ?
                 order by f.created_at asc, f.fill_id asc
                """,
                (oms.account_id,),
            ).fetchall()
            ledger_rows = connection.execute(
                """
                select order_id, metadata_json
                  from cash_ledger
                 where account_id = ? and order_id is not null
                 order by id asc
                """,
                (oms.account_id,),
            ).fetchall()

        ledger_by_fill: dict[str, dict[str, Any]] = {}
        for row in ledger_rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                continue
            fill_id = str(metadata.get("fill_id") or "")
            if fill_id:
                ledger_by_fill[fill_id] = metadata

        ingested: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for fill in fills:
            metadata = ledger_by_fill.get(str(fill["fill_id"]), {})
            try:
                realized_amount = float(metadata.get("realized_pnl_delta") or 0.0)
            except (TypeError, ValueError):
                skipped.append({"fill_id": fill["fill_id"], "reason": "realized_pnl_delta_invalid"})
                continue
            if not math.isfinite(realized_amount) or abs(realized_amount) < 1e-12:
                skipped.append({"fill_id": fill["fill_id"], "reason": "no_realized_pnl"})
                continue

            settlement_status = str(fill["settlement_status"] or "")
            if settlement_status == "pending":
                skipped.append({"fill_id": fill["fill_id"], "reason": "settlement_pending"})
                continue
            settled_at = str(fill["settled_at"] or fill["created_at"])
            settlement_id = str(fill["settlement_id"] or "")
            settlement_mode = "t_plus_2_settled" if settlement_id else "legacy_immediate_paper"
            event_id = f"paper-realized-pnl:{fill['fill_id']}"
            source_receipt = build_source_receipt(
                {
                    "source": "paper_oms",
                    "source_event_id": event_id,
                    "fill_id": str(fill["fill_id"]),
                    "settlement_id": settlement_id or None,
                    "settlement_mode": settlement_mode,
                    "settled_at": settled_at,
                    "account_id": str(oms.account_id),
                    "realized_pnl_amount": round(realized_amount, 12),
                    "ledger_metadata_sha256": _hash(metadata),
                }
            )
            scopes = {"account": str(oms.account_id), "symbol": str(fill["symbol"])}
            try:
                order_payload = json.loads(fill["payload_json"] or "{}")
            except json.JSONDecodeError:
                order_payload = {}
            for scope_type in ("broker", "strategy"):
                scope_value = str(order_payload.get(scope_type) or "").strip()
                if scope_value:
                    scopes[scope_type] = scope_value
            ingested.append(
                self.ingest(
                    {
                        "event_id": event_id,
                        "settled_at": settled_at,
                        "realized_pnl_amount": realized_amount,
                        "realized_pnl_pct": realized_amount / denominator * 100.0,
                        "scopes": scopes,
                        "source_receipt": source_receipt,
                    },
                    as_of=as_of_value or settled_at,
                )
            )

        result: dict[str, Any] = {
            "schema_version": f"{SCHEMA_VERSION}.batch",
            "method": "paper_oms_settlement_pnl_sync",
            "account_id": str(oms.account_id),
            "pnl_denominator": {
                "amount": denominator,
                "currency": str(oms.base_currency),
                "basis": "paper_account_initial_cash",
            },
            "ingested_count": len(ingested),
            "ingested": ingested,
            "skipped": skipped,
        }
        result["receipt_sha256"] = _hash(result)
        return result

    @staticmethod
    def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("settlement_pnl_event_id_required")
        settled_at = parse_timestamp(event.get("settled_at")).isoformat()
        try:
            pnl_pct = float(event.get("realized_pnl_pct"))
        except (TypeError, ValueError):
            raise ValueError("settlement_realized_pnl_pct_required") from None
        if not math.isfinite(pnl_pct):
            raise ValueError("settlement_realized_pnl_pct_must_be_finite")
        scopes = event.get("scopes")
        if not isinstance(scopes, Mapping) or not str(scopes.get("account") or "").strip():
            raise ValueError("settlement_account_scope_required")
        normalized_scopes = {
            str(key).strip().lower(): str(value).strip()
            for key, value in scopes.items()
            if str(value).strip()
        }
        if "account" not in normalized_scopes:
            raise ValueError("settlement_account_scope_required")
        source_receipt = event.get("source_receipt")
        if not isinstance(source_receipt, Mapping):
            raise ValueError("settlement_source_receipt_required")
        verify_source_receipt(source_receipt)
        if str(source_receipt.get("source_event_id") or "") != event_id:
            raise ValueError("settlement_source_event_id_mismatch")
        result: dict[str, Any] = {
            "event_id": event_id,
            "settled_at": settled_at,
            "realized_pnl_pct": pnl_pct,
            "scopes": normalized_scopes,
            "source_receipt": dict(source_receipt),
        }
        if event.get("realized_pnl_amount") is not None:
            try:
                pnl_amount = float(event["realized_pnl_amount"])
            except (TypeError, ValueError):
                raise ValueError("settlement_realized_pnl_amount_invalid") from None
            if not math.isfinite(pnl_amount):
                raise ValueError("settlement_realized_pnl_amount_must_be_finite")
            result["realized_pnl_amount"] = pnl_amount
        return result


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_RECEIPT_SCHEMA_VERSION",
    "SettlementPnLFeed",
    "build_source_receipt",
    "verify_source_receipt",
]
