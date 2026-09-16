from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from stock_ai.brokers import BrokerSequenceTracker
from stock_ai.brokers.contracts import CanonicalMarketEvent
from stock_ai.brokers.feed_quality import BrokerSourceSwitchRecord
from stock_ai.brokers.integration_receipts import BrokerIntegrationReceipt, BrokerIntegrationReceiptStore
from stock_ai.brokers.market_feed_recovery_receipts import BrokerMarketFeedRecoveryReceipt, BrokerMarketFeedRecoveryReceiptStore


def _integration(store):
    return store.record(BrokerIntegrationReceipt.issue(
        receipt_id="BIR-feed-001", broker_id="fubon", account_alias_masked="paper-***-01",
        official_sdk_url="https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/", sdk_version="2.2.8",
        sdk_artifact_sha256="a" * 64, official_checksum_sha256="a" * 64,
        worker_release_sha256="b" * 64, readonly_probe_sha256="c" * 64,
        environment="sandbox", account_owner_confirmed=True,
    )).receipt


def _event(sequence: int) -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        broker_id="fubon", instrument_id="TWSE:2330", exchange="TWSE", market_session="regular_lot",
        channel="trade", exchange_timestamp=datetime(2026, 8, 13, tzinfo=timezone.utc),
        received_at=datetime(2026, 8, 13, tzinfo=timezone.utc), sequence=sequence,
        price="1000", size=1, trading_status="open", raw_payload_hash="d" * 64,
    )


def test_feed_gap_and_snapshot_recovery_receipts_are_durable_and_immutable(tmp_path):
    integrations = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    integration = _integration(integrations)
    tracker = BrokerSequenceTracker()
    store = BrokerMarketFeedRecoveryReceiptStore(tmp_path / "feed.sqlite", integration_store=integrations)
    initial = store.record(BrokerMarketFeedRecoveryReceipt.from_sequence_observation(
        receipt_id="BMF-001", observation=tracker.observe(_event(10)), integration_receipt_sha256=integration.receipt_sha256,
    ))
    gap = store.record(BrokerMarketFeedRecoveryReceipt.from_sequence_observation(
        receipt_id="BMF-002", observation=tracker.observe(_event(13)), integration_receipt_sha256=integration.receipt_sha256,
    ))
    recovery = store.record(BrokerMarketFeedRecoveryReceipt.snapshot_recovered(
        receipt_id="BMF-003", gap_receipt=gap, snapshot_sequence=13,
    ))
    failover = store.record(BrokerMarketFeedRecoveryReceipt.failover_selected(
        receipt_id="BMF-004",
        switch=BrokerSourceSwitchRecord(
            instrument_id="TWSE:2330", market_session="regular_lot", previous_broker_id="taishin",
            selected_broker_id="fubon", selected_event_id="BME-recovered", reason="feed_gap_recovery_failover",
            feed_scores={"fubon": 0.9, "taishin": 0.2}, conflict_present=False,
            trading_allowed=False, switched_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        ),
        integration_receipt_sha256=integration.receipt_sha256, channel="trade",
    ))

    assert initial.kind == "initial"
    assert (gap.kind, gap.missing_from, gap.missing_to) == ("gap", 11, 12)
    assert recovery.kind == "snapshot_recovered"
    assert failover.kind == "failover_selected"
    assert BrokerMarketFeedRecoveryReceiptStore(store.path, integration_store=integrations).receipts("fubon") == [initial, gap, recovery, failover]
    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("delete from broker_market_feed_recovery_receipts")


def test_feed_recovery_rejects_unlinked_or_duplicate_gap_recovery(tmp_path):
    integrations = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    integration = _integration(integrations)
    tracker = BrokerSequenceTracker()
    store = BrokerMarketFeedRecoveryReceiptStore(tmp_path / "feed.sqlite", integration_store=integrations)
    initial_observation = tracker.observe(_event(10))
    with pytest.raises(ValueError, match="not linked"):
        store.record(BrokerMarketFeedRecoveryReceipt.from_sequence_observation(
            receipt_id="BMF-unlinked", observation=initial_observation, integration_receipt_sha256="f" * 64,
        ))
    gap = BrokerMarketFeedRecoveryReceipt.from_sequence_observation(
        receipt_id="BMF-gap", observation=tracker.observe(_event(13)), integration_receipt_sha256=integration.receipt_sha256,
    )
    recovery = BrokerMarketFeedRecoveryReceipt.snapshot_recovered(receipt_id="BMF-recovery", gap_receipt=gap, snapshot_sequence=13)
    with pytest.raises(ValueError, match="durable gap"):
        store.record(recovery)
    store.record(gap)
    store.record(recovery)
    with pytest.raises(ValueError, match="already been recovered"):
        store.record(BrokerMarketFeedRecoveryReceipt.snapshot_recovered(receipt_id="BMF-repeat", gap_receipt=gap, snapshot_sequence=13))
