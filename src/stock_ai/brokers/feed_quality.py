from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId, ReconciledMarketObservation


class BrokerFeedMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: BrokerId
    connection_uptime_ratio: float | None = Field(default=None, ge=0, le=1)
    message_latency_ms: float | None = Field(default=None, ge=0)
    sequence_gap_rate: float | None = Field(default=None, ge=0, le=1)
    duplicate_rate: float | None = Field(default=None, ge=0, le=1)
    parse_error_rate: float | None = Field(default=None, ge=0, le=1)
    reconnection_count: int | None = Field(default=None, ge=0)
    quote_staleness_ms: float | None = Field(default=None, ge=0)
    cross_broker_agreement: float | None = Field(default=None, ge=0, le=1)
    rate_limit_remaining_ratio: float | None = Field(default=None, ge=0, le=1)
    observed_at: datetime


class BrokerFeedScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_feed_score.v1"] = (
        "stock_ai.broker_feed_score.v1"
    )
    broker_id: BrokerId
    score: float | None = Field(default=None, ge=0, le=1)
    eligible_for_selection: bool = False
    missing_metrics: list[str] = Field(default_factory=list)
    components: dict[str, float | None]
    observed_at: datetime


class BrokerSourceSwitchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_source_switch.v1"] = (
        "stock_ai.broker_source_switch.v1"
    )
    switch_id: str = Field(default_factory=lambda: f"BSW-{uuid4().hex}")
    instrument_id: str
    market_session: str
    previous_broker_id: BrokerId | None
    selected_broker_id: BrokerId | None
    selected_event_id: str | None
    reason: str
    feed_scores: dict[str, float | None]
    conflict_present: bool
    trading_allowed: bool
    switched_at: datetime


class BrokerFeedScoreEngine:
    """Calculate a comparable score only when essential metrics are observed."""

    _ESSENTIAL = {
        "connection_uptime_ratio",
        "message_latency_ms",
        "sequence_gap_rate",
        "quote_staleness_ms",
        "cross_broker_agreement",
    }

    def score(self, metrics: BrokerFeedMetrics) -> BrokerFeedScore:
        raw = metrics.model_dump()
        missing = sorted(
            field
            for field in self._ESSENTIAL
            if raw.get(field) is None
        )
        components: dict[str, float | None] = {
            "uptime": metrics.connection_uptime_ratio,
            "latency": self._inverse(metrics.message_latency_ms, 5000),
            "sequence": self._inverse_ratio(metrics.sequence_gap_rate),
            "duplicate": self._inverse_ratio(metrics.duplicate_rate),
            "parse": self._inverse_ratio(metrics.parse_error_rate),
            "reconnection": self._inverse_count(metrics.reconnection_count, 10),
            "staleness": self._inverse(metrics.quote_staleness_ms, 5000),
            "agreement": metrics.cross_broker_agreement,
            "rate_limit": metrics.rate_limit_remaining_ratio,
        }
        if missing:
            return BrokerFeedScore(
                broker_id=metrics.broker_id,
                score=None,
                eligible_for_selection=False,
                missing_metrics=missing,
                components=components,
                observed_at=metrics.observed_at,
            )
        weights = {
            "uptime": 0.20,
            "latency": 0.15,
            "sequence": 0.15,
            "duplicate": 0.10,
            "parse": 0.10,
            "reconnection": 0.05,
            "staleness": 0.10,
            "agreement": 0.10,
            "rate_limit": 0.05,
        }
        # Optional metrics contribute zero rather than an invented neutral value.
        value = sum((components[name] or 0.0) * weight for name, weight in weights.items())
        return BrokerFeedScore(
            broker_id=metrics.broker_id,
            score=round(value, 6),
            eligible_for_selection=True,
            missing_metrics=[],
            components=components,
            observed_at=metrics.observed_at,
        )

    @staticmethod
    def _inverse(value: float | None, ceiling: float) -> float | None:
        if value is None:
            return None
        return max(0.0, 1.0 - min(value, ceiling) / ceiling)

    @staticmethod
    def _inverse_ratio(value: float | None) -> float | None:
        if value is None:
            return None
        return 1.0 - value

    @staticmethod
    def _inverse_count(value: int | None, ceiling: int) -> float | None:
        if value is None:
            return None
        return max(0.0, 1.0 - min(value, ceiling) / ceiling)


class BrokerSourceSelectionLedger:
    """Persistable in-memory switch evidence; callers may store returned records."""

    def __init__(self) -> None:
        self._selected: dict[tuple[str, str], BrokerId | None] = {}
        self._records: list[BrokerSourceSwitchRecord] = []

    def observe(
        self,
        result: ReconciledMarketObservation,
        *,
        market_session: str,
        feed_scores: dict[str, float | None],
    ) -> BrokerSourceSwitchRecord | None:
        key = (result.instrument_id, market_session)
        previous = self._selected.get(key)
        current = result.selected_source
        if key in self._selected and previous == current:
            return None
        record = BrokerSourceSwitchRecord(
            instrument_id=result.instrument_id,
            market_session=market_session,
            previous_broker_id=previous,
            selected_broker_id=current,
            selected_event_id=result.selected_event_id,
            reason=(
                "initial_source_selection"
                if key not in self._selected
                else result.reason
            ),
            feed_scores=dict(feed_scores),
            conflict_present=result.consensus_status == "conflict",
            trading_allowed=result.trading_allowed,
            switched_at=datetime.now(timezone.utc),
        )
        self._selected[key] = current
        self._records.append(record)
        return record

    def records(
        self,
        *,
        instrument_id: str | None = None,
    ) -> list[BrokerSourceSwitchRecord]:
        if instrument_id is None:
            return list(self._records)
        return [
            item for item in self._records if item.instrument_id == instrument_id
        ]
