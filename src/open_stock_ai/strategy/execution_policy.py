from __future__ import annotations

"""Shared, deterministic long-only instructions for research and paper execution.

Sizing is an ORDER notional percentage of decision-time equity, never a target
portfolio weight.  A percentage of zero is an instruction to do nothing.
Protective orders are attached to the shares actually filled, not hypothetical
positions.  These rules are a simulation contract, not broker fill evidence.
"""

from math import floor, isfinite
from typing import Any

from open_stock_ai.types import TradingSignal


POLICY_ID = "long_only_order_equity_pct_bracket.v1"


def policy_metadata() -> dict[str, Any]:
    return {
        "policy_id": POLICY_ID,
        "position_size_basis": "order_notional_pct_of_decision_equity",
        "quantity_fixed_at": "decision_time",
        "hold_behavior": "preserve_quantity_no_rebalancing",
        "sell_reduce_behavior": "explicit_sized_sale_capped_at_owned_quantity",
        "missing_or_zero_sizing": "no_order",
        "entry_price_behavior": "reference_for_sizing_next_bar_market_fill",
        "bracket_scope": "each_filled_entry_lot",
        "stop_gap_behavior": "open_if_below_stop_otherwise_stop",
        "target_gap_behavior": "target_no_favorable_gap_improvement",
        "same_bar_stop_and_target": "stop_first_conservative_ohlc_assumption",
        "protective_partial_exit": "keep_exit_intent_for_residual_even_if_price_recovers",
        "shorting": False,
        "tw_single_order_rounding": "1000_share_board_lot_if_quantity_at_least_1000_else_integer_odd_lot",
        "tw_residual": "retain_cash_or_owned_residual_for_separate_later_order",
        "broker_execution_certified": False,
    }


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def single_order_quantity(quantity: int, *, market: str = "TW", minimum_lot_size: int = 1) -> dict[str, Any]:
    """One valid venue child quantity; caller retains and handles the residual."""
    if isinstance(quantity, bool) or not isfinite(quantity) or quantity < 0 or int(quantity) != quantity:
        raise ValueError("nonnegative_integer_quantity_required")
    if minimum_lot_size < 1:
        raise ValueError("positive_lot_size_required")
    lot_size = max(minimum_lot_size, 1000) if market == "TW" and quantity >= 1000 else minimum_lot_size
    rounded = int(quantity) // lot_size * lot_size
    return {"quantity": rounded, "lot_size": lot_size,
            "lot_type": "board_lot" if market == "TW" and lot_size >= 1000 else "odd_lot",
            "unsubmitted_quantity": int(quantity) - rounded}


def plan_signal_order(
    signal: TradingSignal,
    *,
    equity: float,
    cash: float,
    reference_price: float,
    position_quantity: int,
    commission_bps: float = 0.0,
    exchange_fee_bps: float = 0.0,
    minimum_commission: float = 0.0,
    lot_size: int = 1,
) -> dict[str, Any]:
    """Freeze share quantity/budget using only information at the decision.

    Fill-time gaps can reduce an affordable buy, never enlarge its authorized
    quantity or cash budget.  Costs are charged by the execution ledger again
    at the actual fill price; these inputs only make sizing conservative.
    """
    result: dict[str, Any] = {
        "policy_id": POLICY_ID, "action": signal.action, "side": None,
        "quantity": 0, "cash_budget": 0.0, "position_size_pct": signal.position_size_pct,
        "stop_loss": signal.stop_loss, "target_price": signal.target_price,
        "reference_price": reference_price, "decision_equity": equity,
        "eligible": False, "reason": "hold_preserves_position",
    }
    if signal.action == "hold":
        return result
    if signal.action not in {"buy", "add", "sell", "reduce"}:
        return {**result, "reason": "unsupported_action"}
    pct = _positive(signal.position_size_pct)
    if pct is None or pct > 100:
        return {**result, "reason": "explicit_position_size_between_zero_and_100_required"}
    if _positive(equity) is None or _positive(reference_price) is None or lot_size < 1:
        return {**result, "reason": "invalid_account_or_price"}
    if not isfinite(cash) or cash < 0 or position_quantity < 0:
        return {**result, "reason": "invalid_account_or_price"}
    if any(not isfinite(v) or v < 0 for v in (commission_bps, exchange_fee_bps, minimum_commission)):
        return {**result, "reason": "invalid_cost_assumption"}
    side = "buy" if signal.action in {"buy", "add"} else "sell"
    budget = equity * pct / 100.0
    if side == "buy":
        stop = _positive(signal.stop_loss)
        target = _positive(signal.target_price)
        if stop is None or not stop < reference_price:
            return {**result, "reason": "long_entry_requires_valid_protective_stop"}
        if signal.target_price is not None and (target is None or target <= reference_price):
            return {**result, "reason": "invalid_long_target"}
        budget = min(cash, budget)
        rate = (commission_bps + exchange_fee_bps) / 10000.0
        quantity = min(
            floor(budget / (reference_price * (1 + rate))),
            floor(max(0.0, budget - minimum_commission) / reference_price),
        )
    else:
        quantity = min(position_quantity, floor(budget / reference_price))
    venue_quantity = single_order_quantity(quantity, market=signal.market, minimum_lot_size=lot_size)
    quantity = venue_quantity["quantity"]
    return {
        **result, "side": side, "quantity": quantity,
        **venue_quantity,
        "cash_budget": budget if side == "buy" else 0.0,
        "eligible": quantity > 0,
        "reason": "explicit_sized_order" if quantity > 0 else "quantity_below_one_lot_or_no_position",
    }


def protective_exit(
    *, open_price: float, high: float, low: float,
    stop_loss: float | None, target_price: float | None,
) -> dict[str, Any] | None:
    """Evaluate a previously attached bracket without assuming OHLC ordering.

    If both levels occur in a bar, assume the adverse stop first. Targets get
    no favorable gap improvement. Actual spread/impact/tax still apply later.
    """
    if any(_positive(v) is None for v in (open_price, high, low)) or low > high:
        raise ValueError("invalid_protective_exit_bar")
    stop = _positive(stop_loss)
    target = _positive(target_price)
    if stop is not None and low <= stop:
        return {"reason": "stop_loss", "reference_price": min(open_price, stop),
                "ambiguous_bar": target is not None and high >= target}
    if target is not None and high >= target:
        return {"reason": "target_price", "reference_price": target, "ambiguous_bar": False}
    return None
