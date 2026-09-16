from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from stock_ai.brokers import (
    BrokerIntegrationReceipt,
    BrokerIntegrationReceiptStore,
    BrokerMarketFeedRecoveryReceiptStore,
    BrokerMarketFeedRecoveryService,
    BrokerSourceSwitchRecord,
    FeedRecoveryStream,
    UnifiedBrokerGateway,
    build_runtime_broker_gateway,
)
from stock_ai.broker_tools import BrokerAgentToolProvider
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from stock_ai.brokers.contracts import CanonicalMarketEvent


def _integration(store: BrokerIntegrationReceiptStore, broker_id: str, receipt_id: str):
    return store.record(
        BrokerIntegrationReceipt.issue(
            receipt_id=receipt_id,
            broker_id=broker_id,
            account_alias_masked=f"{broker_id}-***-01",
            official_sdk_url="https://example.invalid/official-sdk",
            sdk_version="1.0.0",
            sdk_artifact_sha256="a" * 64,
            official_checksum_sha256="a" * 64,
            worker_release_sha256="b" * 64,
            readonly_probe_sha256="c" * 64,
            environment="sandbox",
            account_owner_confirmed=True,
        )
    ).receipt


def _event(sequence: int, broker_id: str = "fubon") -> CanonicalMarketEvent:
    observed = datetime(2026, 8, 26, tzinfo=timezone.utc)
    return CanonicalMarketEvent(
        broker_id=broker_id,
        instrument_id="TWSE:2330",
        exchange="TWSE",
        market_session="regular_lot",
        channel="trade",
        exchange_timestamp=observed,
        received_at=observed,
        sequence=sequence,
        price="1000",
        size=1,
        trading_status="open",
        raw_payload_hash="d" * 64,
    )


def test_feed_service_wires_gap_recovery_failover_and_restart(tmp_path):
    integrations = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    fubon = _integration(integrations, "fubon", "BIR-service-fubon")
    taishin = _integration(integrations, "taishin", "BIR-service-taishin")
    store = BrokerMarketFeedRecoveryReceiptStore(
        tmp_path / "feed.sqlite", integration_store=integrations
    )
    ids = iter(("initial", "gap", "recovery", "failover"))
    service = BrokerMarketFeedRecoveryService(
        store,
        lambda broker_id: (
            fubon.receipt_sha256 if broker_id == "fubon" else taishin.receipt_sha256
        ),
        id_factory=lambda: next(ids),
    )

    initial_observation, initial = service.observe(_event(10))
    gap_observation, gap = service.observe(_event(13))

    assert initial_observation.classification == "initial"
    assert gap_observation.requires_snapshot_recovery is True
    assert gap.kind == "gap"
    assert gap.missing_from == 11 and gap.missing_to == 12
    assert service.unresolved_streams() == (
        FeedRecoveryStream("fubon", "TWSE:2330", "regular_lot", "trade"),
    )

    restarted = BrokerMarketFeedRecoveryService(
        store,
        lambda broker_id: (
            fubon.receipt_sha256 if broker_id == "fubon" else taishin.receipt_sha256
        ),
        id_factory=lambda: next(ids),
    )
    recovery = restarted.recover_snapshot(
        FeedRecoveryStream("fubon", "TWSE:2330", "regular_lot", "trade"),
        snapshot_sequence=13,
    )
    failover = restarted.record_failover(
        BrokerSourceSwitchRecord(
            instrument_id="TWSE:2330",
            market_session="regular_lot",
            previous_broker_id="fubon",
            selected_broker_id="taishin",
            selected_event_id="BME-service-recovered",
            reason="feed_gap_recovery_failover",
            feed_scores={"fubon": 0.2, "taishin": 0.9},
            conflict_present=False,
            trading_allowed=False,
            switched_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        ),
        channel="trade",
    )

    assert recovery.kind == "snapshot_recovered"
    assert failover.kind == "failover_selected"
    assert restarted.unresolved_streams() == ()
    assert [item.kind for item in store.receipts()] == [
        "initial",
        "gap",
        "snapshot_recovered",
        "failover_selected",
    ]


def test_feed_service_records_receipt_bound_slo_for_every_live_event(tmp_path):
    integrations = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    fubon = _integration(integrations, "fubon", "BIR-slo-fubon")
    store = BrokerMarketFeedRecoveryReceiptStore(
        tmp_path / "feed.sqlite", integration_store=integrations
    )
    observations: list[dict] = []
    service = BrokerMarketFeedRecoveryService(
        store,
        lambda _broker_id: fubon.receipt_sha256,
        id_factory=iter(("initial", "gap")).__next__,
        slo_recorder=lambda service_name, **payload: observations.append(
            {"service": service_name, **payload}
        )
        or {"status": "recorded"},
    )
    initial_event = _event(10)
    initial_event.received_at = initial_event.received_at.replace(microsecond=250000)
    _, initial_receipt = service.observe(initial_event)
    _, gap_receipt = service.observe(_event(13))

    assert observations == [
        {
            "service": "broker.feed",
            "latency_ms": 250.0,
            "success": True,
            "data_at": initial_event.exchange_timestamp,
            "observation_id": f"broker-feed:{initial_receipt.receipt_sha256}",
        },
        {
            "service": "broker.feed",
            "latency_ms": 0.0,
            "success": False,
            "data_at": initial_event.exchange_timestamp,
            "observation_id": f"broker-feed:{gap_receipt.receipt_sha256}",
        },
    ]


def test_gateway_records_feed_receipt_and_slo_before_yielding_event(tmp_path):
    integrations = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    fubon = _integration(integrations, "fubon", "BIR-gateway-fubon")
    store = BrokerMarketFeedRecoveryReceiptStore(
        tmp_path / "feed.sqlite", integration_store=integrations
    )
    slo_calls: list[dict] = []
    recovery = BrokerMarketFeedRecoveryService(
        store,
        lambda _broker_id: fubon.receipt_sha256,
        id_factory=lambda: "gateway-event",
        slo_recorder=lambda service_name, **payload: slo_calls.append(
            {"service": service_name, **payload}
        )
        or {"status": "recorded"},
    )

    class Adapter:
        broker_id = "fubon"

        async def subscribe_market_data(self, _request):
            async def stream():
                yield _event(10)

            return stream()

    class Registry:
        def get(self, broker_id):
            assert broker_id == "fubon"
            return Adapter()

    class Guard:
        async def call(self, _scope, callback):
            return await callback()

    gateway = UnifiedBrokerGateway(
        registry=Registry(),
        transport_guard=Guard(),
        feed_recovery_service=recovery,
    )

    async def consume():
        stream = await gateway.subscribe_market_data("fubon", {"channel": "trade"})
        return [event async for event in stream]

    received = asyncio.run(consume())

    assert [item.sequence for item in received] == [10]
    assert [item.kind for item in store.receipts()] == ["initial"]
    assert slo_calls[0]["service"] == "broker.feed"
    assert slo_calls[0]["success"] is True


def test_runtime_agent_subscription_records_durable_feed_slo_before_returning_event(tmp_path):
    """The Agent-facing route must not bypass the runtime recovery gateway."""

    database = tmp_path / "agent-runtime.db"
    integrations = BrokerIntegrationReceiptStore(database)
    fubon = _integration(integrations, "fubon", "BIR-runtime-fubon")
    slo_calls: list[dict] = []

    class Adapter:
        broker_id = "fubon"

        async def subscribe_market_data(self, _request):
            async def stream():
                yield _event(10)

            return stream()

    class Registry:
        def get(self, broker_id):
            assert broker_id == "fubon"
            return Adapter()

    class Guard:
        async def call(self, _scope, callback):
            return await callback()

    gateway = build_runtime_broker_gateway(
        Registry(),
        database=database,
        slo_recorder=lambda service_name, **payload: slo_calls.append(
            {"service": service_name, **payload}
        )
        or {"status": "recorded"},
    )
    gateway.transport_guard = Guard()
    provider = BrokerAgentToolProvider(registry=Registry(), gateway=gateway)
    context = AgentRunContext(
        run_id="AR-feed-runtime",
        session_id="AR-feed-runtime",
        autonomy="advisory",
        symbols=("2330.TW",),
        state={"task_kind": "market_information"},
    )

    result = asyncio.run(
        provider.execute(
            "broker.market.subscribe",
            {"broker_id": "fubon", "instrument_id": "TWSE:2330", "channel": "trade"},
            context,
        )
    )

    receipts = BrokerMarketFeedRecoveryReceiptStore(
        database,
        integration_store=integrations,
    ).receipts("fubon")
    assert result["first_event"]["sequence"] == 10
    assert [item.kind for item in receipts] == ["initial"]
    assert receipts[0].integration_receipt_sha256 == fubon.receipt_sha256
    assert slo_calls == [
        {
            "service": "broker.feed",
            "latency_ms": 0.0,
            "success": True,
            "data_at": _event(10).exchange_timestamp,
            "observation_id": f"broker-feed:{receipts[0].receipt_sha256}",
        }
    ]


def test_runtime_agent_subscription_fails_closed_without_official_integration_evidence(tmp_path):
    class Adapter:
        broker_id = "fubon"

        async def subscribe_market_data(self, _request):
            async def stream():
                yield _event(10)

            return stream()

    class Registry:
        def get(self, broker_id):
            assert broker_id == "fubon"
            return Adapter()

    class Guard:
        async def call(self, _scope, callback):
            return await callback()

    gateway = build_runtime_broker_gateway(Registry(), database=tmp_path / "runtime.db")
    gateway.transport_guard = Guard()
    provider = BrokerAgentToolProvider(registry=Registry(), gateway=gateway)
    context = AgentRunContext(
        run_id="AR-feed-unverified",
        session_id="AR-feed-unverified",
        autonomy="advisory",
        symbols=("2330.TW",),
        state={"task_kind": "market_information"},
    )

    with pytest.raises(RuntimeError, match="durable official SDK integration evidence"):
        asyncio.run(
            provider.execute(
                "broker.market.subscribe",
                {"broker_id": "fubon", "instrument_id": "TWSE:2330", "channel": "trade"},
                context,
            )
        )
