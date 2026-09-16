"""Wire live feed ordering decisions to durable recovery evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import BrokerId, CanonicalMarketEvent
from .feed_quality import BrokerSourceSwitchRecord
from .market_feed_recovery_receipts import (
    BrokerMarketFeedRecoveryReceipt,
    BrokerMarketFeedRecoveryReceiptStore,
)
from .sequence_tracker import BrokerSequenceObservation, BrokerSequenceTracker


IntegrationReceiptResolver = Callable[[BrokerId], str]
FeedSLORecorder = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class FeedRecoveryStream:
    broker_id: BrokerId
    instrument_id: str
    market_session: str
    channel: str


class BrokerMarketFeedRecoveryService:
    """Make feed recovery explicit, durable and fail-closed.

    ``observe`` is the only entry point for live events. A gap is persisted
    before callers can request a snapshot recovery. Recovery updates the
    in-memory tracker only after its linked immutable receipt is recorded;
    failover is likewise recorded with the source-switch hash before being
    returned to the caller.
    """

    def __init__(
        self,
        store: BrokerMarketFeedRecoveryReceiptStore,
        integration_receipt_resolver: IntegrationReceiptResolver,
        *,
        tracker: BrokerSequenceTracker | None = None,
        id_factory: Callable[[], str] | None = None,
        slo_recorder: FeedSLORecorder | None = None,
    ) -> None:
        self.store = store
        self.integration_receipt_resolver = integration_receipt_resolver
        self.tracker = tracker or BrokerSequenceTracker()
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.slo_recorder = slo_recorder
        self._latest_gap_by_stream: dict[tuple[str, str, str, str], BrokerMarketFeedRecoveryReceipt] = {}

    def observe(self, event: CanonicalMarketEvent) -> tuple[BrokerSequenceObservation, BrokerMarketFeedRecoveryReceipt]:
        observation = self.tracker.observe(event)
        receipt = self.store.record(
            BrokerMarketFeedRecoveryReceipt.from_sequence_observation(
                receipt_id=f"BMF-{self.id_factory()}",
                observation=observation,
                integration_receipt_sha256=self.integration_receipt_resolver(event.broker_id),
            )
        )
        if receipt.kind == "gap":
            self._latest_gap_by_stream[self._stream_key(receipt)] = receipt
        self._record_feed_slo(event, observation, receipt)
        return observation, receipt

    def recover_snapshot(
        self,
        stream: FeedRecoveryStream,
        *,
        snapshot_sequence: int,
    ) -> BrokerMarketFeedRecoveryReceipt:
        key = (stream.broker_id, stream.instrument_id, stream.market_session, stream.channel)
        gap = self._latest_gap_by_stream.get(key)
        if gap is None:
            gap = self._latest_durable_gap(key)
        if gap is None:
            raise ValueError("snapshot recovery requires an unresolved durable gap")
        receipt = self.store.record(
            BrokerMarketFeedRecoveryReceipt.snapshot_recovered(
                receipt_id=f"BMF-{self.id_factory()}",
                gap_receipt=gap,
                snapshot_sequence=snapshot_sequence,
            )
        )
        self.tracker.mark_snapshot_recovered(
            broker_id=stream.broker_id,
            instrument_id=stream.instrument_id,
            market_session=stream.market_session,
            channel=stream.channel,
            snapshot_sequence=snapshot_sequence,
        )
        self._latest_gap_by_stream.pop(key, None)
        return receipt

    def record_failover(
        self,
        switch: BrokerSourceSwitchRecord,
        *,
        channel: str,
    ) -> BrokerMarketFeedRecoveryReceipt:
        if switch.selected_broker_id is None:
            raise ValueError("feed failover requires a selected broker")
        return self.store.record(
            BrokerMarketFeedRecoveryReceipt.failover_selected(
                receipt_id=f"BMF-{self.id_factory()}",
                switch=switch,
                integration_receipt_sha256=self.integration_receipt_resolver(
                    switch.selected_broker_id
                ),
                channel=channel,
            )
        )

    def unresolved_streams(self) -> tuple[FeedRecoveryStream, ...]:
        return tuple(
            FeedRecoveryStream(*key) for key in sorted(self._latest_gap_by_stream)
        )

    def _latest_durable_gap(
        self, key: tuple[str, str, str, str]
    ) -> BrokerMarketFeedRecoveryReceipt | None:
        gaps = [
            item
            for item in self.store.receipts(key[0])
            if item.kind == "gap" and self._stream_key(item) == key
        ]
        recovered = {
            item.related_receipt_sha256
            for item in self.store.receipts(key[0])
            if item.kind == "snapshot_recovered"
        }
        candidates = [item for item in gaps if item.receipt_sha256 not in recovered]
        return candidates[-1] if candidates else None

    def _record_feed_slo(
        self,
        event: CanonicalMarketEvent,
        observation: BrokerSequenceObservation,
        receipt: BrokerMarketFeedRecoveryReceipt,
    ) -> None:
        """Persist one real feed observation only after its recovery receipt.

        The data timestamp is the source's exchange timestamp, while the
        latency is the measured source-to-host delay.  A broken sequence is a
        failed feed observation even though its immutable gap receipt was
        successfully stored; that distinction keeps the SLO dashboard from
        turning a recovery record into a green market-data result.
        """

        if self.slo_recorder is None:
            return
        exchange_time = _utc(event.exchange_timestamp)
        received_time = _utc(event.received_at)
        latency_ms = max(0.0, (received_time - exchange_time).total_seconds() * 1000.0)
        self.slo_recorder(
            "broker.feed",
            latency_ms=latency_ms,
            success=observation.classification in {"initial", "contiguous"},
            data_at=exchange_time,
            observation_id=f"broker-feed:{receipt.receipt_sha256}",
        )

    @staticmethod
    def _stream_key(receipt: BrokerMarketFeedRecoveryReceipt) -> tuple[str, str, str, str]:
        return (
            receipt.broker_id,
            receipt.instrument_id,
            receipt.market_session,
            receipt.channel,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
