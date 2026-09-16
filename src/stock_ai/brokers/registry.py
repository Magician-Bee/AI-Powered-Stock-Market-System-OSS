from __future__ import annotations

from collections.abc import Iterable

from .base import BrokerAdapter
from .contracts import BrokerId
from .fubon import build_adapter as build_fubon
from .masterlink import build_adapter as build_masterlink
from .sinopac import build_adapter as build_sinopac
from .taishin import build_adapter as build_taishin
from .yuanta import build_adapter as build_yuanta


FIXED_BROKER_IDS: tuple[BrokerId, ...] = (
    "taishin",
    "fubon",
    "sinopac",
    "yuanta",
    "masterlink",
)


class BrokerCapabilityRegistry:
    def __init__(self, adapters: Iterable[BrokerAdapter] | None = None) -> None:
        defaults = (
            build_taishin(),
            build_fubon(),
            build_sinopac(),
            build_yuanta(),
            build_masterlink(),
        )
        selected = tuple(adapters) if adapters is not None else defaults
        self._adapters = {adapter.broker_id: adapter for adapter in selected}

    def list_broker_ids(self) -> list[str]:
        return list(FIXED_BROKER_IDS)

    def get(self, broker_id: BrokerId) -> BrokerAdapter:
        try:
            return self._adapters[broker_id]
        except KeyError as exc:
            raise KeyError(f"broker adapter is not registered: {broker_id}") from exc

    async def probe_all(self) -> list[dict]:
        profiles = []
        for broker_id in FIXED_BROKER_IDS:
            profiles.append((await self.get(broker_id).probe_capabilities()).model_dump(mode="json"))
        return profiles

    async def authorization_matrix(self) -> list[dict]:
        return [
            (await self.get(broker_id).authorization_status()).model_dump(mode="json")
            for broker_id in FIXED_BROKER_IDS
        ]
