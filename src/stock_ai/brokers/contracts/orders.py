from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import BrokerId


class BrokerOrderState(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    RISK_PENDING = "RISK_PENDING"
    USER_APPROVAL_PENDING = "USER_APPROVAL_PENDING"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN_RECONCILIATION_REQUIRED = "UNKNOWN_RECONCILIATION_REQUIRED"


class BrokerOrderIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(default_factory=lambda: f"BOI-{uuid4().hex}")
    broker_id: BrokerId
    account_alias: str
    instrument_id: str
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    price_type: Literal["market", "limit", "limit_up", "limit_down", "reference"]
    limit_price: Decimal | None = Field(default=None, gt=0)
    time_in_force: Literal["ROD", "IOC", "FOK"]
    session: str
    order_purpose: str
    environment: Literal["sandbox", "live"] = "sandbox"
    user_approved: bool = False
    risk_approval_id: str
    change_id: str | None = None
    idempotency_key: str = Field(min_length=16, max_length=200)


class BrokerOrderReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str
    broker_id: BrokerId
    broker_order_id: str
    submitted_at: datetime
    status: BrokerOrderState
    accepted_quantity: Decimal = Field(ge=0)
    rejected_reason: str | None = None
    raw_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_external_receipt_state(self) -> "BrokerOrderReceipt":
        forbidden = {
            BrokerOrderState.CREATED,
            BrokerOrderState.VALIDATING,
            BrokerOrderState.RISK_PENDING,
            BrokerOrderState.USER_APPROVAL_PENDING,
            BrokerOrderState.SUBMITTING,
            BrokerOrderState.CANCEL_PENDING,
        }
        if self.status in forbidden:
            raise ValueError("broker receipts cannot claim a Host-only order state")
        if self.status == BrokerOrderState.REJECTED and not self.rejected_reason:
            raise ValueError("rejected broker receipts require a reason")
        if (
            self.status
            in {
                BrokerOrderState.ACKNOWLEDGED,
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
            }
            and self.accepted_quantity <= 0
        ):
            raise ValueError("accepted broker receipts require a positive quantity")
        return self


class BrokerOrderReport(BaseModel):
    """Canonical broker callback/query result used to drive the Host OMS."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_order_report.v1"] = (
        "stock_ai.broker_order_report.v1"
    )
    report_id: str = Field(default_factory=lambda: f"BOR-{uuid4().hex}")
    intent_id: str
    broker_id: BrokerId
    account_alias: str
    broker_order_id: str
    event_at: datetime
    received_at: datetime
    status: BrokerOrderState
    filled_quantity: Decimal = Field(ge=0)
    remaining_quantity: Decimal = Field(ge=0)
    last_fill_price: Decimal | None = Field(default=None, gt=0)
    fee: Decimal | None = Field(default=None, ge=0)
    tax: Decimal | None = Field(default=None, ge=0)
    rejected_reason: str | None = None
    raw_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_external_order_state(self) -> "BrokerOrderReport":
        forbidden = {
            BrokerOrderState.CREATED,
            BrokerOrderState.VALIDATING,
            BrokerOrderState.RISK_PENDING,
            BrokerOrderState.USER_APPROVAL_PENDING,
            BrokerOrderState.SUBMITTING,
        }
        if self.status in forbidden:
            raise ValueError("broker reports cannot claim a Host pre-submission state")
        if self.status == BrokerOrderState.FILLED and self.remaining_quantity != 0:
            raise ValueError("filled broker reports must have zero remaining quantity")
        if self.status == BrokerOrderState.FILLED and self.filled_quantity <= 0:
            raise ValueError("filled broker reports require a positive filled quantity")
        if (
            self.status == BrokerOrderState.PARTIALLY_FILLED
            and (self.filled_quantity <= 0 or self.remaining_quantity <= 0)
        ):
            raise ValueError(
                "partial-fill reports require positive filled and remaining quantities"
            )
        if self.status == BrokerOrderState.REJECTED and not self.rejected_reason:
            raise ValueError("rejected broker reports require a reason")
        return self


class BrokerOrderReconciliationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_order_reconciliation_snapshot.v1"] = (
        "stock_ai.broker_order_reconciliation_snapshot.v1"
    )
    broker_id: BrokerId
    account_alias: str
    as_of: datetime
    reports: list[BrokerOrderReport] = Field(default_factory=list)
    raw_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
