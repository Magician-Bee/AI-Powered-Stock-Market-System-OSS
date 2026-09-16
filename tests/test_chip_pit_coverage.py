from __future__ import annotations

import sqlite3

import pytest

from stock_ai.chip_history import build_chip_history
from stock_ai.data_platform.chip_pit_coverage import (
    ChipPITCoverageReceiptStore,
    chip_pit_coverage,
    verify_chip_pit_coverage_receipt,
)


def test_chip_history_marks_uncertified_sources_as_unavailable_for_exact_replay():
    result = build_chip_history(
        symbol="2330.TW",
        institutional_items=[{"symbol": "2330.TW", "trade_date": "2026-01-02", "foreign_net": 10}],
        margin_items=[{"symbol": "2330.TW", "trade_date": "2026-01-02", "margin_balance": 10, "short_balance": 2}],
    )

    receipt = result["pit_coverage"]
    assert receipt["exact_replay_eligible"] is False
    assert receipt["zero_fill_used"] is False
    assert receipt["streams"]["institutional_flows"]["historical_pit_eligible"] is False
    assert receipt["streams"]["institutional_flows"]["missingness"] == "local_ingestion_timestamp_missing"
    assert "pit_chip_stream_not_certified:margin_trading:market_schedule" in receipt["blockers"]
    assert "pit_chip_stream_not_certified:borrowed_short:market_schedule" in receipt["blockers"]


def test_chip_history_requires_local_ingestion_evidence_before_certifying_a_reviewed_stream():
    result = build_chip_history(
        symbol="2330.TW",
        institutional_items=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "foreign_net": 10,
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        margin_items=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "margin_balance": 10,
            "short_balance": 2,
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
    )

    receipt = result["pit_coverage"]
    assert receipt["streams"]["institutional_flows"]["historical_pit_eligible"] is True
    assert receipt["streams"]["margin_trading"]["historical_pit_eligible"] is True
    assert receipt["exact_replay_eligible"] is False
    assert "pit_chip_stream_not_certified:tdcc_holding_distribution:source_published" in receipt["blockers"]


def test_chip_history_requires_every_stream_and_uses_actual_source_contracts():
    result = build_chip_history(
        symbol="2330.TW",
        institutional_items=[{"symbol": "2330.TW", "trade_date": "2026-01-02", "source_id": "tpex_official_web", "acquired_at": "2026-01-02T08:00:00+00:00"}],
        margin_items=[{"symbol": "2330.TW", "trade_date": "2026-01-02", "source_id": "tpex_openapi", "acquired_at": "2026-01-02T08:00:00+00:00"}],
        borrowed_short_items=[{"symbol": "2330.TW", "trade_date": "2026-01-02", "source_id": "tpex_official_web", "acquired_at": "2026-01-02T08:00:00+00:00"}],
        tdcc_items=[{"symbol": "2330.TW", "report_date": "2026-01-02", "source_id": "tdcc", "published_at": "2026-01-02T08:00:00+00:00", "acquired_at": "2026-01-02T08:00:00+00:00"}],
    )
    receipt = result["pit_coverage"]
    assert result["status"] == "complete"
    # A caller must not be able to manufacture a production TDCC PIT source by
    # supplying a published_at value.  The current public source only retains
    # one year and does not independently attest that historical timestamp.
    assert receipt["exact_replay_eligible"] is False
    assert receipt["streams"]["institutional_flows"]["source_ids"] == ["tpex_official_web"]
    assert receipt["streams"]["borrowed_short"]["historical_pit_eligible"] is True
    assert receipt["streams"]["tdcc_holding_distribution"]["historical_pit_eligible"] is False
    assert receipt["streams"]["tdcc_holding_distribution"]["source_retention_days"] == [365]
    assert "pit_chip_stream_not_certified:tdcc_holding_distribution:source_published" in receipt["blockers"]
    assert receipt["short_execution_replay"]["eligible"] is False
    assert "short_execution_borrow_rate_history_missing" in receipt["short_execution_replay"]["blockers"]


def test_chip_pit_coverage_emits_hashed_stream_manifests_and_verifies_them():
    receipt = chip_pit_coverage(
        institutional=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_official_web",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        margin=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_openapi",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        borrowed_short=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_official_web",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        tdcc=[{
            "symbol": "2330.TW",
            "report_date": "2026-01-02",
            "source_id": "tdcc",
            "published_at": "2026-01-02T08:00:00+00:00",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
    )

    assert verify_chip_pit_coverage_receipt(receipt) is True
    assert len(receipt["receipt_sha256"]) == 64
    assert len(receipt["streams"]["tdcc_holding_distribution"]["coverage_audit"]["audit_sha256"]) == 64
    assert receipt["streams"]["institutional_flows"]["coverage_audit"]["complete"] is True

    tampered = {**receipt, "streams": {**receipt["streams"]}}
    tampered["streams"]["institutional_flows"] = {
        **tampered["streams"]["institutional_flows"],
        "count": 99,
    }
    with pytest.raises(ValueError, match="receipt hash mismatch"):
        verify_chip_pit_coverage_receipt(tampered)


def test_chip_pit_coverage_hashes_missingness_and_rejects_duplicate_observations():
    receipt = chip_pit_coverage(
        institutional=[
            {"symbol": "2330.TW", "trade_date": "2026-01-02", "acquired_at": "2026-01-02T08:00:00+00:00"},
            {"symbol": "2330.TW", "trade_date": "2026-01-02", "acquired_at": "2026-01-02T08:00:00+00:00"},
        ],
        margin=[],
    )

    audit = receipt["streams"]["institutional_flows"]["coverage_audit"]
    assert audit["complete"] is False
    assert "duplicate_observation_key_in_ingestion" in audit["blockers"]
    assert receipt["streams"]["institutional_flows"]["historical_pit_eligible"] is False
    assert receipt["exact_replay_eligible"] is False
    assert verify_chip_pit_coverage_receipt(receipt) is True


def test_chip_pit_coverage_receipt_is_persisted_once_and_cannot_be_rewritten(tmp_path):
    store = ChipPITCoverageReceiptStore(tmp_path / "chip-coverage.sqlite3")
    receipt = chip_pit_coverage(
        institutional=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_official_web",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        margin=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_openapi",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        receipt_store=store,
    )
    chip_pit_coverage(
        institutional=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_official_web",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        margin=[{
            "symbol": "2330.TW",
            "trade_date": "2026-01-02",
            "source_id": "tpex_openapi",
            "acquired_at": "2026-01-02T08:00:00+00:00",
        }],
        receipt_store=store,
    )
    assert [row["receipt_sha256"] for row in store.receipts()] == [receipt["receipt_sha256"]]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as connection:
            connection.execute(
                "delete from chip_pit_coverage_receipts where receipt_sha256=?",
                (receipt["receipt_sha256"],),
            )
