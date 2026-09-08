from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from open_stock_ai.storage.sqlite_store import SQLiteStore


ACTION_TYPE_ALIASES = {
    "dividend": "cash_dividend",
    "ex_dividend": "cash_dividend",
    "cash_dividend": "cash_dividend",
    "stock_dividend": "stock_dividend",
    "rights_issue": "capital_increase",
    "capital_increase": "capital_increase",
    "capital_reduction": "capital_reduction",
    "split": "split",
    "stock_split": "split",
    "reverse_split": "reverse_split",
    "merger": "merger",
    "treasury_stock": "treasury_stock",
}
SUPPORTED_ACTION_TYPES = frozenset(ACTION_TYPE_ALIASES.values())
POSITION_MULTIPLIER_ACTIONS = frozenset(
    {"stock_dividend", "capital_reduction", "split", "reverse_split"}
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _number(value: Any, *, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric corporate-action term: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError("corporate-action terms must be finite")
    return result


def _iso_date(value: Any, *, field: str, required: bool = True) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CorporateActionLedger:
    """Immutable, source-attributed corporate actions and paper-account effects."""

    store: SQLiteStore

    def import_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(payload)
        payload_hash = sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
        now = _now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                """
                select * from corporate_action_revisions
                 where action_id=? and payload_hash=?
                """,
                (normalized["action_id"], payload_hash),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {
                    "created": False,
                    "idempotent": True,
                    "item": self._serialize_action(existing, conn=conn),
                }

            previous = conn.execute(
                """
                select * from corporate_action_revisions
                 where action_id=? order by revision desc limit 1
                """,
                (normalized["action_id"],),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous else 1
            revision_id = f"CAREV-{uuid4().hex}"
            conn.execute(
                """
                insert into corporate_action_revisions (
                    revision_id, action_id, revision, symbol, market,
                    effective_date, record_date, payment_date, action_type,
                    status, official_verified, source_id, source_url,
                    acquired_at, payload_hash, supersedes_revision_id,
                    terms_json, source_payload_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    normalized["action_id"],
                    revision,
                    normalized["symbol"],
                    normalized["market"],
                    normalized["effective_date"],
                    normalized["record_date"],
                    normalized["payment_date"],
                    normalized["action_type"],
                    normalized["status"],
                    1 if normalized["official_verified"] else 0,
                    normalized["source_id"],
                    normalized["source_url"],
                    normalized["acquired_at"],
                    payload_hash,
                    previous["revision_id"] if previous else None,
                    _canonical_json(normalized["terms"]),
                    _canonical_json(normalized["source_payload"]),
                    now,
                ),
            )
            row = conn.execute(
                "select * from corporate_action_revisions where revision_id=?",
                (revision_id,),
            ).fetchone()
            conn.commit()
            return {
                "created": True,
                "idempotent": False,
                "item": self._serialize_action(row, conn=conn),
            }

    def import_actions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        if not payloads:
            raise ValueError("at least one corporate action is required")
        results = [self.import_action(payload) for payload in payloads]
        return {
            "schema_version": "open_stock_ai.corporate_action_import.v1",
            "count": len(results),
            "created_count": sum(1 for item in results if item["created"]),
            "idempotent_count": sum(1 for item in results if item["idempotent"]),
            "items": [item["item"] for item in results],
            "storage": "immutable_revision_ledger",
        }

    def list_actions(
        self,
        *,
        symbol: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = [
            """
            not exists (
                select 1 from corporate_action_revisions newer
                 where newer.action_id=corporate_action_revisions.action_id
                   and newer.revision>corporate_action_revisions.revision
            )
            """
        ]
        params: list[Any] = []
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol:
            clauses.append("symbol=?")
            params.append(normalized_symbol)
        if start:
            clauses.append("effective_date>=?")
            params.append(_iso_date(start, field="start"))
        if end:
            clauses.append("effective_date<=?")
            params.append(_iso_date(end, field="end"))
        query = (
            "select * from corporate_action_revisions where "
            + " and ".join(clauses)
            + " order by effective_date desc, action_id limit ?"
        )
        params.append(max(1, min(int(limit), 1000)))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            items = [
                self._serialize_action(row, conn=conn, account_id=account_id)
                for row in rows
            ]
        return {
            "schema_version": "open_stock_ai.corporate_actions.v1",
            "symbol": normalized_symbol or None,
            "count": len(items),
            "items": items,
            "source_policy": "official_source_attribution_required_no_inferred_actions",
            "holder_sync_policy": "explicit_exactly_once_paper_account_application",
        }

    def sync_due_actions(
        self,
        *,
        account_id: str,
        as_of: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        cutoff = _iso_date(as_of or date.today().isoformat(), field="as_of")
        listing = self.list_actions(
            symbol=symbol,
            end=cutoff,
            limit=1000,
            account_id=account_id,
        )
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for item in reversed(listing["items"]):
            if item["status"] not in {"confirmed", "effective"}:
                skipped.append({"action_id": item["action_id"], "reason": "not_effective"})
                continue
            if not item["official_verified"]:
                skipped.append({"action_id": item["action_id"], "reason": "not_officially_verified"})
                continue
            if not item["terms_complete"]:
                skipped.append({"action_id": item["action_id"], "reason": "incomplete_terms"})
                continue
            results.append(self.apply_to_account(item=item, account_id=account_id))
        return {
            "schema_version": "open_stock_ai.corporate_action_sync.v1",
            "account_id": account_id,
            "as_of": cutoff,
            "symbol": str(symbol or "").strip().upper() or None,
            "eligible_count": len(results),
            "applied_count": sum(1 for item in results if not item["idempotent"]),
            "idempotent_count": sum(1 for item in results if item["idempotent"]),
            "skipped_count": len(skipped),
            "items": results,
            "skipped": skipped,
            "execution_boundary": "local_paper_account_only",
        }

    def apply_to_account(self, *, item: dict[str, Any], account_id: str) -> dict[str, Any]:
        action_id = str(item["action_id"])
        now = _now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                """
                select * from paper_corporate_action_applications
                 where account_id=? and action_id=?
                """,
                (account_id, action_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self._serialize_application(existing, idempotent=True)

            account = conn.execute(
                "select * from paper_accounts where account_id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise ValueError(f"paper account does not exist: {account_id}")
            position = conn.execute(
                "select * from paper_positions where account_id=? and symbol=?",
                (account_id, item["symbol"]),
            ).fetchone()
            before = self._position_snapshot(position)
            terms = item["terms"]
            cash_delta = 0.0
            entitlement: dict[str, Any] | None = None
            outcome = "no_position"

            if position is not None and float(position["quantity"]) > 0:
                outcome, cash_delta, entitlement = self._apply_position_effect(
                    conn=conn,
                    account_id=account_id,
                    position=position,
                    item=item,
                    terms=terms,
                    now=now,
                )
            elif item["action_type"] == "treasury_stock":
                outcome = "holder_position_unchanged"

            if cash_delta:
                cash_after = float(account["cash_balance"]) + cash_delta
                conn.execute(
                    """
                    update paper_accounts set cash_balance=?, updated_at=?
                     where account_id=?
                    """,
                    (cash_after, now, account_id),
                )
                conn.execute(
                    """
                    insert into cash_ledger (
                        account_id, order_id, created_at, entry_type, amount,
                        balance_after, currency, metadata_json
                    ) values (?, null, ?, 'corporate_action', ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        now,
                        cash_delta,
                        cash_after,
                        terms["currency"],
                        _canonical_json(
                            {
                                "action_id": action_id,
                                "revision_id": item["revision_id"],
                                "action_type": item["action_type"],
                                "official_source": item["source_url"],
                            }
                        ),
                    ),
                )

            after_row = conn.execute(
                "select * from paper_positions where account_id=? and symbol=?",
                (account_id, terms.get("successor_symbol") or item["symbol"]),
            ).fetchone()
            after = self._position_snapshot(after_row)
            application_id = f"CAAPP-{uuid4().hex}"
            notes = {
                "price_treatment": terms["price_treatment"],
                "position_treatment": terms["position_treatment"],
                "automatic_subscription": False,
                "fractional_shares": "preserved_in_paper_ledger",
                "position_snapshot_basis": (
                    "current_paper_position_at_sync"
                    "_without_historical_record_date_reconstruction"
                ),
                "source_url": item["source_url"],
            }
            conn.execute(
                """
                insert into paper_corporate_action_applications (
                    application_id, account_id, action_id, action_revision_id,
                    symbol, action_type, effective_date, outcome, cash_delta,
                    before_json, after_json, notes_json, applied_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    account_id,
                    action_id,
                    item["revision_id"],
                    item["symbol"],
                    item["action_type"],
                    item["effective_date"],
                    outcome,
                    cash_delta,
                    _canonical_json(before),
                    _canonical_json(after),
                    _canonical_json(notes),
                    now,
                ),
            )
            if entitlement is not None:
                conn.execute(
                    """
                    insert into paper_corporate_action_entitlements (
                        entitlement_id, application_id, account_id, action_id,
                        entitlement_type, symbol, quantity, cash_amount,
                        currency, status, metadata_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"CAENT-{uuid4().hex}",
                        application_id,
                        account_id,
                        action_id,
                        entitlement["type"],
                        entitlement["symbol"],
                        entitlement.get("quantity", 0.0),
                        entitlement.get("cash_amount", 0.0),
                        terms["currency"],
                        entitlement["status"],
                        _canonical_json(entitlement.get("metadata", {})),
                        now,
                    ),
                )
            row = conn.execute(
                "select * from paper_corporate_action_applications where application_id=?",
                (application_id,),
            ).fetchone()
            conn.commit()
            return self._serialize_application(row, idempotent=False)

    def _apply_position_effect(
        self,
        *,
        conn: sqlite3.Connection,
        account_id: str,
        position: sqlite3.Row,
        item: dict[str, Any],
        terms: dict[str, Any],
        now: str,
    ) -> tuple[str, float, dict[str, Any] | None]:
        action_type = item["action_type"]
        old_quantity = float(position["quantity"])
        old_average = float(position["average_cost"])
        old_last = float(position["last_price"])
        price_multiplier = float(terms["price_multiplier"])
        cash_delta = old_quantity * float(terms["cash_per_share"])
        entitlement = None

        if action_type in POSITION_MULTIPLIER_ACTIONS:
            share_multiplier = float(terms["share_multiplier"])
            new_quantity = old_quantity * share_multiplier
            remaining_cost_basis = old_quantity * old_average
            if action_type == "capital_reduction":
                remaining_cost_basis = max(0.0, remaining_cost_basis - cash_delta)
            new_average_cost = (
                remaining_cost_basis / new_quantity if new_quantity else 0.0
            )
            conn.execute(
                """
                update paper_positions set quantity=?, average_cost=?,
                    last_price=?, updated_at=? where account_id=? and symbol=?
                """,
                (
                    new_quantity,
                    new_average_cost,
                    old_last * price_multiplier,
                    now,
                    account_id,
                    item["symbol"],
                ),
            )
            outcome = "position_and_price_adjusted"
        elif action_type == "cash_dividend":
            conn.execute(
                """
                update paper_positions set last_price=?, updated_at=?
                 where account_id=? and symbol=?
                """,
                (old_last * price_multiplier, now, account_id, item["symbol"]),
            )
            outcome = "cash_credited_and_price_adjusted"
        elif action_type == "capital_increase":
            conn.execute(
                """
                update paper_positions set last_price=?, updated_at=?
                 where account_id=? and symbol=?
                """,
                (old_last * price_multiplier, now, account_id, item["symbol"]),
            )
            entitlement = {
                "type": "subscription_right",
                "symbol": item["symbol"],
                "quantity": old_quantity * float(terms["subscription_ratio"]),
                "status": "pending_manual_exercise",
                "metadata": {
                    "subscription_price": terms["subscription_price"],
                    "automatic_cash_deduction": False,
                },
            }
            outcome = "rights_recorded_no_automatic_subscription"
        elif action_type == "merger":
            successor = terms["successor_symbol"]
            ratio = float(terms["exchange_ratio"])
            converted = old_quantity * ratio
            successor_row = conn.execute(
                "select * from paper_positions where account_id=? and symbol=?",
                (account_id, successor),
            ).fetchone()
            target_quantity = converted
            target_cost_basis = old_quantity * old_average
            target_realized = float(position["realized_pnl"])
            if successor_row is not None:
                target_quantity += float(successor_row["quantity"])
                target_cost_basis += (
                    float(successor_row["quantity"]) * float(successor_row["average_cost"])
                )
                target_realized += float(successor_row["realized_pnl"])
            target_average = target_cost_basis / target_quantity if target_quantity else 0.0
            conn.execute(
                """
                update paper_positions set quantity=0, average_cost=0, last_price=0,
                    updated_at=? where account_id=? and symbol=?
                """,
                (now, account_id, item["symbol"]),
            )
            conn.execute(
                """
                insert into paper_positions (
                    account_id, symbol, market, quantity, average_cost,
                    last_price, realized_pnl, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(account_id, symbol) do update set
                    market=excluded.market, quantity=excluded.quantity,
                    average_cost=excluded.average_cost, last_price=excluded.last_price,
                    realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at
                """,
                (
                    account_id,
                    successor,
                    terms.get("successor_market") or position["market"],
                    target_quantity,
                    target_average,
                    old_last * price_multiplier,
                    target_realized,
                    now,
                ),
            )
            cash_delta += old_quantity * float(terms["cash_boot_per_share"])
            outcome = "position_transferred_to_successor"
        else:
            outcome = "holder_position_unchanged"
        return outcome, cash_delta, entitlement

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        raw_type = str(payload.get("action_type") or payload.get("event_type") or "").strip().lower()
        action_type = ACTION_TYPE_ALIASES.get(raw_type)
        if action_type not in SUPPORTED_ACTION_TYPES:
            raise ValueError(f"unsupported corporate action type: {raw_type or 'missing'}")
        effective_date = _iso_date(
            payload.get("effective_date") or payload.get("event_date"),
            field="effective_date",
        )
        source_id = str(payload.get("source_id") or "").strip()
        source_url = str(payload.get("source_url") or "").strip()
        if not source_id or not source_url:
            raise ValueError("source_id and source_url are required")
        official_verified = payload.get("official_verified") is True
        source_host = (urlparse(source_url).hostname or "").lower()
        if official_verified and not (
            source_host == "twse.com.tw"
            or source_host.endswith(".twse.com.tw")
            or source_host == "tpex.org.tw"
            or source_host.endswith(".tpex.org.tw")
        ):
            raise ValueError(
                "official_verified requires an official TWSE, MOPS or TPEx source URL"
            )
        source_event_id = str(payload.get("source_event_id") or "").strip()
        action_id = str(payload.get("action_id") or "").strip()
        if not action_id:
            seed = "|".join(
                [source_id, source_event_id or source_url, symbol, action_type, effective_date or ""]
            )
            action_id = f"CA-{uuid5(NAMESPACE_URL, seed).hex}"

        reference_before = _number(payload.get("reference_price_before"))
        reference_after = _number(payload.get("reference_price_after"))
        explicit_price_multiplier = _number(payload.get("price_multiplier"))
        if explicit_price_multiplier is None and reference_before and reference_after is not None:
            explicit_price_multiplier = reference_after / reference_before
        price_multiplier = explicit_price_multiplier if explicit_price_multiplier is not None else 1.0
        share_multiplier = _number(payload.get("share_multiplier"), default=1.0) or 1.0
        cash_per_share = _number(payload.get("cash_per_share"), default=0.0) or 0.0
        subscription_ratio = _number(payload.get("subscription_ratio"), default=0.0) or 0.0
        subscription_price = _number(payload.get("subscription_price"))
        exchange_ratio = _number(payload.get("exchange_ratio"), default=0.0) or 0.0
        cash_boot = _number(payload.get("cash_boot_per_share"), default=0.0) or 0.0
        if price_multiplier <= 0 or share_multiplier <= 0:
            raise ValueError("price_multiplier and share_multiplier must be positive")
        if min(cash_per_share, subscription_ratio, exchange_ratio, cash_boot) < 0:
            raise ValueError("cash, subscription and exchange terms cannot be negative")

        successor_symbol = str(payload.get("successor_symbol") or "").strip().upper() or None
        complete = True
        if action_type in POSITION_MULTIPLIER_ACTIONS:
            complete = (
                share_multiplier != 1.0
                and explicit_price_multiplier is not None
            )
        elif action_type == "cash_dividend":
            complete = "cash_per_share" in payload and explicit_price_multiplier is not None
        elif action_type == "capital_increase":
            complete = (
                explicit_price_multiplier is not None
                and subscription_ratio > 0
                and subscription_price is not None
            )
        elif action_type == "merger":
            complete = bool(successor_symbol and exchange_ratio > 0)

        position_treatment = {
            "cash_dividend": "shares_unchanged_cash_credit",
            "stock_dividend": "shares_multiplied_cost_basis_rebased",
            "capital_increase": "shares_unchanged_subscription_right_recorded",
            "capital_reduction": "shares_reduced_cost_basis_rebased",
            "split": "shares_multiplied_cost_basis_rebased",
            "reverse_split": "shares_consolidated_cost_basis_rebased",
            "merger": "position_transferred_using_official_exchange_ratio",
            "treasury_stock": "holder_position_unchanged",
        }[action_type]
        terms = {
            "currency": str(payload.get("currency") or "TWD").strip().upper(),
            "share_multiplier": share_multiplier,
            "price_multiplier": price_multiplier,
            "reference_price_before": reference_before,
            "reference_price_after": reference_after,
            "cash_per_share": cash_per_share,
            "subscription_ratio": subscription_ratio,
            "subscription_price": subscription_price,
            "successor_symbol": successor_symbol,
            "successor_market": str(payload.get("successor_market") or "").strip() or None,
            "exchange_ratio": exchange_ratio,
            "cash_boot_per_share": cash_boot,
            "position_treatment": position_treatment,
            "price_treatment": (
                "official_reference_ratio"
                if explicit_price_multiplier is not None
                else "unchanged_until_official_reference_is_available"
            ),
            "terms_complete": complete,
        }
        acquired_at = str(payload.get("acquired_at") or _now()).strip()
        try:
            datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("acquired_at must be an ISO-8601 timestamp") from exc
        source_payload = payload.get("source_payload") or {}
        if official_verified and not source_payload:
            raise ValueError(
                "official_verified requires the captured official source payload"
            )
        return {
            "action_id": action_id,
            "symbol": symbol,
            "market": str(payload.get("market") or "").strip().upper() or None,
            "effective_date": effective_date,
            "record_date": _iso_date(payload.get("record_date"), field="record_date", required=False),
            "payment_date": _iso_date(payload.get("payment_date"), field="payment_date", required=False),
            "action_type": action_type,
            "status": str(payload.get("status") or "confirmed").strip().lower(),
            "official_verified": official_verified,
            "source_id": source_id,
            "source_url": source_url,
            "acquired_at": acquired_at,
            "terms": terms,
            "source_payload": source_payload,
        }

    def _serialize_action(
        self,
        row: sqlite3.Row,
        *,
        conn: sqlite3.Connection,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        terms = json.loads(row["terms_json"])
        application = None
        if account_id:
            applied = conn.execute(
                """
                select * from paper_corporate_action_applications
                 where account_id=? and action_id=?
                """,
                (account_id, row["action_id"]),
            ).fetchone()
            if applied is not None:
                application = self._serialize_application(applied, idempotent=True)
        return {
            "revision_id": row["revision_id"],
            "action_id": row["action_id"],
            "revision": int(row["revision"]),
            "symbol": row["symbol"],
            "market": row["market"],
            "effective_date": row["effective_date"],
            "record_date": row["record_date"],
            "payment_date": row["payment_date"],
            "action_type": row["action_type"],
            "status": row["status"],
            "official_verified": bool(row["official_verified"]),
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "acquired_at": row["acquired_at"],
            "payload_hash": row["payload_hash"],
            "supersedes_revision_id": row["supersedes_revision_id"],
            "terms": terms,
            "terms_complete": bool(terms.get("terms_complete")),
            "holder_effect": terms["position_treatment"],
            "price_effect": terms["price_treatment"],
            "application": application,
        }

    @staticmethod
    def _position_snapshot(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "symbol": row["symbol"],
            "market": row["market"],
            "quantity": float(row["quantity"]),
            "average_cost": float(row["average_cost"]),
            "last_price": float(row["last_price"]),
            "realized_pnl": float(row["realized_pnl"]),
        }

    @staticmethod
    def _serialize_application(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
        return {
            "application_id": row["application_id"],
            "account_id": row["account_id"],
            "action_id": row["action_id"],
            "revision_id": row["action_revision_id"],
            "symbol": row["symbol"],
            "action_type": row["action_type"],
            "effective_date": row["effective_date"],
            "outcome": row["outcome"],
            "cash_delta": float(row["cash_delta"]),
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
            "notes": json.loads(row["notes_json"]),
            "applied_at": row["applied_at"],
            "idempotent": idempotent,
        }
