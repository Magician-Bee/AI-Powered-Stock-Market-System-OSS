from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.execution.corporate_actions import CorporateActionLedger
from open_stock_ai.execution.paper_borrow import PaperBorrowLedger
from open_stock_ai.execution.taiwan_settlement import parse_timestamp, settlement_receipt
from open_stock_ai.execution.trading_restrictions import TradingRestrictionLedger
from open_stock_ai.governance.change_management import ChangeManagementError, ChangeManagementRegistry
from open_stock_ai.storage.sqlite_store import SQLiteStore


@dataclass
class PaperOMS:
    """Deterministic local paper order-management system.

    Orders are filled immediately at a configurable simulated price. Assumptions are
    explicit and default to zero fees/tax/slippage rather than pretending that a
    single market's fee schedule is universally correct. The environment variables
    can be set by the user or a future broker simulator:

    - ``OPEN_STOCK_AI_PAPER_INITIAL_CASH``
    - ``OPEN_STOCK_AI_PAPER_COMMISSION_BPS``
    - ``OPEN_STOCK_AI_PAPER_SELL_TAX_BPS``
    - ``OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS``
    - ``OPEN_STOCK_AI_PAPER_LOT_SIZE``
    """

    store: SQLiteStore
    account_id: str = "default-paper"
    base_currency: str = "TWD"
    initial_cash: float = 1_000_000.0
    commission_bps: float = 0.0
    sell_tax_bps: float = 0.0
    slippage_bps: float = 0.0
    lot_size: float = 1.0
    change_management: ChangeManagementRegistry | None = None
    require_change_binding: bool = False

    def __post_init__(self) -> None:
        self.account_id = os.getenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", self.account_id).strip() or self.account_id
        self.base_currency = os.getenv("OPEN_STOCK_AI_PAPER_BASE_CURRENCY", self.base_currency).strip() or self.base_currency
        self.initial_cash = self._env_float("OPEN_STOCK_AI_PAPER_INITIAL_CASH", self.initial_cash, minimum=0.0)
        self.commission_bps = self._env_float("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", self.commission_bps, minimum=0.0)
        self.sell_tax_bps = self._env_float("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", self.sell_tax_bps, minimum=0.0)
        self.slippage_bps = self._env_float("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", self.slippage_bps, minimum=0.0)
        self.lot_size = self._env_float("OPEN_STOCK_AI_PAPER_LOT_SIZE", self.lot_size, minimum=0.00000001)
        governance_store = (
            getattr(self.change_management, "store", None)
            if self.change_management is not None
            else None
        )
        if self.require_change_binding and self.change_management is None:
            raise ValueError("change management registry is required when paper change binding is mandatory")
        if self.require_change_binding and governance_store is None:
            raise ValueError("paper change binding requires a durable governance store")
        if governance_store is not None:
            governance_path = getattr(governance_store, "path", None)
            if governance_path is None or Path(governance_path).resolve() != Path(self.store.path).resolve():
                raise ValueError("paper change binding must use the same durable database as the OMS")
        self.ensure_account()

    def ensure_account(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_account(conn)
            conn.commit()
            return self._account_row(conn)

    def submit_and_fill(self, order: dict[str, Any]) -> dict[str, Any]:
        """Submit a new paper order and fill all of its remaining quantity.

        Broker-style callers that need a queue/volume constrained execution use
        :meth:`submit_partial_fill` instead.  Keeping this entry point means
        existing strategy and API callers retain their explicit immediate-fill
        simulation contract.
        """
        return self.submit_partial_fill(order, fill_quantity=None)

    def submit_partial_fill(
        self,
        order: dict[str, Any],
        *,
        fill_quantity: float | None,
        fill_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one fill against a durable paper order.

        ``fill_id`` is the idempotency key for a broker fill report.  A repeat
        report returns the existing ledger state without changing cash or the
        position.  A ``None`` quantity is the compatibility path used by
        ``submit_and_fill`` and consumes the complete requested quantity.
        """
        order_id = str(order.get("order_id") or "").strip()
        symbol = str(order.get("symbol") or "").strip()
        action = str(order.get("action") or "").strip().lower()
        market = str(order.get("market") or "").strip() or None
        reference_price = self._number(order.get("entry_price"))
        requested_position_pct = self._number(order.get("position_size_pct"))
        now = self._now()

        if not order_id:
            return self._invalid_result("missing_order_id")
        if not symbol:
            return self._invalid_result("missing_symbol", order_id=order_id)
        if action not in {"buy", "add", "sell", "reduce", "short_sell", "buy_to_cover"}:
            return self._invalid_result("unsupported_action", order_id=order_id)
        if order.get("risk_approved") is not True:
            return self._invalid_result("risk_not_approved", order_id=order_id)
        if not math.isfinite(requested_position_pct) or requested_position_pct <= 0:
            return self._invalid_result("missing_position_size", order_id=order_id)
        if reference_price <= 0:
            return self._invalid_result("missing_reference_price", order_id=order_id)
        ticket = dict(order.get("paper_broker_ticket") or {})
        ticket.setdefault("symbol", symbol)
        ticket.setdefault("side", "buy" if action in {"buy", "add", "buy_to_cover"} else "sell")
        ticket.setdefault("order_type", "market")
        restriction_evaluation = TradingRestrictionLedger(self.store).evaluate(
            ticket=ticket,
            market={
                "price": reference_price,
                **dict(order.get("market_context") or {}),
            },
        )
        if not restriction_evaluation["allowed"]:
            result = self._invalid_result(
                str(restriction_evaluation["reason"]),
                order_id=order_id,
            )
            result["trading_restrictions"] = restriction_evaluation
            return result
        settlement = settlement_receipt(market=dict(order.get("market_context") or {}))
        change_binding = self._bind_change_set(order_id, order)
        if isinstance(change_binding, str):
            return self._invalid_result(change_binding, order_id=order_id)
        if change_binding is not None:
            order = {
                **order,
                "change_id": change_binding["change_id"],
                "change_binding": change_binding,
            }

        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            self._ensure_account(conn)
            existing = conn.execute(
                "select * from paper_orders where order_id = ?",
                (order_id,),
            ).fetchone()
            if existing is not None:
                if fill_id and conn.execute(
                    "select 1 from paper_fills where fill_id = ?", (fill_id,)
                ).fetchone() is not None:
                    conn.commit()
                    return self._result_for_order(conn, order_id, idempotent=True)
                if (
                    str(existing["symbol"]) != symbol
                    or str(existing["action"]) != action
                    or (existing["market"] or None) != market
                ):
                    conn.commit()
                    result = self._result_for_order(conn, order_id)
                    result["order_contract_error"] = "order_contract_mismatch"
                    return result
                if str(existing["status"]) in {"filled", "rejected", "cancelled"}:
                    conn.commit()
                    return self._result_for_order(conn, order_id, idempotent=True)
                requested_quantity = float(existing["requested_quantity"] or 0.0)
                filled_quantity = float(existing["filled_quantity"] or 0.0)
                remaining_quantity = max(0.0, requested_quantity - filled_quantity)
                position = conn.execute(
                    "select * from paper_positions where account_id = ? and symbol = ?",
                    (self.account_id, symbol),
                ).fetchone()
            else:
                account = self._account_row(conn)
                position = conn.execute(
                    "select * from paper_positions where account_id = ? and symbol = ?",
                    (self.account_id, symbol),
                ).fetchone()
                account_equity = self._equity(conn, float(account["cash_balance"]))
                quantity, rejection = self._requested_quantity(
                    action=action,
                    requested_position_pct=requested_position_pct,
                    reference_price=reference_price,
                    account_equity=account_equity,
                    position=position,
                )
                conn.execute(
                    """
                    insert into paper_orders (
                        order_id, account_id, created_at, updated_at, symbol, market,
                        action, status, requested_position_pct, requested_quantity,
                        filled_quantity, average_fill_price, rejection_reason, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?, 0, null, null, ?)
                    """,
                    (
                        order_id,
                        self.account_id,
                        now,
                        now,
                        symbol,
                        market,
                        action,
                        requested_position_pct,
                        quantity,
                        json.dumps(order, ensure_ascii=False, default=str),
                    ),
                )
                if rejection:
                    self._reject(conn, order_id, rejection, now)
                    conn.commit()
                    return self._result_for_order(conn, order_id)
                requested_quantity = quantity
                remaining_quantity = quantity

            requested_fill = remaining_quantity if fill_quantity is None else self._number(fill_quantity)
            quantity = min(remaining_quantity, requested_fill)
            if not math.isfinite(quantity) or quantity <= 0:
                conn.commit()
                return self._result_for_order(conn, order_id, idempotent=True)

            position_quantity = float(position["quantity"] or 0.0) if position is not None else 0.0
            if action == "short_sell" and position_quantity > 1e-9:
                self._reject(conn, order_id, "long_position_must_be_closed_before_short_sale", now)
                conn.commit()
                return self._result_for_order(conn, order_id)
            if action in {"buy", "add"} and position_quantity < -1e-9:
                self._reject(conn, order_id, "short_position_requires_buy_to_cover", now)
                conn.commit()
                return self._result_for_order(conn, order_id)

            account = self._account_row(conn)
            is_buy = action in {"buy", "add", "buy_to_cover"}
            slippage_rate = self.slippage_bps / 10_000.0
            fill_price = reference_price * (1.0 + slippage_rate if is_buy else 1.0 - slippage_rate)
            fill_price = max(0.00000001, fill_price)
            gross_amount = quantity * fill_price
            commission = gross_amount * self.commission_bps / 10_000.0
            tax = 0.0 if is_buy else gross_amount * self.sell_tax_bps / 10_000.0
            slippage_cost = abs(fill_price - reference_price) * quantity
            net_cash_delta = -(gross_amount + commission) if is_buy else gross_amount - commission - tax
            cash_before = float(account["cash_balance"])
            available_cash_before = self._available_cash(conn)

            if is_buy and available_cash_before + net_cash_delta < -0.000001:
                if existing is not None and float(existing["filled_quantity"] or 0.0) > 0:
                    # A later fill can be unaffordable even though an earlier
                    # broker report already changed cash and the position.  Do
                    # not erase that durable partial execution by marking the
                    # whole order rejected.
                    conn.execute(
                        """
                        update paper_orders
                        set status = 'partially_filled', updated_at = ?,
                            rejection_reason = 'insufficient_cash_for_remaining_quantity'
                        where order_id = ?
                        """,
                        (now, order_id),
                    )
                else:
                    self._reject(conn, order_id, "insufficient_cash", now)
                conn.commit()
                return self._result_for_order(conn, order_id)

            borrow_ledger = PaperBorrowLedger(self.store, self.account_id)
            borrow_receipt = order.get("borrow_receipt") or ticket.get("borrow_receipt")
            if action == "short_sell":
                borrow_preview = borrow_ledger.preview(
                    symbol=symbol,
                    quantity=quantity,
                    receipt=borrow_receipt if isinstance(borrow_receipt, dict) else None,
                    as_of=(order.get("market_context") or {}).get("source_timestamp") or now,
                )
                if not borrow_preview["allowed"]:
                    self._reject(conn, order_id, str(borrow_preview["reason"]), now)
                    conn.commit()
                    result = self._result_for_order(conn, order_id)
                    result["borrow"] = borrow_preview
                    return result

            fill_id = fill_id or f"FILL-{uuid4().hex}"
            realized_delta = self._update_position(
                conn=conn,
                symbol=symbol,
                market=market,
                action=action,
                quantity=quantity,
                fill_price=fill_price,
                commission=commission,
                tax=tax,
                now=now,
                position=position,
            )
            cash_after = cash_before + net_cash_delta
            conn.execute(
                """
                update paper_accounts
                set cash_balance = ?, realized_pnl = realized_pnl + ?, updated_at = ?
                where account_id = ?
                """,
                (cash_after, realized_delta, now, self.account_id),
            )
            conn.execute(
                """
                insert into paper_fills (
                    fill_id, order_id, account_id, created_at, symbol, side,
                    quantity, reference_price, fill_price, gross_amount,
                    commission, tax, slippage_cost, net_cash_delta
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_id,
                    order_id,
                    self.account_id,
                    now,
                    symbol,
                    "buy" if is_buy else "sell",
                    quantity,
                    reference_price,
                    fill_price,
                    gross_amount,
                    commission,
                    tax,
                    slippage_cost,
                    net_cash_delta,
                ),
            )
            if action == "short_sell":
                borrow_ledger.reserve_short(
                    conn,
                    receipt=borrow_receipt if isinstance(borrow_receipt, dict) else None,
                    symbol=symbol,
                    quantity=quantity,
                    fill_id=fill_id,
                    entry_price=fill_price,
                    now=now,
                    as_of=(order.get("market_context") or {}).get("source_timestamp") or now,
                )
            elif action == "buy_to_cover":
                borrow_ledger.cover_short(conn, symbol=symbol, quantity=quantity, now=now)
            total_filled = float(existing["filled_quantity"] or 0.0) + quantity if existing is not None else quantity
            prior_average = float(existing["average_fill_price"] or 0.0) if existing is not None else 0.0
            average_fill_price = (
                (prior_average * (total_filled - quantity) + fill_price * quantity) / total_filled
                if total_filled > 0
                else None
            )
            status = "filled" if total_filled >= requested_quantity - 1e-8 else "partially_filled"
            conn.execute(
                """
                update paper_orders
                set status = ?, updated_at = ?, filled_quantity = ?,
                    average_fill_price = ?, rejection_reason = null
                where order_id = ?
                """,
                (status, now, total_filled, average_fill_price, order_id),
            )
            conn.execute(
                """
                insert into cash_ledger (
                    account_id, order_id, created_at, entry_type, amount,
                    balance_after, currency, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.account_id,
                    order_id,
                    now,
                    "paper_buy" if is_buy else "paper_sell",
                    net_cash_delta,
                    cash_after,
                    self.base_currency,
                    json.dumps(
                        {
                            "fill_id": fill_id,
                            "gross_amount": gross_amount,
                            "commission": commission,
                            "tax": tax,
                            "slippage_cost": slippage_cost,
                            "realized_pnl_delta": realized_delta,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            if settlement["enforced"]:
                settlement_id = f"PSET-{fill_id}"
                payable = -net_cash_delta if is_buy else 0.0
                receivable = net_cash_delta if not is_buy else 0.0
                conn.execute(
                    """
                    insert into paper_settlements (
                        settlement_id, fill_id, order_id, account_id, created_at,
                        trade_at, settlement_due_at, settlement_date, side, currency,
                        payable, receivable, status, settled_at, receipt_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', null, ?)
                    """,
                    (
                        settlement_id,
                        fill_id,
                        order_id,
                        self.account_id,
                        now,
                        settlement["trade_at"],
                        settlement["settlement_due_at"],
                        settlement["settlement_date"],
                        "buy" if is_buy else "sell",
                        settlement["currency"],
                        payable,
                        receivable,
                        json.dumps(settlement, ensure_ascii=False, sort_keys=True),
                    ),
                )
                conn.execute(
                    """
                    insert into paper_settlement_events (
                        settlement_id, account_id, created_at, event_type, payload_json
                    ) values (?, ?, ?, 'pending', ?)
                    """,
                    (
                        settlement_id,
                        self.account_id,
                        now,
                        json.dumps(
                            {
                                "fill_id": fill_id,
                                "payable": payable,
                                "receivable": receivable,
                                "settlement_due_at": settlement["settlement_due_at"],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            conn.commit()
            return self._result_for_order(conn, order_id)

    def portfolio_summary(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_account(conn)
            conn.commit()
            return self._portfolio_summary(conn)

    def settle_due(self, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        """Move due T+2 claims into settled cash with a durable event receipt.

        Economic cash is already represented once in ``cash_ledger`` at the
        fill.  Settlement therefore changes only the settled/unsettled views;
        it never inserts a second cash amount.
        """

        observed_at = parse_timestamp(as_of or datetime.now(timezone.utc))
        settled_ids: list[str] = []
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            self._ensure_account(conn)
            rows = conn.execute(
                """
                select * from paper_settlements
                 where account_id = ? and status = 'pending'
                 order by settlement_due_at asc, settlement_id asc
                """,
                (self.account_id,),
            ).fetchall()
            for row in rows:
                if parse_timestamp(row["settlement_due_at"]) > observed_at:
                    continue
                account = self._account_row(conn)
                settled_cash = float(account["settled_cash_balance"] or 0.0)
                delta = float(row["receivable"] or 0.0) - float(row["payable"] or 0.0)
                next_settled_cash = settled_cash + delta
                if next_settled_cash < -0.000001:
                    raise RuntimeError("settlement_would_overdraw_settled_cash")
                now = observed_at.isoformat()
                conn.execute(
                    """
                    update paper_accounts
                       set settled_cash_balance = ?, updated_at = ?
                     where account_id = ?
                    """,
                    (next_settled_cash, now, self.account_id),
                )
                conn.execute(
                    """
                    update paper_settlements
                       set status = 'settled', settled_at = ?
                     where settlement_id = ? and status = 'pending'
                    """,
                    (now, row["settlement_id"]),
                )
                conn.execute(
                    """
                    insert into paper_settlement_events (
                        settlement_id, account_id, created_at, event_type, payload_json
                    ) values (?, ?, ?, 'settled', ?)
                    """,
                    (
                        row["settlement_id"],
                        self.account_id,
                        now,
                        json.dumps(
                            {
                                "payable": float(row["payable"] or 0.0),
                                "receivable": float(row["receivable"] or 0.0),
                                "settled_cash_balance": next_settled_cash,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                settled_ids.append(str(row["settlement_id"]))
            conn.commit()
        return {
            "schema_version": "open_stock_ai.paper_settlement_batch.v1",
            "as_of": observed_at.isoformat(),
            "settled_ids": settled_ids,
            "settled_count": len(settled_ids),
            "account": self.portfolio_summary(),
        }

    def accrue_borrow_fees(
        self,
        *,
        as_of: str | datetime | None = None,
        market_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Accrue each open short lot's explicit borrow fee exactly once per day."""

        return PaperBorrowLedger(self.store, self.account_id).accrue_fees(
            as_of=as_of, market_prices=market_prices,
        )

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from paper_orders where account_id = ? order by created_at desc limit ?",
                (self.account_id, limit),
            ).fetchall()
            return [self._serialize_order(row) for row in rows]

    def import_corporate_actions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        return CorporateActionLedger(self.store).import_actions(payloads)

    def import_trading_restrictions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        return TradingRestrictionLedger(self.store).import_restrictions(payloads)

    def trading_restrictions(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return TradingRestrictionLedger(self.store).list_restrictions(
            symbol=symbol,
            as_of=as_of,
            active_only=active_only,
            limit=limit,
        )

    def corporate_actions(
        self,
        *,
        symbol: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return CorporateActionLedger(self.store).list_actions(
            symbol=symbol,
            start=start,
            end=end,
            limit=limit,
            account_id=self.account_id,
        )

    def sync_corporate_actions(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        return CorporateActionLedger(self.store).sync_due_actions(
            account_id=self.account_id,
            symbol=symbol,
            as_of=as_of,
        )

    def _ensure_account(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "select account_id from paper_accounts where account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is not None:
            return
        now = self._now()
        conn.execute(
            """
            insert into paper_accounts (
                account_id, base_currency, initial_cash, cash_balance, settled_cash_balance,
                realized_pnl, created_at, updated_at
            ) values (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                self.account_id,
                self.base_currency,
                self.initial_cash,
                self.initial_cash,
                self.initial_cash,
                now,
                now,
            ),
        )
        conn.execute(
            """
            insert into cash_ledger (
                account_id, order_id, created_at, entry_type, amount,
                balance_after, currency, metadata_json
            ) values (?, null, ?, 'initial_deposit', ?, ?, ?, ?)
            """,
            (
                self.account_id,
                now,
                self.initial_cash,
                self.initial_cash,
                self.base_currency,
                json.dumps({"simulation": True}, ensure_ascii=False),
            ),
        )

    def _requested_quantity(
        self,
        *,
        action: str,
        requested_position_pct: float,
        reference_price: float,
        account_equity: float,
        position: sqlite3.Row | None,
    ) -> tuple[float, str | None]:
        if action in {"buy", "add", "short_sell"}:
            notional = account_equity * requested_position_pct / 100.0
            quantity = self._floor_to_lot(notional / reference_price)
            return (quantity, None) if quantity > 0 else (0.0, "position_size_below_minimum_lot")

        current_quantity = float(position["quantity"]) if position is not None else 0.0
        if action == "buy_to_cover":
            if current_quantity >= 0:
                return 0.0, "insufficient_short_position"
            reduction_fraction = min(1.0, requested_position_pct / 100.0)
            quantity = self._floor_to_lot(abs(current_quantity) * reduction_fraction)
            if quantity <= 0 and abs(current_quantity) >= self.lot_size:
                quantity = self.lot_size
            quantity = min(abs(current_quantity), quantity)
            return (quantity, None) if quantity > 0 else (0.0, "position_size_below_minimum_lot")
        if current_quantity <= 0:
            return 0.0, "insufficient_position"
        reduction_fraction = min(1.0, requested_position_pct / 100.0)
        quantity = self._floor_to_lot(current_quantity * reduction_fraction)
        if quantity <= 0 and current_quantity >= self.lot_size:
            quantity = self.lot_size
        quantity = min(current_quantity, quantity)
        return (quantity, None) if quantity > 0 else (0.0, "position_size_below_minimum_lot")

    def _update_position(
        self,
        *,
        conn: sqlite3.Connection,
        symbol: str,
        market: str | None,
        action: str,
        quantity: float,
        fill_price: float,
        commission: float,
        tax: float,
        now: str,
        position: sqlite3.Row | None,
    ) -> float:
        old_quantity = float(position["quantity"]) if position is not None else 0.0
        old_average_cost = float(position["average_cost"]) if position is not None else 0.0
        old_realized = float(position["realized_pnl"]) if position is not None else 0.0
        if action in {"buy", "add"}:
            new_quantity = old_quantity + quantity
            cost_basis = old_quantity * old_average_cost + quantity * fill_price + commission
            new_average_cost = cost_basis / new_quantity if new_quantity else 0.0
            realized_delta = 0.0
        elif action in {"sell", "reduce"}:
            new_quantity = max(0.0, old_quantity - quantity)
            new_average_cost = old_average_cost if new_quantity > 0 else 0.0
            realized_delta = (fill_price - old_average_cost) * quantity - commission - tax
        elif action == "short_sell":
            short_quantity = max(0.0, -old_quantity)
            new_quantity = old_quantity - quantity
            proceeds_basis = short_quantity * old_average_cost + quantity * fill_price - commission - tax
            new_average_cost = proceeds_basis / abs(new_quantity) if new_quantity else 0.0
            realized_delta = 0.0
        elif action == "buy_to_cover":
            new_quantity = min(0.0, old_quantity + quantity)
            new_average_cost = old_average_cost if new_quantity < 0 else 0.0
            realized_delta = (old_average_cost - fill_price) * quantity - commission
        else:  # guarded above, retained to keep this accounting branch total.
            raise ValueError("unsupported_action")
        conn.execute(
            """
            insert into paper_positions (
                account_id, symbol, market, quantity, average_cost, last_price,
                realized_pnl, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(account_id, symbol) do update set
                market = excluded.market,
                quantity = excluded.quantity,
                average_cost = excluded.average_cost,
                last_price = excluded.last_price,
                realized_pnl = excluded.realized_pnl,
                updated_at = excluded.updated_at
            """,
            (
                self.account_id,
                symbol,
                market,
                new_quantity,
                new_average_cost,
                fill_price,
                old_realized + realized_delta,
                now,
            ),
        )
        return realized_delta

    def _reject(self, conn: sqlite3.Connection, order_id: str, reason: str, now: str) -> None:
        conn.execute(
            """
            update paper_orders
            set status = 'rejected', updated_at = ?, rejection_reason = ?
            where order_id = ?
            """,
            (now, reason, order_id),
        )

    def _result_for_order(self, conn: sqlite3.Connection, order_id: str, *, idempotent: bool = False) -> dict[str, Any]:
        order = conn.execute("select * from paper_orders where order_id = ?", (order_id,)).fetchone()
        fill = conn.execute("select * from paper_fills where order_id = ? order by created_at desc limit 1", (order_id,)).fetchone()
        return {
            "schema_version": "open_stock_ai.paper_oms_result.v1",
            "method": "local_persistent_fill_paper_oms",
            "filled": bool(order is not None and order["status"] == "filled"),
            "partially_filled": bool(order is not None and order["status"] == "partially_filled"),
            "idempotent": idempotent,
            "order": self._serialize_order(order) if order is not None else None,
            "fill": self._serialize_fill(fill) if fill is not None else None,
            "portfolio": self._portfolio_summary(conn),
            "assumptions": self._assumptions(),
            "execution_boundary": "local_paper_only_no_broker_submission",
        }

    def _portfolio_summary(self, conn: sqlite3.Connection) -> dict[str, Any]:
        account = self._account_row(conn)
        rows = conn.execute(
            "select * from paper_positions where account_id = ? and quantity <> 0 order by symbol",
            (self.account_id,),
        ).fetchall()
        cash = float(account["cash_balance"])
        settled_cash = float(
            account["settled_cash_balance"]
            if account["settled_cash_balance"] is not None
            else cash
        )
        settlement_rows = conn.execute(
            """
            select * from paper_settlements
             where account_id = ?
             order by settlement_due_at asc, settlement_id asc
            """,
            (self.account_id,),
        ).fetchall()
        pending_payable = sum(
            float(row["payable"] or 0.0)
            for row in settlement_rows
            if row["status"] == "pending"
        )
        pending_receivable = sum(
            float(row["receivable"] or 0.0)
            for row in settlement_rows
            if row["status"] == "pending"
        )
        # Legacy callers do not carry an exchange settlement receipt and keep
        # the original immediate-cash simulation.  The conservative minimum
        # preserves that contract while preventing pending sale receivables
        # from becoming spendable under the T+2 path.
        available_cash = min(cash, settled_cash - pending_payable)
        positions: list[dict[str, Any]] = []
        market_value_total = 0.0
        unrealized_total = 0.0
        for row in rows:
            quantity = float(row["quantity"])
            average_cost = float(row["average_cost"])
            last_price = float(row["last_price"])
            market_value = quantity * last_price
            unrealized = (last_price - average_cost) * quantity
            market_value_total += market_value
            unrealized_total += unrealized
            positions.append(
                {
                    "symbol": row["symbol"],
                    "market": row["market"],
                    "quantity": round(quantity, 8),
                    "position_side": "short" if quantity < 0 else "long",
                    "average_cost": round(average_cost, 6),
                    "last_price": round(last_price, 6),
                    "market_value": round(market_value, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "realized_pnl": round(float(row["realized_pnl"]), 2),
                    "valuation_basis": "latest_verified_market_mark_or_real_paper_fill",
                }
            )
        total_equity = cash + market_value_total
        for item in positions:
            item["position_size_pct"] = round(abs(item["market_value"]) / total_equity * 100.0, 4) if total_equity else 0.0
        order_count = conn.execute(
            "select count(*) from paper_orders where account_id = ?",
            (self.account_id,),
        ).fetchone()[0]
        fill_count = conn.execute(
            "select count(*) from paper_fills where account_id = ?",
            (self.account_id,),
        ).fetchone()[0]
        corporate_action_count = conn.execute(
            """
            select count(*) from paper_corporate_action_applications
             where account_id = ?
            """,
            (self.account_id,),
        ).fetchone()[0]
        pending_entitlement_count = conn.execute(
            """
            select count(*) from paper_corporate_action_entitlements
             where account_id = ? and status = 'pending_manual_exercise'
            """,
            (self.account_id,),
        ).fetchone()[0]
        dividend_income = conn.execute(
            """
            select coalesce(sum(cash_delta), 0)
              from paper_corporate_action_applications
             where account_id = ? and action_type = 'cash_dividend'
            """,
            (self.account_id,),
        ).fetchone()[0]
        initial_cash = float(account["initial_cash"])
        borrow_summary = PaperBorrowLedger(self.store, self.account_id).summary()
        return {
            "schema_version": "open_stock_ai.paper_account.v1",
            "mode": "paper",
            "is_simulated": True,
            "account_id": self.account_id,
            "base_currency": account["base_currency"],
            "account_created_at": account["created_at"],
            "account_updated_at": account["updated_at"],
            "initial_cash": round(initial_cash, 2),
            "cash_balance": round(cash, 2),
            "settled_cash_balance": round(settled_cash, 2),
            "available_cash": round(available_cash, 2),
            "unsettled_payable": round(pending_payable, 2),
            "unsettled_receivable": round(pending_receivable, 2),
            "pending_settlement_count": sum(
                1 for row in settlement_rows if row["status"] == "pending"
            ),
            "settlements": [self._serialize_settlement(row) for row in settlement_rows[:100]],
            "holdings_market_value": round(market_value_total, 2),
            "total_equity": round(total_equity, 2),
            "realized_pnl": round(float(account["realized_pnl"]), 2),
            "unrealized_pnl": round(unrealized_total, 2),
            "total_return_pct": round((total_equity - initial_cash) / initial_cash * 100.0, 4) if initial_cash else 0.0,
            "position_count": len(positions),
            "order_count": int(order_count),
            "fill_count": int(fill_count),
            "corporate_action_count": int(corporate_action_count),
            "pending_entitlement_count": int(pending_entitlement_count),
            "dividend_income": round(float(dividend_income or 0.0), 2),
            "positions": positions,
            "borrow": borrow_summary,
            "valuation_basis": "cash_plus_latest_verified_market_marks_or_real_fills",
            "assumptions": self._assumptions(),
            "execution_boundary": "local_paper_only_no_broker_submission",
        }

    def _account_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "select * from paper_accounts where account_id = ?",
            (self.account_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Paper account was not initialized")
        return row

    def _equity(self, conn: sqlite3.Connection, cash: float) -> float:
        row = conn.execute(
            "select coalesce(sum(quantity * last_price), 0) from paper_positions where account_id = ?",
            (self.account_id,),
        ).fetchone()
        return cash + float(row[0] or 0.0)

    def _serialize_order(self, row: sqlite3.Row) -> dict[str, Any]:
        serialized = {
            "order_id": row["order_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "symbol": row["symbol"],
            "market": row["market"],
            "action": row["action"],
            "status": row["status"],
            "requested_position_pct": row["requested_position_pct"],
            "requested_quantity": row["requested_quantity"],
            "filled_quantity": row["filled_quantity"],
            "remaining_quantity": max(
                0.0,
                float(row["requested_quantity"] or 0.0) - float(row["filled_quantity"] or 0.0),
            ),
            "average_fill_price": row["average_fill_price"],
            "rejection_reason": row["rejection_reason"],
        }
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {}
        change_binding = payload.get("change_binding") if isinstance(payload, dict) else None
        if isinstance(change_binding, dict):
            serialized["change_binding"] = change_binding
        return serialized

    def _bind_change_set(self, order_id: str, order: dict[str, Any]) -> dict[str, Any] | str | None:
        """Bind the durable paper order to a pre-approved immutable change set.

        The registry is deliberately never asked to create or approve a change
        here.  A caller may only name a change set that an authorised human has
        already pinned, and strict mode fails closed when that identifier is
        absent.  The returned receipt is embedded in the immutable order payload
        as well as retained in the shared governance authority.
        """

        change_id = str(order.get("change_id") or "").strip()
        if self.require_change_binding and not change_id:
            return "missing_host_approved_change_set_binding"
        if not change_id:
            return None
        if self.change_management is None:
            return "paper_order_change_set_requires_governance_registry"
        if getattr(self.change_management, "store", None) is None:
            return "paper_order_change_set_requires_durable_governance_registry"
        try:
            return self.change_management.bind_order(order_id, change_id=change_id)
        except ChangeManagementError as exc:
            return f"paper_order_change_set_binding_rejected:{exc}"

    def _serialize_fill(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "fill_id": row["fill_id"],
            "order_id": row["order_id"],
            "created_at": row["created_at"],
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": row["quantity"],
            "reference_price": row["reference_price"],
            "fill_price": row["fill_price"],
            "gross_amount": row["gross_amount"],
            "commission": row["commission"],
            "tax": row["tax"],
            "slippage_cost": row["slippage_cost"],
            "net_cash_delta": row["net_cash_delta"],
        }

    def _serialize_settlement(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "settlement_id": row["settlement_id"],
            "fill_id": row["fill_id"],
            "order_id": row["order_id"],
            "trade_at": row["trade_at"],
            "settlement_due_at": row["settlement_due_at"],
            "settlement_date": row["settlement_date"],
            "side": row["side"],
            "currency": row["currency"],
            "payable": round(float(row["payable"] or 0.0), 2),
            "receivable": round(float(row["receivable"] or 0.0), 2),
            "status": row["status"],
            "settled_at": row["settled_at"],
        }

    def _available_cash(self, conn: sqlite3.Connection) -> float:
        account = self._account_row(conn)
        settled_cash = float(
            account["settled_cash_balance"]
            if account["settled_cash_balance"] is not None
            else account["cash_balance"]
        )
        payable = conn.execute(
            """
            select coalesce(sum(payable), 0) from paper_settlements
             where account_id = ? and status = 'pending'
            """,
            (self.account_id,),
        ).fetchone()[0]
        return min(
            float(account["cash_balance"]),
            settled_cash - float(payable or 0.0),
        )

    def _assumptions(self) -> dict[str, Any]:
        return {
            "fill_model": "immediate_or_broker_constrained_partial_fill",
            "commission_bps": self.commission_bps,
            "sell_tax_bps": self.sell_tax_bps,
            "slippage_bps": self.slippage_bps,
            "lot_size": self.lot_size,
            "fees_are_configurable_simulation_assumptions": True,
            "settlement_model": "exchange_quote_enforced_t_plus_2_dvp_or_legacy_immediate_paper_cash",
            "short_sale_model": "borrow_locate_required_with_durable_fee_and_recall_receipts",
        }

    def _invalid_result(self, reason: str, *, order_id: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.paper_oms_result.v1",
            "method": "local_persistent_fill_paper_oms",
            "filled": False,
            "idempotent": False,
            "order": {"order_id": order_id, "status": "rejected", "rejection_reason": reason},
            "fill": None,
            "portfolio": self.portfolio_summary(),
            "assumptions": self._assumptions(),
            "execution_boundary": "local_paper_only_no_broker_submission",
        }

    def _floor_to_lot(self, quantity: float) -> float:
        if quantity <= 0:
            return 0.0
        lots = math.floor((quantity + 1e-12) / self.lot_size)
        return round(lots * self.lot_size, 8)

    def _env_float(self, name: str, default: float, *, minimum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
