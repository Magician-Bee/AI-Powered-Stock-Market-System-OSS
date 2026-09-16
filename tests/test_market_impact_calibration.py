from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.research.market_impact_calibration import (
    MarketImpactCalibrationStore,
    build_reviewed_source_receipt,
    build_source_receipt,
    verify_calibration_artifact,
)


def _observation(index: int, *, reviewed: bool = False, provider_verified: bool = True) -> dict:
    observation_id = f"fill-{index}"
    source_payload = {
        "source": "broker_reconciliation",
        "source_event_id": observation_id,
        "provider_verified": provider_verified,
    }
    source_receipt = (
        build_reviewed_source_receipt(
            source_payload,
            broker_reconciliation_receipt_id=f"broker-fill-{index}",
            exchange_execution_receipt_id=f"exchange-trade-{index}",
            account_owner_review_receipt_sha256=(f"{index:x}" * 64)[:64],
        )
        if reviewed
        else build_source_receipt(source_payload)
    )
    return {
        "observation_id": observation_id,
        "venue": "TWSE",
        "product_type": "stock",
        "side": "buy",
        "reference_price": 100.0,
        "fill_price": 100.05 + index / 1000.0,
        "quantity": 100.0,
        "adv_volume_shares": 10_000.0,
        "captured_at": f"2026-08-2{index + 1}T01:00:00+00:00",
        "source_receipt": source_receipt,
    }


def test_impact_observations_are_immutable_and_identity_bound(tmp_path) -> None:
    store = MarketImpactCalibrationStore(tmp_path / "impact.sqlite")
    observation = store.record(_observation(0))
    assert len(observation["observation_sha256"]) == 64
    assert store.record(_observation(0)) == observation

    with pytest.raises(ValueError, match="different_evidence"):
        store.record({**_observation(0), "fill_price": 101.0})
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute("delete from market_impact_observations where observation_id='fill-0'")


def test_reviewed_observations_produce_deterministic_empirical_artifact(tmp_path) -> None:
    store = MarketImpactCalibrationStore(tmp_path / "impact.sqlite")
    store.record_many(_observation(index, reviewed=True) for index in range(5))

    artifact = store.calibrate(
        calibration_id="twse-stock-buy-2026-08-v1",
        venue="TWSE",
        product_type="stock",
        side="buy",
        min_samples=3,
    )

    assert artifact["calibration_status"] == "empirically_calibrated"
    assert artifact["execution_evidence_eligible"] is True
    assert artifact["rules"][0]["participation_bucket"] == "0.005_to_0.02"
    assert artifact["rules"][0]["sample_count"] == 5
    assert artifact["rules"][0]["median_adverse_impact_bps"] == pytest.approx(5.2)
    assert verify_calibration_artifact(artifact)
    assert store.calibrations() == [artifact]


def test_unreviewed_provider_claims_never_become_execution_evidence(tmp_path) -> None:
    store = MarketImpactCalibrationStore(tmp_path / "impact.sqlite")
    store.record_many(_observation(index, provider_verified=True) for index in range(3))

    artifact = store.calibrate(
        calibration_id="twse-stock-buy-partial-v1",
        venue="TWSE",
        product_type="stock",
        side="buy",
        min_samples=3,
    )

    assert artifact["execution_evidence_eligible"] is False
    assert artifact["calibration_status"] == "partial_unverified"
    assert "reviewed_provider_evidence_missing:0.005_to_0.02" in artifact["blockers"]
    assert verify_calibration_artifact(artifact)


def test_incomplete_review_chain_is_rejected_even_when_hash_is_valid(tmp_path) -> None:
    store = MarketImpactCalibrationStore(tmp_path / "impact.sqlite")
    observation = _observation(0)
    observation["source_receipt"] = build_source_receipt(
        {
            "source": "broker_reconciliation",
            "source_event_id": "fill-0",
            "provider_verified": True,
            "reviewed_provider_evidence": True,
            "review_document_receipt_ids": {"broker_reconciliation": "broker-fill-0"},
            "account_owner_review_receipt_sha256": "a" * 64,
        }
    )

    with pytest.raises(ValueError, match="review_chain_invalid"):
        store.record(observation)


def test_tampered_source_receipt_is_rejected(tmp_path) -> None:
    store = MarketImpactCalibrationStore(tmp_path / "impact.sqlite")
    observation = _observation(0)
    observation["source_receipt"]["fill_price"] = 999

    with pytest.raises(ValueError, match="hash_mismatch"):
        store.record(observation)
