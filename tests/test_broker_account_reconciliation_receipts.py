from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from stock_ai.brokers import BrokerAccountReconciliationEngine, HostAccountLedger
from stock_ai.brokers.account_reconciliation_receipts import BrokerAccountReconciliationReceipt, BrokerAccountReconciliationReceiptStore
from stock_ai.brokers.integration_receipts import BrokerIntegrationReceipt, BrokerIntegrationReceiptStore
from stock_ai.brokers.contracts import BrokerAccountSnapshot


def test_account_reconciliation_receipt_is_linked_durable_and_immutable(tmp_path):
    integration_store = BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    integration = integration_store.record(BrokerIntegrationReceipt.issue(
        receipt_id="BIR-reconcile-001", broker_id="fubon", account_alias_masked="paper-***-01",
        official_sdk_url="https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/", sdk_version="2.2.8",
        sdk_artifact_sha256="a" * 64, official_checksum_sha256="a" * 64,
        worker_release_sha256="b" * 64, readonly_probe_sha256="c" * 64,
        environment="sandbox", account_owner_confirmed=True,
    )).receipt
    broker = BrokerAccountSnapshot(broker_id="fubon", account_id_masked="paper-***-01", account_type="cash", currency="TWD", available_cash="100", settlement_due="0", as_of=datetime(2026, 8, 13, tzinfo=timezone.utc))
    host = HostAccountLedger(broker_id="fubon", account_id_masked="paper-***-01", account_type="cash", currency="TWD", available_cash="100", settlement_due="0", as_of=datetime(2026, 8, 13, tzinfo=timezone.utc))
    result = BrokerAccountReconciliationEngine().reconcile(broker, host)
    store = BrokerAccountReconciliationReceiptStore(tmp_path / "reconciliation.sqlite", integration_store=integration_store)
    receipt = store.record(BrokerAccountReconciliationReceipt.issue(receipt_id="BAR-001", broker=broker, host=host, result=result, integration_receipt_sha256=integration.receipt_sha256))

    assert receipt.status == "matched"
    assert BrokerAccountReconciliationReceiptStore(store.path, integration_store=integration_store).receipts("fubon") == [receipt]
    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("delete from broker_account_reconciliation_receipts")


def test_account_reconciliation_receipt_rejects_unlinked_integration_evidence(tmp_path):
    broker = BrokerAccountSnapshot(broker_id="fubon", account_id_masked="paper-***-01", account_type="cash", currency="TWD", as_of=datetime.now(timezone.utc))
    host = HostAccountLedger(broker_id="fubon", account_id_masked="paper-***-01", account_type="cash", currency="TWD", as_of=datetime.now(timezone.utc))
    result = BrokerAccountReconciliationEngine().reconcile(broker, host)
    receipt = BrokerAccountReconciliationReceipt.issue(receipt_id="BAR-no-link", broker=broker, host=host, result=result, integration_receipt_sha256="f" * 64)
    with pytest.raises(ValueError, match="not linked"):
        BrokerAccountReconciliationReceiptStore(tmp_path / "reconciliation.sqlite", integration_store=BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")).record(receipt)
