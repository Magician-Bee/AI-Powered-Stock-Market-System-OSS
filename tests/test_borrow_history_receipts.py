from __future__ import annotations

import sqlite3
import hashlib
import json

import pytest

from stock_ai.data_platform.borrow_history_receipts import (
    ShortBorrowHistoryReceiptStore,
    short_borrow_history_coverage,
    verify_short_borrow_history_receipt,
)


def _items() -> dict[str, list[dict[str, object]]]:
    common = {"symbol": "2330.TW", "as_of": "2026-08-25T09:00:00+08:00", "source_id": "broker-test", "acquired_at": "2026-08-25T09:05:00+08:00"}
    return {
        "borrow_availability": [{**common, "available_quantity": 1000, "provider_verified": True}],
        "borrow_rate": [{**common, "annual_rate_bps": 350, "provider_verified": True}],
        "borrow_recall": [{**common, "recall_at": "2026-08-29T09:00:00+08:00", "provider_verified": True}],
        "borrow_fee_history": [{**common, "fee_bps": 25, "provider_verified": True}],
    }


def test_four_borrow_history_streams_are_hash_verified_but_missing_provider_is_fail_closed() -> None:
    values = _items()
    values["borrow_availability"][0]["provider_verified"] = False
    receipt = short_borrow_history_coverage(**values)

    assert receipt["historical_pit_eligible"] is True
    assert receipt["execution_replay_eligible"] is False
    assert sorted(receipt["available_streams"]) == sorted(values)
    assert "short_execution_borrow_provider_not_verified" in receipt["blockers"]
    assert verify_short_borrow_history_receipt(receipt) is True


def test_missing_streams_are_explicit_and_do_not_use_zero_fill() -> None:
    receipt = short_borrow_history_coverage()

    assert receipt["historical_pit_eligible"] is False
    assert receipt["execution_replay_eligible"] is False
    assert receipt["zero_fill_used"] is False
    assert "short_execution_borrow_rate_history_missing" in receipt["blockers"]


def test_borrow_history_receipt_store_is_immutable(tmp_path) -> None:
    store = ShortBorrowHistoryReceiptStore(tmp_path / "borrow-history.sqlite3")
    receipt = short_borrow_history_coverage(receipt_store=store)
    assert [row["receipt_sha256"] for row in store.receipts()] == [receipt["receipt_sha256"]]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as connection:
            connection.execute(
                "delete from short_borrow_history_receipts where receipt_sha256=?",
                (receipt["receipt_sha256"],),
            )


def test_borrow_history_coverage_binds_symbol_and_pit_window() -> None:
    receipt = short_borrow_history_coverage(**{
        stream: [{
            **item,
            "as_of": "2026-07-15T02:00:00+00:00",
            "acquired_at": "2026-07-15T02:01:00+00:00",
        } for item in items]
        for stream, items in _items().items()
    })

    assert receipt["symbols"] == ["2330.TW"]
    assert receipt["first_as_of"] == "2026-07-15T02:00:00+00:00"
    assert receipt["last_as_of"] == "2026-07-15T02:00:00+00:00"
    assert verify_short_borrow_history_receipt(receipt)


def test_borrow_history_receipt_rejects_rehashed_inconsistent_scope() -> None:
    receipt = short_borrow_history_coverage(**_items())
    tampered = dict(receipt)
    tampered["symbols"] = ["9999.TW"]
    body = dict(tampered)
    body.pop("receipt_sha256")
    tampered["receipt_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

    with pytest.raises(ValueError, match="symbol manifest mismatch"):
        verify_short_borrow_history_receipt(tampered)
