from __future__ import annotations

import hashlib
import json

import pytest

from open_stock_ai.research.pit_dataset import FeatureRecord, PointInTimeDatasetBuilder
from stock_ai.data_platform.availability import get_data_availability_registry
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.source_registry import get_source_registry
from stock_ai.data_platform.warehouse import STANDARD_WAREHOUSE_DATASETS


def test_official_daily_price_contract_uses_reviewed_market_close_schedule():
    receipt = get_data_availability_registry().resolve(
        source_id="twse_openapi",
        dataset="prices_daily",
    ).receipt(
        observed_at="2026-01-02",
        published_at=None,
        acquired_at="2026-01-05T01:00:00+00:00",
    )

    assert receipt["historical_pit_eligible"] is True
    assert receipt["basis"] == "market_schedule"
    assert receipt["available_at"] == "2026-01-02T07:00:00+00:00"
    assert receipt["production_contract_covered"] is True
    assert receipt["contract_scope"] == "dataset_source"
    assert len(receipt["contract_sha256"]) == 64


def test_every_standard_research_dataset_has_an_explicit_source_contract():
    coverage = get_data_availability_registry().research_coverage()

    assert coverage["passed"] is True
    assert coverage["missing_datasets"] == []
    assert set(coverage["domains"]) == set(STANDARD_WAREHOUSE_DATASETS)
    for domain, datasets in STANDARD_WAREHOUSE_DATASETS.items():
        assert set(coverage["domains"][domain]) == set(datasets)
        assert all(coverage["datasets"][dataset] for dataset in datasets)
    assert len(coverage["registry_sha256"]) == 64


def test_mops_archive_is_explicitly_acquisition_only_not_historical_knowledge():
    receipt = get_data_availability_registry().resolve(
        source_id="mops_archive",
        dataset="revenues_monthly",
    ).receipt(
        observed_at="2025-12-31",
        published_at=None,
        acquired_at="2026-08-11T08:00:00+00:00",
    )

    assert receipt["historical_pit_eligible"] is False
    assert receipt["basis"] == "acquisition_only"
    assert receipt["available_at"] == "2026-08-11T08:00:00+00:00"


def test_every_financial_archive_endpoint_is_explicitly_unavailable_for_historical_pit():
    """No financial archive endpoint may silently become known-at-the-time.

    MOPS archive responses are useful immutable source records, but the
    archive transport does not attest their original disclosure timestamp.
    Keep the exact endpoint-to-warehouse mapping audited here so adding a new
    historical financial importer cannot bypass the fail-closed contract.
    """

    source_registry = get_source_registry()
    availability_registry = get_data_availability_registry()
    archive_datasets = {
        item.dataset_id: item.domain
        for item in source_registry.datasets.values()
        if item.source_id == "mops_archive" and item.domain in {"revenue", "fundamentals"}
    }
    assert archive_datasets == {
        "mops_monthly_revenue_archive": "revenue",
        "mops_income_statement_archive": "fundamentals",
        "mops_balance_sheet_archive": "fundamentals",
        "mops_cash_flow_statement_archive": "fundamentals",
    }
    source = source_registry.source("mops_archive")
    assert source.field_contract["archive_publication_timestamp_available"] is False

    warehouse_dataset_by_domain = {
        "revenue": "revenues_monthly",
        "fundamentals": "fundamentals_quarterly",
    }
    for domain in sorted(set(archive_datasets.values())):
        contract = availability_registry.resolve(
            source_id="mops_archive", dataset=warehouse_dataset_by_domain[domain]
        )
        receipt = contract.receipt(
            observed_at="2025-12-31T00:00:00+00:00",
            published_at=None,
            acquired_at="2026-08-11T08:00:00+00:00",
        )
        assert receipt["basis"] == "acquisition_only"
        assert receipt["historical_pit_eligible"] is False
        assert receipt["reason"] == "historical_availability_not_provided_by_source"

    for dataset in (
        "revenues_monthly",
        "fundamentals",
        "fundamentals_quarterly",
        "valuation_metrics",
    ):
        live_contract = availability_registry.resolve(source_id="mops", dataset=dataset)
        assert live_contract.basis == "source_published"
        assert live_contract.requires_published_at is True


def test_unknown_source_dataset_combination_fails_closed_for_exact_replay():
    receipt = get_data_availability_registry().resolve(
        source_id="unreviewed_source",
        dataset="unreviewed_dataset",
    ).receipt(
        observed_at="2026-01-02T08:00:00+00:00",
        published_at=None,
        acquired_at="2026-01-02T08:05:00+00:00",
    )

    assert receipt["historical_pit_eligible"] is False
    assert receipt["basis"] == "unverified"
    assert receipt["production_contract_covered"] is False


def test_source_default_never_masquerades_as_a_production_dataset_contract():
    receipt = get_data_availability_registry().resolve(
        source_id="twse_openapi",
        dataset="unreviewed_feature_dataset",
    ).receipt(
        observed_at="2026-01-02T08:00:00+00:00",
        published_at=None,
        acquired_at="2026-01-02T08:05:00+00:00",
    )

    assert receipt["contract_scope"] == "source_default"
    assert receipt["historical_pit_eligible"] is False
    assert receipt["production_contract_covered"] is False


def test_source_defaults_cannot_certify_pit_even_for_sources_with_reviewed_datasets():
    registry = get_data_availability_registry()
    for source_id in ("mops", "tdcc", "google_news"):
        receipt = registry.resolve(
            source_id=source_id,
            dataset="unreviewed_feature_dataset",
        ).receipt(
            observed_at="2026-01-02T08:00:00+00:00",
            published_at="2026-01-02T08:00:00+00:00",
            acquired_at="2026-01-02T08:05:00+00:00",
        )
        assert receipt["historical_pit_eligible"] is False
        assert receipt["production_contract_covered"] is False
        assert receipt["basis"] == "unverified"


def test_mops_archive_feature_blocks_an_exact_pit_dataset():
    availability_snapshot = get_data_availability_registry().resolve(
        source_id="mops_archive",
        dataset="revenues_monthly",
    ).snapshot(
        observed_at="2025-12-31T00:00:00+00:00",
        published_at=None,
        acquired_at="2026-08-11T08:00:00+00:00",
    )
    archived_financial = FeatureRecord.from_warehouse(
        "financials",
        {
            "entity_id": "EQ-2330",
            "dataset": "revenues_monthly",
            "source_id": "mops_archive",
            "observation_key": "2025-12",
            "revision_id": "REV-mops-archive",
            "observed_at": "2025-12-31T00:00:00+00:00",
            "published_at": None,
            "available_at": "2026-08-11T08:00:00+00:00",
            "acquired_at": "2026-08-11T08:00:00+00:00",
            "effective_at": "2025-12-31T00:00:00+00:00",
            "record": {"revenue": 100.0},
            "availability_contract_snapshot": availability_snapshot,
        },
    )
    common = dict(
        entity_id="EQ-2330",
        event_time="2026-01-02T08:00:00+00:00",
        published_at="2026-01-02T08:00:00+00:00",
        available_at="2026-01-02T08:00:00+00:00",
        effective_at="2026-01-02T08:00:00+00:00",
        ingested_at="2026-01-02T08:00:00+00:00",
        transformation_id="fixture.v1",
        transformation_sha="fixture",
        dataset_version="fixture:v1",
    )
    price = FeatureRecord(
        feature_id="prices:fixture",
        value={"close": 100.0},
        source_revision_id="REV-price",
        domain="prices",
        **common,
    )
    flow = FeatureRecord(
        feature_id="flows:fixture",
        value={"flow": 1},
        source_revision_id="REV-flow",
        domain="flows",
        **common,
    )
    event = FeatureRecord(
        feature_id="events:fixture",
        value={"event": "none"},
        source_revision_id="REV-event",
        domain="events",
        **common,
    )
    dataset = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-08-12T00:00:00+00:00",
        records_by_domain={
            "prices": [price],
            "financials": [archived_financial],
            "flows": [flow],
            "events": [event],
        },
        intelligence_records=[
            {
                "available_at": "2026-01-02T08:00:00+00:00",
                "published_at": "2026-01-02T08:00:00+00:00",
                "ingested_at": "2026-01-02T08:00:00+00:00",
            }
        ],
    )

    assert archived_financial.historical_pit_eligible is False
    assert dataset.manifest.exact_replay_eligible is False
    assert "pit_availability_not_certified:mops_archive:revenues_monthly:acquisition_only" in dataset.manifest.blockers


def test_legacy_feature_without_a_persisted_contract_snapshot_fails_closed():
    feature = FeatureRecord.from_warehouse(
        "prices",
        {
            "entity_id": "EQ-2330",
            "dataset": "prices_daily",
            "source_id": "twse_openapi",
            "observation_key": "2026-01-02",
            "revision_id": "REV-daily-price",
            "observed_at": "2026-01-02T00:00:00+00:00",
            "published_at": None,
            # A legacy import incorrectly recorded download time as availability.
            "available_at": "2026-01-02T02:00:00+00:00",
            "acquired_at": "2026-01-02T02:00:00+00:00",
            "effective_at": "2026-01-02T00:00:00+00:00",
            "record": {"close": 100.0},
        },
    )

    assert feature.historical_pit_eligible is False
    assert feature.availability_basis == "snapshot_missing"
    assert feature.availability_reason == "availability_contract_snapshot_missing"
    assert feature.available_at == "2026-01-02T02:00:00+00:00"
    assert feature.ingested_at == "2026-01-02T02:00:00+00:00"


def test_feature_record_uses_the_persisted_contract_decision_not_current_registry():
    snapshot = get_data_availability_registry().resolve(
        source_id="twse_openapi",
        dataset="prices_daily",
    ).snapshot(
        observed_at="2026-01-02T00:00:00+00:00",
        published_at=None,
        acquired_at="2026-01-03T08:00:00+00:00",
    )
    # This represents a future registry revision that changes the reviewed
    # close window. The immutable revision must keep its stored decision.
    snapshot["decision"]["available_at"] = "2026-01-02T08:00:00+00:00"
    feature = FeatureRecord.from_warehouse(
        "prices",
        {
            "entity_id": "EQ-2330",
            "dataset": "prices_daily",
            "source_id": "twse_openapi",
            "observation_key": "2026-01-02",
            "revision_id": "REV-persisted-contract",
            "observed_at": "2026-01-02T00:00:00+00:00",
            "published_at": None,
            "available_at": "2026-01-02T07:00:00+00:00",
            "acquired_at": "2026-01-03T08:00:00+00:00",
            "effective_at": "2026-01-02T00:00:00+00:00",
            "record": {"close": 100.0},
            "availability_contract_snapshot": snapshot,
        },
    )

    assert feature.historical_pit_eligible is True
    assert feature.available_at == "2026-01-02T08:00:00+00:00"
    assert feature.availability_contract_sha256 == snapshot["contract"]["contract_sha256"]


def test_daily_price_warehouse_persists_market_availability_separate_from_ingestion(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market-data.sqlite")
    acquired_at = "2026-01-03T08:00:00+00:00"
    payload = {"date": "2026-01-02", "close": 100.0}
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_official_web",
        payload=[payload],
        requested_at=acquired_at,
        received_at=acquired_at,
    )
    platform.warehouse.write_daily_price_revision_batch(
        entity_id="EQ-2330",
        source_id="twse_official_web",
        records=[payload],
        raw_payload_id=raw_payload_id,
        acquired_at=acquired_at,
        is_fallback=False,
        transformation_id="test.daily_price.v1",
        code_version="test",
    )

    record = platform.warehouse.standard_records(
        "prices",
        entity_id="EQ-2330",
        knowledge_at="2026-01-03T08:00:00+00:00",
        effective_at="2026-01-03T08:00:00+00:00",
    )[0]

    assert record["available_at"] == "2026-01-02T07:00:00+00:00"
    assert record["acquired_at"] == acquired_at
    assert record["availability_contract_snapshot"]["schema_version"] == (
        "stock_ai.data_availability_contract_snapshot.v1"
    )
    assert record["availability_contract_snapshot"]["contract"]["dataset"] == "prices_daily"


def test_every_registered_source_has_an_explicit_availability_default():
    registry = get_source_registry()

    assert set(registry.sources) == {
        "fugle_marketdata",
        "fugle_stream",
        "google_news",
        "mops",
        "mops_archive",
        "taifex_openapi",
        "tdcc",
        "tdcc_openapi",
        "tpex_official_web",
        "tpex_openapi",
        "twse_mis",
        "twse_official_web",
        "twse_openapi",
        "yahoo_finance",
    }


def test_every_active_source_endpoint_has_a_reviewed_availability_contract():
    source_registry = get_source_registry()
    coverage = get_data_availability_registry().endpoint_coverage(
        source_registry.datasets.values()
    )

    assert coverage["passed"] is True
    assert coverage["missing_endpoint_contracts"] == []
    assert {item["dataset_id"] for item in coverage["endpoints"]} == set(
        source_registry.datasets
    )
    assert all(item["contract_scope"] == "endpoint_dataset" for item in coverage["endpoints"])
    assert all(len(item["contract_sha256"]) == 64 for item in coverage["endpoints"])
    assert all(len(item["contract_manifest_sha256"]) == 64 for item in coverage["endpoints"])

    by_id = {item["dataset_id"]: item for item in coverage["endpoints"]}
    assert by_id["twse_borrowed_short"]["basis"] == "market_schedule"
    assert by_id["google_news_search"]["basis"] == "source_published"
    assert by_id["tpex_daytrade"]["historical_pit_eligible"] is False
    tdcc = by_id["tdcc_holding_distribution"]
    assert tdcc["historical_pit_eligible"] is False
    assert tdcc["source_retention_days"] == 365


def test_tdcc_public_history_contract_never_certifies_a_caller_supplied_timestamp():
    receipt = get_data_availability_registry().resolve(
        source_id="tdcc",
        dataset="tdcc_holding_distribution",
    ).receipt(
        observed_at="2026-01-02T00:00:00+00:00",
        published_at="2026-01-02T08:00:00+00:00",
        acquired_at="2026-01-02T08:01:00+00:00",
    )

    assert receipt["historical_pit_eligible"] is False
    assert receipt["source_retention_days"] == 365
    assert receipt["reason"] == (
        "tdcc_public_history_is_limited_to_one_year_and_publication_time_is_not_independently_attested"
    )


def test_endpoint_contract_is_bound_to_the_exact_dataset_id():
    registry = get_data_availability_registry()
    daily_volume = registry.resolve_endpoint(
        source_id="twse_official_web",
        domain="prices_daily",
        dataset_id="twse_daily_volume",
    )
    stock_day = registry.resolve_endpoint(
        source_id="twse_official_web",
        domain="prices_daily",
        dataset_id="twse_stock_day",
    )

    assert daily_volume.contract_scope == "endpoint_dataset"
    assert stock_day.contract_scope == "endpoint_dataset"
    assert daily_volume.dataset == "twse_daily_volume"
    assert stock_day.dataset == "twse_stock_day"
    assert daily_volume.contract_sha256 != stock_day.contract_sha256


def test_availability_audit_receipt_is_exposed_and_content_hashed():
    source_registry = get_source_registry()
    audit = source_registry.availability_audit

    assert audit["schema_version"] == "stock_ai.data_availability_audit.v1"
    assert audit["passed"] is True
    assert audit["blockers"] == []
    assert len(audit["registry_sha256"]) == 64
    assert len(audit["receipt_sha256"]) == 64
    assert audit["endpoint_coverage"]["missing_endpoint_contracts"] == []
    assert audit["research_coverage"]["missing_datasets"] == []
    assert source_registry.as_dict()["availability_audit"] == audit
    get_data_availability_registry().verify_audit_receipt(audit)
    tampered = {**audit, "passed": False}
    with pytest.raises(ValueError, match="receipt hash mismatch"):
        get_data_availability_registry().verify_audit_receipt(tampered)

    nested_tampered = json.loads(json.dumps(audit))
    endpoint = nested_tampered["endpoint_coverage"]["endpoints"][0]
    endpoint["reason"] = "tampered"
    payload = dict(nested_tampered)
    payload.pop("receipt_sha256")
    nested_tampered["receipt_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="contract manifest hash mismatch"):
        get_data_availability_registry().verify_audit_receipt(nested_tampered)

    assert all(
        len(manifest["manifest_sha256"]) == 64
        for manifests in audit["research_coverage"]["contract_manifests"].values()
        for manifest in manifests
    )
