from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from open_stock_ai.research.cost_model import (
    TaiwanMarketCostRulebook,
    TaiwanSecondaryMarketCostSchedule,
    UnsupportedCostSchedule,
)
from open_stock_ai.research.cost_schedule_receipts import (
    BrokerCostScheduleReceipt,
    BrokerCostScheduleReceiptStore,
)


def _receipt(*, valid_from: date = date(2026, 1, 1)) -> BrokerCostScheduleReceipt:
    return BrokerCostScheduleReceipt.issue(
        receipt_id="BCSR-test-account-v1",
        broker_id="broker-sandbox",
        account_alias="paper-account-1",
        venues=["TWSE"],
        product_types=["stock"],
        lot_types=["board_lot"],
        sides=["buy", "sell"],
        valid_from=valid_from,
        valid_to=None,
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="broker-sandbox-fees-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="broker-sandbox-no-pass-through-v1",
        source_sha256="a" * 64,
        source_published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def test_account_cost_schedule_receipt_is_durable_scoped_and_immutable(tmp_path):
    store = BrokerCostScheduleReceiptStore(tmp_path / "cost-receipts.sqlite")
    receipt = _receipt()

    durable = store.record(receipt)
    assert durable.receipt == receipt
    reopened = BrokerCostScheduleReceiptStore(store.path)
    assert reopened.effective_receipt(
        broker_id="broker-sandbox",
        account_alias="paper-account-1",
        venue="TWSE",
        product_type="stock",
        lot_type="board_lot",
        side="buy",
        trade_date=date(2026, 2, 1),
    ).receipt.receipt_sha256 == receipt.receipt_sha256
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "delete from broker_cost_schedule_receipts where receipt_id=?", (receipt.receipt_id,)
        )


def test_cost_quote_requires_reviewed_account_documents_for_execution_evidence(tmp_path):
    schedule = TaiwanSecondaryMarketCostSchedule()
    raw = schedule.quote(
        venue="TWSE", product_type="stock", lot_type="board_lot", side="buy",
        trade_at="2026-02-01T01:00:00+00:00", broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0, broker_fee_schedule_id="broker-sandbox-fees-v1",
        exchange_fee_bps=0.0, exchange_fee_schedule_id="broker-sandbox-no-pass-through-v1",
    )
    assert raw.execution_evidence_eligible is False

    store = BrokerCostScheduleReceiptStore(tmp_path / "verified-cost-receipts.sqlite")
    receipt = _receipt()
    durable = store.record(receipt)
    verified = schedule.quote(
        venue="TWSE", product_type="stock", lot_type="board_lot", side="buy",
        trade_at="2026-02-01T01:00:00+00:00", broker_id="broker-sandbox",
        account_alias="paper-account-1", broker_schedule_receipt=durable,
    )
    evidence = verified.receipt(10_000)
    assert verified.broker_schedule_verified is True
    assert verified.exchange_schedule_verified is True
    assert verified.reviewed_account_schedule is False
    assert verified.execution_evidence_eligible is False
    assert evidence["broker_schedule_receipt_id"] == receipt.receipt_id
    assert evidence["broker_schedule_receipt_sha256"] == receipt.receipt_sha256
    assert evidence["broker_schedule_persistence_sha256"] == durable.persistence_sha256
    assert evidence["reviewed_account_schedule"] is False
    assert evidence["execution_evidence_eligible"] is False

    with pytest.raises(UnsupportedCostSchedule, match="receipt_scope_unverified"):
        schedule.quote(
            venue="TWSE", product_type="stock", lot_type="board_lot", side="buy",
            trade_at="2026-02-01T01:00:00+00:00", broker_id="other-broker",
            account_alias="paper-account-1", broker_schedule_receipt=durable,
        )


def test_cost_quote_rejects_raw_number_that_conflicts_with_account_receipt(tmp_path):
    store = BrokerCostScheduleReceiptStore(tmp_path / "conflicting-cost-receipts.sqlite")
    with pytest.raises(UnsupportedCostSchedule, match="receipt_mismatch:broker_commission_bps"):
        TaiwanSecondaryMarketCostSchedule().quote(
            venue="TWSE", product_type="stock", lot_type="board_lot", side="buy",
            trade_at="2026-02-01T01:00:00+00:00", broker_id="broker-sandbox",
            account_alias="paper-account-1", broker_commission_bps=9.0,
            broker_schedule_receipt=store.record(_receipt()),
        )


def test_cost_quote_rejects_unpersisted_receipt_even_when_its_hash_is_valid():
    with pytest.raises(UnsupportedCostSchedule, match="receipt_not_durable"):
        TaiwanSecondaryMarketCostSchedule().quote(
            venue="TWSE", product_type="stock", lot_type="board_lot", side="buy",
            trade_at="2026-02-01T01:00:00+00:00", broker_id="broker-sandbox",
            account_alias="paper-account-1", broker_schedule_receipt=_receipt(),  # type: ignore[arg-type]
        )


def test_taiwan_cost_rulebook_binds_official_source_capture_hashes():
    rulebook = TaiwanMarketCostRulebook()
    receipt = rulebook.receipt()
    captures = receipt["official_source_receipts"]

    assert len(captures) == 3
    assert {item["source_id"] for item in captures} == set(rulebook.sources)
    assert all(len(item["content_sha256"]) == 64 for item in captures)
    assert all(item["retrieved_at"].endswith("+08:00") for item in captures)
    assert all(item["url"].startswith("https://") for item in captures)
