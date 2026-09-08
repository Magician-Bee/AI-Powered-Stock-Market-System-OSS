from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from stock_ai.brokers.integration_receipts import BrokerIntegrationReceipt, BrokerIntegrationReceiptStore
from stock_ai.brokers.session_receipts import BrokerSessionLifecycleReceipt, BrokerSessionLifecycleStore


def _integration(store: BrokerIntegrationReceiptStore):
    return store.record(BrokerIntegrationReceipt.issue(
        receipt_id="BIR-session-fubon-001", broker_id="fubon", account_alias_masked="paper-***-01",
        official_sdk_url="https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/", sdk_version="2.2.8",
        sdk_artifact_sha256="a" * 64, official_checksum_sha256="a" * 64,
        worker_release_sha256="b" * 64, readonly_probe_sha256="c" * 64,
        environment="sandbox", account_owner_confirmed=True,
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )).receipt


def _event(integration, *, event: str, receipt_id: str):
    return BrokerSessionLifecycleReceipt.issue(
        receipt_id=receipt_id, broker_id="fubon", session_id="BSS-fubon-001", event=event,  # type: ignore[arg-type]
        integration_receipt_sha256=integration.receipt_sha256, session_reference_sha256="d" * 64,
        external_evidence_sha256="e" * 64, account_owner_confirmed=True,
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


def test_session_lifecycle_is_durable_ordered_and_requires_snapshot_recovery(tmp_path):
    integration_store = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    integration = _integration(integration_store)
    store = BrokerSessionLifecycleStore(tmp_path / "session.sqlite", integration_store=integration_store)
    for event, receipt_id in (
        ("certificate_verified", "BSR-1"), ("login_readonly", "BSR-2"),
        ("reconnect_transport", "BSR-3"), ("reconnect_snapshot_recovered", "BSR-4"),
        ("session_refreshed", "BSR-5"), ("logout", "BSR-6"),
    ):
        store.record(_event(integration, event=event, receipt_id=receipt_id))

    reopened = BrokerSessionLifecycleStore(store.path, integration_store=integration_store)
    status = reopened.status("fubon", "BSS-fubon-001")
    assert status["events"][-3:] == ["reconnect_snapshot_recovered", "session_refreshed", "logout"]
    assert status["state"] == "logged_out"
    with reopened._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("delete from broker_session_lifecycle_receipts")


def test_session_lifecycle_rejects_out_of_order_or_unlinked_evidence(tmp_path):
    integration_store = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    integration = _integration(integration_store)
    store = BrokerSessionLifecycleStore(tmp_path / "session.sqlite", integration_store=integration_store)
    with pytest.raises(ValueError, match="invalid broker session transition"):
        store.record(_event(integration, event="login_readonly", receipt_id="BSR-bad-order"))
    with pytest.raises(ValueError, match="not linked"):
        store.record(BrokerSessionLifecycleReceipt.issue(
            receipt_id="BSR-unlinked", broker_id="fubon", session_id="BSS-fubon-002", event="certificate_verified",
            integration_receipt_sha256="f" * 64, session_reference_sha256="d" * 64,
            external_evidence_sha256="e" * 64, account_owner_confirmed=True,
        ))
