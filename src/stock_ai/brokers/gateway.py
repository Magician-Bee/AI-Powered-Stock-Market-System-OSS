from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
import inspect
from typing import Any

from .contracts import (
    BrokerId,
    BrokerLiveTradingDisabled,
    BrokerOrderIntent,
    CanonicalMarketEvent,
)
from .registry import BrokerCapabilityRegistry
from .supervisor import BrokerConnectionSupervisor
from .transport_guard import BrokerTransportGuard, default_broker_transport_guard
from .market_feed_recovery_service import BrokerMarketFeedRecoveryService


class UnifiedBrokerGateway:
    """Only host surface that Agent tools may call."""

    def __init__(
        self,
        registry: BrokerCapabilityRegistry | None = None,
        *,
        live_trading_enabled: bool = False,
        transport_guard: BrokerTransportGuard | None = None,
        feed_recovery_service: BrokerMarketFeedRecoveryService | None = None,
    ) -> None:
        self.registry = registry or BrokerCapabilityRegistry()
        self.supervisor = BrokerConnectionSupervisor(self.registry)
        self.live_trading_enabled = live_trading_enabled
        self.transport_guard = transport_guard or default_broker_transport_guard()
        self.feed_recovery_service = feed_recovery_service

    async def list_connections(self) -> dict:
        # These are local control-plane projections, not broker transports.
        # Guarding them with the shared network limiter made concurrent UI
        # refreshes turn a harmless status read into a 500 response.
        matrix = await self.registry.authorization_matrix()
        return {
            "schema_version": "stock_ai.broker_connections.v1",
            "brokers": matrix,
            "live_trading_enabled": False,
        }

    async def health(self) -> dict:
        workers = await self.supervisor.health()
        return {
            "schema_version": "stock_ai.broker_health.v1",
            "workers": workers,
        }

    async def capability_profiles(self) -> dict:
        profiles = await self.registry.probe_all()
        return {
            "schema_version": "stock_ai.broker_capabilities.v1",
            "profiles": profiles,
        }

    async def login_readonly(
        self, broker_id: BrokerId, *, secret_reference: str | None = None
    ):
        adapter = self.registry.get(broker_id)
        if secret_reference is None:
            callback = adapter.login_readonly
        else:
            callback = lambda: adapter.login_readonly(secret_reference=secret_reference)
        return await self._adapter_call(broker_id, "login_readonly", callback)

    async def probe_readonly_quote(
        self,
        broker_id: BrokerId,
        *,
        secret_reference: str | None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Run an owner-authorised, bounded SDK quote probe inside its Worker.

        This is deliberately separate from the generic market-snapshot path:
        a broker SDK login requires a Host-held secure reference and cannot be
        initiated by Agent-provided market arguments alone.
        """

        return await self._adapter_call(
            broker_id,
            "probe_readonly_quote",
            lambda: self.registry.get(broker_id).probe_readonly_quote(
                secret_reference=secret_reference,
                request=request,
            ),
        )

    async def subscribe_market_data(self, broker_id: BrokerId, request: dict):
        async def open_stream():
            stream = self.registry.get(broker_id).subscribe_market_data(request)
            if inspect.isawaitable(stream):
                stream = await stream
            return stream

        stream = await self._adapter_call(broker_id, "subscribe_market_data", open_stream)
        if self.feed_recovery_service is None:
            return stream
        return self._observe_feed_stream(stream)

    async def _observe_feed_stream(
        self, stream: AsyncIterator[CanonicalMarketEvent]
    ) -> AsyncIterator[CanonicalMarketEvent]:
        """Record each actual adapter event before making it consumable.

        The recovery service writes a receipt and broker-feed SLO sample before
        downstream consumers receive the event.  If receipt persistence or
        sequence validation fails, the stream fails closed instead of exposing
        an unverifiable quote to a caller.
        """

        assert self.feed_recovery_service is not None
        async for event in stream:
            self.feed_recovery_service.observe(event)
            yield event

    async def unsubscribe_market_data(self, broker_id: BrokerId, request: dict):
        return await self._adapter_call(
            broker_id,
            "unsubscribe_market_data",
            lambda: self.registry.get(broker_id).unsubscribe_market_data(request),
        )

    async def market_snapshot(self, broker_id: BrokerId, request: dict):
        return await self._adapter_call(
            broker_id,
            "market_snapshot",
            lambda: self.registry.get(broker_id).get_market_snapshot(request),
        )

    async def account_snapshot(self, broker_id: BrokerId, account_alias: str):
        return await self._adapter_call(
            broker_id,
            "account_snapshot",
            lambda: self.registry.get(broker_id).get_account_snapshot(account_alias),
        )

    async def preview_order(self, intent: BrokerOrderIntent):
        return await self._adapter_call(
            intent.broker_id,
            "preview_order",
            lambda: self.registry.get(intent.broker_id).preview_order(intent),
        )

    async def place_order(self, intent: BrokerOrderIntent):
        if intent.environment == "live" and not self.live_trading_enabled:
            raise BrokerLiveTradingDisabled(
                "live broker submission is disabled; models may only produce proposals"
            )
        return await self._adapter_call(
            intent.broker_id,
            "place_order",
            lambda: self.registry.get(intent.broker_id).place_order(intent),
        )

    async def modify_order(self, broker_id: BrokerId, request: dict):
        return await self._adapter_call(
            broker_id,
            "modify_order",
            lambda: self.registry.get(broker_id).modify_order(request),
        )

    async def cancel_order(self, broker_id: BrokerId, request: dict):
        return await self._adapter_call(
            broker_id,
            "cancel_order",
            lambda: self.registry.get(broker_id).cancel_order(request),
        )

    async def reconcile_orders(self, broker_id: BrokerId):
        return await self._adapter_call(
            broker_id,
            "reconcile_orders",
            self.registry.get(broker_id).reconcile_orders,
        )

    async def logout(self, broker_id: BrokerId) -> None:
        await self._adapter_call(broker_id, "logout", self.registry.get(broker_id).logout)

    async def _adapter_call(
        self,
        broker_id: BrokerId,
        operation: str,
        callback: Callable[[], Awaitable[Any]],
    ) -> Any:
        return await self._guarded(f"broker:{broker_id}", operation, callback)

    async def _guarded(
        self,
        scope: str,
        operation: str,
        callback: Callable[[], Awaitable[Any]],
    ) -> Any:
        return await self.transport_guard.call(f"{scope}:{operation}", callback)
