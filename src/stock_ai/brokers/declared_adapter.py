from __future__ import annotations

from typing import Any, AsyncIterator

from .contracts import (
    BrokerAccountSnapshot,
    BrokerAuthorizationRequired,
    BrokerCapabilities,
    BrokerCapabilityProfile,
    BrokerId,
    BrokerOrderIntent,
    BrokerOrderReceipt,
    CanonicalMarketEvent,
    HumanAuthorizationStatus,
)


class DeclaredBrokerAdapter:
    """Safe placeholder until an official SDK produces real probe receipts.

    Registering an adapter name is not an integration claim. Every capability
    remains ``None`` and every data operation stops at an authorization
    checkpoint until a broker-specific worker is installed and verified.
    """

    adapter_version = "0.1.0"

    def __init__(
        self,
        *,
        broker_id: BrokerId,
        official_url: str,
        required_actions: list[str],
    ) -> None:
        self.broker_id = broker_id
        self.official_url = official_url
        self.required_actions = required_actions

    async def authorization_status(self) -> HumanAuthorizationStatus:
        return HumanAuthorizationStatus(
            broker_id=self.broker_id,
            state="requires_user_action",
            required_actions=self.required_actions,
            official_url=self.official_url,
            safe_to_continue_automatically=False,
        )

    async def probe_capabilities(self) -> BrokerCapabilityProfile:
        return BrokerCapabilityProfile(
            broker_id=self.broker_id,
            adapter_version=self.adapter_version,
            sdk_version="unverified",
            platform="unverified",
            capabilities=BrokerCapabilities(),
            verified_at=None,
            verification_receipts=[],
        )

    def _authorization_required(self) -> BrokerAuthorizationRequired:
        return BrokerAuthorizationRequired(
            f"{self.broker_id} requires account-owner authorization and an official SDK probe"
        )

    async def login_readonly(self, *, secret_reference: str | None = None) -> dict[str, Any]:
        del secret_reference
        raise self._authorization_required()

    async def probe_readonly_quote(
        self,
        *,
        secret_reference: str | None = None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        del secret_reference, request
        raise self._authorization_required()

    async def subscribe_market_data(
        self, request: dict[str, Any]
    ) -> AsyncIterator[CanonicalMarketEvent]:
        del request
        raise self._authorization_required()
        yield  # pragma: no cover

    async def unsubscribe_market_data(self, request: dict[str, Any]) -> dict[str, Any]:
        del request
        raise self._authorization_required()

    async def get_market_snapshot(self, request: dict[str, Any]) -> CanonicalMarketEvent:
        del request
        raise self._authorization_required()

    async def get_account_snapshot(self, account_alias: str) -> BrokerAccountSnapshot:
        del account_alias
        raise self._authorization_required()

    async def preview_order(self, intent: BrokerOrderIntent) -> dict[str, Any]:
        del intent
        raise self._authorization_required()

    async def place_order(self, intent: BrokerOrderIntent) -> BrokerOrderReceipt:
        del intent
        raise self._authorization_required()

    async def modify_order(self, request: dict[str, Any]) -> BrokerOrderReceipt:
        del request
        raise self._authorization_required()

    async def cancel_order(self, request: dict[str, Any]) -> BrokerOrderReceipt:
        del request
        raise self._authorization_required()

    async def reconcile_orders(self) -> dict[str, Any]:
        raise self._authorization_required()

    async def logout(self) -> None:
        return None
