from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId


class BrokerReconnectState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_reconnect_state.v1"] = (
        "stock_ai.broker_reconnect_state.v1"
    )
    broker_id: BrokerId
    channel: str
    state: Literal["connected", "disconnected", "backoff", "recovering", "failed"]
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime | None = None
    requires_snapshot_recovery: bool = False
    last_error: str | None = None
    observed_at: datetime


class BrokerReconnectPlanner:
    """Deterministic reconnect schedule; network code consumes rather than sleeps here."""

    def __init__(
        self,
        *,
        base_delay_seconds: float = 1.0,
        maximum_delay_seconds: float = 60.0,
        maximum_attempts: int = 8,
    ) -> None:
        self.base_delay_seconds = base_delay_seconds
        self.maximum_delay_seconds = maximum_delay_seconds
        self.maximum_attempts = maximum_attempts
        self._states: dict[tuple[str, str], BrokerReconnectState] = {}

    def disconnected(
        self,
        broker_id: BrokerId,
        channel: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> BrokerReconnectState:
        observed_at = now or datetime.now(timezone.utc)
        key = (broker_id, channel)
        previous = self._states.get(key)
        attempts = (previous.attempt_count if previous else 0) + 1
        if attempts > self.maximum_attempts:
            state = BrokerReconnectState(
                broker_id=broker_id,
                channel=channel,
                state="failed",
                attempt_count=attempts,
                requires_snapshot_recovery=True,
                last_error=error,
                observed_at=observed_at,
            )
        else:
            delay = min(
                self.maximum_delay_seconds,
                self.base_delay_seconds * (2 ** (attempts - 1)),
            )
            state = BrokerReconnectState(
                broker_id=broker_id,
                channel=channel,
                state="backoff",
                attempt_count=attempts,
                next_attempt_at=observed_at + timedelta(seconds=delay),
                requires_snapshot_recovery=True,
                last_error=error,
                observed_at=observed_at,
            )
        self._states[key] = state
        return state

    def transport_connected(
        self,
        broker_id: BrokerId,
        channel: str,
        *,
        now: datetime | None = None,
    ) -> BrokerReconnectState:
        observed_at = now or datetime.now(timezone.utc)
        key = (broker_id, channel)
        previous = self._states.get(key)
        state = BrokerReconnectState(
            broker_id=broker_id,
            channel=channel,
            state="recovering",
            attempt_count=previous.attempt_count if previous else 0,
            requires_snapshot_recovery=True,
            observed_at=observed_at,
        )
        self._states[key] = state
        return state

    def snapshot_recovered(
        self,
        broker_id: BrokerId,
        channel: str,
        *,
        now: datetime | None = None,
    ) -> BrokerReconnectState:
        observed_at = now or datetime.now(timezone.utc)
        key = (broker_id, channel)
        previous = self._states.get(key)
        if previous is None or previous.state != "recovering":
            raise ValueError("transport must reconnect before snapshot recovery")
        state = BrokerReconnectState(
            broker_id=broker_id,
            channel=channel,
            state="connected",
            attempt_count=0,
            requires_snapshot_recovery=False,
            observed_at=observed_at,
        )
        self._states[key] = state
        return state

    def state(
        self,
        broker_id: BrokerId,
        channel: str,
    ) -> BrokerReconnectState | None:
        return self._states.get((broker_id, channel))
