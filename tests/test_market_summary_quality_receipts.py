from __future__ import annotations

from stock_ai.models import Entity, PricePoint
from stock_ai.services import (
    _observe_independent_same_day_quote,
    _summary_from_real_payload,
)


def _payload() -> dict:
    return {
        "entity": Entity(
            entity_id="fixture:2330.TW",
            symbol="2330.TW",
            name="台積電",
            entity_type="stock",
            market="taiwan",
            exchange="TWSE",
            currency="TWD",
        ),
        "history": [
            PricePoint(
                date="2026-08-20",
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1000,
            )
        ],
        "latest": PricePoint(
            date="2026-08-20",
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1000,
        ),
        "change_percent": 1.0,
        "events": [],
        "source": "TWSE official close",
        "data_timestamp": "2026-08-20T07:00:00+00:00",
        "freshness_note": "official close",
        "reliability_note": "fixture",
    }


def test_legacy_market_summary_persists_shared_quality_receipt_without_fake_certification():
    summary = _summary_from_real_payload(_payload())

    receipt = summary.data_quality_receipt
    assert receipt is not None
    assert receipt.schema_version == "stock_ai.decision_data_quality_receipt.v1"
    assert receipt.symbol == "2330.TW"
    assert receipt.source == "TWSE official close"
    assert receipt.source_disagreement_status == "not_observed"
    assert receipt.certification_status == "partial"
    assert "cross_source_observation_not_available" in receipt.data_quality.quality_flags
    assert len(receipt.receipt_sha256) == 64


def test_market_summary_receipt_is_present_in_serialized_overview_payload():
    payload = _payload()
    summary = _summary_from_real_payload(payload)

    serialized = summary.model_dump(mode="json")
    assert serialized["data_quality_receipt"]["receipt_id"] == summary.data_quality_receipt.receipt_id
    assert serialized["data_quality_receipt"]["source_disagreement_status"] == "not_observed"


def test_same_day_independent_quote_is_recorded_without_replacing_primary_quote(monkeypatch):
    secondary = PricePoint(
        date="2026-08-20",
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.5,
        volume=900,
    )
    monkeypatch.setattr(
        "stock_ai.services.fetch_yahoo_summary",
        lambda _symbol: {"source": "Yahoo fixture", "points": [secondary]},
    )
    observation = _observe_independent_same_day_quote(
        "2330.TW",
        primary_close=101.0,
        primary_date="20260820 13:30:00",
    )
    summary = _summary_from_real_payload(
        _payload(),
        cross_source_observation=observation,
    )

    assert observation["status"] == "observed"
    assert summary.latest_price.close == 101.0
    assert summary.data_quality_receipt.source_disagreement_status == "passed"
    assert summary.data_quality_receipt.data_quality.source_observation["secondary_close"] == 101.5


def test_independent_quote_conflict_blocks_quality_certification(monkeypatch):
    secondary = PricePoint(
        date="2026-08-20",
        open=120.0,
        high=122.0,
        low=119.0,
        close=120.0,
        volume=900,
    )
    monkeypatch.setattr(
        "stock_ai.services.fetch_yahoo_summary",
        lambda _symbol: {"source": "Yahoo fixture", "points": [secondary]},
    )
    observation = _observe_independent_same_day_quote(
        "2330.TW",
        primary_close=101.0,
        primary_date="2026-08-20",
    )
    summary = _summary_from_real_payload(
        _payload(),
        cross_source_observation=observation,
    )

    assert observation["status"] == "conflict"
    assert summary.data_quality_receipt.source_disagreement_status == "conflict"
    assert summary.data_quality_receipt.certification_status == "blocked"
