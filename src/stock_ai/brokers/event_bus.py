from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from .contracts import BrokerOrderReport, BrokerRawEvent, CanonicalMarketEvent


class CanonicalFinancialEventBus:
    """Host-side event bus retaining raw and canonical broker evidence."""

    def __init__(self) -> None:
        self._raw_by_hash: dict[str, BrokerRawEvent] = {}
        self._canonical: list[CanonicalMarketEvent] = []
        self._order_reports: list[BrokerOrderReport] = []
        self._subscribers: dict[str, list[Callable[[CanonicalMarketEvent], None]]] = defaultdict(list)
        self._order_subscribers: list[Callable[[BrokerOrderReport], None]] = []

    def publish(
        self,
        *,
        raw_event: BrokerRawEvent,
        canonical_event: CanonicalMarketEvent,
    ) -> None:
        if canonical_event.raw_payload_hash != raw_event.payload_hash:
            raise ValueError("canonical event must reference the retained raw payload hash")
        self._raw_by_hash[raw_event.payload_hash] = raw_event
        self._canonical.append(canonical_event)
        for subscriber in self._subscribers.get(canonical_event.channel, []):
            subscriber(canonical_event)

    def subscribe(
        self,
        channel: str,
        callback: Callable[[CanonicalMarketEvent], None],
    ) -> None:
        self._subscribers[channel].append(callback)

    def events(self, *, instrument_id: str | None = None) -> list[CanonicalMarketEvent]:
        if instrument_id is None:
            return list(self._canonical)
        return [item for item in self._canonical if item.instrument_id == instrument_id]

    def raw_event(self, payload_hash: str) -> BrokerRawEvent | None:
        return self._raw_by_hash.get(payload_hash)

    def publish_order_report(
        self,
        *,
        raw_event: BrokerRawEvent,
        order_report: BrokerOrderReport,
    ) -> None:
        if order_report.raw_payload_hash != raw_event.payload_hash:
            raise ValueError(
                "canonical order report must reference the retained raw payload hash"
            )
        if order_report.broker_id != raw_event.broker_id:
            raise ValueError("raw and canonical order report broker IDs must match")
        self._raw_by_hash[raw_event.payload_hash] = raw_event
        self._order_reports.append(order_report)
        for subscriber in self._order_subscribers:
            subscriber(order_report)

    def subscribe_order_reports(
        self,
        callback: Callable[[BrokerOrderReport], None],
    ) -> None:
        self._order_subscribers.append(callback)

    def order_reports(
        self,
        *,
        broker_id: str | None = None,
        intent_id: str | None = None,
    ) -> list[BrokerOrderReport]:
        values = self._order_reports
        if broker_id is not None:
            values = [item for item in values if item.broker_id == broker_id]
        if intent_id is not None:
            values = [item for item in values if item.intent_id == intent_id]
        return list(values)
