from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.governance.durable_store import SQLiteRetentionStore
from stock_ai.brokers import (
    AccountReconciliationSchedule,
    BrokerAccountReconciliationService,
    BrokerAccountSnapshot,
    BrokerFill,
    BrokerIntegrationReceipt,
    BrokerIntegrationReceiptStore,
    BrokerOpenOrder,
    BrokerPosition,
    BrokerSettlementAmount,
    HostAccountLedger,
    BrokerAccountReconciliationReceiptStore,
)


def _integration(tmp_path):
    return BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite").record(
        BrokerIntegrationReceipt.issue(
            receipt_id="BIR-periodic-001",
            broker_id="fubon",
            account_alias_masked="paper-***-01",
            official_sdk_url="https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/",
            sdk_version="2.2.8",
            sdk_artifact_sha256="a" * 64,
            official_checksum_sha256="a" * 64,
            worker_release_sha256="b" * 64,
            readonly_probe_sha256="c" * 64,
            environment="sandbox",
            account_owner_confirmed=True,
        )
    ).receipt


def _account_pair():
    observed = datetime(2026, 8, 26, tzinfo=timezone.utc)
    position = BrokerPosition(
        instrument_id="2330.TW",
        quantity="1000",
        sellable_quantity="1000",
        average_price="900",
        position_type="cash",
    )
    order = BrokerOpenOrder(
        broker_order_id_masked="ORD-***-01",
        instrument_id="2330.TW",
        side="buy",
        quantity="1000",
        remaining_quantity="1000",
        status="open",
        submitted_at=observed,
    )
    fill = BrokerFill(
        broker_fill_id_masked="FIL-***-01",
        broker_order_id_masked="ORD-***-01",
        instrument_id="2330.TW",
        side="buy",
        quantity="1000",
        price="900",
        fee="12",
        tax="0",
        filled_at=observed,
    )
    settlement = BrokerSettlementAmount(
        settlement_date="2026-08-28", currency="TWD", receivable="0", payable="900012"
    )
    broker = BrokerAccountSnapshot(
        broker_id="fubon",
        account_id_masked="paper-***-01",
        account_type="cash",
        currency="TWD",
        available_cash="1000000",
        settlement_due="900012",
        positions=[position],
        open_orders=[order],
        fills=[fill],
        settlements=[settlement],
        as_of=observed,
    )
    host = HostAccountLedger(
        broker_id="fubon",
        account_id_masked="paper-***-01",
        account_type="cash",
        currency="TWD",
        available_cash="1000000",
        settlement_due="900012",
        positions=[position],
        open_orders=[order],
        fills=[fill],
        settlements=[settlement],
        as_of=observed,
    )
    return broker, host


def test_periodic_account_service_is_durable_and_deduplicates_events(tmp_path):
    integration = _integration(tmp_path)
    broker, host = _account_pair()
    database = tmp_path / "reconciliation.sqlite"
    retention = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    store = BrokerAccountReconciliationReceiptStore(
        database,
        integration_store=BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite"),
        retention_ledger=retention,
        require_critical_retention=True,
    )
    loader = lambda broker_id, alias: (broker, host, integration.receipt_sha256)
    service = BrokerAccountReconciliationService(
        store,
        loader,
        schedule=AccountReconciliationSchedule(periodic_seconds=300),
        clock=lambda: datetime(2026, 8, 26, 1, tzinfo=timezone.utc),
    )

    startup = service.run_startup([("fubon", "paper-01")])
    periodic = service.run_periodic([("fubon", "paper-01")])
    event = service.handle_event(
        "broker.account.snapshot", "ACCOUNT-EVENT-1", [("fubon", "paper-01")]
    )
    duplicate = service.handle_event(
        "broker.account.snapshot", "ACCOUNT-EVENT-1", [("fubon", "paper-01")]
    )

    assert service.triggers()["periodic_seconds"] == 300
    assert [item.status for item in startup + periodic + event] == ["completed"] * 3
    assert duplicate == []
    assert len(store.receipts("fubon")) == 3
    assert len(store.execution_receipts("fubon")) == 3
    assert all(item.verify() is None for item in store.receipts("fubon"))
    retained = list(retention._records.values())
    assert len(retained) == 6
    assert {item["kind"] for item in retained} == {
        "execution_account_reconciliation",
        "execution_account_reconciliation_trigger",
    }
    assert all(item["critical"] is True for item in retained)

    restarted = BrokerAccountReconciliationService(store, loader)
    assert restarted.handle_event(
        "broker.account.snapshot", "ACCOUNT-EVENT-1", [("fubon", "paper-01")]
    ) == []
    assert len(BrokerAccountReconciliationReceiptStore(
        store.path,
        integration_store=BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite"),
    ).execution_receipts("fubon")) == 3


def test_account_reconciliation_store_requires_same_retention_authority(tmp_path):
    database = tmp_path / "reconciliation.sqlite"
    with pytest.raises(ValueError, match="critical retention ledger"):
        BrokerAccountReconciliationReceiptStore(
            database,
            require_critical_retention=True,
        )
    with pytest.raises(ValueError, match="share its durable database"):
        BrokerAccountReconciliationReceiptStore(
            database,
            retention_ledger=ContentAddressedRetentionLedger(
                store=SQLiteRetentionStore(tmp_path / "other.sqlite")
            ),
        )


def test_account_service_blocks_and_preserves_mismatch_evidence(tmp_path):
    integration = _integration(tmp_path)
    broker, host = _account_pair()
    host.available_cash = host.available_cash - 1
    store = BrokerAccountReconciliationReceiptStore(
        tmp_path / "reconciliation.sqlite", integration_store=BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    )
    service = BrokerAccountReconciliationService(
        store,
        lambda *_: (broker, host, integration.receipt_sha256),
    )

    receipt = service.run_periodic([("fubon", "paper-01")])[0]

    assert receipt.status == "blocked"
    assert len(store.receipts("fubon")) == 1
    assert store.receipts("fubon")[0].status == "mismatch"


def test_account_schedule_rejects_stale_interval():
    with pytest.raises(ValueError, match="at least 60"):
        AccountReconciliationSchedule(periodic_seconds=59)


def test_account_reconciliation_execution_evidence_is_immutable(tmp_path):
    integration = _integration(tmp_path)
    broker, host = _account_pair()
    store = BrokerAccountReconciliationReceiptStore(
        tmp_path / "reconciliation.sqlite", integration_store=BrokerIntegrationReceiptStore(tmp_path / "integration.sqlite")
    )
    receipt = BrokerAccountReconciliationService(
        store, lambda *_: (broker, host, integration.receipt_sha256)
    ).run_startup([("fubon", "paper-01")])[0]

    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "delete from broker_account_reconciliation_executions where receipt_id = ?",
            (receipt.receipt_id,),
        )
