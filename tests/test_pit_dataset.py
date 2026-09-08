from __future__ import annotations

from dataclasses import replace

import pytest

from open_stock_ai.data.market_data_hub import MarketDataHub
from open_stock_ai.research.pit_dataset import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    FeatureRecord,
    PointInTimeDatasetBuilder,
    verify_dataset_materialization,
)


def _feature(
    domain: str,
    revision: str,
    *,
    event_time: str = "2026-01-02T08:00:00+00:00",
    available_at: str = "2026-01-02T08:00:00+00:00",
    ingested_at: str | None = None,
    value: dict | None = None,
) -> FeatureRecord:
    return FeatureRecord(
        entity_id="EQ-2330",
        feature_id=f"{domain}:{revision}",
        value=value or {"value": revision},
        event_time=event_time,
        published_at=available_at,
        available_at=available_at,
        effective_at=event_time,
        ingested_at=ingested_at or available_at,
        source_revision_id=revision,
        transformation_id="test.normalizer.v1",
        transformation_sha=f"sha-{revision}",
        dataset_version=f"dataset:{revision}",
        domain=domain,
    )


def _intelligence(available_at: str = "2026-01-02T08:00:00+00:00") -> dict:
    return {
        "summary": "Known-at-the-time intelligence",
        "sentiment_score": 0.1,
        "technical_view": "uptrend",
        "fundamental_view": "positive",
        "event_time": available_at,
        "published_at": available_at,
        "available_at": available_at,
        "effective_at": available_at,
        "ingested_at": available_at,
    }


def _historical_universe() -> list[dict]:
    return [
        {
            "entity_id": "EQ-2330",
            "symbol": "2330.TW",
            "effective_from": "1994-09-05T00:00:00+00:00",
            "available_at": "1994-09-05T00:00:00+00:00",
            "ingested_at": "1994-09-05T00:00:00+00:00",
            "historical_pit_eligible": True,
            "universe_scope_id": "taiwan-listed-and-otc-securities",
            "universe_coverage_complete": True,
            "source_revision_id": "universe-2330-listed",
        }
    ]


def test_pit_dataset_defers_a_price_until_its_verified_known_at_time():
    builder = PointInTimeDatasetBuilder()
    data = builder.build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [
                _feature(
                    "prices",
                    "price-backfilled",
                    event_time="2026-01-02T08:00:00+00:00",
                    available_at="2026-01-03T08:00:00+00:00",
                    value={"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1_000},
                )
            ],
            "financials": [_feature("financials", "financial")],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.replay_row_count == 1
    assert data.manifest.exact_replay_eligible is True
    assert data.replay_rows[0]["event_time"] == "2026-01-02T08:00:00+00:00"
    assert data.replay_rows[0]["timestamp"] == "2026-01-03T08:00:00+00:00"
    assert "pit_replay_rows_unavailable" not in data.manifest.blockers


def test_pit_dataset_manifest_binds_feature_schema_partitions_and_replay_rows():
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [_feature("prices", "price", value={"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1_000})],
            "financials": [_feature("financials", "financial")],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    manifest = data.manifest.to_dict()
    assert manifest["schema_version"] == DATASET_MANIFEST_SCHEMA_VERSION
    assert set(manifest["partition_hashes"]) == {
        "features:prices", "features:financials", "features:flows", "features:events", "replay_rows",
    }
    assert manifest["feature_schema_versions"]["prices"] == ["open_stock_ai.feature_record.v2"]
    graph = data.feature_provenance_graph()
    assert graph["valid"] is True
    assert manifest["feature_lineage_sha256"] == graph["graph_sha256"]
    assert data.verify_materialization()["passed"] is True

    tampered_rows = [dict(row) for row in data.replay_rows]
    tampered_rows[0]["close"] = 101.0
    verification = verify_dataset_materialization(
        manifest,
        features=data.features,
        replay_rows=tampered_rows,
    )
    assert verification["passed"] is False
    assert "dataset_partition_hash_mismatch:replay_rows" in verification["errors"]


def test_feature_lineage_binds_formula_inputs_and_exact_projection_code():
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [_feature("prices", "price", value={"close": 100.0})],
            "financials": [_feature("financials", "financial")],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    price = next(item for item in data.features if item.domain == "prices")
    lineage = price.to_dict()["lineage"]
    assert lineage["formula"] == "stock_ai.warehouse_revision_projection.v1"
    assert lineage["input_revision_ids"] == ["price"]
    assert len(lineage["formula_sha256"]) == len(lineage["code_sha256"]) == 64
    graph = data.feature_provenance_graph()
    assert {edge["relationship"] for edge in graph["edges"]} == {"consumes_revision"}

    tampered_features = [item.to_dict() for item in data.features]
    tampered_features[0]["lineage"]["code_sha256"] = "0" * 64
    verification = verify_dataset_materialization(
        data.manifest.to_dict(), features=tampered_features, replay_rows=data.replay_rows
    )
    assert verification["passed"] is False
    assert "dataset_manifest_feature_lineage_hash_mismatch" in verification["errors"]


def test_pit_dataset_only_materializes_features_known_at_each_decision_time():
    builder = PointInTimeDatasetBuilder()
    data = builder.build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [
                _feature(
                    "prices",
                    "price-1",
                    value={"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1_000, "venue": "TWSE"},
                )
            ],
            "financials": [
                _feature("financials", "financial-known"),
                _feature(
                    "financials",
                    "financial-future",
                    event_time="2026-01-04T08:00:00+00:00",
                    available_at="2026-01-04T08:00:00+00:00",
                ),
            ],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.exact_replay_eligible is True
    assert data.manifest.replay_row_count == 1
    feature_ids = {item["source_revision_id"] for item in data.replay_rows[0]["feature_records"]}
    assert "financial-known" in feature_ids
    assert "financial-future" not in feature_ids
    assert data.replay_rows[0]["pit_intelligence"]["available_at"] == "2026-01-02T08:00:00+00:00"
    assert data.replay_rows[0]["universe_membership"]["required_entity_in_universe"] is True


def test_pit_dataset_defers_archive_price_until_local_ingestion_is_complete():
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [
                _feature(
                    "prices",
                    "price-backfilled",
                    ingested_at="2026-01-03T08:00:00+00:00",
                    value={"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1_000},
                )
            ],
            "financials": [_feature("financials", "financial")],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.replay_row_count == 1
    assert data.manifest.coverage["prices"]["excluded_after_as_of"] == 0
    assert data.replay_rows[0]["timestamp"] == "2026-01-03T08:00:00+00:00"
    assert "pit_replay_rows_unavailable" not in data.manifest.blockers


def test_pit_dataset_excludes_feature_ingested_after_the_as_of_cutoff():
    late_financial = _feature(
        "financials",
        "financial-late",
        ingested_at="2026-01-04T09:00:00+00:00",
    )
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [_feature("prices", "price", value={"close": 100.0})],
            "financials": [late_financial],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.exact_replay_eligible is False
    assert data.manifest.coverage["financials"]["excluded_after_as_of"] == 1
    assert "pit_required_domain_missing:financials" in data.manifest.blockers


@pytest.mark.parametrize("domain", ["prices", "financials", "flows", "events"])
@pytest.mark.parametrize("temporal_field", ["available_at", "ingested_at"])
def test_pit_dataset_rejects_future_feature_in_each_required_domain(
    domain: str, temporal_field: str
):
    """A future data revision cannot be smuggled through any required domain."""

    future_time = "2026-01-02T08:00:01+00:00"
    records_by_domain = {
        name: [
            _feature(
                name,
                f"{name}-future-{temporal_field}",
                available_at=(future_time if temporal_field == "available_at" else "2026-01-02T08:00:00+00:00"),
                ingested_at=(future_time if temporal_field == "ingested_at" else None),
                value=(
                    {"close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0, "volume": 1_000}
                    if name == "prices"
                    else None
                ),
            )
        ]
        for name in ("prices", "financials", "flows", "events")
    }
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-02T08:00:00+00:00",
        records_by_domain=records_by_domain,
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.exact_replay_eligible is False
    assert data.manifest.coverage[domain]["excluded_after_as_of"] == 1
    assert f"pit_required_domain_missing:{domain}" in data.manifest.blockers
    assert data.replay_rows == ()


def test_pit_dataset_reports_missing_source_streams_and_intelligence_without_guessing():
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-02T08:00:00+00:00",
        records_by_domain={"prices": [_feature("prices", "price")]},
    )

    assert data.manifest.exact_replay_eligible is False
    assert "pit_required_domain_missing:financials" in data.manifest.blockers
    assert "pit_required_domain_missing:flows" in data.manifest.blockers
    assert "pit_required_domain_missing:events" in data.manifest.blockers
    assert "pit_intelligence_missing" in data.manifest.blockers
    assert data.replay_rows == ()


def test_pit_dataset_rejects_a_feature_without_an_explicit_production_contract():
    uncontracted = replace(
        _feature("financials", "uncontracted"),
        source_id="twse_openapi",
        dataset_id="unreviewed_feature_dataset",
        availability_basis="source_default",
        historical_pit_eligible=False,
        availability_reason="availability_contract_unverified",
        transformation_sha="1" * 64,
        availability_contract_sha256="0" * 64,
        production_contract_covered=False,
    )
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [_feature("prices", "price", value={"close": 100.0})],
            "financials": [uncontracted],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
        universe_records=_historical_universe(),
    )

    assert data.manifest.exact_replay_eligible is False
    assert "pit_production_contract_missing:twse_openapi:unreviewed_feature_dataset" in data.manifest.blockers


def test_pit_dataset_refuses_exact_replay_without_a_known_historical_universe():
    data = PointInTimeDatasetBuilder().build_from_records(
        entity_id="EQ-2330",
        as_of="2026-01-04T08:00:00+00:00",
        records_by_domain={
            "prices": [_feature("prices", "price", value={"close": 100.0, "volume": 1_000})],
            "financials": [_feature("financials", "financial")],
            "flows": [_feature("flows", "flows")],
            "events": [_feature("events", "events")],
        },
        intelligence_records=[_intelligence()],
    )

    assert data.manifest.exact_replay_eligible is False
    assert "pit_historical_universe_unavailable" in data.manifest.blockers


def test_live_market_snapshot_does_not_scan_the_historical_warehouse():
    class Platform:
        @property
        def warehouse(self):
            raise AssertionError("the live UI must not materialize a replay dataset")

        @staticmethod
        def resolve_entity(_symbol: str, *, identifier_type: str) -> dict:
            assert identifier_type == "display_symbol"
            return {"status": "resolved", "entity": {"entity_id": "EQ-2330"}}

    class Gateway:
        platform = Platform()

    receipt = MarketDataHub._point_in_time_dataset(gateway=Gateway(), symbol="2330.TW")

    assert receipt["exact_replay_eligible"] is False
    assert "pit_intelligence_missing" in receipt["blockers"]
    assert "pit_replay_rows_unavailable" in receipt["blockers"]
