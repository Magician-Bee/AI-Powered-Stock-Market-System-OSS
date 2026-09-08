from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BrokerId = Literal["taishin", "fubon", "sinopac", "yuanta", "masterlink"]


class CapabilityVerificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1)
    probe: str = Field(min_length=1)
    verified_at: datetime
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: Literal["documentation", "sandbox", "production_readonly", "production_live"]


class BrokerCapabilities(BaseModel):
    """True=supported by a real probe, False=unsupported, None=not verified."""

    model_config = ConfigDict(extra="forbid")

    instrument_master: bool | None = None
    stock_quotes: bool | None = None
    market_snapshot: bool | None = None
    stock_orderbook: bool | None = None
    stock_trades: bool | None = None
    tick_history: bool | None = None
    intraday_candles: bool | None = None
    historical_candles: bool | None = None
    odd_lot_quotes: bool | None = None
    index_quotes: bool | None = None
    futures_quotes: bool | None = None
    options_quotes: bool | None = None
    night_session_quotes: bool | None = None
    trading_status: bool | None = None
    warning_flags: bool | None = None
    stock_orders: bool | None = None
    futures_orders: bool | None = None
    modify_order: bool | None = None
    cancel_order: bool | None = None
    positions: bool | None = None
    open_orders: bool | None = None
    fills: bool | None = None
    available_cash: bool | None = None
    buying_power: bool | None = None
    maintenance_ratio: bool | None = None
    realized_pnl: bool | None = None
    unrealized_pnl: bool | None = None
    settlement_balance: bool | None = None
    bank_balance: bool | None = None
    sandbox: bool | None = None
    websocket: bool | None = None


class BrokerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_subscriptions: int | None = Field(default=None, ge=1)
    rest_requests_per_minute: int | None = Field(default=None, ge=1)


class BrokerCapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_capability_profile.v1"] = (
        "stock_ai.broker_capability_profile.v1"
    )
    broker_id: BrokerId
    adapter_version: str
    sdk_version: str
    platform: str
    capabilities: BrokerCapabilities = Field(default_factory=BrokerCapabilities)
    limits: BrokerLimits = Field(default_factory=BrokerLimits)
    verified_at: datetime | None = None
    verification_receipts: list[CapabilityVerificationReceipt] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_receipts_for_resolved_capabilities(self) -> "BrokerCapabilityProfile":
        resolved = {
            name
            for name, value in self.capabilities.model_dump().items()
            if value is not None
        }
        receipted = {item.capability for item in self.verification_receipts}
        missing = resolved - receipted
        if missing:
            raise ValueError(
                "resolved capabilities require probe or official-document receipts: "
                + ", ".join(sorted(missing))
            )
        if resolved and self.verified_at is None:
            raise ValueError("verified_at is required when capabilities are resolved")
        return self
