from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from open_stock_ai.research.cost_model import TaiwanSecondaryMarketCostSchedule
from open_stock_ai.research.cost_schedule_receipts import (
    BrokerCostScheduleReceipt,
    BrokerCostScheduleReceiptStore,
    CostScheduleDocumentReceipt,
)


def _schedule() -> BrokerCostScheduleReceipt:
    return BrokerCostScheduleReceipt.issue(
        receipt_id="BCSR-reviewed-account-v1",
        broker_id="broker-sandbox",
        account_alias="paper-account-1",
        venues=["TWSE"],
        product_types=["stock"],
        lot_types=["board_lot"],
        sides=["buy", "sell"],
        valid_from=date(2026, 1, 1),
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


def _document(kind: str, digest: str) -> CostScheduleDocumentReceipt:
    return CostScheduleDocumentReceipt.issue(
        receipt_id=f"CSDR-{kind}-v1",
        document_kind=kind,
        broker_id="broker-sandbox",
        account_alias="paper-account-1",
        source_locator=f"owner-reviewed://{kind}/v1",
        source_sha256=digest,
        source_published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        reviewed_by="account-owner-test",
        reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )


def test_reviewed_schedule_binds_broker_and_exchange_documents_and_quote(tmp_path):
    store = BrokerCostScheduleReceiptStore(tmp_path / "reviewed-cost.sqlite")
    reviewed = store.record_reviewed(
        _schedule(),
        [_document("broker_account_schedule", "a" * 64), _document("exchange_fee_schedule", "b" * 64)],
        reviewed_by="account-owner-test",
        reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    reviewed.verify()
    assert {item.receipt.document_kind for item in reviewed.documents} == {
        "broker_account_schedule",
        "exchange_fee_schedule",
    }
    assert len(store.documents()) == 2

    quote = TaiwanSecondaryMarketCostSchedule().quote(
        venue="TWSE",
        product_type="stock",
        lot_type="board_lot",
        side="buy",
        trade_at="2026-02-01T01:00:00+00:00",
        broker_id="broker-sandbox",
        account_alias="paper-account-1",
        reviewed_broker_schedule_receipt=reviewed,
    )
    evidence = quote.receipt(10_000)
    assert quote.execution_evidence_eligible is True
    assert evidence["reviewed_account_schedule"] is True
    assert evidence["review_receipt_sha256"] == reviewed.review_receipt_sha256
    assert set(evidence["review_document_receipt_ids"]) == {
        "CSDR-broker_account_schedule-v1",
        "CSDR-exchange_fee_schedule-v1",
    }

    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "delete from cost_schedule_document_receipts where receipt_id=?",
            ("CSDR-broker_account_schedule-v1",),
        )


def test_reviewed_schedule_rejects_missing_or_cross_account_document(tmp_path):
    store = BrokerCostScheduleReceiptStore(tmp_path / "reviewed-cost-invalid.sqlite")
    broker = _document("broker_account_schedule", "a" * 64)
    exchange = _document("exchange_fee_schedule", "b" * 64)
    with pytest.raises(ValueError, match="requires broker and exchange documents"):
        store.record_reviewed(
            _schedule(),
            [broker],
            reviewed_by="account-owner-test",
            reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

    other_account = CostScheduleDocumentReceipt.issue(
        receipt_id="CSDR-other-account-v1",
        document_kind="exchange_fee_schedule",
        broker_id="broker-sandbox",
        account_alias="different-account",
        source_locator="owner-reviewed://exchange_fee_schedule/other",
        source_sha256="b" * 64,
        source_published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        reviewed_by="account-owner-test",
        reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="account scope mismatch"):
        store.record_reviewed(
            _schedule(),
            [broker, other_account],
            reviewed_by="account-owner-test",
            reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )


def test_reviewed_schedule_rejects_tampered_source_hash(tmp_path):
    store = BrokerCostScheduleReceiptStore(tmp_path / "reviewed-cost-tamper.sqlite")
    broker = _document("broker_account_schedule", "c" * 64)
    with pytest.raises(ValueError, match="source hash mismatch"):
        store.record_reviewed(
            _schedule(),
            [broker, _document("exchange_fee_schedule", "b" * 64)],
            reviewed_by="account-owner-test",
            reviewed_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )
