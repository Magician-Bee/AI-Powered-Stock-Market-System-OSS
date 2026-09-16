from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId, CanonicalMarketEvent


class BrokerSequenceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_sequence_observation.v1"] = (
        "stock_ai.broker_sequence_observation.v1"
    )
    broker_id: BrokerId
    instrument_id: str
    market_session: str
    channel: str
    classification: Literal[
        "initial",
        "contiguous",
        "gap",
        "duplicate",
        "out_of_order",
        "unsequenced",
    ]
    previous_sequence: int | None = Field(default=None, ge=0)
    observed_sequence: int | None = Field(default=None, ge=0)
    missing_from: int | None = Field(default=None, ge=0)
    missing_to: int | None = Field(default=None, ge=0)
    requires_snapshot_recovery: bool = False
    trading_allowed: bool = False
    observed_at: datetime


class BrokerSequenceTracker:
    """Track feed ordering independently per broker, instrument, session and channel."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str, str, str], int] = {}
        self._unresolved: set[tuple[str, str, str, str]] = set()
        self._observations: list[BrokerSequenceObservation] = []

    def observe(self, event: CanonicalMarketEvent) -> BrokerSequenceObservation:
        key = (
            event.broker_id,
            event.instrument_id,
            event.market_session,
            event.channel,
        )
        previous = self._last.get(key)
        sequence = event.sequence
        if sequence is None:
            classification = "unsequenced"
            requires_recovery = False
            trading_allowed = False
            missing_from = None
            missing_to = None
        elif previous is None:
            classification = "initial"
            requires_recovery = False
            trading_allowed = True
            missing_from = None
            missing_to = None
            self._last[key] = sequence
        elif sequence == previous:
            classification = "duplicate"
            requires_recovery = False
            trading_allowed = key not in self._unresolved
            missing_from = None
            missing_to = None
        elif sequence < previous:
            classification = "out_of_order"
            requires_recovery = False
            trading_allowed = False
            missing_from = None
            missing_to = None
        elif sequence == previous + 1:
            classification = "contiguous"
            requires_recovery = False
            trading_allowed = key not in self._unresolved
            missing_from = None
            missing_to = None
            self._last[key] = sequence
        else:
            classification = "gap"
            requires_recovery = True
            trading_allowed = False
            missing_from = previous + 1
            missing_to = sequence - 1
            self._last[key] = sequence
            self._unresolved.add(key)

        observation = BrokerSequenceObservation(
            broker_id=event.broker_id,
            instrument_id=event.instrument_id,
            market_session=event.market_session,
            channel=event.channel,
            classification=classification,
            previous_sequence=previous,
            observed_sequence=sequence,
            missing_from=missing_from,
            missing_to=missing_to,
            requires_snapshot_recovery=requires_recovery,
            trading_allowed=trading_allowed,
            observed_at=datetime.now(timezone.utc),
        )
        self._observations.append(observation)
        return observation

    def mark_snapshot_recovered(
        self,
        *,
        broker_id: BrokerId,
        instrument_id: str,
        market_session: str,
        channel: str,
        snapshot_sequence: int,
    ) -> None:
        key = (broker_id, instrument_id, market_session, channel)
        previous = self._last.get(key)
        if previous is not None and snapshot_sequence < previous:
            raise ValueError("recovery snapshot cannot move sequence state backwards")
        self._last[key] = snapshot_sequence
        self._unresolved.discard(key)

    def has_unresolved_gap(
        self,
        *,
        broker_id: BrokerId,
        instrument_id: str,
        market_session: str,
        channel: str,
    ) -> bool:
        return (broker_id, instrument_id, market_session, channel) in self._unresolved

    def observations(
        self,
        *,
        broker_id: BrokerId | None = None,
    ) -> list[BrokerSequenceObservation]:
        if broker_id is None:
            return list(self._observations)
        return [item for item in self._observations if item.broker_id == broker_id]
