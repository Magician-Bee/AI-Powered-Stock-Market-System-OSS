"""Runtime composition for broker-feed recovery and SLO evidence.

The recovery service deliberately remains independent of any HTTP or Agent
framework.  This module is the one production composition point: it binds the
service to the same durable Agent runtime database and delays importing the
runtime singleton until an actual verified market event is observed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from open_stock_ai.agent_runtime.runtime_paths import AgentRuntimePaths

from .gateway import UnifiedBrokerGateway
from .integration_receipts import BrokerIntegrationReceiptStore
from .market_feed_recovery_receipts import BrokerMarketFeedRecoveryReceiptStore
from .market_feed_recovery_service import BrokerMarketFeedRecoveryService
from .registry import BrokerCapabilityRegistry


def build_runtime_feed_recovery_service(
    *,
    database: str | Path | None = None,
    slo_recorder: Callable[..., dict[str, Any]] | None = None,
) -> BrokerMarketFeedRecoveryService:
    """Build the fail-closed feed recorder for the active application runtime.

    A subscription can only yield events when the selected broker already has
    durable, account-owner-confirmed official SDK probe evidence.  The SLO
    callback is lazy so tool registration never initializes the Agent runtime
    recursively during application startup.
    """

    database_path = (
        Path(database).expanduser().resolve()
        if database is not None
        else AgentRuntimePaths.discover().database
    )
    integration_store = BrokerIntegrationReceiptStore(database_path)
    recovery_store = BrokerMarketFeedRecoveryReceiptStore(
        database_path,
        integration_store=integration_store,
    )

    def latest_integration_receipt(broker_id: str) -> str:
        receipts = integration_store.receipts(broker_id)
        if not receipts:
            raise RuntimeError(
                "broker market feed is unavailable until durable official SDK "
                f"integration evidence exists for {broker_id}"
            )
        return receipts[-1].receipt.receipt_sha256

    def record_slo(service: str, **payload: Any) -> dict[str, Any]:
        if slo_recorder is not None:
            return slo_recorder(service, **payload)
        # Import only after a verified event has been durably recorded.  Doing
        # this at module import time would recurse through Agent tool setup.
        from stock_ai.agent_service import get_agent_run_runtime

        return get_agent_run_runtime().record_slo_observation(service, **payload)

    return BrokerMarketFeedRecoveryService(
        recovery_store,
        latest_integration_receipt,
        slo_recorder=record_slo,
    )


def build_runtime_broker_gateway(
    registry: BrokerCapabilityRegistry | None = None,
    *,
    database: str | Path | None = None,
    slo_recorder: Callable[..., dict[str, Any]] | None = None,
) -> UnifiedBrokerGateway:
    """Return the runtime gateway with mandatory broker-feed observability."""

    return UnifiedBrokerGateway(
        registry,
        feed_recovery_service=build_runtime_feed_recovery_service(
            database=database,
            slo_recorder=slo_recorder,
        ),
    )
