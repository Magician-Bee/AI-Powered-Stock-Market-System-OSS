from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_board_lot import BoardLotTradeModel
from open_stock_ai.execution.paper_execution_model import (
    HistoricalOrderBookReceiptStore,
    resolve_execution,
)
from open_stock_ai.execution.paper_borrow import PaperBorrowLedger
from open_stock_ai.execution.taiwan_market_rules import TaiwanPaperMarketRules
from open_stock_ai.execution.taiwan_settlement import settlement_receipt
from open_stock_ai.execution.trading_restrictions import TradingRestrictionLedger
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.storage.sqlite_store import SQLiteStore


OPEN_STATUSES = {"submitted", "acknowledged", "open", "triggered", "partially_filled"}
FINAL_STATUSES = {"filled", "canceled", "rejected", "replaced", "expired"}
ORDER_TYPES = {"market", "limit", "stop", "stop_limit"}
TIME_IN_FORCE = {"rod", "ioc", "fok"}
LOT_TYPES = {"board_lot", "odd_lot"}
SESSIONS = {"regular", "after_hours"}


@dataclass
class PaperBrokerSimulator:
    """Broker-style order lifecycle on top of the persistent Paper OMS.

    The broker simulator owns order tickets, acknowledgement, pending states,
    replacement, expiry, triggers and cancellations. Actual cash, fills and
    positions remain delegated to ``PaperOMS`` so every execution path shares
    one accounting ledger.
    """

    store: SQLiteStore
    oms: PaperOMS
    historical_order_book_store: HistoricalOrderBookReceiptStore | None = None
    retention_ledger: ContentAddressedRetentionLedger | None = None
    odd_lot_execution_model: OddLotBoardProxyModel | None = None
    board_lot_execution_model: BoardLotTradeModel | None = None
    host_execution_context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.retention_ledger is not None and self.retention_ledger.store is not None:
            retention_store = getattr(self.retention_ledger.store, "path", None)
            if retention_store is None or Path(retention_store).resolve() != Path(self.store.path).resolve():
                raise ValueError("paper_broker_retention_must_share_runtime_database")
            if self.oms.retention_ledger is None:
                self.oms.retention_ledger = self.retention_ledger
            elif self.oms.retention_ledger is not self.retention_ledger:
                oms_retention_store = getattr(self.oms.retention_ledger.store, "path", None)
                if oms_retention_store is None or Path(oms_retention_store).resolve() != Path(self.store.path).resolve():
                    raise ValueError("paper_broker_oms_retention_must_share_runtime_database")

    def preview(self, ticket: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_ticket(ticket)
        price = self._positive(market.get("price"), "market_price")
        account = self.oms.portfolio_summary()
        quantity = self._resolve_quantity(normalized, account, price)
        current_position = self._position(account, normalized["symbol"])
        marketable, triggered = self._marketability(normalized, price, previously_triggered=False)
        estimated_fill = self._estimated_fill_price(normalized, price, marketable=marketable)
        market_rules = self._market_rules({**normalized, "requested_quantity": quantity}, market)
        simulation = market_rules.get("paper_odd_lot_simulation") or market_rules.get("paper_board_lot_simulation")
        if simulation and simulation.get("simulated_fill_price") is not None:
            estimated_fill = simulation["simulated_fill_price"]
            marketable, triggered = self._marketability(normalized, estimated_fill, previously_triggered=False)
            if not marketable:
                # A resting limit cannot spend above its limit merely because
                # this quote's adverse scenario price is currently unmarketable.
                estimated_fill = self._estimated_fill_price(normalized, price, marketable=False)
        validation = self._validate_account_side(
            side=normalized["side"],
            quantity=quantity,
            current_position=current_position,
            account=account,
            price=estimated_fill,
            order_id=normalized.get("order_id"),
        )
        settlement = settlement_receipt(market=market)
        restrictions = TradingRestrictionLedger(self.store).evaluate(
            ticket=normalized,
            market=market,
        )
        if validation["valid"] and not restrictions["allowed"]:
            validation = {
                "valid": False,
                "reason": restrictions["reason"],
                "restriction_gate": True,
            }
        if validation["valid"] and market_rules["enforced"] and not market_rules["allowed"]:
            validation = {
                "valid": False,
                "reason": market_rules["reason"] or "exchange_market_rule_rejected",
                "market_rule_gate": True,
            }
        gross_amount = quantity * estimated_fill
        commission = self.oms.commission_for_notional(gross_amount, order_id=normalized.get("order_id"))
        is_buy = self._is_buy_side(normalized["side"])
        tax = gross_amount * self.oms.sell_tax_bps / 10_000.0 if not is_buy else 0.0
        estimated_total = gross_amount + commission if is_buy else gross_amount - commission - tax
        cost_evidence = self._local_paper_cost_evidence()
        borrow = None
        if normalized["side"] == "short_sell":
            borrow = PaperBorrowLedger(self.store, self.oms.account_id).preview(
                symbol=normalized["symbol"], quantity=quantity,
                receipt=normalized.get("borrow_receipt"), as_of=market.get("source_timestamp"),
            )
            if validation["valid"] and not borrow["allowed"]:
                validation = {"valid": False, "reason": borrow["reason"], "borrow_gate": True, "borrow": borrow}
        return {
            "schema_version": "open_stock_ai.paper_broker_preview.v1",
            "ticket": {**normalized, "requested_quantity": quantity},
            "market": market,
            "marketable_now": marketable,
            "stop_triggered_now": triggered,
            "estimated_fill_price": round(estimated_fill, 6),
            "estimated_costs": {
                "gross_amount": round(gross_amount, 2),
                "commission": round(commission, 2),
                "tax": round(tax, 2),
                "estimated_total": round(estimated_total, 2),
            },
            # PaperOMS deliberately accepts local simulation assumptions.  Keep
            # the assumptions adjacent to every quote so a zero-value setting
            # cannot be mistaken for an account-verified broker cost schedule.
            "cost_evidence": cost_evidence,
            "paper_odd_lot_simulation": market_rules.get("paper_odd_lot_simulation"),
            **({"paper_board_lot_simulation": simulation} if market_rules.get("paper_board_lot_simulation") else {}),
            "account": account,
            "validation": validation,
            "market_rules": market_rules,
            "settlement": settlement,
            "trading_restrictions": restrictions,
            "borrow": borrow,
            "can_submit": validation["valid"],
            "execution_expectation": (
                "blocked_by_trading_restriction"
                if validation.get("restriction_gate")
                else self._execution_expectation(normalized, marketable, triggered)
                if not market_rules["enforced"] or market_rules["matching_allowed"]
                else "wait_for_exchange_session"
            ),
        }

    def _market_rules(self, ticket: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        rules = TaiwanPaperMarketRules().evaluate(ticket=ticket, market=market)
        model = self.odd_lot_execution_model if ticket.get("lot_type") == "odd_lot" else self.board_lot_execution_model
        if model is None:
            return rules
        simulation = model.evaluate(
            ticket=ticket, market=market, rules=rules, now=self._timestamp(self._now()), slippage_bps=self.oms.slippage_bps,
        )
        # Preserve the strict exchange result alongside the explicit scenario.
        # Never mutate odd_lot_auction_matched or the observed market price.
        return {**rules, "strict_matching_allowed": rules["matching_allowed"],
                "matching_allowed": bool(simulation["eligible"]),
                ("paper_odd_lot_simulation" if ticket.get("lot_type") == "odd_lot" else "paper_board_lot_simulation"): simulation,
                "matching_boundary": simulation["assumption"],
                "session": {**rules["session"], "matching_allowed": bool(simulation["eligible"]),
                            "reason": None if simulation["eligible"] else (simulation["blockers"] or ["awaiting_paper_proxy_opportunity"])[0]}}

    def _local_paper_cost_evidence(self) -> dict[str, Any]:
        """Describe paper-cost assumptions without certifying a live account.

        The Paper Broker is intentionally useful before a broker account exists.
        Its environment-configured commission, tax and slippage are therefore a
        local scenario only, never a substitute for Q-003's immutable broker
        schedule receipt and empirical market-impact calibration.
        """

        configured = {
            "commission_bps": float(self.oms.commission_bps),
            "minimum_commission": float(self.oms.minimum_commission),
            "sell_tax_bps": float(self.oms.sell_tax_bps),
            "slippage_bps": float(self.oms.slippage_bps),
        }
        missing_assumptions = [
            field for field, value in configured.items() if value == 0.0
        ]
        blockers = [
            "broker_account_cost_schedule_receipt_missing",
            "reviewed_exchange_fee_schedule_receipt_missing",
            "empirical_market_impact_calibration_missing",
        ]
        if missing_assumptions:
            blockers.append("local_paper_cost_assumption_zero_or_unconfigured")
        return {
            "schema_version": "open_stock_ai.paper_cost_evidence.v1",
            "cost_source": "local_paper_configuration",
            "execution_evidence_eligible": False,
            "is_simulated": True,
            "assumptions": configured,
            "zero_or_unconfigured_assumptions": missing_assumptions,
            "blockers": blockers,
        }

    def submit(self, ticket: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        self.oms.settle_due(as_of=market.get("source_timestamp"))
        self.oms.accrue_borrow_fees(
            as_of=market.get("source_timestamp"),
            market_prices={str(market.get("symbol") or ticket.get("symbol") or "").upper(): float(market.get("price") or 0)},
        )
        normalized = self._normalize_ticket(ticket)
        order_id = str(normalized.get("order_id") or f"PB-{uuid4().hex}")
        normalized["order_id"] = order_id
        preview = self.preview(normalized, market)
        if not preview["validation"]["valid"]:
            return self._persist_rejected(
                order_id=order_id,
                ticket=normalized,
                preview=preview,
                market=market,
                reason=str(preview["validation"]["reason"]),
            )

        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "select * from paper_broker_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if existing is not None:
                result = self._result(conn, order_id, idempotent=True)
                conn.commit()
            else:
                self._insert_order(
                    conn,
                    order_id=order_id,
                    ticket=normalized,
                    preview=preview,
                    market=market,
                )
                conn.commit()
                result = None

        if result is not None:
            return self._retain_execution_result(result)
        return self._retain_execution_result(self._acknowledge_and_evaluate(order_id, market))

    def process_market_tick(self, symbol: str, market: dict[str, Any]) -> dict[str, Any]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("missing_symbol")
        settlements = self.oms.settle_due(as_of=market.get("source_timestamp"))
        borrow_fees = self.oms.accrue_borrow_fees(
            as_of=market.get("source_timestamp"),
            market_prices={normalized_symbol: float(market.get("price") or 0)},
        )
        expired = self.expire_due(as_of=market.get("source_timestamp"))
        expired.extend(self._expire_rod_orders(market))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select order_id from paper_broker_orders
                where account_id = ? and upper(symbol) = ? and status in ('submitted', 'acknowledged', 'open', 'triggered', 'partially_filled')
                order by created_at asc
                """,
                (self.oms.account_id, normalized_symbol),
            ).fetchall()
        results = [self._evaluate_order(str(row["order_id"]), market) for row in rows]
        results = [self._retain_execution_result(result) for result in results]
        expired = [self._retain_execution_result(result) for result in expired]
        return {
            "schema_version": "open_stock_ai.paper_broker_tick.v1",
            "symbol": normalized_symbol,
            "market": market,
            "settlements": settlements,
            "borrow_fees": borrow_fees,
            "expired": expired,
            "processed_count": len(results),
            "results": results,
        }

    def cancel(self, order_id: str, *, reason: str = "user_requested") -> dict[str, Any]:
        now = self._now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from paper_broker_orders where order_id = ? and account_id = ?",
                (order_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            if str(row["status"]) in FINAL_STATUSES:
                result = self._result(conn, order_id, idempotent=True)
                conn.commit()
                return self._retain_execution_result(result)
            conn.execute(
                """
                update paper_broker_orders
                set status = 'canceled', canceled_at = ?, updated_at = ?, rejection_reason = ?
                where order_id = ?
                """,
                (now, now, reason, order_id),
            )
            self._event(
                conn,
                order_id=order_id,
                event_type="canceled",
                status="canceled",
                market_price=row["last_market_price"],
                detail=reason,
                payload={},
            )
            conn.commit()
            result = self._result(conn, order_id)
        return self._retain_execution_result(result)

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Return one durable paper order and its complete lifecycle events."""

        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from paper_broker_orders where order_id = ? and account_id = ?",
                (order_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            self._reconcile_committed_fills(conn, row)
            conn.commit()
            return self._result(conn, order_id)

    def replace(
        self,
        order_id: str,
        replacement: dict[str, Any],
        market: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace an active order while retaining every existing fill.

        A replacement never mutates an original ticket.  The old order reaches
        the terminal ``replaced`` state, while a fresh order references it via
        ``replaces_order_id``.  Quantity defaults to the old order's remaining
        quantity and may only be reduced, so a replace cannot silently enlarge
        exposure after a partial fill.
        """

        market_price = self._positive(market.get("price"), "market_price")
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from paper_broker_orders where order_id = ? and account_id = ?",
                (order_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            if str(row["status"]) in FINAL_STATUSES:
                return self._result(conn, order_id, idempotent=True)
            if str(row["status"]) not in OPEN_STATUSES:
                raise ValueError("paper_broker_order_not_replaceable")

            filled_quantity = self._filled_quantity(order_id, conn=conn)
            remaining_quantity = max(0.0, float(row["requested_quantity"] or 0.0) - filled_quantity)
            if remaining_quantity <= 0:
                raise ValueError("paper_broker_order_has_no_remaining_quantity")
            ticket = self._ticket_from_row(row)
            ticket.update(
                {
                    "order_id": str(replacement.get("order_id") or f"PB-{uuid4().hex}"),
                    "quantity_lots": None,
                    "quantity_shares": remaining_quantity,
                    "cash_amount": None,
                    "position_size_pct": None,
                    "replaces_order_id": order_id,
                }
            )
            allowed = {
                "order_id",
                "quantity_lots",
                "quantity_shares",
                "cash_amount",
                "position_size_pct",
                "limit_price",
                "stop_price",
                "expires_at",
                "rationale",
                "actor",
            }
            ticket.update({key: value for key, value in replacement.items() if key in allowed and value is not None})
            if str(ticket["order_id"]) == order_id:
                raise ValueError("replacement_order_id_must_differ")
            normalized = self._normalize_ticket(ticket)
            if (
                normalized["symbol"] != str(row["symbol"])
                or normalized["side"] != str(row["side"])
                or normalized.get("market") != row["market"]
                or normalized["order_type"] != str(row["order_type"])
                or normalized["time_in_force"] != str(row["time_in_force"])
                or normalized["lot_type"] != str(row["lot_type"])
                or normalized["session"] != str(row["session"])
            ):
                raise ValueError("replacement_may_only_change_price_quantity_expiry_or_rationale")
            preview = self.preview(normalized, market)
            replacement_quantity = float(preview["ticket"]["requested_quantity"] or 0.0)
            if replacement_quantity > remaining_quantity + 1e-9:
                raise ValueError("replacement_quantity_exceeds_remaining_quantity")
            if not preview["validation"]["valid"]:
                raise ValueError(f"replacement_rejected:{preview['validation']['reason']}")
            duplicate = conn.execute(
                "select 1 from paper_broker_orders where order_id = ?",
                (normalized["order_id"],),
            ).fetchone()
            if duplicate is not None:
                return self._result(conn, normalized["order_id"], idempotent=True)

            now = self._now()
            conn.execute(
                """
                update paper_broker_orders
                   set status = 'replaced', replaced_at = ?, replaced_by_order_id = ?,
                       updated_at = ?, rejection_reason = null, last_market_price = ?
                 where order_id = ?
                """,
                (now, normalized["order_id"], now, market_price, order_id),
            )
            self._event(
                conn,
                order_id=order_id,
                event_type="replaced",
                status="replaced",
                market_price=market_price,
                detail="Remaining quantity replaced by a new immutable order ticket",
                payload={"replacement_order_id": normalized["order_id"], "remaining_quantity": remaining_quantity},
            )
            self._insert_order(
                conn,
                order_id=normalized["order_id"],
                ticket=normalized,
                preview=preview,
                market=market,
                replaces_order_id=order_id,
            )
            conn.commit()

        replacement_result = self._acknowledge_and_evaluate(normalized["order_id"], market)
        replacement_result["replaced_order_id"] = order_id
        self._retain_execution_result(self.get_order(order_id))
        return self._retain_execution_result(replacement_result)

    def expire_due(self, *, as_of: str | datetime | None = None) -> list[dict[str, Any]]:
        """Persist expiry for every active ticket whose explicit deadline passed."""

        observed_at = self._timestamp(as_of) if as_of is not None else datetime.now(timezone.utc)
        expired_ids: list[str] = []
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from paper_broker_orders
                 where account_id = ? and expires_at is not null
                   and status in ('submitted', 'acknowledged', 'open', 'triggered', 'partially_filled')
                """,
                (self.oms.account_id,),
            ).fetchall()
            for row in rows:
                if self._timestamp(row["expires_at"]) > observed_at:
                    continue
                self._mark_expired(
                    conn,
                    row=row,
                    observed_at=observed_at,
                    market_price=row["last_market_price"],
                    reason="explicit_order_expiry_reached",
                )
                expired_ids.append(str(row["order_id"]))
            conn.commit()
        return [self._retain_execution_result(self.get_order(order_id)) for order_id in expired_ids]

    def _expire_rod_orders(self, market: dict[str, Any]) -> list[dict[str, Any]]:
        """Expire active ROD orders after their declared Taiwan session ends."""

        if market.get("exchange_rules_enforced") is not True:
            return []
        rules = TaiwanPaperMarketRules()
        expired_ids: list[str] = []
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from paper_broker_orders
                 where account_id = ?
                   and status in ('submitted', 'acknowledged', 'open', 'triggered', 'partially_filled')
                   and time_in_force = 'rod'
                """,
                (self.oms.account_id,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"] or "{}"))
                    submitted_market = dict(payload.get("market_at_submit") or {})
                    ticket = self._ticket_from_row(row)
                    if not rules.session_expired(
                        ticket=ticket,
                        submitted_market=submitted_market,
                        observed_market=market,
                    ):
                        continue
                    observed_at = self._timestamp(market.get("source_timestamp"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    # A legacy row without a source timestamp keeps its prior
                    # explicit-expiry contract; it is never silently expired.
                    continue
                self._mark_expired(
                    conn,
                    row=row,
                    observed_at=observed_at,
                    market_price=self._number(market.get("price")) or None,
                    reason="rod_exchange_session_expired",
                )
                expired_ids.append(str(row["order_id"]))
            conn.commit()
        return [self._retain_execution_result(self.get_order(order_id)) for order_id in expired_ids]

    def recent_orders(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        params: list[Any] = [self.oms.account_id]
        where = "account_id = ?"
        normalized_status = str(status or "").strip().lower()
        if normalized_status == "open":
            where += " and status in ('submitted', 'acknowledged', 'open', 'triggered', 'partially_filled')"
        elif normalized_status in FINAL_STATUSES | {"submitted"}:
            where += " and status = ?"
            params.append(normalized_status)
        params.append(limit)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from paper_broker_orders where {where} order by created_at desc limit ?",
                tuple(params),
            ).fetchall()
            return [self._serialize_order(conn, row) for row in rows]

    def open_order_reservations(self) -> list[dict[str, Any]]:
        """Return every active commitment for this account, without changing cash.

        Limit orders reserve at their limit. Market/stop orders use the latest
        observed price (or the more conservative buy stop) including configured
        slippage; this estimate cannot guarantee a future market fill price.
        Remaining costs are commission/tax only, since the reservation price
        already includes slippage. Invalid legacy rows remain visible and block
        shared risk admission instead of silently reserving zero.
        """
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """select b.*, coalesce(f.filled_quantity,0) as filled_quantity,
                          coalesce(f.filled_gross,0) as filled_gross,
                          coalesce(f.charged_commission,0) as charged_commission
                   from paper_broker_orders b
                   left join (
                       select order_id, sum(quantity) as filled_quantity,
                              sum(gross_amount) as filled_gross, sum(commission) as charged_commission
                       from paper_fills where account_id=? group by order_id
                   ) f on f.order_id=b.order_id
                   where b.account_id=? and b.status in
                         ('submitted','acknowledged','open','triggered','partially_filled')
                   order by b.created_at,b.order_id""",
                (self.oms.account_id, self.oms.account_id),
            ).fetchall()
        reservations = []
        for row in rows:
            blockers = []
            remaining = self._number(row["requested_quantity"]) - self._number(row["filled_quantity"])
            if not math.isfinite(remaining) or remaining <= 0:
                blockers.append("open_order_remaining_quantity_invalid")
                remaining = None
            kind, side = str(row["order_type"]), str(row["side"])
            price = self._number(row["limit_price"] if kind in {"limit", "stop_limit"} else row["last_market_price"])
            basis = "order_limit" if kind in {"limit", "stop_limit"} else "latest_market_mark_with_slippage"
            if kind == "stop":
                stop = self._number(row["stop_price"])
                if not math.isfinite(stop) or stop <= 0:
                    blockers.append("open_order_stop_price_missing")
                else:
                    price = max(price, stop) if side == "buy" and math.isfinite(price) and price > 0 else price
                basis = "latest_market_mark_or_buy_stop_with_slippage"
            if side not in {"buy", "sell"} or kind not in ORDER_TYPES:
                blockers.append("open_order_type_or_side_unsupported")
            if math.isfinite(price) and price > 0 and kind not in {"limit", "stop_limit"}:
                price = self._estimated_fill_price({"order_type": kind, "side": side}, price, marketable=True)
            if not math.isfinite(price) or price <= 0:
                blockers.append("open_order_reservation_price_missing")
                price = None
            cost = None
            if remaining is not None and price is not None:
                gross = remaining * price
                commission = max(0.0, max(self.oms.minimum_commission,
                    (float(row["filled_gross"]) + gross) * self.oms.commission_bps / 10_000.0) - float(row["charged_commission"]))
                cost = commission + (gross * self.oms.sell_tax_bps / 10_000.0 if side == "sell" else 0)
                if not math.isfinite(gross + cost):
                    blockers.append("open_order_reservation_cost_invalid")
                    cost = None
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                industry = (payload.get("market_at_submit") or {}).get("industry")
            except (ValueError, TypeError, AttributeError):
                industry = None
            reservations.append({"order_id": row["order_id"], "account_id": self.oms.account_id,
                "symbol": row["symbol"], "side": side, "status": row["status"], "industry": industry,
                "remaining_quantity": remaining, "reservation_price": price, "price_basis": basis,
                "estimated_remaining_cost": cost, "cost_basis": "incremental_order_commission_plus_sell_tax",
                "valid": not blockers, "blockers": blockers})
        return reservations

    def recent_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select f.*, b.order_type, b.time_in_force, b.lot_type, b.session,
                       b.episode_id, b.actor, b.rationale,
                       s.settlement_due_at, s.status as settlement_status,
                       s.payable as settlement_payable, s.receivable as settlement_receivable
                from paper_fills f
                left join paper_broker_orders b on b.order_id = f.order_id
                left join paper_settlements s on s.fill_id = f.fill_id
                where f.account_id = ?
                order by f.created_at desc
                limit ?
                """,
                (self.oms.account_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _retain_execution_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Persist each broker lifecycle projection as immutable critical evidence."""

        if self.retention_ledger is None or not isinstance(result, dict):
            return result
        if isinstance(result.get("retention"), dict):
            return result
        order = result.get("order") if isinstance(result.get("order"), dict) else {}
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return result
        payload = {
            "schema_version": "stock_ai.paper_broker_execution_snapshot.v1",
            "order_id": order_id,
            "source_of_truth": "paper_broker_simulator",
            "result": dict(result),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        snapshot_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        retained = self.retention_ledger.append(
            f"paper-broker-execution-{order_id}-{snapshot_hash[:32]}",
            payload,
            critical=True,
            kind="execution_broker_state",
            occurred_at=str(order.get("updated_at") or order.get("created_at") or "") or None,
        )
        return {**result, "retention": retained}

    def open_symbols(self) -> list[str]:
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                select distinct symbol from paper_broker_orders
                where account_id = ? and status in ('submitted', 'acknowledged', 'open', 'triggered', 'partially_filled')
                order by symbol
                """,
                (self.oms.account_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def _evaluate_order(self, order_id: str, market: dict[str, Any]) -> dict[str, Any]:
        market_price = self._positive(market.get("price"), "market_price")
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from paper_broker_orders where order_id = ? and account_id = ?",
                (order_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            row = self._reconcile_committed_fills(conn, row)
            conn.commit()
            if str(row["status"]) in FINAL_STATUSES:
                return self._result(conn, order_id, idempotent=True)
            observed_at = self._timestamp(market.get("source_timestamp")) if market.get("source_timestamp") else datetime.now(timezone.utc)
            if row["expires_at"] and self._timestamp(row["expires_at"]) <= observed_at:
                self._mark_expired(
                    conn,
                    row=row,
                    observed_at=observed_at,
                    market_price=market_price,
                    reason="explicit_order_expiry_reached",
                )
                conn.commit()
                return self._result(conn, order_id)
            ticket = self._ticket_from_row(row)
            restrictions = TradingRestrictionLedger(self.store).evaluate(
                ticket=ticket,
                market=market,
            )
            if not restrictions["allowed"]:
                now = self._now()
                next_status = (
                    "canceled"
                    if ticket["time_in_force"] in {"ioc", "fok"}
                    else "partially_filled"
                    if str(row["status"]) == "partially_filled"
                    else "open"
                )
                conn.execute(
                    """
                    update paper_broker_orders
                       set status=?, updated_at=?, last_market_price=?,
                           canceled_at=case when ?='canceled' then ? else canceled_at end,
                           rejection_reason=?
                     where order_id=?
                    """,
                    (
                        next_status,
                        now,
                        market_price,
                        next_status,
                        now,
                        restrictions["reason"],
                        order_id,
                    ),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="restricted",
                    status=next_status,
                    market_price=market_price,
                    detail=str(restrictions["reason"]),
                    payload={"market": market, "trading_restrictions": restrictions},
                )
                conn.commit()
                result = self._result(conn, order_id)
                result["trading_restrictions"] = restrictions
                return result
            market_rules = self._market_rules(ticket, market)
            if market_rules["enforced"] and (not market_rules["order_valid"] or not market_rules["matching_allowed"]):
                conn.commit()
                result = self._rest_active(
                    order_id,
                    market_price,
                    market,
                    (
                        str(market_rules["reason"] or "exchange_market_rule_pending")
                        if not market_rules["order_valid"]
                        else str(market_rules["session"]["reason"] or "awaiting_exchange_match")
                    ),
                    execution={key: market_rules[key] for key in ("paper_odd_lot_simulation", "paper_board_lot_simulation") if key in market_rules} | {
                               "execution_evidence_eligible": False, "execution_allowed": False}
                    if market_rules.get("paper_odd_lot_simulation") or market_rules.get("paper_board_lot_simulation") else None,
                )
                result["market_rules"] = market_rules
                return result
            previously_triggered = str(row["status"]) == "triggered" or bool(row["triggered_at"])
            simulation = market_rules.get("paper_odd_lot_simulation") or market_rules.get("paper_board_lot_simulation")
            matching_price = simulation["simulated_fill_price"] if simulation and simulation["eligible"] else market_price
            marketable, triggered = self._marketability(ticket, matching_price, previously_triggered=previously_triggered)
            now = self._now()
            conn.execute(
                "update paper_broker_orders set updated_at = ?, last_market_price = ? where order_id = ?",
                (now, market_price, order_id),
            )
            if triggered and not previously_triggered:
                conn.execute(
                    "update paper_broker_orders set status = 'triggered', triggered_at = ? where order_id = ?",
                    (now, order_id),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="triggered",
                    status="triggered",
                    market_price=market_price,
                    detail="Stop condition reached",
                    payload={"market": market},
                )
            if marketable:
                conn.commit()
                return self._fill(order_id, ticket, market, simulation=simulation)

            if ticket["time_in_force"] in {"ioc", "fok"}:
                conn.execute(
                    """
                    update paper_broker_orders
                    set status = 'canceled', canceled_at = ?, updated_at = ?, rejection_reason = 'not_marketable_immediately'
                    where order_id = ?
                    """,
                    (now, now, order_id),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="canceled",
                    status="canceled",
                    market_price=market_price,
                    detail="IOC/FOK order was not marketable at submission",
                    payload={"market": market},
                )
            else:
                next_status = (
                    "partially_filled"
                    if str(row["status"]) == "partially_filled"
                    else "triggered"
                    if triggered or previously_triggered
                    else "open"
                )
                conn.execute(
                    "update paper_broker_orders set status = ? where order_id = ?",
                    (next_status, order_id),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="resting",
                    status=next_status,
                    market_price=market_price,
                    detail="Order remains active until its price condition is met or it is canceled",
                    payload={"market": market},
                )
            conn.commit()
            return self._result(conn, order_id)

    def _fill(self, order_id: str, ticket: dict[str, Any], market: dict[str, Any], *, simulation: dict[str, Any] | None = None) -> dict[str, Any]:
        account = self.oms.portfolio_summary()
        price = float(market["price"])
        requested_quantity = float(ticket["requested_quantity"])
        already_filled = self._filled_quantity(order_id)
        remaining_quantity = max(0.0, requested_quantity - already_filled)
        fill_sequence = self._fill_count(order_id) + 1
        execution_market = self._execution_market(market)
        if simulation:
            supplied_cap = execution_market.get("volume_cap")
            execution_market["volume_cap"] = min(float(supplied_cap) if supplied_cap is not None else remaining_quantity, simulation["quantity_cap"])
        execution = resolve_execution(
            order_id=order_id,
            fill_sequence=fill_sequence,
            remaining_quantity=remaining_quantity,
            market=execution_market,
            prior_state=ticket.get("execution_state"),
        )
        available_quantity = float(execution["available_quantity"])
        quantity = float(execution["fill_quantity"])
        model = self.odd_lot_execution_model if ticket.get("lot_type") == "odd_lot" else self.board_lot_execution_model
        if simulation and model is not None:
            try:
                quantity = model.allocate(
                    self.store, account_id=self.oms.account_id, order_id=order_id, fill_sequence=fill_sequence,
                    requested=quantity, receipt=simulation,
                )
            except ValueError as exc:
                quantity = 0.0
                execution["blockers"].append(str(exc))
            simulation_key = "paper_odd_lot_simulation" if ticket.get("lot_type") == "odd_lot" else "paper_board_lot_simulation"
            execution.update(fill_quantity=quantity, execution_allowed=quantity > 0, execution_evidence_eligible=False,
                             **{simulation_key: simulation})
            execution["evidence_blockers"].append("simulated_odd_lot_board_proxy_is_not_execution_evidence"
                if ticket.get("lot_type") == "odd_lot" else "simulated_board_trade_is_not_execution_evidence")
            if quantity <= 0 and not execution["blockers"]:
                execution["blockers"].append("paper_odd_lot_proxy_quote_budget_exhausted"
                    if ticket.get("lot_type") == "odd_lot" else "paper_board_trade_quote_budget_exhausted")
        self._persist_execution_state(order_id, execution)
        if ticket["time_in_force"] == "fok" and quantity < remaining_quantity - 1e-9:
            return self._cancel_unfilled(
                order_id, price, market,
                "fok_insufficient_available_quantity",
                execution=execution,
            )
        if quantity <= 0:
            if ticket["time_in_force"] == "ioc":
                reason = str(execution["blockers"][0]) if execution["blockers"] else "ioc_no_available_quantity"
                return self._cancel_unfilled(order_id, price, market, reason, execution=execution)
            detail = (
                "Execution model blocked this tick: " + ", ".join(execution["blockers"])
                if execution["blockers"]
                else "No executable quantity is available in this tick"
            )
            return self._rest_active(order_id, price, market, detail, execution=execution)
        position = self._position(account, ticket["symbol"])
        if ticket["side"] in {"buy", "short_sell"}:
            equity = float(account.get("total_equity") or 0.0)
            # The OMS must retain the original ticket quantity; only the
            # separate ``fill_quantity`` is constrained by this market tick.
            position_size_pct = requested_quantity * price / equity * 100.0 if equity > 0 else 0.0
            action = "short_sell" if ticket["side"] == "short_sell" else "buy"
        else:
            current_quantity = float((position or {}).get("quantity") or 0.0)
            if ticket["side"] == "buy_to_cover":
                position_size_pct = requested_quantity / abs(current_quantity) * 100.0 if current_quantity < 0 else 0.0
                action = "buy_to_cover"
            else:
                position_size_pct = requested_quantity / current_quantity * 100.0 if current_quantity > 0 else 0.0
                action = "sell" if requested_quantity >= current_quantity - 1e-9 else "reduce"

        reference_price = self._execution_reference(
            ticket,
            price,
            extra_impact_bps=float(execution["total_impact_bps"]),
        )
        if simulation:
            # The adverse tick-rounded scenario price already includes OMS
            # slippage; the OMS applies that same slippage exactly once.
            multiplier = 1 + self.oms.slippage_bps / 10000 if ticket["side"] == "buy" else 1 - self.oms.slippage_bps / 10000
            reference_price = simulation["simulated_fill_price"] / multiplier
        oms_result = self.oms.submit_partial_fill(
            {
                "order_id": order_id,
                "symbol": ticket["symbol"],
                "market": ticket.get("market"),
                "action": action,
                "entry_price": reference_price,
                "position_size_pct": position_size_pct,
                # The broker simulator reaches this point only after its
                # account-side and trading-restriction preview gates pass.
                "risk_approved": True,
                "change_id": ticket.get("change_id"),
                "paper_broker_ticket": ticket,
                "borrow_receipt": ticket.get("borrow_receipt"),
                "market_context": market,
            },
            fill_quantity=quantity,
            fill_id=f"PBF-{order_id}-{fill_sequence}",
            execution_context={**(self.host_execution_context or {}), "execution_model": execution},
        )
        now = self._now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            oms_order = dict(oms_result.get("order") or {})
            filled_quantity = float(oms_order.get("filled_quantity") or 0.0)
            remaining_after = float(oms_order.get("remaining_quantity") or 0.0)
            if oms_result.get("filled"):
                conn.execute(
                    """
                    update paper_broker_orders
                    set status = 'filled', filled_at = ?, updated_at = ?, rejection_reason = null,
                        last_market_price = ?
                    where order_id = ?
                    """,
                    (now, now, price, order_id),
                )
                event_type = "filled"
                detail = "Order filled by Paper OMS"
                status = "filled"
            elif oms_result.get("partially_filled"):
                if ticket["time_in_force"] == "ioc":
                    conn.execute(
                        """
                        update paper_broker_orders
                           set status = 'canceled', canceled_at = ?, updated_at = ?, rejection_reason = 'ioc_remaining_cancelled',
                               last_market_price = ?
                         where order_id = ?
                        """,
                        (now, now, price, order_id),
                    )
                    event_type = "partially_filled_then_canceled"
                    detail = "IOC order filled available quantity and canceled remainder"
                    status = "canceled"
                else:
                    rejection_reason = str((oms_result.get("order") or {}).get("rejection_reason") or "") or None
                    conn.execute(
                        """
                        update paper_broker_orders
                           set status = 'partially_filled', updated_at = ?, rejection_reason = ?,
                               last_market_price = ?
                         where order_id = ?
                        """,
                        (now, rejection_reason, price, order_id),
                    )
                    event_type = "partially_filled"
                    detail = "Order remains active with unfilled quantity"
                    status = "partially_filled"
            else:
                reason = str((oms_result.get("order") or {}).get("rejection_reason") or "paper_oms_rejected")
                conn.execute(
                    """
                    update paper_broker_orders
                    set status = 'rejected', updated_at = ?, rejection_reason = ?, last_market_price = ?
                    where order_id = ?
                    """,
                    (now, reason, price, order_id),
                )
                event_type = "rejected"
                detail = reason
                status = "rejected"
            self._event(
                conn,
                order_id=order_id,
                event_type=event_type,
                status=status,
                market_price=price,
                detail=detail,
                payload={
                    "market": market,
                    "oms": oms_result,
                    "filled_quantity": filled_quantity,
                    "remaining_quantity": remaining_after,
                    "available_quantity": available_quantity,
                    "execution_model": execution,
                },
            )
            conn.commit()
            result = self._result(conn, order_id)
            result["oms"] = oms_result
            return result

    def _execution_market(self, market: dict[str, Any]) -> dict[str, Any]:
        """Resolve an explicit historical order-book ID before simulation."""

        receipt_id = str(market.get("historical_order_book_receipt_id") or "").strip()
        if not receipt_id or self.historical_order_book_store is None:
            return dict(market)
        resolved = dict(market)
        try:
            receipt = self.historical_order_book_store.by_receipt(receipt_id)
        except ValueError:
            receipt = None
        if receipt is not None:
            resolved["historical_order_book_receipt"] = receipt
        return resolved

    def _insert_order(
        self,
        conn: sqlite3.Connection,
        *,
        order_id: str,
        ticket: dict[str, Any],
        preview: dict[str, Any],
        market: dict[str, Any],
        replaces_order_id: str | None = None,
    ) -> None:
        now = self._now()
        conn.execute(
            """
            insert into paper_broker_orders (
                order_id, account_id, created_at, updated_at, symbol, market,
                side, order_type, time_in_force, lot_type, session,
                requested_lots, requested_quantity, limit_price, stop_price,
                status, triggered_at, acknowledged_at, filled_at, canceled_at,
                expires_at, expired_at, replaced_at, replaces_order_id, replaced_by_order_id,
                rejection_reason, episode_id, actor, rationale,
                last_market_price, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                self.oms.account_id,
                now,
                now,
                ticket["symbol"],
                ticket.get("market"),
                ticket["side"],
                ticket["order_type"],
                ticket["time_in_force"],
                ticket["lot_type"],
                ticket["session"],
                ticket.get("quantity_lots"),
                preview["ticket"]["requested_quantity"],
                ticket.get("limit_price"),
                ticket.get("stop_price"),
                "submitted",
                None,
                None,
                None,
                None,
                ticket.get("expires_at"),
                None,
                None,
                replaces_order_id,
                None,
                None,
                ticket.get("episode_id"),
                ticket["actor"],
                ticket.get("rationale"),
                float(market["price"]),
                json.dumps(
                    {
                        "ticket": ticket,
                        "market_at_submit": market,
                        "preview": preview,
                        "replaces_order_id": replaces_order_id,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ),
        )
        self._event(
            conn,
            order_id=order_id,
            event_type="submitted",
            status="submitted",
            market_price=float(market["price"]),
            detail="Order ticket accepted by paper broker simulator",
            payload={"ticket": ticket, "replaces_order_id": replaces_order_id},
        )

    def _acknowledge_and_evaluate(self, order_id: str, market: dict[str, Any]) -> dict[str, Any]:
        market_price = self._positive(market.get("price"), "market_price")
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from paper_broker_orders where order_id = ? and account_id = ?",
                (order_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            if str(row["status"]) == "submitted":
                now = self._now()
                conn.execute(
                    """
                    update paper_broker_orders
                       set status = 'acknowledged', acknowledged_at = ?, updated_at = ?,
                           last_market_price = ?
                     where order_id = ?
                    """,
                    (now, now, market_price, order_id),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="acknowledged",
                    status="acknowledged",
                    market_price=market_price,
                    detail="Paper broker simulator durably acknowledged the order ticket",
                    payload={"market": market},
                )
                conn.commit()
        return self._evaluate_order(order_id, market)

    def _persist_rejected(
        self,
        *,
        order_id: str,
        ticket: dict[str, Any],
        preview: dict[str, Any],
        market: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "select 1 from paper_broker_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if existing is not None:
                result = self._result(conn, order_id, idempotent=True)
                conn.commit()
            else:
                self._insert_order(
                    conn,
                    order_id=order_id,
                    ticket=ticket,
                    preview=preview,
                    market=market,
                )
                now = self._now()
                conn.execute(
                    """
                    update paper_broker_orders
                       set status = 'rejected', updated_at = ?, rejection_reason = ?
                     where order_id = ?
                    """,
                    (now, reason, order_id),
                )
                self._event(
                    conn,
                    order_id=order_id,
                    event_type="rejected",
                    status="rejected",
                    market_price=float(market["price"]),
                    detail=reason,
                    payload={"preview": preview},
                )
                conn.commit()
                result = self._result(conn, order_id)
                result["preview"] = preview
        return self._retain_execution_result(result)

    def _mark_expired(
        self,
        conn: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        observed_at: datetime,
        market_price: float | None,
        reason: str,
    ) -> None:
        now = self._now()
        order_id = str(row["order_id"])
        conn.execute(
            """
            update paper_broker_orders
               set status = 'expired', expired_at = ?, updated_at = ?,
                   rejection_reason = ?, last_market_price = coalesce(?, last_market_price)
             where order_id = ?
            """,
            (observed_at.isoformat(), now, reason, market_price, order_id),
        )
        self._event(
            conn,
            order_id=order_id,
            event_type="expired",
            status="expired",
            market_price=market_price,
            detail=reason,
            payload={"expires_at": row["expires_at"], "observed_at": observed_at.isoformat()},
        )

    def _normalize_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        symbol = str(ticket.get("symbol") or "").strip().upper()
        side = str(ticket.get("side") or "buy").strip().lower()
        order_type = str(ticket.get("order_type") or "market").strip().lower()
        time_in_force = str(ticket.get("time_in_force") or "rod").strip().lower()
        lot_type = str(ticket.get("lot_type") or "board_lot").strip().lower()
        session = str(ticket.get("session") or "regular").strip().lower()
        if not symbol:
            raise ValueError("missing_symbol")
        if side not in {"buy", "sell", "short_sell", "buy_to_cover"}:
            raise ValueError("side_must_be_buy_sell_short_sell_or_buy_to_cover")
        if order_type not in ORDER_TYPES:
            raise ValueError("unsupported_order_type")
        if time_in_force not in TIME_IN_FORCE:
            raise ValueError("unsupported_time_in_force")
        if lot_type not in LOT_TYPES:
            raise ValueError("unsupported_lot_type")
        if session not in SESSIONS:
            raise ValueError("unsupported_session")
        limit_price = self._optional_positive(ticket.get("limit_price"), "limit_price")
        stop_price = self._optional_positive(ticket.get("stop_price"), "stop_price")
        if order_type in {"limit", "stop_limit"} and limit_price is None:
            raise ValueError("limit_price_required")
        if order_type in {"stop", "stop_limit"} and stop_price is None:
            raise ValueError("stop_price_required")
        expires_at = ticket.get("expires_at")
        normalized_expiry = self._timestamp(expires_at).isoformat() if expires_at not in {None, ""} else None
        return {
            **ticket,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "lot_type": lot_type,
            "session": session,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "expires_at": normalized_expiry,
            "actor": str(ticket.get("actor") or "user").strip().lower() or "user",
            "rationale": str(ticket.get("rationale") or "").strip(),
        }

    def _resolve_quantity(self, ticket: dict[str, Any], account: dict[str, Any], price: float) -> float:
        direct = self._number(ticket.get("quantity_shares"))
        if direct > 0:
            return round(direct, 8)
        lots = self._number(ticket.get("quantity_lots"))
        if lots > 0:
            multiplier = self._lot_multiplier(ticket)
            return round(lots * multiplier, 8)
        cash_amount = self._number(ticket.get("cash_amount"))
        if cash_amount > 0:
            return math.floor(cash_amount / price)
        pct = self._number(ticket.get("position_size_pct"))
        if pct > 0:
            if ticket["side"] in {"buy", "short_sell"}:
                return math.floor(float(account.get("total_equity") or 0.0) * pct / 100.0 / price)
            position = self._position(account, ticket["symbol"])
            held = float((position or {}).get("quantity") or 0.0)
            return math.floor(abs(held) * min(pct, 100.0) / 100.0)
        raise ValueError("quantity_required")

    def _validate_account_side(
        self,
        *,
        side: str,
        quantity: float,
        current_position: dict[str, Any] | None,
        account: dict[str, Any],
        price: float,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        if quantity <= 0:
            return {"valid": False, "reason": "quantity_below_minimum"}
        if side == "sell":
            held = float((current_position or {}).get("quantity") or 0.0)
            if held <= 0:
                return {"valid": False, "reason": "no_position_to_sell", "held_quantity": held}
            if quantity > held + 1e-9:
                return {"valid": False, "reason": "sell_quantity_exceeds_position", "held_quantity": held}
        elif side == "buy_to_cover":
            short = float((current_position or {}).get("quantity") or 0.0)
            if short >= 0:
                return {"valid": False, "reason": "no_short_position_to_cover", "short_quantity": short}
            if quantity > abs(short) + 1e-9:
                return {"valid": False, "reason": "cover_quantity_exceeds_short_position", "short_quantity": short}
            gross = quantity * price
            estimated = gross + self.oms.commission_for_notional(gross, order_id=order_id)
            cash = float(account.get("available_cash", account.get("cash_balance")) or 0.0)
            if estimated > cash + 1e-6:
                return {"valid": False, "reason": "insufficient_settled_cash", "available_cash": cash, "estimated_required": estimated}
        elif side == "short_sell":
            held = float((current_position or {}).get("quantity") or 0.0)
            if held > 1e-9:
                return {"valid": False, "reason": "long_position_must_be_closed_before_short_sale", "held_quantity": held}
        else:
            gross = quantity * price
            estimated = gross + self.oms.commission_for_notional(gross, order_id=order_id)
            cash = float(account.get("available_cash", account.get("cash_balance")) or 0.0)
            if estimated > cash + 1e-6:
                return {
                    "valid": False,
                    "reason": "insufficient_settled_cash",
                    "available_cash": cash,
                    "estimated_required": estimated,
                }
        return {"valid": True, "reason": None}

    def _marketability(self, ticket: dict[str, Any], price: float, *, previously_triggered: bool) -> tuple[bool, bool]:
        is_buy = self._is_buy_side(ticket["side"])
        order_type = ticket["order_type"]
        limit_price = ticket.get("limit_price")
        stop_price = ticket.get("stop_price")
        stop_reached = previously_triggered
        if order_type in {"stop", "stop_limit"} and not stop_reached:
            stop_reached = price >= float(stop_price) if is_buy else price <= float(stop_price)
        if order_type == "market":
            return True, False
        if order_type == "limit":
            return (price <= float(limit_price) if is_buy else price >= float(limit_price)), False
        if order_type == "stop":
            return stop_reached, stop_reached
        limit_crossed = price <= float(limit_price) if is_buy else price >= float(limit_price)
        return stop_reached and limit_crossed, stop_reached

    def _estimated_fill_price(
        self,
        ticket: dict[str, Any],
        price: float,
        *,
        marketable: bool,
        extra_impact_bps: float = 0.0,
    ) -> float:
        if not marketable:
            if ticket["order_type"] in {"limit", "stop_limit"}:
                return float(ticket["limit_price"])
            if ticket["order_type"] == "stop":
                return float(ticket["stop_price"])
            return price
        slip = (self.oms.slippage_bps + max(0.0, float(extra_impact_bps))) / 10_000.0
        estimated = price * (1.0 + slip if self._is_buy_side(ticket["side"]) else 1.0 - slip)
        if ticket["order_type"] in {"limit", "stop_limit"}:
            limit_price = float(ticket["limit_price"])
            estimated = min(estimated, limit_price) if self._is_buy_side(ticket["side"]) else max(estimated, limit_price)
        return estimated

    def _execution_reference(
        self,
        ticket: dict[str, Any],
        price: float,
        *,
        extra_impact_bps: float = 0.0,
    ) -> float:
        desired = self._estimated_fill_price(
            ticket,
            price,
            marketable=True,
            extra_impact_bps=extra_impact_bps,
        )
        slip = self.oms.slippage_bps / 10_000.0
        divisor = 1.0 + slip if self._is_buy_side(ticket["side"]) else max(1e-12, 1.0 - slip)
        return desired / divisor

    @staticmethod
    def _is_buy_side(side: str) -> bool:
        return side in {"buy", "buy_to_cover"}

    def _execution_expectation(self, ticket: dict[str, Any], marketable: bool, triggered: bool) -> str:
        if marketable:
            return "fill_now"
        if ticket["time_in_force"] in {"ioc", "fok"}:
            return "cancel_if_not_immediately_marketable"
        if ticket["order_type"] in {"stop", "stop_limit"} and not triggered:
            return "wait_for_stop_trigger"
        return "rest_as_open_order"

    def _ticket_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            payload_ticket = payload.get("ticket") or {}
            execution_state = payload.get("execution_state") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload_ticket = {}
            execution_state = {}
        return {
            "order_id": row["order_id"],
            "symbol": row["symbol"],
            "market": row["market"],
            "side": row["side"],
            "order_type": row["order_type"],
            "time_in_force": row["time_in_force"],
            "lot_type": row["lot_type"],
            "session": row["session"],
            "quantity_lots": row["requested_lots"],
            "requested_quantity": row["requested_quantity"],
            "quantity_shares": row["requested_quantity"],
            "limit_price": row["limit_price"],
            "stop_price": row["stop_price"],
            "expires_at": row["expires_at"],
            "episode_id": row["episode_id"],
            "actor": row["actor"],
            "rationale": row["rationale"],
            # The order may be acknowledged/restarted before it reaches
            # PaperOMS.  Recover the originally submitted immutable change-set
            # identifier from the durable broker ticket instead of silently
            # dropping it on the broker-to-OMS handoff.
            "change_id": payload_ticket.get("change_id"),
            "borrow_receipt": payload_ticket.get("borrow_receipt"),
            "execution_state": execution_state,
        }

    def _reconcile_committed_fills(self, conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        """Recover only broker projections after an OMS commit/event crash gap."""
        if row["status"] not in OPEN_STATUSES:
            return row
        oms_order = conn.execute("select * from paper_orders where order_id=?", (row["order_id"],)).fetchone()
        if oms_order is None:
            if conn.execute("select 1 from paper_fills where order_id=? limit 1", (row["order_id"],)).fetchone():
                raise ValueError("paper_broker_oms_order_missing_with_fills")
            return row
        try:
            requested = float(row["requested_quantity"])
            oms_requested = float(oms_order["requested_quantity"])
            oms_filled = float(oms_order["filled_quantity"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("paper_broker_oms_contract_mismatch") from None
        actions = {"buy": {"buy"}, "sell": {"sell", "reduce"}, "short_sell": {"short_sell"}, "buy_to_cover": {"buy_to_cover"}}
        if (oms_order["account_id"] != row["account_id"] or oms_order["symbol"] != row["symbol"]
                or oms_order["market"] != row["market"] or oms_order["action"] not in actions.get(row["side"], set())
                or not all(math.isfinite(value) for value in (requested, oms_requested, oms_filled))
                or requested <= 0 or abs(oms_requested - requested) > 1e-8):
            raise ValueError("paper_broker_oms_contract_mismatch")
        fills = conn.execute("select * from paper_fills where order_id=? order by created_at,fill_id", (row["order_id"],)).fetchall()
        if not fills:
            return row
        side = "buy" if self._is_buy_side(row["side"]) else "sell"
        if any(fill["account_id"] != row["account_id"] or fill["symbol"] != row["symbol"] or fill["side"] != side
               or not math.isfinite(fill["quantity"]) or fill["quantity"] <= 0 for fill in fills):
            raise ValueError("paper_broker_oms_fill_identity_mismatch")
        filled = sum(float(fill["quantity"]) for fill in fills)
        if (not math.isfinite(filled) or not math.isfinite(requested) or requested <= 0
                or filled > requested + 1e-8 or abs(filled - oms_filled) > 1e-8):
            raise ValueError("paper_broker_oms_fill_quantity_mismatch")
        complete = abs(filled - requested) <= 1e-8
        oms_status = "filled" if complete else "partially_filled"
        if oms_order["status"] != oms_status:
            return row
        status = "canceled" if not complete and row["time_in_force"] == "ioc" else oms_status
        latest = conn.execute(
            "select payload_json from paper_broker_order_events where order_id=? "
            "and event_type in ('filled','partially_filled','partially_filled_then_canceled','fills_reconciled') order by id desc limit 1",
            (row["order_id"],),
        ).fetchone()
        try:
            reported = float(json.loads(latest[0]).get("filled_quantity", 0)) if latest else 0.0
        except (TypeError, ValueError):
            reported = 0.0
        if row["status"] == status and reported == filled:
            return row
        now = self._now()
        last_fill_at = max(fills, key=lambda fill: self._timestamp(fill["created_at"]))["created_at"]
        conn.execute("update paper_broker_orders set status=?,updated_at=?,filled_at=?,canceled_at=?,rejection_reason=? where order_id=?",
                     (status, now, last_fill_at if complete else row["filled_at"], now if status == "canceled" else row["canceled_at"],
                      "ioc_remaining_cancelled" if status == "canceled" else None, row["order_id"]))
        self._event(conn, order_id=row["order_id"], event_type="fills_reconciled", status=status,
                    market_price=None, detail="Recovered committed Paper OMS fills; original fill evidence is unchanged",
                    payload={"source_of_truth": "paper_oms", "filled_quantity": filled,
                             "fill_ids": [fill["fill_id"] for fill in fills], "new_fill_created": False})
        return conn.execute("select * from paper_broker_orders where order_id=?", (row["order_id"],)).fetchone()

    def _result(self, conn: sqlite3.Connection, order_id: str, *, idempotent: bool = False) -> dict[str, Any]:
        row = conn.execute("select * from paper_broker_orders where order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("paper_broker_order_not_found")
        events = conn.execute(
            "select * from paper_broker_order_events where order_id = ? order by id asc",
            (order_id,),
        ).fetchall()
        order = self._serialize_order(conn, row)
        return {
            "schema_version": "open_stock_ai.paper_broker_result.v1",
            "idempotent": idempotent,
            "filled": order["status"] == "filled",
            "pending": order["status"] in OPEN_STATUSES,
            "order": order,
            "events": [dict(event) for event in events],
            "portfolio": self.oms.portfolio_summary(),
            "execution_boundary": "local_paper_broker_only_no_live_submission",
        }

    def _serialize_order(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        fill = conn.execute(
            "select * from paper_fills where order_id = ? order by created_at desc limit 1",
            (row["order_id"],),
        ).fetchone()
        filled_quantity = self._filled_quantity(str(row["order_id"]), conn=conn)
        requested_quantity = float(row["requested_quantity"] or 0.0)
        return {
            **dict(row),
            "fill": dict(fill) if fill is not None else None,
            "filled_quantity": filled_quantity,
            "remaining_quantity": max(0.0, requested_quantity - filled_quantity),
            "is_open": str(row["status"]) in OPEN_STATUSES,
            "can_cancel": str(row["status"]) in OPEN_STATUSES | {"submitted"},
            "can_replace": str(row["status"]) in OPEN_STATUSES,
        }

    def _filled_quantity(self, order_id: str, *, conn: sqlite3.Connection | None = None) -> float:
        if conn is None:
            with self.store._connect() as owned:
                row = owned.execute(
                    "select coalesce(sum(quantity), 0) from paper_fills where order_id = ?",
                    (order_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "select coalesce(sum(quantity), 0) from paper_fills where order_id = ?",
                (order_id,),
            ).fetchone()
        return float(row[0] or 0.0)

    def _fill_count(self, order_id: str) -> int:
        with self.store._connect() as conn:
            row = conn.execute(
                "select count(*) from paper_fills where order_id = ?", (order_id,)
            ).fetchone()
        return int(row[0] or 0)

    def _available_quantity(self, market: dict[str, Any], remaining_quantity: float) -> float:
        raw = market.get("available_quantity", market.get("available_volume"))
        if raw is None:
            return remaining_quantity
        value = self._number(raw)
        return max(0.0, value) if math.isfinite(value) else 0.0

    def _persist_execution_state(self, order_id: str, execution: dict[str, Any]) -> None:
        """Persist the replay cursor without mutating the original order ticket."""

        with self.store._connect() as conn:
            row = conn.execute(
                "select payload_json from paper_broker_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            try:
                payload = json.loads(str(row[0] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            payload["execution_state"] = {
                "schema_version": execution.get("schema_version"),
                "queue_ahead_remaining": execution.get("queue_ahead_remaining", 0.0),
                "last_fill_sequence": execution.get("fill_sequence", 0),
                "last_total_impact_bps": execution.get("total_impact_bps", 0.0),
            }
            conn.execute(
                "update paper_broker_orders set payload_json = ?, updated_at = ? where order_id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), self._now(), order_id),
            )
            conn.commit()

    def _cancel_unfilled(
        self,
        order_id: str,
        price: float,
        market: dict[str, Any],
        reason: str,
        *,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                update paper_broker_orders
                   set status='canceled', canceled_at=?, updated_at=?, rejection_reason=?, last_market_price=?
                 where order_id=?
                """,
                (now, now, reason, price, order_id),
            )
            self._event(
                conn,
                order_id=order_id,
                event_type="canceled",
                status="canceled",
                market_price=price,
                detail=reason,
                payload={"market": market, "execution_model": execution} if execution else {"market": market},
            )
            conn.commit()
            return self._result(conn, order_id)

    def _rest_active(
        self,
        order_id: str,
        price: float,
        market: dict[str, Any],
        detail: str,
        *,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select triggered_at from paper_broker_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("paper_broker_order_not_found")
            filled_quantity = self._filled_quantity(order_id, conn=conn)
            status = (
                "partially_filled"
                if filled_quantity > 1e-9
                else "triggered"
                if row["triggered_at"]
                else "open"
            )
            conn.execute(
                "update paper_broker_orders set status=?, updated_at=?, last_market_price=? where order_id=?",
                (status, self._now(), price, order_id),
            )
            self._event(
                conn,
                order_id=order_id,
                event_type="resting",
                status=status,
                market_price=price,
                detail=detail,
                payload={"market": market, "execution_model": execution} if execution else {"market": market},
            )
            conn.commit()
            return self._result(conn, order_id)

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        order_id: str,
        event_type: str,
        status: str,
        market_price: float | None,
        detail: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            insert into paper_broker_order_events (
                order_id, account_id, created_at, event_type, status,
                market_price, detail, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                self.oms.account_id,
                self._now(),
                event_type,
                status,
                market_price,
                detail,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )

    def _position(self, account: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        key = str(symbol or "").upper()
        return next(
            (item for item in account.get("positions") or [] if str(item.get("symbol") or "").upper() == key),
            None,
        )

    def _lot_multiplier(self, ticket: dict[str, Any]) -> float:
        market = str(ticket.get("market") or "").upper()
        symbol = str(ticket.get("symbol") or "").upper()
        is_taiwan = market == "TW" or symbol.endswith(".TW") or symbol.endswith(".TWO")
        if ticket["lot_type"] == "board_lot" and is_taiwan:
            return 1000.0
        return 1.0

    def _positive(self, value: Any, name: str) -> float:
        number = self._number(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name}_must_be_positive")
        return number

    def _optional_positive(self, value: Any, name: str) -> float | None:
        if value is None or value == "":
            return None
        return self._positive(value, name)

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _timestamp(self, value: str | datetime) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                raise ValueError("timestamp_required")
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("timestamp_must_be_iso8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("timestamp_timezone_required")
        return parsed.astimezone(timezone.utc)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
