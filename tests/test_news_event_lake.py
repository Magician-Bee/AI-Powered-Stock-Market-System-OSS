from __future__ import annotations

import hashlib
import json

import pytest

from stock_ai.data_platform.news_event_lake import ingest_news_events
from stock_ai.data_platform.news_history_coverage import (
    NewsHistoryCoverageReceipt,
    NewsHistoryCoverageStore,
)
from stock_ai.data_platform.service import MarketDataPlatform
from open_stock_ai.research.pit_dataset import FeatureRecord


def _item(*, published_at: str | None = "2026-01-02T08:00:00+00:00") -> dict:
    return {
        "title": "台積電公告重要消息",
        "summary": "公告內容",
        "source_url": "https://news.google.com/rss/articles/example",
        "published_at": published_at,
        "related_symbols": ["2330.TW"],
    }


def _coverage(*, reviewed: bool = True) -> NewsHistoryCoverageReceipt:
    return NewsHistoryCoverageReceipt.issue(
        receipt_id="NHCR-google-news-2026-q1",
        source_id="google_news",
        coverage_start="2026-01-01T00:00:00+00:00",
        coverage_end="2026-03-31T23:59:59+00:00",
        provider_document_sha256="b" * 64,
        reviewed_provider_coverage=reviewed,
        provider_contract_receipt_id="provider-contract-google-news-2026-q1" if reviewed else None,
        account_owner_review_receipt_sha256="d" * 64 if reviewed else None,
        original_publication_timestamps_complete=True,
        source_event_count=1,
        observed_at="2026-04-01T00:00:00+00:00",
    )


def test_news_event_lake_preserves_original_publication_time_for_as_of_replay(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    coverage_store = NewsHistoryCoverageStore(tmp_path / "news-coverage.sqlite")
    coverage_store.record(_coverage())
    receipt = ingest_news_events(
        platform,
        entity_id="EQ-2330",
        items=[_item()],
        acquired_at="2026-01-02T08:00:00+00:00",
        coverage_store=coverage_store,
    )

    assert receipt["accepted_count"] == 1
    assert receipt["historical_pit_eligible_count"] == 1
    assert platform.warehouse.standard_records("events", entity_id="EQ-2330", knowledge_at="2026-01-02T07:59:59+00:00", effective_at="2026-01-02T07:59:59+00:00") == []
    records = platform.warehouse.standard_records("events", entity_id="EQ-2330", knowledge_at="2026-01-02T08:00:00+00:00", effective_at="2026-01-02T08:00:00+00:00")
    assert len(records) == 1
    assert records[0]["published_at"] == "2026-01-02T08:00:00+00:00"
    assert records[0]["record"]["event_key"].startswith("EVT-")
    assert records[0]["record"]["news_history_coverage_receipt_id"] == _coverage().receipt_id
    audits = coverage_store.ingestion_audits()
    assert len(audits) == 1
    assert audits[0]["source_id"] == "google_news"
    assert audits[0]["complete"] is True


def test_news_event_lake_does_not_certify_pit_without_historical_coverage_receipt(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    receipt = ingest_news_events(
        platform,
        entity_id="EQ-2330",
        items=[_item()],
        acquired_at="2026-01-02T08:00:00+00:00",
    )

    assert receipt["accepted_count"] == 1
    assert receipt["historical_pit_eligible_count"] == 0
    assert receipt["coverage_receipt_required_for_pit"] is True
    record = platform.warehouse.standard_records(
        "events", entity_id="EQ-2330", knowledge_at="2026-01-02T08:00:00+00:00", effective_at="2026-01-02T08:00:00+00:00"
    )[0]
    assert record["record"]["news_history_coverage_verified"] is False
    feature = FeatureRecord.from_warehouse("events", record)
    assert feature.historical_pit_eligible is False
    assert feature.availability_reason == "news_historical_coverage_receipt_missing"


def test_unreviewed_provider_coverage_claim_never_certifies_pit_replay(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    coverage_store = NewsHistoryCoverageStore(tmp_path / "news-coverage.sqlite")
    coverage_store.record(_coverage(reviewed=False))

    result = ingest_news_events(
        platform,
        entity_id="EQ-2330",
        items=[_item()],
        acquired_at="2026-01-02T08:00:00+00:00",
        coverage_store=coverage_store,
    )

    audit = result["coverage_audits"]["google_news"]
    assert audit["complete"] is False
    assert "provider_coverage_review_missing" in audit["blockers"]
    assert result["historical_pit_eligible_count"] == 0


def test_news_coverage_receipt_is_immutable_and_range_bound(tmp_path):
    store = NewsHistoryCoverageStore(tmp_path / "coverage.sqlite")
    receipt = store.record(_coverage())
    assert store.covering_receipt(
        source_id="google_news", published_at="2026-01-02T08:00:00+00:00"
    ) == receipt
    assert store.covering_receipt(
        source_id="google_news", published_at="2026-04-01T00:00:00+00:00"
    ) is None


def test_news_event_lake_rejects_missing_original_publication_time(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    receipt = ingest_news_events(platform, entity_id="EQ-2330", items=[_item(published_at=None)])

    assert receipt["accepted_count"] == 0
    assert receipt["rejected"][0]["reason"] == "original_publication_timestamp_missing"


def test_news_event_lake_withholds_pit_when_provider_count_does_not_match_ingested_event_set(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    coverage_store = NewsHistoryCoverageStore(tmp_path / "news-coverage.sqlite")
    coverage_store.record(
        NewsHistoryCoverageReceipt.issue(
            receipt_id="NHCR-google-news-count-mismatch",
            source_id="google_news",
            coverage_start="2026-01-01T00:00:00+00:00",
            coverage_end="2026-03-31T23:59:59+00:00",
            provider_document_sha256="c" * 64,
            original_publication_timestamps_complete=True,
            source_event_count=2,
            observed_at="2026-04-01T00:00:00+00:00",
        )
    )

    result = ingest_news_events(
        platform,
        entity_id="EQ-2330",
        items=[_item()],
        acquired_at="2026-01-02T08:00:00+00:00",
        coverage_store=coverage_store,
    )

    audit = result["coverage_audits"]["google_news"]
    assert audit["complete"] is False
    assert audit["provider_event_count"] == 2
    assert audit["ingested_event_count"] == 1
    assert "provider_event_count_does_not_match_ingested_event_count" in audit["blockers"]
    assert len(audit["audit_sha256"]) == 64
    assert result["historical_pit_eligible_count"] == 0
    assert coverage_store.ingestion_audits()[0]["complete"] is False


def test_news_history_receipt_audit_hash_binds_the_ingested_event_manifest():
    receipt = _coverage()
    audit = receipt.audit_ingestion(event_keys=["EVT-b", "EVT-a"])

    assert audit["complete"] is False
    assert audit["unique_ingested_event_count"] == 2
    assert audit["ingested_event_manifest_sha256"]
    assert "provider_event_count_does_not_match_ingested_event_count" in audit["blockers"]


def test_news_history_store_rejects_hashed_but_inconsistent_complete_audit(tmp_path):
    store = NewsHistoryCoverageStore(tmp_path / "coverage.sqlite")
    audit = _coverage().audit_ingestion(event_keys=["EVT-only"])
    audit["provider_event_count"] = 1
    audit["complete"] = True
    audit["blockers"] = []
    audit.pop("receipt_id", None)
    audit.pop("receipt_sha256", None)
    audit["audit_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in audit.items() if key != "audit_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(ValueError, match="requires its coverage receipt"):
        store.record_ingestion_audit(audit)


def test_news_history_status_only_certifies_the_latest_bound_ingestion_audit(tmp_path):
    store = NewsHistoryCoverageStore(tmp_path / "coverage.sqlite")
    receipt = store.record(_coverage())

    waiting = store.status_summary()
    assert waiting["provider_wide_historical_coverage_certified"] is False
    assert waiting["sources"][0]["status"] == "receipt_recorded_waiting_ingestion_audit"
    assert waiting["sources"][0]["latest_ingestion_pit_eligible"] is False
    assert waiting["sources"][0]["blockers"] == ["ingestion_coverage_audit_missing"]

    audit = receipt.audit_ingestion(event_keys=["EVT-only"])
    store.record_ingestion_audit(audit)
    verified = store.status_summary()

    assert verified["verified_source_count"] == 1
    assert verified["sources"][0]["status"] == "pit_replay_eligible"
    assert verified["sources"][0]["latest_ingestion_pit_eligible"] is True
    assert verified["sources"][0]["receipt"]["receipt_sha256"] == receipt.receipt_sha256
    assert verified["sources"][0]["latest_ingestion_audit"]["audit_sha256"] == audit["audit_sha256"]


def test_news_history_store_rejects_complete_audit_without_locally_stored_receipt(tmp_path):
    store = NewsHistoryCoverageStore(tmp_path / "coverage.sqlite")
    audit = _coverage().audit_ingestion(event_keys=["EVT-only"])

    with pytest.raises(ValueError, match="locally stored coverage receipt"):
        store.record_ingestion_audit(audit)


def test_news_history_store_rejects_missing_receipt_audit_that_claims_completion(tmp_path):
    store = NewsHistoryCoverageStore(tmp_path / "coverage.sqlite")
    audit = {
        "schema_version": "stock_ai.news_history_ingestion_coverage_audit.v1",
        "source_id": "google_news",
        "provider_event_count": None,
        "ingested_event_count": 1,
        "unique_ingested_event_count": 1,
        "ingested_event_manifest_sha256": "a" * 64,
        "blockers": [],
        "complete": True,
    }
    audit["audit_sha256"] = hashlib.sha256(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    with pytest.raises(ValueError, match="without a provider receipt must fail closed"):
        store.record_ingestion_audit(audit)
