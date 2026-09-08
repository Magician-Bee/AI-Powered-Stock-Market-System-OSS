from __future__ import annotations

import sqlite3
import hashlib
import json

import pytest

from stock_ai.data_quality_contracts import DataQuality, DecisionDataQualityReceipt
from stock_ai.models import Entity, PricePoint
from stock_ai.shared_quality_store import SharedDecisionQualityStore
from stock_ai.services import _summary_from_real_payload


def _receipt() -> DecisionDataQualityReceipt:
    return DecisionDataQualityReceipt.issue(
        snapshot_id="SUMMARY-2330.TW-2026-08-20",
        symbol="2330.TW",
        decision_at="2026-08-20T07:00:00+00:00",
        snapshot_data_as_of="2026-08-20T07:00:00+00:00",
        data_quality=DataQuality(
            status="partial",
            score=0.6,
            source="TWSE official close",
            data_as_of="2026-08-20T07:00:00+00:00",
            quality_flags=["cross_source_observation_not_available"],
        ),
        evidence_ids=["market-summary:2330.TW", "quote:TWSE official close:2026-08-20"],
    )


def test_shared_quality_receipt_is_idempotent_and_immutable(tmp_path):
    store = SharedDecisionQualityStore(tmp_path / "quality.sqlite3")
    receipt = _receipt()

    store.save(receipt, surface="market_summary")
    store.save(receipt, surface="market_summary")

    rows = store.list(surface="market_summary", symbol="2330.TW")
    assert len(rows) == 1
    assert rows[0]["receipt"]["receipt_sha256"] == receipt.receipt_sha256

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as conn:
            conn.execute(
                "update decision_quality_receipts set surface='screener' where receipt_id=?",
                (receipt.receipt_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as conn:
            conn.execute(
                "delete from decision_quality_receipts where receipt_id=?",
                (receipt.receipt_id,),
            )


def test_shared_quality_receipt_cannot_be_rebound_to_another_surface(tmp_path):
    store = SharedDecisionQualityStore(tmp_path / "quality.sqlite3")
    receipt = _receipt()
    store.save(receipt, surface="market_summary")

    with pytest.raises(ValueError, match="different evidence"):
        store.save(receipt, surface="screener")


def test_shared_quality_store_hash_verifies_rows_on_read(tmp_path):
    store = SharedDecisionQualityStore(tmp_path / "quality.sqlite3")
    receipt = _receipt()
    store.save(receipt, surface="market_summary")
    with sqlite3.connect(store.path) as conn:
        conn.execute("drop trigger trg_decision_quality_receipts_immutable_update")
        conn.execute(
            "update decision_quality_receipts set payload_json=? where receipt_id=?",
            ('{"receipt_id":"tampered"}', receipt.receipt_id),
        )
        conn.commit()

    with pytest.raises(ValueError):
        store.list(surface="market_summary")


def test_quality_receipt_validator_accepts_legacy_v1_payload_without_source_observation():
    current = _receipt().model_dump(mode="json")
    legacy = dict(current)
    legacy_quality = dict(legacy["data_quality"])
    legacy_quality.pop("source_observation", None)
    legacy["data_quality"] = legacy_quality
    payload = {
        key: value
        for key, value in legacy.items()
        if key not in {"receipt_id", "receipt_sha256"}
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    legacy["receipt_id"] = f"DQDR-{digest[:32]}"
    legacy["receipt_sha256"] = digest

    restored = DecisionDataQualityReceipt.model_validate(legacy)

    assert restored.receipt_sha256 == digest
    assert restored.data_quality.source_observation == {}


def test_legacy_summary_persists_receipt_to_shared_store(monkeypatch, tmp_path):
    store = SharedDecisionQualityStore(tmp_path / "summary.sqlite3")
    monkeypatch.setattr("stock_ai.services._decision_quality_store", lambda: store)
    entity = Entity(
        entity_id="fixture:2330.TW",
        symbol="2330.TW",
        name="台積電",
        entity_type="stock",
        market="taiwan",
        exchange="TWSE",
        currency="TWD",
    )
    point = PricePoint(
        date="2026-08-20",
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=1000,
    )

    summary = _summary_from_real_payload(
        {
            "entity": entity,
            "history": [point],
            "latest": point,
            "change_percent": 1.0,
            "events": [],
            "source": "TWSE official close",
            "data_timestamp": "2026-08-20T07:00:00+00:00",
            "freshness_note": "official close",
            "reliability_note": "fixture",
        },
        persist_quality_receipt=True,
    )

    rows = store.list(surface="market_summary", symbol="2330.TW")
    assert summary.data_quality_receipt is not None
    assert len(rows) == 1
    assert rows[0]["receipt"]["receipt_id"] == summary.data_quality_receipt.receipt_id
