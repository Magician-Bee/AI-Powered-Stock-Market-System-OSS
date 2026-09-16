from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerOrderIntent


class BrokerRiskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_risk_decision.v2"] = (
        "stock_ai.broker_risk_decision.v2"
    )
    risk_approval_id: str
    approved: bool
    checked_at: datetime
    checks: dict[str, bool]
    reasons: list[str] = Field(default_factory=list)
    pretrade_receipt: BrokerPreTradeRiskReceipt | None = None

    def verify_pretrade_receipt(self, intent: BrokerOrderIntent) -> bool:
        """Verify that a live approval is bound to this exact intent and evidence."""

        receipt = self.pretrade_receipt
        if receipt is None:
            return False
        return receipt.verify(intent, self)


class BrokerPreTradeRiskContext(BaseModel):
    """Host-observed inputs required to approve a live broker order.

    An order proposal is deliberately not enough to infer these values.  The
    host must bind the approval to one fresh quote, a market phase and explicit
    account/exposure limits so a model cannot convert a stale or unbounded
    proposal into a live order.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_pretrade_risk_context.v1"] = (
        "stock_ai.broker_pretrade_risk_context.v1"
    )
    reference_price: Decimal = Field(gt=0)
    quote_observed_at: datetime
    max_quote_age_seconds: int = Field(gt=0, le=300)
    market_phase: Literal[
        "pre_open", "continuous", "closing_auction", "odd_lot", "closed", "halted"
    ]
    allowed_market_phases: set[str] = Field(default_factory=lambda: {"continuous"})
    max_quantity: Decimal = Field(gt=0)
    max_order_notional: Decimal = Field(gt=0)
    price_collar_bps: Decimal = Field(gt=0, le=10_000)
    adv_quantity: Decimal = Field(gt=0)
    max_adv_participation_pct: Decimal = Field(gt=0, le=100)
    current_symbol_notional: Decimal = Field(ge=0)
    max_symbol_notional: Decimal = Field(gt=0)
    current_account_notional: Decimal = Field(ge=0)
    max_account_notional: Decimal = Field(gt=0)
    daily_loss_notional: Decimal = Field(ge=0)
    max_daily_loss_notional: Decimal = Field(gt=0)
    sellable_quantity: Decimal | None = Field(default=None, ge=0)
    strategy_execution_authorized: bool
    model_execution_eligible: bool


class BrokerPreTradeRiskReceipt(BaseModel):
    """Content-addressed evidence for one live pre-trade risk decision.

    The receipt carries the complete host-observed risk context, calculated
    values and every check result.  It is intentionally independent of model
    text so OMS can verify the approval without trusting a caller-supplied
    boolean.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_pretrade_risk_receipt.v1"] = (
        "stock_ai.broker_pretrade_risk_receipt.v1"
    )
    receipt_id: str
    risk_approval_id: str
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pretrade_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    pretrade_context: BrokerPreTradeRiskContext
    observed: dict[str, str]
    checks: dict[str, bool]
    reasons: list[str]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def issue(
        cls,
        *,
        intent: BrokerOrderIntent,
        pretrade_context: BrokerPreTradeRiskContext,
        checked_at: datetime,
        checks: dict[str, bool],
        reasons: list[str],
        observed: dict[str, str],
    ) -> "BrokerPreTradeRiskReceipt":
        checked_at = _utc(checked_at)
        decision = _decision_payload(
            risk_approval_id=intent.risk_approval_id,
            approved=not reasons,
            checked_at=checked_at,
            checks=checks,
            reasons=reasons,
        )
        payload: dict[str, Any] = {
            "schema_version": "stock_ai.broker_pretrade_risk_receipt.v1",
            "risk_approval_id": intent.risk_approval_id,
            "intent_sha256": _digest(intent.model_dump(mode="json")),
            "pretrade_context_sha256": _digest(
                pretrade_context.model_dump(mode="json")
            ),
            "decision_sha256": _digest(decision),
            "evaluated_at": checked_at.isoformat(),
            "pretrade_context": pretrade_context.model_dump(mode="json"),
            "observed": observed,
            "checks": checks,
            "reasons": reasons,
        }
        # Hash Pydantic's JSON representation, not the pre-validation input.
        # In particular, it canonicalizes UTC datetimes (``Z``) and sets.
        draft = cls(
            **payload,
            receipt_id="BPR-pending",
            receipt_sha256="0" * 64,
        )
        receipt_sha256 = _digest(
            draft.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        return cls(
            **payload,
            receipt_id=f"BPR-{receipt_sha256[:32]}",
            receipt_sha256=receipt_sha256,
        )

    def verify(self, intent: BrokerOrderIntent, decision: BrokerRiskDecision) -> bool:
        """Return false for any altered receipt, decision or submitted intent."""

        checked_at = _utc(decision.checked_at)
        if self.risk_approval_id != decision.risk_approval_id:
            return False
        if self.risk_approval_id != intent.risk_approval_id:
            return False
        if self.evaluated_at != checked_at:
            return False
        if self.intent_sha256 != _digest(intent.model_dump(mode="json")):
            return False
        if self.pretrade_context_sha256 != _digest(
            self.pretrade_context.model_dump(mode="json")
        ):
            return False
        if self.checks != decision.checks or self.reasons != decision.reasons:
            return False
        if self.decision_sha256 != _digest(
            _decision_payload(
                risk_approval_id=decision.risk_approval_id,
                approved=decision.approved,
                checked_at=checked_at,
                checks=decision.checks,
                reasons=decision.reasons,
            )
        ):
            return False
        payload = self.model_dump(
            mode="json", exclude={"receipt_id", "receipt_sha256"}
        )
        expected = _digest(payload)
        return self.receipt_sha256 == expected and self.receipt_id == f"BPR-{expected[:32]}"


class CentralBrokerRiskGate:
    def evaluate(
        self,
        intent: BrokerOrderIntent,
        *,
        account_exists: bool,
        api_permission_enabled: bool,
        certificate_valid: bool,
        market_data_fresh: bool,
        broker_healthy: bool,
        instrument_tradable: bool,
        buying_power: Decimal | None,
        estimated_notional: Decimal | None,
        duplicate_order_exists: bool,
        kill_switch_enabled: bool,
        live_trading_enabled: bool,
        pretrade: BrokerPreTradeRiskContext | None = None,
        evaluated_at: datetime | None = None,
    ) -> BrokerRiskDecision:
        checked_at = _utc(evaluated_at or datetime.now(timezone.utc))
        checks = {
            "account_exists": account_exists,
            "api_permission_enabled": api_permission_enabled,
            "certificate_valid": certificate_valid,
            "market_data_fresh": market_data_fresh,
            "broker_healthy": broker_healthy,
            "instrument_tradable": instrument_tradable,
            "buying_power_known": buying_power is not None,
            "estimated_notional_known": estimated_notional is not None,
            "buying_power_sufficient": (
                buying_power is not None
                and estimated_notional is not None
                and buying_power >= estimated_notional
            ),
            "no_duplicate_order": not duplicate_order_exists,
            "user_approved": intent.user_approved,
            "kill_switch_clear": not kill_switch_enabled,
            "environment_allowed": (
                intent.environment == "sandbox"
                or (intent.environment == "live" and live_trading_enabled)
            ),
        }
        observed: dict[str, str] | None = None
        if pretrade is not None:
            pretrade_checks, observed = self._pretrade_checks(
                intent,
                pretrade=pretrade,
                evaluated_at=checked_at,
            )
            checks.update(pretrade_checks)
        elif intent.environment == "live":
            # Live is fail-closed: the historical boolean gate must never be
            # mistaken for a bounded pre-trade approval.
            checks["pretrade_context_present"] = False
        reasons = [name for name, passed in checks.items() if not passed]
        receipt = (
            BrokerPreTradeRiskReceipt.issue(
                intent=intent,
                pretrade_context=pretrade,
                checked_at=checked_at,
                checks=checks,
                reasons=reasons,
                observed=observed or {},
            )
            if intent.environment == "live" and pretrade is not None
            else None
        )
        return BrokerRiskDecision(
            risk_approval_id=intent.risk_approval_id,
            approved=not reasons,
            checked_at=checked_at,
            checks=checks,
            reasons=reasons,
            pretrade_receipt=receipt,
        )

    @staticmethod
    def _pretrade_checks(
        intent: BrokerOrderIntent,
        *,
        pretrade: BrokerPreTradeRiskContext,
        evaluated_at: datetime,
    ) -> tuple[dict[str, bool], dict[str, str]]:
        quote_observed_at = pretrade.quote_observed_at
        if quote_observed_at.tzinfo is None:
            quote_observed_at = quote_observed_at.replace(tzinfo=timezone.utc)
        quote_age_seconds = (evaluated_at - quote_observed_at).total_seconds()
        expected_price = intent.limit_price or pretrade.reference_price
        estimated_notional = intent.quantity * expected_price
        deviation_bps = (
            abs(expected_price - pretrade.reference_price)
            / pretrade.reference_price
            * Decimal("10000")
        )
        participation_pct = intent.quantity / pretrade.adv_quantity * Decimal("100")
        proposed_symbol_notional = (
            pretrade.current_symbol_notional + estimated_notional
            if intent.side == "buy"
            else max(Decimal("0"), pretrade.current_symbol_notional - estimated_notional)
        )
        proposed_account_notional = (
            pretrade.current_account_notional + estimated_notional
            if intent.side == "buy"
            else max(Decimal("0"), pretrade.current_account_notional - estimated_notional)
        )
        checks = {
            "pretrade_context_present": True,
            "quote_within_freshness_window": 0 <= quote_age_seconds <= pretrade.max_quote_age_seconds,
            "market_phase_allowed": pretrade.market_phase in pretrade.allowed_market_phases,
            "order_quantity_within_limit": intent.quantity <= pretrade.max_quantity,
            "order_notional_within_limit": estimated_notional <= pretrade.max_order_notional,
            "price_within_collar": deviation_bps <= pretrade.price_collar_bps,
            "adv_participation_within_limit": participation_pct <= pretrade.max_adv_participation_pct,
            "symbol_exposure_within_limit": proposed_symbol_notional <= pretrade.max_symbol_notional,
            "account_exposure_within_limit": proposed_account_notional <= pretrade.max_account_notional,
            "daily_loss_within_limit": pretrade.daily_loss_notional <= pretrade.max_daily_loss_notional,
            "strategy_execution_authorized": pretrade.strategy_execution_authorized,
            "model_execution_eligible": pretrade.model_execution_eligible,
        }
        if intent.side == "sell":
            checks["sellable_quantity_known"] = pretrade.sellable_quantity is not None
            checks["sellable_quantity_sufficient"] = (
                pretrade.sellable_quantity is not None
                and pretrade.sellable_quantity >= intent.quantity
            )
        observed = {
            "quote_age_seconds": _decimal_text(Decimal(str(quote_age_seconds))),
            "expected_price": _decimal_text(expected_price),
            "estimated_notional": _decimal_text(estimated_notional),
            "price_deviation_bps": _decimal_text(deviation_bps),
            "adv_participation_pct": _decimal_text(participation_pct),
            "proposed_symbol_notional": _decimal_text(proposed_symbol_notional),
            "proposed_account_notional": _decimal_text(proposed_account_notional),
        }
        return checks, observed


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decision_payload(
    *,
    risk_approval_id: str,
    approved: bool,
    checked_at: datetime,
    checks: dict[str, bool],
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "risk_approval_id": risk_approval_id,
        "approved": approved,
        "checked_at": _utc(checked_at).isoformat(),
        "checks": checks,
        "reasons": reasons,
    }


def _digest(payload: Any) -> str:
    rendered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return sha256(rendered.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
