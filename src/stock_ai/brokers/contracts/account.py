from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import BrokerId


class BrokerPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    quantity: Decimal
    sellable_quantity: Decimal | None = None
    average_price: Decimal | None = None
    market_value: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    position_type: Literal["cash", "margin", "short", "futures", "options"]
    financing_amount: Decimal | None = Field(default=None, ge=0)
    interest_rate: Decimal | None = Field(default=None, ge=0)
    collateral_maintenance_ratio: Decimal | None = Field(default=None, ge=0)
    short_source: str | None = None
    short_cost: Decimal | None = Field(default=None, ge=0)
    cover_deadline: datetime | None = None


class BrokerOpenOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_order_id_masked: str
    instrument_id: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    remaining_quantity: Decimal
    status: str
    submitted_at: datetime

    @field_validator("broker_order_id_masked")
    @classmethod
    def require_masked_broker_order_id(cls, value: str) -> str:
        if not re.search(r"[*•xX]", value):
            raise ValueError("broker order identifiers must be masked")
        return value


class BrokerFill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_fill_id_masked: str
    broker_order_id_masked: str
    instrument_id: str
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal | None = Field(default=None, ge=0)
    tax: Decimal | None = Field(default=None, ge=0)
    filled_at: datetime

    @field_validator("broker_fill_id_masked", "broker_order_id_masked")
    @classmethod
    def require_masked_broker_ids(cls, value: str) -> str:
        if not re.search(r"[*•xX]", value):
            raise ValueError("broker fill and order identifiers must be masked")
        return value


class BrokerSettlementAmount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settlement_date: str
    currency: str
    receivable: Decimal | None = Field(default=None, ge=0)
    payable: Decimal | None = Field(default=None, ge=0)


class BrokerAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_account_snapshot.v1"] = (
        "stock_ai.broker_account_snapshot.v1"
    )
    broker_id: BrokerId
    account_id_masked: str
    account_type: str
    currency: str
    available_cash: Decimal | None = None
    settlement_due: Decimal | None = None
    bank_balance: Decimal | None = None
    buying_power: Decimal | None = None
    margin_available: Decimal | None = None
    maintenance_ratio: Decimal | None = None
    positions: list[BrokerPosition] = Field(default_factory=list)
    open_orders: list[BrokerOpenOrder] = Field(default_factory=list)
    fills: list[BrokerFill] = Field(default_factory=list)
    settlements: list[BrokerSettlementAmount] = Field(default_factory=list)
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    as_of: datetime

    @field_validator("account_id_masked")
    @classmethod
    def require_masked_account_id(cls, value: str) -> str:
        if not re.search(r"[*•xX]", value):
            raise ValueError("broker account identifiers must be masked")
        return value
