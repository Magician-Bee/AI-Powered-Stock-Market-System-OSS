"""Execution boundary shared by durable plans and broker implementations.

Strategies never branch on paper/live. A live implementation must normalize its
own authoritative order/account receipts to this contract before it can be used.
"""
from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Any, Protocol

from .paper_broker import PaperBrokerSimulator
from .paper_odd_lot import OddLotBoardProxyModel
from .paper_board_lot import BoardLotTradeModel
from .paper_fill_evidence import project_paper_fill_evidence
from .paper_training import PaperTrainingLab
from .trading_plan import utc_time


class BrokerPort(Protocol):
    account_id: str
    mode: str
    paper_quote_execution_model: str | None

    async def account(self, *, now: datetime) -> dict[str, Any]: ...
    async def mark(self, market: dict[str, Any]) -> None: ...
    async def observe(self, market: dict[str, Any]) -> None: ...
    async def order(self, order_id: str) -> dict[str, Any] | None: ...
    async def preview(self, intent: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]: ...
    async def submit(self, intent: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]: ...
    async def cancel(self, order_id: str, *, reason: str) -> dict[str, Any]: ...
    async def fills(self, order_ids: list[str]) -> list[dict[str, Any]]: ...


class PaperBrokerPort:
    mode = "paper"

    def __init__(self, broker: PaperBrokerSimulator, *, public_board_quote_simulation: bool = False):
        if not isinstance(public_board_quote_simulation, bool):
            raise ValueError("public_board_quote_simulation_requires_host_boolean")
        self.broker = broker
        self.account_id = broker.oms.account_id
        if public_board_quote_simulation and broker.board_lot_execution_model is None:
            broker.board_lot_execution_model = BoardLotTradeModel()

    @property
    def paper_quote_execution_model(self) -> str | None:
        # Host configuration, never an Agent-provided argument or quote field.
        odd = isinstance(self.broker.odd_lot_execution_model, OddLotBoardProxyModel)
        board = isinstance(self.broker.board_lot_execution_model, BoardLotTradeModel)
        if board:
            return "bounded_public_quote_paper" if odd else "bounded_board_trade_paper"
        return "bounded_board_trade_odd_lot_proxy" if odd else None

    async def account(self, *, now: datetime) -> dict[str, Any]:
        snapshot_time = max(utc_time(now), utc_time(self.broker.oms._now()))
        return {**self.broker.oms.portfolio_summary(as_of=snapshot_time),
                "open_order_reservations": self.broker.open_order_reservations()}

    async def mark(self, market: dict[str, Any]) -> None:
        PaperTrainingLab(self.broker.store, self.broker.oms).apply_market_mark(
            symbol=str(market["symbol"]), market=str(market.get("market") or "TW"),
            price=float(market["price"]), price_source=str(market.get("price_source") or market["source_envelope"]["provider_id"]),
            source_timestamp=str(market["source_timestamp"]), is_realtime=bool(market.get("is_realtime")),
            is_fallback=bool(market.get("is_fallback")),
            metadata={"source_envelope": market["source_envelope"], "purpose": "autonomous_plan_valuation"},
        )

    async def observe(self, market: dict[str, Any]) -> None:
        self.broker.process_market_tick(str(market["symbol"]), market)

    async def order(self, order_id: str) -> dict[str, Any] | None:
        try:
            return self._receipt(self.broker.get_order(order_id))
        except ValueError as exc:
            if str(exc) != "paper_broker_order_not_found":
                raise
            return None

    async def preview(self, intent: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        return self.broker.preview(self._ticket(intent), market)

    async def submit(self, intent: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        return self._receipt(self.broker.submit(self._ticket(intent), market))

    async def cancel(self, order_id: str, *, reason: str) -> dict[str, Any]:
        return self._receipt(self.broker.cancel(order_id, reason=reason))

    async def fills(self, order_ids: list[str]) -> list[dict[str, Any]]:
        if not order_ids:
            return []
        with self.broker.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin")
            rows = conn.execute(
                "select f.* from paper_fills f "
                "where f.account_id=? and f.order_id in (" + ",".join("?" for _ in order_ids) + ") order by f.created_at,f.fill_id",
                (self.account_id, *order_ids),
            ).fetchall()
            ledger_entries = conn.execute(
                "select account_id,order_id,entry_type,created_at,amount,metadata_json from cash_ledger "
                "where account_id=? and order_id in (" + ",".join("?" for _ in order_ids) + ") "
                "and entry_type in ('paper_buy','paper_sell')",
                (self.account_id, *order_ids),
            ).fetchall()
        by_order: dict[str, list[dict[str, Any]]] = {}
        for entry in ledger_entries:
            by_order.setdefault(entry["order_id"], []).append(dict(entry))
        return [{**dict(row), **project_paper_fill_evidence(dict(row), ledger_entries=by_order.get(row["order_id"], []))}
                for row in rows]

    @staticmethod
    def _ticket(intent: dict[str, Any]) -> dict[str, Any]:
        # Integer quantity is frozen by the decision policy, never converted
        # into a fresh portfolio percentage at the time of execution.
        return {
            **intent, "order_id": intent["order_id"], "order_type": "limit",
            "limit_price": intent["limit_price"], "time_in_force": "rod",
            "lot_type": "board_lot" if int(intent["quantity_shares"]) % 1000 == 0 else "odd_lot",
            "session": "regular", "actor": "autonomous_trading_plan",
        }

    def _receipt(self, result: dict[str, Any]) -> dict[str, Any]:
        order = result["order"]
        with self.broker.store._connect() as conn:
            first = conn.execute("select min(created_at) from paper_fills where account_id=? and order_id=?",
                                 (self.account_id, order["order_id"])).fetchone()
        return {
            "order_id": order["order_id"], "status": order["status"],
            "account_id": self.account_id, "symbol": order["symbol"],
            "first_filled_at": first[0] if first else None,
            "filled_quantity": float(order["filled_quantity"]),
            "remaining_quantity": float(order["remaining_quantity"]),
            "is_open": bool(order["is_open"]), "raw": result,
        }
