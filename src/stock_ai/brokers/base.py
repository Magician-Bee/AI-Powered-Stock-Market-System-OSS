from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .contracts import (
    BrokerAccountSnapshot,
    BrokerCapabilityProfile,
    BrokerOrderIntent,
    BrokerOrderReceipt,
    CanonicalMarketEvent,
    HumanAuthorizationStatus,
)


@runtime_checkable
class BrokerAdapter(Protocol):
    broker_id: str

    async def authorization_status(self) -> HumanAuthorizationStatus: ...

    async def probe_capabilities(self) -> BrokerCapabilityProfile: ...

    async def login_readonly(
        self, *, secret_reference: str | None = None
    ) -> dict[str, Any]: ...

    async def probe_readonly_quote(
        self,
        *,
        secret_reference: str | None = None,
        request: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def subscribe_market_data(
        self, request: dict[str, Any]
    ) -> AsyncIterator[CanonicalMarketEvent]: ...

    async def unsubscribe_market_data(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def get_market_snapshot(self, request: dict[str, Any]) -> CanonicalMarketEvent: ...

    async def get_account_snapshot(self, account_alias: str) -> BrokerAccountSnapshot: ...

    async def preview_order(self, intent: BrokerOrderIntent) -> dict[str, Any]: ...

    async def place_order(self, intent: BrokerOrderIntent) -> BrokerOrderReceipt: ...

    async def modify_order(self, request: dict[str, Any]) -> BrokerOrderReceipt: ...

    async def cancel_order(self, request: dict[str, Any]) -> BrokerOrderReceipt: ...

    async def reconcile_orders(self) -> dict[str, Any]: ...

    async def logout(self) -> None: ...
