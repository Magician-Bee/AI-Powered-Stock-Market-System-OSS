from __future__ import annotations

from collections.abc import Iterable

from .contracts import CanonicalMarketEvent, ReconciledMarketObservation


def _event_identity(event: CanonicalMarketEvent) -> tuple:
    return (
        event.instrument_id,
        event.exchange,
        event.market_session,
        event.exchange_timestamp,
        event.sequence,
        event.price,
        event.size,
        tuple((level.price, level.size) for level in event.bids),
        tuple((level.price, level.size) for level in event.asks),
        event.trading_status,
        event.channel,
    )


def _value_signature(event: CanonicalMarketEvent) -> tuple:
    return (
        event.price,
        event.size,
        tuple((level.price, level.size) for level in event.bids),
        tuple((level.price, level.size) for level in event.asks),
        event.trading_status,
    )


class MarketDataReconciliationEngine:
    def reconcile(
        self,
        events: Iterable[CanonicalMarketEvent],
        *,
        feed_health: dict[str, float] | None = None,
    ) -> ReconciledMarketObservation:
        health = feed_health or {}
        unique: dict[tuple, CanonicalMarketEvent] = {}
        for event in events:
            unique.setdefault(_event_identity(event), event)
        observations = list(unique.values())
        if not observations:
            raise ValueError("at least one broker market event is required")
        instruments = {item.instrument_id for item in observations}
        sessions = {(item.exchange, item.market_session) for item in observations}
        if len(instruments) != 1 or len(sessions) != 1:
            raise ValueError("events from different instruments or market sessions cannot be merged")

        ranked = sorted(
            observations,
            key=lambda item: (
                item.exchange_timestamp_verified,
                item.exchange_timestamp,
                item.sequence if item.sequence is not None else -1,
                health.get(item.broker_id, 0.0),
                item.received_at,
            ),
            reverse=True,
        )
        selected = ranked[0]
        leading_coordinate = (
            selected.exchange_timestamp,
            selected.sequence,
            selected.channel,
        )
        competing = [
            item
            for item in ranked
            if (item.exchange_timestamp, item.sequence, item.channel) == leading_coordinate
        ]
        values = {_value_signature(item) for item in competing}
        conflict = len(values) > 1
        if conflict:
            return ReconciledMarketObservation(
                instrument_id=selected.instrument_id,
                consensus_status="conflict",
                selected_source=selected.broker_id,
                selected_event_id=selected.event_id,
                observations=ranked,
                reason="same_exchange_coordinate_contains_conflicting_values",
                trading_allowed=False,
            )
        return ReconciledMarketObservation(
            instrument_id=selected.instrument_id,
            consensus_status="single_source" if len(ranked) == 1 else "consistent",
            selected_source=selected.broker_id,
            selected_event_id=selected.event_id,
            observations=ranked,
            reason="latest_exchange_coordinate_then_feed_health",
            trading_allowed=True,
        )
