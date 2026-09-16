from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from stock_ai.brokers.integration_receipts import (
    BrokerIntegrationReceipt,
    BrokerIntegrationReceiptStore,
)
from stock_ai.brokers.provisioning import BrokerSdkArtifactReceipt


def _receipt(*, receipt_id: str = "BIR-fubon-readonly-001") -> BrokerIntegrationReceipt:
    return BrokerIntegrationReceipt.issue(
        receipt_id=receipt_id,
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
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


def test_broker_integration_receipt_is_durable_restart_safe_and_immutable(tmp_path):
    store = BrokerIntegrationReceiptStore(tmp_path / "broker-integration.sqlite")
    durable = store.record(_receipt())
    reopened = BrokerIntegrationReceiptStore(store.path)

    assert reopened.receipts("fubon") == [durable]
    readiness = reopened.readiness("fubon")
    assert readiness["official_sdk_verified"] is True
    assert readiness["readonly_probe_recorded"] is True
    assert readiness["adapter_claim"] == "evidence_recorded_not_live_session"
    with reopened._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("delete from broker_integration_receipts")


def test_broker_integration_receipt_rejects_unverified_sdk_or_unmasked_account():
    with pytest.raises(ValueError, match="checksum"):
        replace(_receipt(), sdk_artifact_sha256="d" * 64).verify()
    with pytest.raises(ValueError, match="masked"):
        replace(_receipt(), account_alias_masked="paper-account-01").verify()


def test_broker_integration_readiness_does_not_invent_evidence(tmp_path):
    store = BrokerIntegrationReceiptStore(tmp_path / "empty.sqlite")

    readiness = store.readiness("fubon")
    assert readiness["official_sdk_verified"] is False
    assert readiness["readonly_probe_recorded"] is False
    assert readiness["adapter_claim"] == "not_verified"


def test_broker_integration_receipt_binds_to_verified_official_sdk_artifact():
    artifact = BrokerSdkArtifactReceipt(
        broker_id="fubon",
        source_url="https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/",
        sdk_version="2.2.8",
        artifact_name="fubon-sdk.pkg",
        artifact_bytes=42,
        artifact_sha256="a" * 64,
        recorded_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        account_owner_confirmed_official_download=True,
        official_checksum_verified=True,
        install_allowed=True,
    )
    receipt = BrokerIntegrationReceipt.from_sdk_artifact(
        artifact,
        receipt_id="BIR-fubon-readonly-from-sdk",
        account_alias_masked="paper-***-01",
        worker_release_sha256="b" * 64,
        readonly_probe_sha256="c" * 64,
        environment="sandbox",
    )

    assert receipt.sdk_artifact_sha256 == artifact.artifact_sha256
    assert receipt.official_sdk_url == artifact.source_url
