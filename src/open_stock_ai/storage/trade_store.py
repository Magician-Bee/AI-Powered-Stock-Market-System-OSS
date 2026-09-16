from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.governance.change_management import ChangeManagementRegistry
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.external_sources.ai_trader_source import AITraderSource
from open_stock_ai.risk.kill_switch import DurableRiskControlStore
from open_stock_ai.risk.settlement_pnl_feed import SettlementPnLFeed
from open_stock_ai.storage.sqlite_store import SQLiteStore


@dataclass
class TradeStore:
    store: SQLiteStore
    ai_trader: AITraderSource | None = None
    paper_oms: PaperOMS | None = None
    risk_control: DurableRiskControlStore | None = None
    settlement_pnl_feed: SettlementPnLFeed | None = None
    retention_ledger: ContentAddressedRetentionLedger | None = None
    change_management: ChangeManagementRegistry | None = None
    require_change_binding: bool = False

    def __post_init__(self) -> None:
        if self.paper_oms is None:
            self.paper_oms = PaperOMS(
                store=self.store,
                change_management=self.change_management,
                require_change_binding=self.require_change_binding,
                retention_ledger=self.retention_ledger,
            )
        elif self.require_change_binding and self.paper_oms.require_change_binding is not True:
            raise ValueError("trade store paper OMS must require the shared change-management authority")
        if self.retention_ledger is not None and self.paper_oms is not None:
            if self.paper_oms.retention_ledger is None:
                self.paper_oms.retention_ledger = self.retention_ledger
            else:
                oms_retention_path = getattr(self.paper_oms.retention_ledger.store, "path", None)
                runtime_retention_path = getattr(self.retention_ledger.store, "path", None)
                if (
                    oms_retention_path is None
                    or runtime_retention_path is None
                    or oms_retention_path != runtime_retention_path
                ):
                    raise ValueError("trade store paper OMS retention must share its durable database")
        if self.risk_control is not None and self.settlement_pnl_feed is None:
            self.settlement_pnl_feed = SettlementPnLFeed(
                self.risk_control,
                retention_ledger=self.retention_ledger,
            )

    def save_preview(self, trade: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_trade(trade)

    def save_paper_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = payload.get("request") or {}
        signal = payload.get("signal") or {}
        execution = payload.get("execution") or {}
        risk = payload.get("risk") or {}
        if risk.get("approved") is not True:
            return {
                "saved": False,
                "schema_version": "open_stock_ai.paper_order.v1",
                "order_id": execution.get("order_id"),
                "filled": False,
                "oms": {
                    "schema_version": "open_stock_ai.paper_oms_result.v1",
                    "filled": False,
                    "order": {
                        "order_id": execution.get("order_id"),
                        "status": "rejected",
                        "rejection_reason": "risk_not_approved",
                    },
                    "fill": None,
                },
            }
        adjusted_position = risk.get("adjusted_position_size_pct")
        if adjusted_position is None:
            adjusted_position = signal.get("position_size_pct")
        order = {
            "schema_version": "open_stock_ai.paper_order.v1",
            "extended_schema_version": "open_stock_ai.paper_order.v2",
            "order_id": execution.get("order_id"),
            "mode": execution.get("mode"),
            "symbol": request.get("symbol") or signal.get("symbol"),
            "market": request.get("market") or signal.get("market"),
            "horizon": request.get("horizon") or signal.get("horizon"),
            "action": signal.get("action"),
            "confidence": signal.get("confidence"),
            "entry_price": signal.get("entry_price"),
            "target_price": signal.get("target_price"),
            "stop_loss": signal.get("stop_loss"),
            "position_size_pct": adjusted_position,
            "signal_position_size_pct": signal.get("position_size_pct"),
            "risk_approved": risk.get("approved") is True,
            "risk_schema_version": risk.get("schema_version"),
            "risk_gate_checks": risk.get("gate_checks") or [],
            "risk_policy": risk.get("policy") or {},
            "execution_reason": execution.get("reason"),
            "change_id": execution.get("change_id"),
            "decision_schema": signal.get("decision_schema") or {},
            "source_modules": signal.get("source_modules") or [],
        }
        validation = (self.ai_trader or AITraderSource()).validate_paper_order(order)
        order["ai_trader_validation"] = {
            key: value
            for key, value in validation.items()
            if key != "row"
        }
        order["ai_trader_trade_row"] = validation.get("row", {})
        order["ai_trader_interop_projection"] = (self.ai_trader or AITraderSource()).build_trade_interop_projection(
            validation
        )
        oms = self._oms().submit_and_fill(order)
        order["paper_oms"] = oms
        order["oms_status"] = (oms.get("order") or {}).get("status")
        retention = self._retain_execution_snapshot(order, oms)
        order["settlement_risk"] = self.sync_settlement_risk()
        result = self.store.save_trade(order)
        return {
            **result,
            "schema_version": order["schema_version"],
            "extended_schema_version": order["extended_schema_version"],
            "order_id": order["order_id"],
            "ai_trader_valid": validation.get("valid") is True,
            "filled": oms.get("filled") is True,
            "oms": oms,
            "retention": retention,
        }

    def _retain_execution_snapshot(
        self,
        order: dict[str, Any],
        oms: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.retention_ledger is None:
            return None
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return None
        record_id = f"paper-oms-execution-{order_id}"
        existing = self.retention_ledger.get(record_id)
        if existing is not None:
            return existing
        payload = {
            "schema_version": "stock_ai.paper_oms_execution_snapshot.v1",
            "order_id": order_id,
            "source_of_truth": "paper_oms",
            "order": dict(order),
            "oms": dict(oms),
        }
        return self.retention_ledger.append(
            record_id,
            payload,
            critical=True,
            kind="execution_oms_state",
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.recent_trades(limit=limit)

    def recent_oms_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._oms().recent_orders(limit=limit)

    def account_summary(self) -> dict[str, Any]:
        return self._oms().portfolio_summary()

    def sync_settlement_risk(self, *, as_of: str | None = None) -> dict[str, Any]:
        """Feed durable PaperOMS realized P&L into the configured risk gate."""

        if self.settlement_pnl_feed is None:
            return {
                "schema_version": "open_stock_ai.settlement_pnl_feed.v1",
                "connected": False,
                "order_allowed": None,
                "reason": "durable_risk_control_not_configured",
            }
        result = self.settlement_pnl_feed.ingest_paper_oms(self._oms(), as_of=as_of)
        result["connected"] = True
        return result

    def import_corporate_actions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        return self._oms().import_corporate_actions(payloads)

    def import_trading_restrictions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        return self._oms().import_trading_restrictions(payloads)

    def trading_restrictions(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._oms().trading_restrictions(
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
        return self._oms().corporate_actions(
            symbol=symbol,
            start=start,
            end=end,
            limit=limit,
        )

    def sync_corporate_actions(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        return self._oms().sync_corporate_actions(symbol=symbol, as_of=as_of)

    def exposure(self) -> dict[str, Any]:
        account = self.account_summary()
        if int(account.get("order_count") or 0) == 0:
            legacy = self.store.paper_portfolio_exposure()
            if int(legacy.get("order_count") or 0) > 0:
                return {
                    **legacy,
                    "source_of_truth": "legacy_trade_ledger_fallback",
                    "paper_account": account,
                }
        positions = account.get("positions") or []
        symbols = {
            str(item.get("symbol")): {
                "symbol": item.get("symbol"),
                "market": item.get("market"),
                "position_size_pct": item.get("position_size_pct", 0.0),
                "quantity": item.get("quantity", 0.0),
                "average_cost": item.get("average_cost", 0.0),
                "last_price": item.get("last_price", 0.0),
                "market_value": item.get("market_value", 0.0),
                "unrealized_pnl": item.get("unrealized_pnl", 0.0),
                "realized_pnl": item.get("realized_pnl", 0.0),
            }
            for item in positions
            if item.get("symbol")
        }
        total_position_size = round(
            sum(float(item.get("position_size_pct") or 0.0) for item in symbols.values()),
            4,
        )
        return {
            "method": "open_stock_ai_paper_portfolio_exposure",
            "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
            "extended_schema_version": "open_stock_ai.paper_portfolio_exposure.v2",
            "source_of_truth": "paper_oms_positions_and_cash_ledger",
            "total_position_size_pct": total_position_size,
            "symbol_count": len(symbols),
            "order_count": account.get("order_count", 0),
            "fill_count": account.get("fill_count", 0),
            "symbols": dict(sorted(symbols.items())),
            "paper_account": account,
            "db_path": str(self.store.path),
        }

    def _oms(self) -> PaperOMS:
        if self.paper_oms is None:
            self.paper_oms = PaperOMS(
                store=self.store,
                change_management=self.change_management,
                require_change_binding=self.require_change_binding,
                retention_ledger=self.retention_ledger,
            )
        return self.paper_oms
