from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import BrokerId


MarketChannel = Literal[
    "trade",
    "quote",
    "book",
    "candle_1m",
    "candle_5m",
    "aggregate",
    "index",
    "auction",
    "odd_lot",
    "futures",
    "options",
    "order_report",
    "fill_report",
]


def raw_payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class PriceLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(gt=0)
    size: int = Field(ge=0)


class BrokerRawEvent(BaseModel):
    """Raw payload is retained host-side and never exposed to model context by default."""

    model_config = ConfigDict(extra="forbid")

    raw_event_id: str = Field(default_factory=lambda: f"BRE-{uuid4().hex}")
    broker_id: BrokerId
    received_at: datetime
    payload: dict[str, Any]
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("payload_hash", mode="before")
    @classmethod
    def accept_empty_hash(cls, value: Any) -> Any:
        return value or None

    def model_post_init(self, __context: Any) -> None:
        expected = raw_payload_hash(self.payload)
        if self.payload_hash is not None and self.payload_hash != expected:
            raise ValueError("raw payload hash does not match payload")
        self.payload_hash = expected


class CanonicalMarketEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.canonical_market_event.v1"] = (
        "stock_ai.canonical_market_event.v1"
    )
    event_id: str = Field(default_factory=lambda: f"BME-{uuid4().hex}")
    broker_id: BrokerId
    account_scope: str | None = None
    instrument_id: str
    exchange: str
    market_session: str
    channel: MarketChannel
    exchange_timestamp: datetime
    exchange_timestamp_verified: bool = False
    received_at: datetime
    sequence: int | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, gt=0)
    size: int | None = Field(default=None, ge=0)
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    trading_status: str
    raw_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconciledMarketObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.reconciled_broker_market.v1"] = (
        "stock_ai.reconciled_broker_market.v1"
    )
    instrument_id: str
    consensus_status: Literal["single_source", "consistent", "conflict", "stale"]
    selected_source: BrokerId | None
    selected_event_id: str | None
    observations: list[CanonicalMarketEvent]
    reason: str
    trading_allowed: bool = False
