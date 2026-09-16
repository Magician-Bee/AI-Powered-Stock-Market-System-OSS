"""Immutable per-plan learning observations from reconciled broker fills."""
from __future__ import annotations

import json
import math
from typing import Any

from .trading_plan import content_hash


class AutonomousOutcomeLedger:
    def __init__(self, store):
        self.store = store
        with store._connect() as conn:
            conn.execute("""create table if not exists autonomous_trade_outcomes (
                plan_id text primary key, account_id text not null, closed_at text not null,
                strategy_id text not null, net_pnl real not null, receipt_sha256 text not null,
                payload_json text not null
            )""")
            conn.commit()

    def record(self, *, plan: dict[str, Any], fills: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        # BrokerPort adds a provenance view to the same authoritative fill
        # rows. Keep the v1 accounting receipt identical to direct ledger reads
        # and previously sealed outcomes. Source evidence stays in cash_ledger,
        # linked by fill_id and audited separately; it is not a trading reward.
        fills = [{key: value for key, value in fill.items()
                  if key not in {"fill_evidence", "fill_evidence_verification"}} for fill in fills]
        state = plan["state"]
        if state["status"] != "closed" or float(state["remaining_quantity"]) != 0 or not fills:
            raise ValueError("outcome_requires_closed_reconciled_position")
        if mode not in {"paper", "live"}:
            raise ValueError("outcome_execution_mode_required")
        # Bind learning to this plan's actual dispatch journal, including
        # earlier partial exits. A different round trip in the same symbol
        # and account cannot be substituted as this plan's reward.
        with self.store._connect() as conn:
            events = conn.execute("select payload_json from autonomous_trading_plan_events where plan_id=?", (plan["plan_id"],)).fetchall()
        intents = {}
        for event in events:
            payload = json.loads(event[0])
            for phase in ("entry_intent", "exit_intent"):
                intent = payload.get(phase) or {}
                if intent.get("order_id"):
                    intents[intent["order_id"]] = intent
        if len({fill.get("fill_id") for fill in fills}) != len(fills) or any(not fill.get("fill_id") for fill in fills):
            raise ValueError("duplicate_or_missing_outcome_fill_id")
        for fill in fills:
            intent = intents.get(fill.get("order_id"))
            if not intent or fill["side"] != intent["side"]:
                raise ValueError("outcome_fill_not_in_plan_dispatch_journal")
            if fill["account_id"] != plan["account_id"] or fill["symbol"] != plan["symbol"]:
                raise ValueError("outcome_fill_scope_mismatch")
            if fill["side"] not in {"buy", "sell"}:
                raise ValueError("outcome_unsupported_side")
            for field in ("quantity", "fill_price", "commission", "tax", "slippage_cost", "net_cash_delta"):
                if not math.isfinite(float(fill[field])):
                    raise ValueError("outcome_nonfinite_accounting")
            if float(fill["quantity"]) <= 0 or float(fill["fill_price"]) <= 0 or min(float(fill[k]) for k in ("commission", "tax", "slippage_cost")) < 0:
                raise ValueError("outcome_invalid_accounting")
            gross = float(fill["quantity"]) * float(fill["fill_price"])
            expected = (-gross if fill["side"] == "buy" else gross) - float(fill["commission"]) - float(fill["tax"])
            if abs(expected - float(fill["net_cash_delta"])) > .011:
                raise ValueError("outcome_fill_cashflow_mismatch")
        for order_id, intent in intents.items():
            quantity = sum(float(fill["quantity"]) for fill in fills if fill["order_id"] == order_id)
            if quantity > float(intent["quantity_shares"]):
                raise ValueError("outcome_fills_exceed_frozen_order")
        buys = [f for f in fills if f["side"] == "buy"]
        sells = [f for f in fills if f["side"] == "sell"]
        bought, sold = sum(float(f["quantity"]) for f in buys), sum(float(f["quantity"]) for f in sells)
        if bought != sold or bought != float(state["filled_quantity"]):
            raise ValueError("outcome_position_not_flat")
        entry_cost = -sum(float(f["net_cash_delta"]) for f in buys)
        pnl = sum(float(f["net_cash_delta"]) for f in fills)
        payload = {
            "schema_version": "open_stock_ai.autonomous_trade_outcome.v1", "plan_id": plan["plan_id"],
            "account_id": plan["account_id"], "symbol": plan["symbol"], "mode": mode,
            "strategy_id": plan["definition"]["strategy_id"], "strategy_version": plan["definition"]["strategy_version"],
            "plan_definition_hash": plan["definition_hash"], "closed_at": state["closed_at"],
            "quantity": bought, "net_pnl": pnl, "entry_cash_used": entry_cost,
            "net_return_pct": pnl / entry_cost * 100,
            "commission": sum(float(f["commission"]) for f in fills),
            "tax": sum(float(f["tax"]) for f in fills),
            "slippage_cost_already_in_fill_prices": sum(float(f["slippage_cost"]) for f in fills),
            "fills": sorted(fills, key=lambda f: (f["created_at"], f["fill_id"])),
            "exit_reason": state.get("exit_reason"),
            "attribution": "this_plan_fill_cashflows_only; unrelated_positions_not_in_reward",
            "execution_evidence_eligible": False, "positive_ev_qualified": False,
            "learning_status": "observed_closed_trade; strategy_change_requires_new_evaluation",
        }
        digest = content_hash(payload)
        payload["receipt_sha256"] = digest
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            old = conn.execute("select receipt_sha256,payload_json from autonomous_trade_outcomes where plan_id=?", (plan["plan_id"],)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("closed_outcome_changed_requires_reconciliation")
                return json.loads(old[1])
            conn.execute("insert into autonomous_trade_outcomes values (?,?,?,?,?,?,?)",
                         (plan["plan_id"], plan["account_id"], state["closed_at"], payload["strategy_id"], pnl, digest,
                          json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            conn.commit()
        return payload

    def list(self, *, account_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute("select payload_json from autonomous_trade_outcomes where account_id=? order by closed_at,plan_id", (account_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def summary(self, *, account_id: str) -> dict[str, Any]:
        rows = self.list(account_id=account_id)
        return {"closed_trade_count": len(rows), "net_pnl": sum(r["net_pnl"] for r in rows),
                "winning_trades": sum(r["net_pnl"] > 0 for r in rows), "losing_trades": sum(r["net_pnl"] < 0 for r in rows),
                "positive_ev_qualified": False, "outcomes": rows}
