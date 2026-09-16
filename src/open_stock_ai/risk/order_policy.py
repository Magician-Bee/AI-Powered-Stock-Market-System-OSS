"""Shared account/order admission for paper and live broker adapters.

This module never places orders. Evidence must come from the Host's retained
tool/research results, not an Agent's JSON assertions. Receipt hashes protect
content integrity; the caller is responsible for trusted receipt provenance.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from math import isfinite
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .risk_engine import RiskEngine


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def order_risk_evidence_receipt(*, kind: str, payload: dict[str, Any], source: str, passed: bool) -> dict[str, Any]:
    """Wrap an already validated Host result; this is not a validation shortcut."""
    body = {"schema_version": "open_stock_ai.order_risk_evidence.v1", "kind": kind,
            "source": source, "payload": payload, "passed": passed}
    return {**body, "receipt_sha256": _hash(body)}


def _receipt_valid(value: Any, kind: str) -> bool:
    if not isinstance(value, dict):
        return False
    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    try:
        return bool(
            body.get("schema_version") == "open_stock_ai.order_risk_evidence.v1"
            and body.get("kind") == kind and body.get("passed") is True
            and str(body.get("source") or "").strip()
            and isinstance(body.get("payload"), dict) and body["payload"]
            and value.get("receipt_sha256") == _hash(body)
        )
    except (ValueError, TypeError):
        return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _reservations(account: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    rows = account.get("open_order_reservations", [])
    result = {"valid": isinstance(rows, list), "buy_cash": 0.0, "buy_notional": 0.0,
              "symbol_buy_notional": 0.0, "industry_buy_notional": 0.0, "symbol_sell_quantity": 0.0,
              "order_ids": [], "invalid_order_ids": []}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            result["valid"] = False
            continue
        order_id, side, symbol = str(row.get("order_id") or ""), row.get("side"), row.get("symbol")
        quantity = _number(row.get("remaining_quantity"))
        price = _number(row.get("reservation_price", row.get("limit_price")))
        cost = _number(row.get("estimated_remaining_cost"))
        valid = bool(row.get("valid") is not False and not row.get("blockers")
                     and order_id and order_id not in result["order_ids"] and symbol and side in {"buy", "sell"}
                     and quantity is not None and quantity > 0 and price is not None and price > 0
                     and cost is not None and cost >= 0 and isfinite(quantity * price + cost))
        if not valid:
            result["valid"] = False
            result["invalid_order_ids"].append(order_id)
            continue
        result["order_ids"].append(order_id)
        if side == "buy":
            notional = quantity * price
            result["buy_cash"] += notional + cost
            result["buy_notional"] += notional
            if symbol == intent.get("symbol"):
                result["symbol_buy_notional"] += notional
            if row.get("industry") == intent.get("industry"):
                result["industry_buy_notional"] += notional
        elif symbol == intent.get("symbol"):
            result["symbol_sell_quantity"] += quantity
    if not all(isfinite(result[key]) for key in ("buy_cash", "buy_notional", "symbol_buy_notional", "industry_buy_notional", "symbol_sell_quantity")):
        result["valid"] = False
    return result


def evaluate_order_intent(
    engine: RiskEngine, *, intent: dict[str, Any], account_summary: dict[str, Any],
    evidence: dict[str, Any], mode: str = "paper", eligibility: str = "bounded_experiment",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check one frozen order against shared limits and declared dependencies.

    ``bounded_experiment`` is paper-only and needs explicit experiment limits.
    ``qualified`` additionally needs a verifier-approved qualification receipt.
    Neither route enables a broker, creates a fill, or proves future returns.
    """
    symbol, side = str(intent.get("symbol") or ""), str(intent.get("side") or "").lower()
    quantity, price = _number(intent.get("quantity_shares")), _number(intent.get("reference_price"))
    equity = _number(account_summary.get("total_equity"))
    cash = _number(account_summary.get("available_cash", account_summary.get("cash_balance")))
    positions = account_summary.get("positions")
    positions_valid = isinstance(positions, list) and all(
        isinstance(item, dict) and str(item.get("symbol") or "")
        and _number(item.get("quantity")) is not None and _number(item.get("market_value")) is not None
        for item in (positions or [])
    )
    held = sum(float(item["quantity"]) for item in positions if item["symbol"] == symbol) if positions_valid else 0.0
    reservations = _reservations(account_summary, intent)
    available_held = held - reservations["symbol_sell_quantity"] if reservations["valid"] else 0
    cash_for_order = cash - reservations["buy_cash"] if cash is not None and reservations["valid"] else 0
    reduction = bool(side == "sell" and quantity is not None and 0 < quantity <= available_held)
    checks: list[dict[str, Any]] = []

    def gate(code: str, passed: bool, observed: Any, limit: Any, message: str, *, applies: bool = True) -> None:
        checks.append(engine._gate(code=code, passed=passed, observed=observed, limit=limit, message=message, applies=applies))

    valid_order = bool(symbol and side in {"buy", "sell"} and quantity is not None and quantity > 0 and price is not None and price > 0
                       and isfinite(quantity * price))
    gate("order_input", valid_order, {"symbol": symbol, "side": side, "quantity": quantity, "price": price},
         "finite positive quantity/price, supported long-only side", "Validate the frozen order; no quantity or price fallback.")
    valid_account = bool(equity is not None and equity > 0 and cash is not None and cash >= 0 and positions_valid)
    gate("account_state", valid_account, {"equity": equity, "available_cash": cash, "positions_valid": positions_valid},
         "finite authoritative equity/cash/positions", "Account risk requires an authoritative normalized snapshot.")
    gate("open_order_reservations", reservations["valid"], reservations, "complete normalized active-order reservations",
         "Pending buys reserve cash/exposure; pending sells reserve owned quantity, never provisional holdings.")
    gate("position_after_reservations", side != "sell" or reduction, available_held, quantity,
         "Sell orders cannot reuse quantity already reserved by another active order.")
    gate("reduce_only", intent.get("reduce_only") is not True or reduction, reduction, "sell no more than held quantity",
         "A reduce-only label cannot create a short or add risk.")

    # Reuse the existing central exposure policy, then add execution-specific
    # checks. An actual reduction may proceed despite pre-existing loss/exposure
    # breaches; it may not evade position ownership or an operator kill switch.
    preview = engine.evaluate_order_preview(
        symbol=symbol, side=side, quantity_shares=quantity if valid_order else 0, reference_price=price if valid_order else 0,
        industry=intent.get("industry"),
        account_summary={**account_summary, "cash_balance": max(0, cash_for_order), "positions": positions if positions_valid else [],
                         "total_equity": equity or 0, "today_pnl": _number(account_summary.get("today_pnl"))},
    )
    for check in preview["gate_checks"]:
        if check["code"] == "paper_mode":
            continue
        item = dict(check)
        reserved_exposure = {
            "symbol_exposure": reservations["symbol_buy_notional"], "total_exposure": reservations["buy_notional"],
            "industry_exposure": reservations["industry_buy_notional"],
        }.get(item["code"], 0)
        if reserved_exposure and equity and equity > 0:
            item["observed"] += reserved_exposure / equity * 100
            item["passed"] = item["observed"] <= item["limit"]
            item["message"] = "Proposed exposure includes outstanding buy commitments; pending sells do not reduce exposure."
        if reduction and item["code"] in {"order_notional", "symbol_exposure", "total_exposure", "industry_exposure", "daily_loss"}:
            item.update(passed=True, message="Verified position reduction does not add the prohibited exposure.")
        checks.append(item)

    pnl = _number(account_summary.get("today_pnl"))
    peak = _number(account_summary.get("peak_equity"))
    drawdown = _number(account_summary.get("current_drawdown_pct"))
    if drawdown is None and peak is not None and equity is not None and peak >= equity and peak > 0:
        drawdown = (peak - equity) / peak * 100
    loss_state_known = pnl is not None and drawdown is not None and drawdown >= 0
    gate("account_loss_state", reduction or loss_state_known, {"today_pnl": pnl, "drawdown_pct": drawdown},
         "observed daily P&L and high-water drawdown", "Missing loss state cannot be silently treated as zero for new exposure.")
    gate("account_drawdown", reduction or (drawdown is not None and 0 <= drawdown <= engine.max_total_drawdown_pct),
         drawdown, engine.max_total_drawdown_pct, "Account drawdown limits new exposure; verified reductions remain available.")

    stop = _number(intent.get("stop_loss"))
    stop_valid = reduction or bool(side == "buy" and price is not None and stop is not None and 0 < stop < price)
    gate("protective_stop", stop_valid, stop, "0 < long stop < entry price", "New long exposure needs a valid frozen protective stop.")
    exit_floor = _number(intent.get("exit_minimum_limit_price"))
    exit_floor_declared = "exit_minimum_limit_price" in intent
    exit_floor_valid = (not exit_floor_declared or bool(
        isinstance(intent.get("exit_minimum_limit_price"), (int, float))
        and exit_floor is not None and stop is not None and 0 < exit_floor <= stop
    ))
    if exit_floor_declared:
        gate("exit_price_floor", reduction or exit_floor_valid, exit_floor, "0 < exit floor <= protective stop",
             "An explicit exit replacement floor must retain the frozen protective stop and risk budget.")
    risk_exit_price = min(stop, exit_floor) if exit_floor_declared and exit_floor_valid and stop is not None else stop

    receipts = evidence.get("receipts") if isinstance(evidence.get("receipts"), dict) else {}
    declared = evidence.get("required_evidence")
    required = set(str(item) for item in declared if str(item)) if isinstance(declared, (list, tuple)) else set()
    if not reduction:
        gate("strategy_dependencies_declared", bool(required), sorted(required), "Host strategy manifest dependencies",
             "The strategy declares its inputs; unrelated external-model or financial-report gates are not inferred.")
    required.add("cost_model")
    if reduction:
        required = {"cost_model"}
    dependencies = []
    for kind in sorted(required):
        receipt = receipts.get(kind)
        passed = _receipt_valid(receipt, kind)
        gate(f"evidence:{kind}", passed, (receipt or {}).get("receipt_sha256") if isinstance(receipt, dict) else None,
             "bound validated Host receipt", f"Required {kind} evidence must be present and intact.")
        dependencies.append({"kind": kind, "passed": passed, "receipt_sha256": receipt.get("receipt_sha256") if isinstance(receipt, dict) else None})

    cost_receipt = receipts.get("cost_model") or {}
    cost_payload = (cost_receipt.get("payload") or {}) if isinstance(cost_receipt, dict) else {}
    cost_payload = cost_payload if isinstance(cost_payload, dict) else {}
    cost = _number(cost_payload.get("estimated_total_cost")) if isinstance(cost_payload, dict) else None
    cost_bound = bool(
        _receipt_valid(cost_receipt, "cost_model") and cost is not None and cost >= 0
        and cost_payload.get("symbol") == symbol and cost_payload.get("side") == side
        and _number(cost_payload.get("quantity_shares")) == quantity
        and _number(cost_payload.get("reference_price")) == price
    )
    gate("order_cost_binding", cost_bound, cost, "same symbol/side/quantity/reference_price", "The cost estimate belongs to this frozen order.")
    notional = quantity * price if valid_order else 0
    gate("cash_after_costs", side != "buy" or bool(cost_bound and cash is not None and notional + cost <= cash_for_order),
         notional + cost if cost is not None else None, cash_for_order, "Buy notional plus validated costs must fit cash after open-order reservations.")
    loss_pct = ((price - risk_exit_price) * quantity + (cost or 0)) / equity * 100 if valid_order and risk_exit_price is not None and equity and equity > 0 else None
    gate("stop_loss_exposure", reduction or bool(stop_valid and exit_floor_valid and cost_bound and loss_pct is not None and loss_pct <= engine.max_daily_loss_pct),
         loss_pct, engine.max_daily_loss_pct, "Loss to the protective stop or lower explicit exit floor, plus entry costs, must fit the common risk budget.")

    if engine.risk_control is not None:
        scopes = {"global": "global"}
        if symbol:
            scopes["symbol"] = symbol
        for scope, key in (("account", "account_id"), ("broker", "broker_id"), ("strategy", "strategy_id")):
            if value := intent.get(key) or account_summary.get(key):
                scopes[scope] = str(value)
        control = engine.risk_control.order_gate(scopes)
        automatic_loss_only = bool(control["active_switches"]) and all(
            switch.get("activated_by") == "loss_limit_monitor" for switch in control["active_switches"]
        )
        gate("durable_risk_control", control["allowed"] is True or (reduction and automatic_loss_only), control,
             "no active stop; only automatic loss stops permit verified reductions", "Operator kill switches remain authoritative.")

    qualified = False
    qualification = evidence.get("qualification")
    if isinstance(qualification, dict) and intent.get("strategy_id") and intent.get("strategy_version_hash"):
        try:
            from open_stock_ai.research.candle_qualification import verify_qualification_receipt
            qualified = verify_qualification_receipt(
                qualification, strategy_id=intent.get("strategy_id"),
                strategy_version_hash=intent.get("strategy_version_hash"),
            ) is True
        except (ImportError, TypeError, ValueError, KeyError):
            qualified = False
    gate("execution_mode", mode in {"paper", "live"} and eligibility in {"bounded_experiment", "qualified"},
         {"mode": mode, "eligibility": eligibility}, "explicit supported mode and eligibility", "Paper and live use the same order/account checks.")
    if not reduction and eligibility == "bounded_experiment":
        limits = evidence.get("experiment_limits") if isinstance(evidence.get("experiment_limits"), dict) else {}
        order_limit, total_limit = _number(limits.get("max_order_notional_pct")), _number(limits.get("max_total_exposure_pct"))
        experiment_bounded = bool(
            mode == "paper" and order_limit is not None and 0 < order_limit <= engine.max_position_size_pct
            and total_limit is not None and 0 < total_limit <= engine.max_total_paper_exposure_pct
            and preview["metrics"]["order_notional_pct"] <= order_limit
            and preview["metrics"]["proposed_total_exposure_pct"] + (reservations["buy_notional"] / equity * 100 if equity and equity > 0 else 0) <= total_limit
        )
        gate("bounded_experiment", experiment_bounded, limits, "explicit bounded paper budget within central risk limits",
             "A bounded paper experiment gathers evidence and makes no positive-EV deployment claim.")
    elif not reduction:
        gate("strategy_qualification", qualified, qualification.get("receipt_sha256") if isinstance(qualification, dict) else None,
             "verified matching positive-EV qualification", "Qualified deployment requires the strategy's actual evaluation receipt.")
    if mode == "live":
        gate("live_policy_enabled", engine.live_trading_enabled and eligibility == "qualified", engine.live_trading_enabled,
             True, "This policy check does not bypass broker readiness or execution authorization.")
        gate("live_cost_evidence", cost_bound and cost_payload.get("execution_evidence_eligible") is True,
             cost_payload.get("execution_evidence_eligible"), True, "Live orders require verified account cost evidence.")

    failures = [item["code"] for item in checks if item["applies"] and item["severity"] == "block" and not item["passed"]]
    return {
        "schema_version": "open_stock_ai.order_risk_decision.v1", "approved": not failures,
        "evaluated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "mode": mode, "eligibility": eligibility, "reduce_only": reduction,
        "strategy_id": intent.get("strategy_id"), "strategy_version_hash": intent.get("strategy_version_hash"),
        "positive_ev_qualified": qualified, "evidence_dependencies": dependencies,
        "gate_checks": checks, "blockers": failures, "metrics": {**preview["metrics"], "estimated_stop_loss_pct": loss_pct,
            "risk_exit_price": risk_exit_price, "exit_minimum_limit_price": exit_floor,
            "open_buy_reserved_cash": reservations["buy_cash"], "available_cash_after_reservations": cash_for_order,
            "symbol_sellable_quantity_after_reservations": available_held},
        "execution_boundary": "risk_admission_only_no_order_or_fill; broker_and_quote_authorization_still_required",
    }
