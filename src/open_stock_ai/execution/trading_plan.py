"""Broker-independent, durable intent for an autonomous investment decision.

A plan can wait for a future time and/or price without spending another model
call. Broker receipts, rather than an assistant sentence, advance its lifecycle.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from typing import Any

from open_stock_ai.strategy.execution_policy import single_order_quantity


def utc_time(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Trading-plan times must include a timezone")
    return result.astimezone(timezone.utc)


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ExitOrderPolicy:
    """Explicit bounds for replacing unfilled limit exits; never a market order."""

    wait_seconds: int
    max_replacements: int
    minimum_limit_price: float

    def __post_init__(self) -> None:
        if isinstance(self.wait_seconds, bool) or not isinstance(self.wait_seconds, int) or self.wait_seconds < 1:
            raise ValueError("invalid_exit_order_wait")
        if (isinstance(self.max_replacements, bool) or not isinstance(self.max_replacements, int)
                or not 1 <= self.max_replacements <= 10):
            raise ValueError("invalid_exit_order_replacements")
        if (isinstance(self.minimum_limit_price, bool) or not isinstance(self.minimum_limit_price, (int, float))
                or not math.isfinite(self.minimum_limit_price)
                or self.minimum_limit_price <= 0):
            raise ValueError("invalid_exit_order_price_floor")
        object.__setattr__(self, "minimum_limit_price", float(self.minimum_limit_price))


@dataclass(frozen=True)
class TradingPlan:
    symbol: str
    strategy_id: str
    strategy_version: str
    evidence_ids: tuple[str, ...]
    reference_price: float
    position_size_pct: float
    stop_loss: float
    quantity_shares: int
    cash_budget: float
    target_price: float | None = None
    entry_condition: str = "immediate"
    trigger_price: float | None = None
    not_before: str | None = None
    expires_at: str | None = None
    max_holding_seconds: int = 30 * 86400
    exit_not_after: str | None = None
    market: str = "TW"
    rationale: str = ""
    qualification_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    exit_order_policy: ExitOrderPolicy | None = None

    def __post_init__(self) -> None:
        if isinstance(self.exit_order_policy, dict):
            object.__setattr__(self, "exit_order_policy", ExitOrderPolicy(**self.exit_order_policy))
        if self.exit_order_policy is not None and not isinstance(self.exit_order_policy, ExitOrderPolicy):
            raise ValueError("invalid_exit_order_policy")
        normalized = str(self.symbol).strip().upper()
        if not normalized or not self.strategy_id or not self.strategy_version:
            raise ValueError("symbol_and_versioned_strategy_required")
        if not self.evidence_ids or any(not str(item).strip() for item in self.evidence_ids):
            raise ValueError("plan_evidence_required")
        object.__setattr__(self, "symbol", normalized)
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        for name in ("reference_price", "position_size_pct", "stop_loss", "cash_budget"):
            value = float(getattr(self, name))
            if isinstance(getattr(self, name), bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid_{name}")
            object.__setattr__(self, name, value)
        if isinstance(self.quantity_shares, bool) or not isinstance(self.quantity_shares, int) or self.quantity_shares < 1:
            raise ValueError("invalid_frozen_quantity")
        object.__setattr__(self, "market", str(self.market).strip().upper())
        if single_order_quantity(self.quantity_shares, market=self.market)["quantity"] != self.quantity_shares:
            raise ValueError("frozen_quantity_requires_single_venue_lot")
        if self.quantity_shares * self.reference_price > self.cash_budget + 0.01:
            raise ValueError("frozen_quantity_exceeds_budget")
        if self.position_size_pct > 100 or self.stop_loss >= self.reference_price:
            raise ValueError("invalid_plan_risk_parameters")
        if self.exit_order_policy and self.exit_order_policy.minimum_limit_price > self.stop_loss:
            raise ValueError("exit_order_price_floor_above_stop_loss")
        if self.exit_order_policy and self.executable_exit_price_floor() > self.stop_loss:
            raise ValueError("exit_order_executable_floor_above_stop_loss")
        if self.target_price is not None and (
            not math.isfinite(self.target_price) or self.target_price <= self.reference_price
        ):
            raise ValueError("invalid_target_price")
        if self.entry_condition not in {"immediate", "price_at_or_above", "price_at_or_below"}:
            raise ValueError("unsupported_entry_condition")
        if self.entry_condition != "immediate" and (
            self.trigger_price is None or not math.isfinite(self.trigger_price) or self.trigger_price <= 0
        ):
            raise ValueError("trigger_price_required")
        if isinstance(self.max_holding_seconds, bool) or not isinstance(self.max_holding_seconds, int) or self.max_holding_seconds < 60:
            raise ValueError("invalid_holding_period")
        if self.not_before:
            utc_time(self.not_before)
        if self.expires_at:
            expiry = utc_time(self.expires_at)
            if self.not_before and expiry <= utc_time(self.not_before):
                raise ValueError("entry_expiry_precedes_start")
        if self.exit_not_after:
            deadline = utc_time(self.exit_not_after)
            if self.not_before and deadline <= utc_time(self.not_before):
                raise ValueError("exit_deadline_precedes_entry_start")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Preserve the exact serialized definition and idempotency hash of
        # pre-policy plans. Loading them must not authorize new price changes.
        if self.exit_order_policy is None:
            result.pop("exit_order_policy")
        return result

    def executable_exit_price_floor(self) -> float:
        """Keep the declared floor intact while respecting supported venue ticks."""
        if self.exit_order_policy is None:
            raise ValueError("explicit_exit_order_policy_required")
        floor = self.exit_order_policy.minimum_limit_price
        if self.market in {"TW", "TWSE", "TPEX"} or self.symbol.endswith((".TW", ".TWO")):
            from .taiwan_market_rules import tick_size

            value = Decimal(str(floor))
            unit = tick_size(value)
            return float((value / unit).to_integral_value(rounding=ROUND_CEILING) * unit)
        return floor

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TradingPlan":
        return cls(**value)

    def entry_status(self, *, price: float, now: datetime) -> str:
        instant = utc_time(now)
        if self.expires_at and instant >= utc_time(self.expires_at):
            return "expired"
        if self.exit_not_after and instant >= utc_time(self.exit_not_after):
            return "expired"
        if self.not_before and instant < utc_time(self.not_before):
            return "waiting_time"
        if not math.isfinite(price) or price <= 0:
            return "waiting_price"
        # The bracket describes the intended position after entry. A breakout
        # may start below its future stop, and a pullback above its target.
        # Check that the trigger is reached before applying that bracket.
        if self.entry_condition == "price_at_or_above" and price < float(self.trigger_price):
            return "waiting_price"
        if self.entry_condition == "price_at_or_below" and price > float(self.trigger_price):
            return "waiting_price"
        if price <= self.stop_loss:
            return "thesis_invalidated"
        if self.target_price is not None and price >= self.target_price:
            return "opportunity_passed"
        return "triggered"

    def exit_reason(self, *, price: float, entered_at: str, now: datetime) -> str | None:
        if not math.isfinite(price) or price <= 0:
            return self.time_exit_reason(entered_at=entered_at, now=now)
        if price <= self.stop_loss:
            return "stop_loss"
        if self.target_price is not None and price >= self.target_price:
            return "take_profit"
        return self.time_exit_reason(entered_at=entered_at, now=now)

    def time_exit_reason(self, *, entered_at: str, now: datetime) -> str | None:
        if self.exit_not_after and utc_time(now) >= utc_time(self.exit_not_after):
            return "absolute_exit_deadline"
        if (utc_time(now) - utc_time(entered_at)).total_seconds() >= self.max_holding_seconds:
            return "holding_period_expired"
        return None
