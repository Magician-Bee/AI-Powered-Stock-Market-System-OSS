from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerAccountSnapshot, ReconciledMarketObservation


class RealtimeBrokerMarketContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_quote: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = Field(default_factory=list)
    orderbook: dict[str, Any] | None = None
    intraday_candles: list[dict[str, Any]] = Field(default_factory=list)
    broker_sources: list[str] = Field(default_factory=list)
    feed_health: dict[str, float | None] = Field(default_factory=dict)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    trading_allowed: bool = False


class BrokerAccountContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    broker_id: str | None = None
    account_id_masked: str | None = None
    positions: list[dict[str, Any]] = Field(default_factory=list)
    available_cash: str | None = None
    settlement: str | None = None
    buying_power: str | None = None
    as_of: str | None = None


class StockBrokerEvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.stock_broker_evidence_pack.v1"] = (
        "stock_ai.stock_broker_evidence_pack.v1"
    )
    realtime_market: RealtimeBrokerMarketContext
    broker_account_context: BrokerAccountContext = Field(
        default_factory=BrokerAccountContext
    )


class BrokerEvidenceContextBuilder:
    """Build model-safe context from canonical host evidence.

    Raw broker payloads never enter this projection. Private account data is
    excluded unless the host issued an explicit approval for this run.
    """

    def build(
        self,
        market: ReconciledMarketObservation,
        *,
        feed_health: dict[str, float | None] | None = None,
        account: BrokerAccountSnapshot | None = None,
        account_access_approved: bool = False,
    ) -> StockBrokerEvidencePack:
        selected = next(
            (
                event
                for event in market.observations
                if event.event_id == market.selected_event_id
            ),
            None,
        )
        events = [
            event.model_dump(
                mode="json",
                exclude={"account_scope", "raw_payload_hash"},
            )
            for event in market.observations
        ]
        selected_payload = (
            selected.model_dump(
                mode="json",
                exclude={"account_scope", "raw_payload_hash"},
            )
            if selected is not None
            else None
        )
        conflict_items = []
        if market.consensus_status == "conflict":
            conflict_items = [
                {
                    "event_id": event["event_id"],
                    "broker_id": event["broker_id"],
                    "exchange_timestamp": event["exchange_timestamp"],
                    "sequence": event["sequence"],
                    "price": event["price"],
                    "size": event["size"],
                }
                for event in events
            ]
        realtime = RealtimeBrokerMarketContext(
            selected_quote=selected_payload,
            trades=[item for item in events if item["channel"] == "trade"],
            orderbook=next(
                (item for item in events if item["channel"] == "book"),
                None,
            ),
            intraday_candles=[
                item
                for item in events
                if item["channel"] in {"candle_1m", "candle_5m"}
            ],
            broker_sources=sorted({item["broker_id"] for item in events}),
            feed_health=dict(feed_health or {}),
            conflicts=conflict_items,
            trading_allowed=market.trading_allowed,
        )
        account_context = BrokerAccountContext()
        if account_access_approved and account is not None:
            account_context = BrokerAccountContext(
                enabled=True,
                broker_id=account.broker_id,
                account_id_masked=account.account_id_masked,
                positions=[
                    item.model_dump(mode="json")
                    for item in account.positions
                ],
                available_cash=(
                    str(account.available_cash)
                    if account.available_cash is not None
                    else None
                ),
                settlement=(
                    str(account.settlement_due)
                    if account.settlement_due is not None
                    else None
                ),
                buying_power=(
                    str(account.buying_power)
                    if account.buying_power is not None
                    else None
                ),
                as_of=account.as_of.isoformat(),
            )
        return StockBrokerEvidencePack(
            realtime_market=realtime,
            broker_account_context=account_context,
        )
