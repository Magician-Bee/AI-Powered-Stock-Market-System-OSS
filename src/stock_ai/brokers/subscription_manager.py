from __future__ import annotations

from dataclasses import dataclass

from .contracts import BrokerId


@dataclass(frozen=True)
class BrokerSubscription:
    broker_id: BrokerId
    instrument_id: str
    channel: str
    market_session: str


class BrokerSubscriptionManager:
    def __init__(self) -> None:
        self._active: set[BrokerSubscription] = set()

    def add(self, subscription: BrokerSubscription, *, maximum: int | None) -> None:
        if subscription in self._active:
            return
        broker_count = sum(
            item.broker_id == subscription.broker_id for item in self._active
        )
        if maximum is not None and broker_count >= maximum:
            raise RuntimeError("broker subscription limit reached")
        self._active.add(subscription)

    def remove(self, subscription: BrokerSubscription) -> None:
        self._active.discard(subscription)

    def list(self, broker_id: BrokerId | None = None) -> list[BrokerSubscription]:
        values = self._active
        if broker_id is not None:
            values = {item for item in values if item.broker_id == broker_id}
        return sorted(
            values,
            key=lambda item: (
                item.broker_id,
                item.instrument_id,
                item.market_session,
                item.channel,
            ),
        )
