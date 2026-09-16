from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from stock_ai.data_platform.api import (
    cache_status,
    create_revision_snapshot,
    daily_data_quality,
    data_sources,
    entity_lifecycle,
    entity_registry_status,
    ingestion_run,
    ingestion_runs,
    invalidate_dataset_cache,
    query_data,
    query_standard_warehouse,
    quality_report_detail,
    quality_reports,
    reconciliation_conflicts,
    reconciliation_run,
    reconciliation_runs,
    reconciliation_status,
    revision_history,
    revision_snapshot,
    run_reconciliation,
    dataset_cache_status,
    resolve_entity_identifier,
    security_lifecycle_summary,
    source_failover_run,
    source_failover_runs,
    source_failover_status,
    source_observability,
)
from stock_ai.data_platform.contracts import (
    DataQuery,
    EntityRecord,
    FailoverObservation,
    SourceFetchPayload,
    TemporalCoordinates,
)
from stock_ai.data_platform.failover import SourceFailoverExhausted, SourceFetchError
from stock_ai.data_platform.incremental import IncrementalLoader
from stock_ai.data_platform.security_loader import OfficialSecurityMasterLoader
from stock_ai.data_platform.gateway import UnifiedResearchDataGateway
from stock_ai.data_platform.service import (
    MarketDataPlatform,
    get_market_data_platform,
    stable_entity_id,
)
from stock_ai.data_platform.warehouse import content_hash


def _platform(tmp_path) -> MarketDataPlatform:
    return MarketDataPlatform(database_path=tmp_path / "market-data.sqlite")


def test_comprehensive_upgrade_ledger_tracks_every_numbered_requirement() -> None:
    root = Path(__file__).resolve().parents[1]
    ledger = yaml.safe_load(
        (root / "config" / "comprehensive_upgrade_status.yaml").read_text(encoding="utf-8")
    )
    expected_counts = {
        "phase_1": 15,
        "phase_2": 46,
        "phase_3": 18,
        "phase_4": 20,
        "phase_5": 23,
        "phase_6": 25,
        "phase_7": 37,
        "phase_8": 10,
        "phase_9": 15,
        "phase_10": 20,
    }

    assert {
        phase: len(ledger[phase])
        for phase in expected_counts
    } == expected_counts
    statuses = {
        status
        for phase in expected_counts
        for status in ledger[phase].values()
    }
    assert statuses <= {"complete", "partial", "unverified"}
    assert sum(expected_counts.values()) == 229
    assert ledger["phase_1"]["DATA-001"] == "complete"
    assert ledger["phase_1"]["DATA-002"] == "complete"
    assert ledger["phase_1"]["DATA-003"] == "complete"
    assert set(ledger["phase_1"].values()) == {"complete"}
    assert ledger["phase_2"]["STOCK-001"] == "complete"
    assert ledger["phase_2"]["STOCK-002"] == "complete"
    assert ledger["phase_2"]["STOCK-003"] == "complete"
    assert ledger["phase_2"]["STOCK-004"] == "complete"
    assert ledger["phase_2"]["STOCK-005"] == "complete"
    assert ledger["phase_2"]["STOCK-006"] == "complete"
    assert ledger["phase_2"]["STOCK-007"] == "complete"
    assert ledger["phase_2"]["STOCK-008"] == "complete"
    assert ledger["phase_2"]["FIN-001"] == "complete"
    assert ledger["phase_2"]["FIN-002"] == "complete"
    assert all(
        status == "unverified"
        for phase in tuple(expected_counts)[1:]
        for requirement, status in ledger[phase].items()
        if (phase, requirement)
        not in {
            ("phase_2", "STOCK-001"),
            ("phase_2", "STOCK-002"),
            ("phase_2", "STOCK-003"),
            ("phase_2", "STOCK-004"),
            ("phase_2", "STOCK-005"),
            ("phase_2", "STOCK-006"),
            ("phase_2", "STOCK-007"),
            ("phase_2", "STOCK-008"),
            ("phase_2", "FIN-001"),
            ("phase_2", "FIN-002"),
            ("phase_2", "FIN-003"),
            ("phase_2", "FIN-004"),
            ("phase_2", "FIN-005"),
            ("phase_2", "FIN-006"),
            ("phase_2", "FIN-007"),
            ("phase_2", "FIN-008"),
            ("phase_2", "FIN-009"),
            ("phase_2", "FIN-010"),
            ("phase_2", "VAL-001"),
            ("phase_2", "VAL-002"),
            ("phase_2", "VAL-003"),
            ("phase_2", "VAL-004"),
            ("phase_2", "VAL-005"),
            ("phase_2", "VAL-006"),
            ("phase_2", "VAL-007"),
            ("phase_2", "VAL-008"),
            ("phase_2", "CHIP-001"),
            ("phase_2", "CHIP-002"),
            ("phase_2", "CHIP-003"),
            ("phase_2", "CHIP-004"),
        }
    )


def test_data_platform_and_open_stock_ai_import_in_fresh_process() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from stock_ai.data_platform import MarketDataPlatform;"
                "from open_stock_ai.data.market_data_hub import MarketDataHub;"
                "print(MarketDataPlatform.__name__, MarketDataHub.__name__)"
            ),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MarketDataPlatform MarketDataHub"


def test_market_data_migration_creates_point_in_time_warehouse(tmp_path) -> None:
    platform = _platform(tmp_path)

    with sqlite3.connect(platform.warehouse.path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
        version = conn.execute("pragma user_version").fetchone()[0]

    assert version == LATEST_SCHEMA_VERSION == 47
    assert {
        "data_sources",
        "market_entities",
        "entity_identifiers",
        "raw_data_payloads",
        "raw_data_objects",
        "raw_payload_objects",
        "raw_reprocessing_runs",
        "market_prices",
        "financial_facts",
        "ownership_flows",
        "market_events",
        "macro_observations",
        "data_revisions",
        "data_lineage_edges",
        "data_lineage_artifacts",
        "data_artifact_lineage_edges",
        "data_ingestion_checkpoints",
        "data_ingestion_runs",
        "data_ingestion_batches",
        "data_revision_snapshots",
        "data_revision_snapshot_items",
        "data_quality_reports",
        "data_quality_issues",
        "data_cache_entries",
        "data_cache_invalidations",
        "data_cache_refresh_leases",
        "data_source_failover_runs",
        "data_source_failover_attempts",
        "data_reconciliation_runs",
        "data_reconciliation_conflicts",
        "entity_lifecycle_events",
        "entity_identity_merges",
    }.issubset(tables)
    with sqlite3.connect(platform.warehouse.path) as conn:
        identifier_columns = {
            row[1]
            for row in conn.execute("pragma table_info(entity_identifiers)").fetchall()
        }
    assert {
        "normalized_value",
        "confidence",
        "is_primary",
        "superseded_by_entity_id",
        "superseded_at",
    }.issubset(identifier_columns)
    with sqlite3.connect(platform.warehouse.path) as conn:
        revision_columns = {
            row[1]
            for row in conn.execute("pragma table_info(data_revisions)").fetchall()
        }
    assert {
        "field_provenance_json",
        "temporal_contract_version",
        "time_basis",
        "trade_date",
        "fiscal_period",
        "period_start",
        "period_end",
    }.issubset(revision_columns)


def test_standard_market_warehouse_projects_every_research_domain(tmp_path) -> None:
    platform = _platform(tmp_path)
    temporal = TemporalCoordinates(
        available_at="2026-07-25T08:00:00+00:00",
        acquired_at="2026-07-25T08:01:00+00:00",
        effective_at="2026-07-25T00:00:00+00:00",
    )
    examples = {
        "prices_daily": ("twse_openapi", {"date": "2026-07-25", "close": 1150.0}),
        "revenues_monthly": ("mops", {"period": "2026-06", "current_revenue": 1000}),
        "institutional_flows": (
            "twse_openapi",
            {"trade_date": "2026-07-25", "foreign_net": 1200},
        ),
        "events": (
            "mops",
            {"event_id": "EV-1", "event_time": "2026-07-25", "title": "重大訊息"},
        ),
        "macro_series": (
            "yahoo_finance",
            {"series_id": "US10Y", "period": "2026-07-25", "value": 4.1},
        ),
    }

    revision_ids = {}
    for dataset, (source_id, payload) in examples.items():
        revision = platform.warehouse.write_revision(
            dataset=dataset,
            entity_id=f"ENT-{dataset}",
            observation_key="2026-07-25",
            source_id=source_id,
            temporal=temporal,
            payload=payload,
            raw_payload_id=None,
        )
        revision_ids[dataset] = revision.revision_id

    expected = {
        "prices": ("market_prices", "prices_daily"),
        "financials": ("financial_facts", "revenues_monthly"),
        "flows": ("ownership_flows", "institutional_flows"),
        "events": ("market_events", "events"),
        "macro": ("macro_observations", "macro_series"),
    }
    for domain, (table_name, dataset) in expected.items():
        records = platform.warehouse.standard_records(domain, dataset=dataset)
        assert len(records) == 1
        assert records[0]["revision_id"] == revision_ids[dataset]
        assert records[0]["record"] == examples[dataset][1]
        with sqlite3.connect(platform.warehouse.path) as conn:
            assert conn.execute(f"select count(*) from {table_name}").fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(
                    f"update {table_name} set quality_status='invalid'"
                )

    status = platform.warehouse.status()["standard_warehouse"]
    assert status["record_count"] == 5
    assert set(status["domains"]) == set(expected)


def test_standard_market_warehouse_api_uses_the_shared_read_path(
    tmp_path,
    monkeypatch,
) -> None:
    platform = _platform(tmp_path)
    revision = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-api-price",
        observation_key="2026-07-25",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            available_at="2026-07-25",
            acquired_at="2026-07-25",
            effective_at="2026-07-25",
        ),
        payload={"date": "2026-07-25", "close": 1150.0},
        raw_payload_id=None,
    )
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )

    payload = query_standard_warehouse("prices", dataset="prices_daily", limit=100)

    assert payload["schema_version"] == "stock_ai.standard_warehouse_query.v1"
    assert payload["count"] == 1
    assert payload["items"][0]["revision_id"] == revision.revision_id


def test_concurrent_standard_ingestion_batches_are_serialized(tmp_path: Path) -> None:
    platform = _platform(tmp_path)
    second_platform = MarketDataPlatform(database_path=platform.warehouse.path)
    timestamp = "2026-07-25T08:00:00+00:00"

    def ingest(batch: int) -> int:
        rows = [
            {
                "date": "2026-07-25",
                "symbol": f"{batch}{index:03d}.TW",
                "close": float(index),
            }
            for index in range(20)
        ]
        return len(
            (platform if batch % 2 == 0 else second_platform).ingest_records(
                source_id="twse_openapi",
                dataset="prices_daily",
                records=rows,
                entity_id_for=lambda row: f"ENT-{row['symbol']}",
                observation_key_for=lambda row: str(row["date"]),
                available_at_for=lambda _row: timestamp,
                effective_at_for=lambda _row: timestamp,
                trade_date_for=lambda row: str(row["date"]),
            )
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(ingest, range(4))) == [20, 20, 20, 20]

    assert len(platform.standard_query(domain="prices", limit=100)) == 80


def test_schema_v17_backfills_standard_domain_tables(tmp_path) -> None:
    platform = _platform(tmp_path)
    revision = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-v17-price",
        observation_key="2026-07-25",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            available_at="2026-07-25",
            acquired_at="2026-07-25",
            effective_at="2026-07-25",
        ),
        payload={"date": "2026-07-25", "close": 1150.0},
        raw_payload_id=None,
    )
    with sqlite3.connect(platform.warehouse.path) as conn:
        conn.execute("drop table market_prices")
        conn.execute("delete from schema_migrations where version>=17")
        conn.execute("pragma user_version=16")
        conn.commit()
        apply_migrations(conn)
        restored = conn.execute(
            "select revision_id, record_json from market_prices"
        ).fetchone()

    assert restored[0] == revision.revision_id
    assert json.loads(restored[1])["close"] == 1150.0


def test_raw_data_lake_preserves_exact_json_bytes_and_replays_cleaning(tmp_path) -> None:
    platform = _platform(tmp_path)
    payload = [{"Code": "2330", "Name": "台積電"}]
    raw_body = b'[ { "Code": "2330", "Name": "\\u53f0\\u7a4d\\u96fb" } ]\n'

    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=payload,
        request_url="https://openapi.twse.com.tw/v1/example",
        content_type="application/json; charset=utf-8",
        raw_body=raw_body,
        metadata={"dataset": "security_master"},
    )
    raw = platform.warehouse.raw_payload(raw_payload_id)
    replay = platform.warehouse.reprocess_raw_payload(
        raw_payload_id,
        dataset="security_master",
        code_version="test",
    )

    assert raw is not None
    assert raw["wire_hash"] == hashlib.sha256(raw_body).hexdigest()
    assert raw["byte_length"] == len(raw_body)
    assert raw["integrity_status"] == "passed"
    assert raw["metadata"]["capture_representation"] == "exact_source_bytes"
    assert replay["status"] == "passed"
    assert replay["input_wire_hash"] == raw["wire_hash"]
    assert replay["output_payload_hash"] == content_hash(payload)
    assert replay["output_record_count"] == 1

    with sqlite3.connect(platform.warehouse.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "update raw_data_objects set byte_length=0 where raw_object_id=?",
                (raw["raw_object_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "delete from raw_data_payloads where raw_payload_id=?",
                (raw_payload_id,),
            )


def test_raw_data_lake_replays_csv_with_audited_parser(tmp_path) -> None:
    platform = _platform(tmp_path)
    csv_text = "symbol,name\n2330,台積電\n2317,鴻海\n"
    payload = [
        {"symbol": "2330", "name": "台積電"},
        {"symbol": "2317", "name": "鴻海"},
    ]
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=payload,
        request_url="https://example.invalid/security-master.csv",
        content_type="text/csv",
        raw_body=csv_text,
        metadata={"dataset": "security_master"},
    )

    replay = platform.warehouse.reprocess_raw_payload(raw_payload_id)
    status = platform.warehouse.status()

    assert replay["parser_id"] == "stock_ai.raw.csv.v1"
    assert replay["status"] == "passed"
    assert replay["output_record_count"] == 2
    assert status["tables"]["raw_objects"] == 1
    assert status["tables"]["raw_reprocessing_runs"] == 1
    assert status["raw_data_lake"]["immutable"] is True


def test_raw_cleaning_replay_waits_for_a_concurrent_warehouse_writer(tmp_path) -> None:
    platform = _platform(tmp_path)
    payload = [{"symbol": "2330"}]
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=payload,
        raw_body=json.dumps(payload),
    )
    locked = threading.Event()

    def hold_write_lock() -> None:
        with sqlite3.connect(platform.warehouse.path) as conn:
            conn.execute("begin immediate")
            locked.set()
            time.sleep(0.25)
            conn.commit()

    writer = threading.Thread(target=hold_write_lock)
    writer.start()
    assert locked.wait(timeout=2)
    replay = platform.warehouse.reprocess_raw_payload(raw_payload_id)
    writer.join(timeout=2)

    assert replay["status"] == "passed"


def test_global_market_data_platform_initialization_is_single_flight(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STOCK_AI_MARKET_DATA_DB", str(tmp_path / "single-flight.sqlite"))
    get_market_data_platform.cache_clear()
    barrier = threading.Barrier(6)
    instances: list[MarketDataPlatform] = []

    def resolve() -> None:
        barrier.wait()
        instances.append(get_market_data_platform())

    workers = [threading.Thread(target=resolve) for _ in range(6)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        assert len(instances) == 6
        assert len({id(item) for item in instances}) == 1
    finally:
        get_market_data_platform.cache_clear()


def test_schema_v14_backfills_field_provenance_for_existing_revisions(tmp_path) -> None:
    platform = _platform(tmp_path)
    temporal = TemporalCoordinates(
        available_at="2026-07-20T01:00:00+00:00",
        acquired_at="2026-07-20T01:01:00+00:00",
        effective_at="2026-07-20",
    )
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload={"close": 99.5, "metrics": {"volume": 800}},
        received_at=temporal.acquired_at,
    )
    revision = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-migration-backfill",
        observation_key="2026-07-20",
        source_id="twse_openapi",
        temporal=temporal,
        payload={"close": 99.5, "metrics": {"volume": 800}},
        raw_payload_id=raw_payload_id,
    )

    with sqlite3.connect(platform.warehouse.path) as conn:
        conn.execute("drop trigger if exists trg_data_revisions_immutable_update")
        conn.execute(
            "update data_revisions set field_provenance_json='{}' where revision_id=?",
            (revision.revision_id,),
        )
        conn.execute("delete from schema_migrations where version=14")
        conn.execute("pragma user_version=13")
        conn.commit()
        apply_migrations(conn)
        stored = conn.execute(
            "select field_provenance_json, created_at from data_revisions where revision_id=?",
            (revision.revision_id,),
        ).fetchone()

    provenance = json.loads(stored[0])
    assert set(provenance) == {"/close", "/metrics/volume"}
    assert provenance["/close"]["source_id"] == "twse_openapi"
    assert provenance["/close"]["updated_at"] == stored[1]
    assert provenance["/close"]["quality_status"] == "valid"


def test_schema_v15_backfills_trade_and_fiscal_dimensions(tmp_path) -> None:
    platform = _platform(tmp_path)
    trade_temporal = TemporalCoordinates(
        available_at="2026-07-20T01:00:00+00:00",
        acquired_at="2026-07-20T01:01:00+00:00",
        effective_at="2026-07-20",
    )
    trade_revision = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-v15-trade",
        observation_key="2026-07-20",
        source_id="twse_openapi",
        temporal=trade_temporal,
        payload={"date": "2026-07-20", "close": 99.5},
        raw_payload_id=None,
    )
    fiscal_revision = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-v15-fiscal",
        observation_key="2026-05-10",
        source_id="mops",
        temporal=TemporalCoordinates(
            available_at="2026-05-10",
            acquired_at="2026-05-11",
            effective_at="2026-05-10",
        ),
        payload={
            "period": "2026-03",
            "report_date": "2026-05-10",
            "current_revenue": 123,
        },
        raw_payload_id=None,
    )

    with sqlite3.connect(platform.warehouse.path) as conn:
        conn.execute("drop trigger if exists trg_data_revisions_immutable_update")
        conn.execute(
            """
            update data_revisions
               set temporal_contract_version='', time_basis='snapshot',
                   trade_date=null, fiscal_period=null,
                   period_start=null, period_end=null
            """
        )
        conn.execute("delete from schema_migrations where version=15")
        conn.execute("pragma user_version=14")
        conn.commit()
        apply_migrations(conn)
        trade = conn.execute(
            """
            select temporal_contract_version, time_basis, trade_date
              from data_revisions where revision_id=?
            """,
            (trade_revision.revision_id,),
        ).fetchone()
        fiscal = conn.execute(
            """
            select temporal_contract_version, time_basis, fiscal_period,
                   period_start, period_end, published_at, effective_at
              from data_revisions where revision_id=?
            """,
            (fiscal_revision.revision_id,),
        ).fetchone()

    assert trade == (
        "stock_ai.temporal_contract.v1",
        "trade_date",
        "2026-07-20T00:00:00+00:00",
    )
    assert fiscal == (
        "stock_ai.temporal_contract.v1",
        "fiscal_period",
        "2026-03",
        "2026-03-01T00:00:00+00:00",
        "2026-03-31T00:00:00+00:00",
        "2026-05-10T00:00:00+00:00",
        "2026-03-31T00:00:00+00:00",
    )


def test_source_registry_is_centralized_and_declares_failover(tmp_path) -> None:
    registry = _platform(tmp_path).source_registry()

    assert registry["schema_version"] == "stock_ai.source_registry.v2"
    assert {item["source_id"] for item in registry["items"]} >= {
        "twse_openapi",
        "tpex_openapi",
        "mops",
        "tdcc",
        "tdcc_openapi",
        "taifex_openapi",
        "twse_mis",
        "yahoo_finance",
    }
    mis = next(item for item in registry["items"] if item["source_id"] == "twse_mis")
    assert mis["failover_source_ids"] == ["twse_openapi"]
    assert mis["field_contract"]["authorization_required_for_execution"] is True
    policies = {item["dataset"]: item for item in registry["cache_policies"]}
    assert policies["prices_intraday"]["ttl_seconds"] == 5
    assert "corporate_action" in policies["prices_daily"]["invalidate_on"]
    datasets = {item["dataset_id"]: item for item in registry["datasets"]}
    assert registry["dataset_count"] == len(datasets) >= 29
    assert datasets["twse_companies"]["endpoint_template"].endswith(
        "/v1/opendata/t187ap03_L"
    )
    assert datasets["twse_companies"]["required_fields"] == ["code", "legal_name"]
    assert datasets["twse_stock_day"]["failure_strategy"][
        "failover_dataset_ids"
    ] == ["twse_quotes"]
    assert datasets["twse_mis_quote"]["license_status"] == (
        "trading_information_contract_required"
    )
    assert datasets["fugle_intraday_quote"]["effective_reliability_tier"] == 1
    assert datasets["tdcc_holding_distribution"]["transport"] == "import_only"
    assert datasets["tdcc_holding_distribution_openapi"]["transport"] == "http_json"
    assert datasets["tdcc_holding_distribution_openapi"]["endpoint_template"].endswith(
        "/v1/opendata/1-5"
    )


def test_source_failover_retries_and_preserves_actual_source_identity(
    monkeypatch,
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    platform.source_failover_service.sleeper = lambda _seconds: None
    calls: list[str] = []
    acquired_at = "2026-07-27T03:00:00+00:00"

    def fetch(candidate, _url, _attempt):
        calls.append(candidate.source_id)
        if candidate.dataset_id == "twse_stock_day":
            raise SourceFetchError(
                "primary unavailable",
                http_status=503,
                response_payload={"error": "primary unavailable"},
            )
        return SourceFetchPayload(
            payload={"data": [{"symbol": "1111", "close": 42.5}]},
            metadata={"fixture": "reviewed failover"},
        )

    def normalize(payload, provenance):
        assert provenance["resolved_source_id"] in {
            "twse_official_web",
            "twse_openapi",
        }
        row = payload["data"][0]
        observation_acquired_at = provenance["acquired_at"]
        return [
            FailoverObservation(
                entity_id="ENT-example",
                observation_key="2026-07-27",
                temporal=TemporalCoordinates(
                    time_basis="trade_date",
                    trade_date="2026-07-27",
                    observed_at="2026-07-27",
                    available_at=observation_acquired_at,
                    acquired_at=observation_acquired_at,
                    effective_at="2026-07-27",
                ),
                payload={"symbol": row["symbol"], "close": row["close"]},
            )
        ]

    failover = platform.ingest_with_failover(
        dataset_id="twse_stock_day",
        normalized_dataset="prices_daily",
        fetch=fetch,
        normalize=normalize,
        acquired_at=acquired_at,
    )
    run = platform.source_failover_run(failover["run_id"])
    revisions = platform.query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-27T04:00:00+00:00",
        limit=10,
    )
    preferred_fallback = platform.preferred_query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-27T04:00:00+00:00",
    )

    assert calls == [
        "twse_official_web",
        "twse_official_web",
        "twse_official_web",
        "twse_openapi",
    ]
    assert failover["requested_source_id"] == "twse_official_web"
    assert failover["selected_source_id"] == "twse_openapi"
    assert failover["is_failover"] is True
    assert failover["source_difference_preserved"] is True
    assert run is not None
    assert [item["source_id"] for item in run["attempts"]] == calls
    assert [item["status"] for item in run["attempts"]] == [
        "failed",
        "failed",
        "failed",
        "succeeded",
    ]
    assert all(
        platform.warehouse.raw_payload(item["raw_payload_id"])["source_id"]
        == item["source_id"]
        for item in run["attempts"]
    )
    assert len(revisions) == 1
    assert revisions[0].source_id == "twse_openapi"
    assert revisions[0].is_fallback is True
    assert {
        item.source_id for item in revisions[0].field_provenance.values()
    } == {"twse_openapi"}
    assert preferred_fallback["source_id"] == "twse_openapi"
    assert preferred_fallback["fallback_used"] is True

    primary = platform.ingest_with_failover(
        dataset_id="twse_stock_day",
        normalized_dataset="prices_daily",
        fetch=lambda _candidate, _url, _attempt: SourceFetchPayload(
            payload={"data": [{"symbol": "1111", "close": 42.0}]}
        ),
        normalize=normalize,
        acquired_at="2026-07-27T03:05:00+00:00",
    )
    preferred_primary = platform.preferred_query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-27T04:00:00+00:00",
    )

    assert primary["selected_source_id"] == "twse_official_web"
    assert primary["is_failover"] is False
    assert {
        item.source_id
        for item in platform.query(
            dataset="prices_daily",
            entity_id="ENT-example",
            as_of="2026-07-27T04:00:00+00:00",
            limit=10,
        )
    } == {"twse_official_web", "twse_openapi"}
    assert preferred_primary["source_id"] == "twse_official_web"
    assert preferred_primary["fallback_used"] is False

    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    api_status = source_failover_status()
    api_runs = source_failover_runs(limit=10)
    api_run = source_failover_run(failover["run_id"])
    assert api_status["run_count"] == 2
    assert api_status["failover_count"] == 1
    assert api_status["source_identity_preserved"] is True
    assert api_runs["count"] == 2
    assert api_run["item"]["selected_source_id"] == "twse_openapi"
    with sqlite3.connect(platform.warehouse.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "update data_source_failover_attempts set status='failed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("delete from data_source_failover_attempts")


def test_source_failover_fails_closed_without_an_unreviewed_backup(tmp_path) -> None:
    platform = _platform(tmp_path)
    platform.source_failover_service.sleeper = lambda _seconds: None
    calls: list[str] = []

    def fetch(candidate, _url, _attempt):
        calls.append(candidate.source_id)
        raise SourceFetchError(
            "authorization denied",
            http_status=401,
            response_payload={"error": "authorization denied"},
        )

    with pytest.raises(SourceFailoverExhausted) as failure:
        platform.ingest_with_failover(
            dataset_id="twse_companies",
            normalized_dataset="security_master",
            fetch=fetch,
            normalize=lambda _payload, _provenance: [],
        )

    run = platform.source_failover_run(failure.value.run_id)
    assert calls == ["twse_openapi"]
    assert run is not None
    assert run["status"] == "failed"
    assert run["selected_source_id"] is None
    assert run["attempt_count"] == 1
    assert run["attempts"][0]["http_status"] == 401
    assert run["attempts"][0]["source_id"] == "twse_openapi"


def test_source_observability_exposes_slo_lag_missing_partitions_and_revision_alerts(
    monkeypatch,
    tmp_path,
) -> None:
    rules_path = tmp_path / "observability.yaml"
    rules_path.write_text(
        """
schema_version: stock_ai.data_observability_rules.v1
defaults:
  window_hours: 24
  success_rate_minimum: 0.95
  lag_multiplier: 3
  revision_anomaly_minimum: 3
partition_expectations:
  - source_id: twse_openapi
    dataset: prices_daily
    partition_keys: [all, 2026-08-20]
""".strip(),
        encoding="utf-8",
    )
    platform = MarketDataPlatform(
        database_path=tmp_path / "market-data.sqlite",
        observability_rules_path=rules_path,
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_text = now.isoformat()
    run_id = platform.warehouse.start_source_failover_run(
        requested_dataset_id="twse_quotes",
        requested_source_id="twse_openapi",
        normalized_dataset="prices_daily",
        policy={"fixture": "source-observability"},
        started_at=now_text,
    )
    platform.warehouse.record_source_failover_attempt(
        run_id=run_id,
        sequence=1,
        dataset_id="twse_quotes",
        source_id="twse_openapi",
        failover_depth=0,
        attempt_number=1,
        request_url="https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        status="failed",
        http_status=503,
        error={"type": "SourceFetchError", "message": "fixture outage"},
        started_at=now_text,
        completed_at=now_text,
    )
    platform.warehouse.finish_source_failover_run(
        run_id,
        status="failed",
        attempt_count=1,
        error={"type": "SourceFailoverExhausted", "message": "fixture outage"},
        completed_at=now_text,
    )
    platform.warehouse.save_checkpoint(
        source_id="twse_openapi",
        dataset="prices_daily",
        partition_key="all",
        status="succeeded",
    )
    temporal = TemporalCoordinates(
        time_basis="trade_date",
        trade_date="2026-08-20",
        observed_at="2026-08-20",
        published_at=now_text,
        available_at=now_text,
        acquired_at=now_text,
        effective_at="2026-08-20",
    )
    for close in (100.0, 101.0, 102.0, 103.0):
        platform.warehouse.write_revision(
            dataset="prices_daily",
            entity_id="ENT-observability",
            observation_key="2026-08-20",
            source_id="twse_openapi",
            temporal=temporal,
            payload={"trade_date": "2026-08-20", "close": close},
            raw_payload_id=None,
        )

    inspection_time = (now + timedelta(seconds=10)).isoformat()
    dashboard = platform.source_observability(as_of=inspection_time)
    item = next(
        entry
        for entry in dashboard["sources"]
        if entry["source_id"] == "twse_openapi" and entry["dataset"] == "prices_daily"
    )
    assert dashboard["schema_version"] == "stock_ai.source_observability_dashboard.v1"
    assert dashboard["automatic_alert_evaluation"] is True
    assert dashboard["notification_delivery_configured"] is False
    assert item["slo_status"] == "failed"
    assert item["lag_status"] == "passed"
    assert item["missing_partitions"] == [
        {
            "partition_key": "2026-08-20",
            "status": "missing",
            "last_success_at": None,
        }
    ]
    assert item["revision_status"] == "warning"
    assert {alert["code"] for alert in dashboard["alerts"]} >= {
        "source_slo_breach",
        "missing_partition",
        "revision_anomaly",
    }

    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    assert source_observability(as_of=inspection_time)["alerts"] == dashboard["alerts"]


def test_operator_data_platform_status_defers_expensive_field_json_scan(tmp_path) -> None:
    platform = _platform(tmp_path)
    temporal = TemporalCoordinates(
        time_basis="trade_date",
        trade_date="2026-08-20",
        observed_at="2026-08-20",
        published_at="2026-08-20T01:00:00+00:00",
        available_at="2026-08-20T01:00:00+00:00",
        acquired_at="2026-08-20T02:00:00+00:00",
        effective_at="2026-08-20",
    )
    platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-status-summary",
        observation_key="2026-08-20",
        source_id="twse_openapi",
        temporal=temporal,
        payload={"trade_date": "2026-08-20", "close": 100.0},
        raw_payload_id=None,
    )
    summary = platform.status(operations_summary=True)
    full = platform.status()
    assert summary["source_registry"]["schema_version"] == "stock_ai.source_registry_summary.v1"
    assert summary["warehouse"]["tables"]["traced_fields"] is None
    assert summary["warehouse"]["tables"]["untraced_revisions"] is None
    assert full["warehouse"]["tables"]["traced_fields"] > 0


def test_market_data_readers_do_not_own_upstream_endpoint_hosts() -> None:
    root = Path(__file__).resolve().parents[1]
    production_readers = [
        "src/stock_ai/taiwan_official.py",
        "src/stock_ai/phase1_data.py",
        "src/stock_ai/official_derivatives.py",
        "src/stock_ai/official_events.py",
        "src/stock_ai/twse_openapi.py",
        "src/stock_ai/realtime_quotes.py",
        "src/stock_ai/intraday_candles.py",
        "src/stock_ai/realtime_data.py",
        "src/stock_ai/services.py",
        "src/stock_ai/data_platform/security_loader.py",
        "src/stock_ai/data_platform/security_lifecycle.py",
        "src/stock_ai/data_platform/service.py",
    ]
    forbidden_hosts = (
        "openapi.twse.com.tw",
        "www.twse.com.tw",
        "www.tpex.org.tw",
        "mops.twse.com.tw",
        "openapi.taifex.com.tw",
        "mis.twse.com.tw",
        "api.fugle.tw",
        "finance.yahoo.com",
        "news.google.com",
    )

    violations = {
        relative: host
        for relative in production_readers
        for host in forbidden_hosts
        if host in (root / relative).read_text(encoding="utf-8")
    }
    assert violations == {}


def test_security_master_uses_internal_entity_ids_and_raw_lineage(tmp_path) -> None:
    platform = _platform(tmp_path)
    result = platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "1111",
                "公司簡稱": "甲",
                "公司名稱": "甲股份有限公司",
                "產業別": "01",
                "上市日期": "1150102",
            }
        ],
        twse_quotes=[{"Code": "1111", "Name": "甲"}],
        tpex_companies=[
            {
                "SecuritiesCompanyCode": "2222",
                "CompanyAbbreviation": "乙",
                "CompanyName": "乙股份有限公司",
                "SecuritiesIndustryCode": "02",
                "DateOfListing": "20260103",
            }
        ],
        tpex_quotes=[{"SecuritiesCompanyCode": "2222", "CompanyName": "乙"}],
        acquired_at="2026-07-24T03:00:00+00:00",
    )

    items = platform.securities(limit=10)
    by_symbol = {item["symbol"]: item for item in items}
    assert result["count"] == 2
    assert by_symbol["1111.TW"]["entity_id"].startswith("ENT-")
    assert ".TW" not in by_symbol["1111.TW"]["entity_id"]
    assert by_symbol["2222.TWO"]["entity_id"].startswith("ENT-")
    revisions = platform.query(dataset="security_master", limit=10)
    assert len(revisions) == 2
    lineage = platform.warehouse.lineage(revisions[0].revision_id)
    assert lineage["raw_payload"]["payload_hash"]
    assert lineage["edges"][0]["transformation_id"] == "stock_ai.security_lifecycle_normalizer.v2"


def test_complete_lineage_traces_conclusion_through_indicator_to_raw_sources(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    revisions = []
    closes = [100.0, 105.0, 110.0]
    for index, close in enumerate(closes, start=1):
        timestamp = f"2026-07-{20 + index:02d}T08:00:00+00:00"
        raw_payload_id = platform.warehouse.record_raw_payload(
            source_id="twse_openapi",
            payload={"date": timestamp[:10], "close": close},
            request_url=f"https://openapi.twse.com.tw/verify/{index}",
            requested_at=timestamp,
            received_at=timestamp,
            metadata={"dataset": "prices_daily"},
        )
        revisions.append(
            platform.warehouse.write_revision(
                dataset="prices_daily",
                entity_id="ENT-LINEAGE",
                observation_key=timestamp[:10],
                source_id="twse_openapi",
                temporal=TemporalCoordinates(
                    available_at=timestamp,
                    acquired_at=timestamp,
                    effective_at=timestamp,
                ),
                payload={"close": close},
                raw_payload_id=raw_payload_id,
                transformation_id="stock_ai.price_normalizer.v1",
                code_version="test-data-015",
            )
        )

    indicator = platform.warehouse.record_lineage_artifact(
        artifact_type="indicator",
        name="sma_3",
        entity_id="ENT-LINEAGE",
        observation_key="2026-07-23",
        value={"value": 105.0, "window": 3},
        inputs=[
            {
                "kind": "revision",
                "id": revision.revision_id,
                "role": f"close_{index}",
                "fields": ["/close"],
            }
            for index, revision in enumerate(revisions, start=1)
        ],
        transformation_id="stock_ai.indicator.sma.v1",
        code_version="test-data-015",
        parameters={"window": 3},
    )
    conclusion = platform.warehouse.record_lineage_artifact(
        artifact_type="conclusion",
        name="price_above_sma_3",
        entity_id="ENT-LINEAGE",
        observation_key="2026-07-23",
        value={"state": "above", "difference": 5.0},
        inputs=[
            {
                "kind": "revision",
                "id": revisions[-1].revision_id,
                "role": "latest_close",
                "fields": ["/close"],
            },
            {
                "kind": "artifact",
                "id": indicator["artifact_id"],
                "role": "baseline",
                "fields": ["/value"],
            },
        ],
        transformation_id="stock_ai.conclusion.price_vs_sma.v1",
        code_version="test-data-015",
        parameters={"comparison": "close > sma_3"},
    )

    graph = platform.warehouse.lineage_graph(conclusion["artifact_id"])
    assert graph["target"]["artifact_type"] == "conclusion"
    assert graph["completeness"]["status"] == "complete"
    assert graph["completeness"]["raw_payload_count"] == 3
    assert graph["completeness"]["source_count"] == 1
    assert graph["sources"] == ["twse_openapi"]
    assert {
        "stock_ai.price_normalizer.v1",
        "stock_ai.indicator.sma.v1",
        "stock_ai.conclusion.price_vs_sma.v1",
    }.issubset(graph["transformation_ids"])
    assert {
        node["type"] for node in graph["nodes"]
    } == {"source", "raw_payload", "revision", "artifact"}
    assert platform.warehouse.status()["data_lineage"]["status"] == "complete"

    untraced = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-UNTRACED",
        observation_key="2026-07-23",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            available_at="2026-07-23T08:00:00+00:00",
            acquired_at="2026-07-23T08:00:00+00:00",
            effective_at="2026-07-23T08:00:00+00:00",
        ),
        payload={"close": 1.0},
        raw_payload_id=None,
    )
    with pytest.raises(ValueError, match="does not reach verified raw data"):
        platform.warehouse.record_lineage_artifact(
            artifact_type="conclusion",
            name="must_fail_closed",
            value={"state": "unknown"},
            inputs=[
                {
                    "kind": "revision",
                    "id": untraced.revision_id,
                    "fields": ["/close"],
                }
            ],
            transformation_id="stock_ai.conclusion.invalid.v1",
        )

    with sqlite3.connect(platform.warehouse.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "update data_lineage_artifacts set name='mutated' where artifact_id=?",
                (conclusion["artifact_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "delete from data_artifact_lineage_edges where output_artifact_id=?",
                (conclusion["artifact_id"],),
            )


def test_security_lifecycle_covers_venues_instruments_and_delisting(tmp_path) -> None:
    from test_catalogue_issue_identity import retain_catalogue

    platform = _platform(tmp_path)
    for dataset, code, name, isin, listed in (
        ("twse_isin_listed", "030012", "測試購01", "TW0000300120", "2026/01/02"),
        ("tpex_isin_otc", "72124U", "測試售01", "TW00072124U0", "2025/01/07"),
    ):
        retain_catalogue(platform, dataset_id=dataset,
            rows=[{"code": code, "name": name, "isin": isin, "listed_on": listed}],
            acquired_at="2026-07-24T03:00:00+00:00", source_updated_on="2026/07/24")
    result = platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "1111",
                "公司簡稱": "甲",
                "公司名稱": "甲股份有限公司",
                "營利事業統一編號": "12345678",
                "上市日期": "1150102",
            }
        ],
        twse_quotes=[{"Code": "1111", "Name": "甲"}],
        tpex_emerging_companies=[
            {
                "SecuritiesCompanyCode": "1111",
                "CompanyAbbreviation": "甲",
                "CompanyName": "甲股份有限公司",
                "UnifiedBusinessNo.": "12345678",
                "DateOfListing": "20200103",
            },
            {
                "SecuritiesCompanyCode": "3333",
                "CompanyAbbreviation": "丙",
                "CompanyName": "丙股份有限公司",
                "UnifiedBusinessNo.": "33333333",
                "DateOfListing": "20260104",
            },
        ],
        tpex_emerging_quotes=[
            {"SecuritiesCompanyCode": "3333", "CompanyName": "丙"}
        ],
        twse_etfs=[
            {
                "基金代號": "00999",
                "基金簡稱": "測試ETF",
                "基金中文名稱": "測試證券投資信託基金",
                "基金統一編號": "99999999",
                "基金類型": "ETF",
                "上市日期": "1150105",
            }
        ],
        twse_warrants=[
            {
                "權證代號": "030012",
                "權證簡稱": "測試購01",
                "權證類型": "認購",
                "履約開始日": "1150106",
                "履約截止日": "1160106",
            }
        ],
        tpex_warrants=[
            {
                "Code": "72124U",
                "Name": "測試售01",
                "ListedDate": "20250107",
                "ExpiryDate": "20260107",
                "Type": "認售",
            }
        ],
        twse_indices=[{"日期": "1150724", "指數": "測試加權指數"}],
        tpex_indices=[
            {
                "IndexCode": "TPEX",
                "IndexName": "櫃買指數",
                "Endpoint": "/tpex_index",
                "LatestObservation": {"Date": "20260724", "Close": "100"},
            }
        ],
        tpex_delisted=[
            {
                "Code": "1111",
                "Company": "甲股份有限公司",
                "DelistingDate": "114-12-31",
                "Reason": "轉上市",
            },
            {
                "Code": "4444",
                "Company": "丁股份有限公司",
                "DelistingDate": "114-12-30",
                "Reason": "終止上櫃",
            },
        ],
        acquired_at="2026-07-24T03:00:00+00:00",
    )

    assert result["by_entity_type"] == {
        "stock": 3,
        "etf": 1,
        "warrant": 2,
        "index": 2,
    }
    expected_listing_counts = {
        "listed": 1,
        "emerging": 2,
        "etf": 1,
        "warrant": 2,
        "index": 2,
        "delisted": 2,
    }
    assert all(
        result["lifecycle"]["by_listing_type"].get(key, 0) >= count
        for key, count in expected_listing_counts.items()
    )
    transferred = platform.securities(query="1111", limit=10)
    assert len(transferred) == 1
    assert transferred[0]["exchange"] == "TWSE"
    assert transferred[0]["trading_status"] == "active"
    history = platform.warehouse.entity_lifecycle(transferred[0]["entity_id"])
    assert {item["event_type"] for item in history["events"]} >= {
        "entered_emerging",
        "listed_twse",
        "delisted",
    }
    reused_code = platform.securities(query="4444", limit=10)
    assert reused_code[0]["trading_status"] == "delisted"
    assert platform.securities(market="emerging", limit=10)[0]["symbol"] == "3333.TWO"
    assert len(platform.securities(market="warrant", limit=10)) == 2
    assert len(platform.securities(market="index", limit=10)) == 2
    twse_warrant = platform.securities(query="030012", limit=10)[0]
    twse_warrant_profile = platform.warehouse.entity_profile(twse_warrant["entity_id"])
    assert twse_warrant_profile is not None
    assert {
        item["valid_from"]
        for item in twse_warrant_profile["identifiers"]
        if item["source_id"] == "twse_openapi"
    } == {"2026-01-01T16:00:00+00:00"}


def test_security_lifecycle_api_exposes_summary_and_history(monkeypatch, tmp_path) -> None:
    platform = _platform(tmp_path)
    platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "1111",
                "公司簡稱": "甲",
                "公司名稱": "甲股份有限公司",
                "上市日期": "1150102",
            }
        ],
        twse_quotes=[{"Code": "1111", "Name": "甲"}],
        acquired_at="2026-07-24T03:00:00+00:00",
    )
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )

    summary = security_lifecycle_summary()
    entity_id = platform.securities(limit=1)[0]["entity_id"]
    history = entity_lifecycle(entity_id)

    assert summary["event_count"] == 1
    assert summary["by_entity_type"]["stock"] == 1
    assert history["entity"]["entity_id"] == entity_id
    assert history["events"][0]["event_type"] == "listed_twse"


def test_security_identity_unifies_business_transfer_but_separates_code_reuse(tmp_path) -> None:
    platform = _platform(tmp_path)
    platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "2222",
                "公司簡稱": "甲新",
                "公司名稱": "甲股份有限公司",
                "營利事業統一編號": "12345678",
                "上市日期": "1150102",
            },
            {
                "公司代號": "5555",
                "公司簡稱": "新公司",
                "公司名稱": "新公司股份有限公司",
                "營利事業統一編號": "55555555",
                "上市日期": "1150103",
            },
        ],
        twse_quotes=[
            {"Code": "2222", "Name": "甲新"},
            {"Code": "5555", "Name": "新公司"},
        ],
        tpex_emerging_companies=[
            {
                "SecuritiesCompanyCode": "1111",
                "CompanyAbbreviation": "甲舊",
                "CompanyName": "甲股份有限公司",
                "UnifiedBusinessNo.": "12345678",
                "DateOfListing": "20200103",
            }
        ],
        tpex_delisted=[
            {
                "Code": "5555",
                "Company": "不同舊公司股份有限公司",
                "DelistingDate": "114-01-01",
                "Reason": "代號後續重用",
            }
        ],
        acquired_at="2026-07-24T03:00:00+00:00",
    )

    transferred_by_old = platform.securities(query="1111", limit=10)
    transferred_by_new = platform.securities(query="2222", limit=10)
    reused = platform.securities(query="5555", limit=10)

    assert transferred_by_old[0]["entity_id"] == transferred_by_new[0]["entity_id"]
    assert transferred_by_new[0]["trading_status"] == "active"
    assert len({item["entity_id"] for item in reused}) == 2
    assert {item["trading_status"] for item in reused} == {"active", "delisted"}


def test_etfs_from_same_issuer_keep_distinct_security_entities(tmp_path) -> None:
    platform = _platform(tmp_path)
    payload = {
        "twse_companies": [
            {
                "公司代號": "00643",
                "公司簡稱": "群益深證中小",
                "公司名稱": "群益深證中小基金",
                "營利事業統一編號": "42323777",
            },
            {
                "公司代號": "00643K",
                "公司簡稱": "群益深證中小+R",
                "公司名稱": "群益深證中小基金",
                "營利事業統一編號": "42323777",
            },
        ],
        "twse_quotes": [
            {"Code": "00643", "Name": "群益深證中小", "ClosingPrice": "19.29"},
            {"Code": "00643K", "Name": "群益深證中小+R", "ClosingPrice": "3.97"},
        ],
        "acquired_at": "2026-08-10T08:00:00+00:00",
    }
    first = platform.sync_security_master_payloads(**payload)
    first_plain = platform.resolve_entity("00643.TW")["entity"]
    first_renminbi = platform.resolve_entity("00643K.TW")["entity"]
    assert first_plain["entity_id"] != first_renminbi["entity_id"]

    second = platform.sync_security_master_payloads(
        **{**payload, "acquired_at": "2026-08-10T08:05:00+00:00"}
    )
    assert second["status"] == "unchanged"
    assert second["revision_count"] == 0
    assert platform.resolve_entity("00643.TW")["entity"]["entity_id"] == first_plain["entity_id"]
    assert (
        platform.resolve_entity("00643K.TW")["entity"]["entity_id"]
        == first_renminbi["entity_id"]
    )


def test_entity_registry_resolves_cross_source_aliases_and_historical_code_reuse(
    monkeypatch,
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "2222",
                "公司簡稱": "甲新",
                "公司名稱": "甲股份有限公司",
                "營利事業統一編號": "12345678",
                "上市日期": "1150102",
            },
            {
                "公司代號": "5555",
                "公司簡稱": "新公司",
                "公司名稱": "新公司股份有限公司",
                "營利事業統一編號": "55555555",
                "上市日期": "1150103",
            },
            {
                "公司代號": "7777",
                "公司簡稱": "上市同碼",
                "公司名稱": "上市同碼股份有限公司",
                "營利事業統一編號": "77777771",
                "上市日期": "1150104",
            },
            {
                "公司代號": "00888",
                "公司簡稱": "測試基金",
                "公司名稱": "測試基金簡稱",
                "上市日期": "1150106",
            },
            {
                "公司代號": "9101",
                "公司簡稱": "境外甲",
                "公司名稱": "境外甲有限公司",
                "營利事業統一編號": "00000000",
                "上市日期": "1150107",
            },
            {
                "公司代號": "9102",
                "公司簡稱": "境外乙",
                "公司名稱": "境外乙有限公司",
                "營利事業統一編號": "00000000",
                "上市日期": "1150108",
            },
        ],
        twse_quotes=[
            {"Code": "2222", "Name": "甲新"},
            {"Code": "5555", "Name": "新公司"},
            {"Code": "7777", "Name": "上市同碼"},
            {"Code": "00888", "Name": "測試基金"},
            {"Code": "9101", "Name": "境外甲"},
            {"Code": "9102", "Name": "境外乙"},
        ],
        twse_etfs=[
            {
                "基金代號": "00888",
                "基金簡稱": "測試基金",
                "基金中文名稱": "名稱格式不同之測試證券投資信託基金",
                "上市日期": "1150106",
            }
        ],
        tpex_companies=[
            {
                "SecuritiesCompanyCode": "7777",
                "CompanyAbbreviation": "上櫃同碼",
                "CompanyName": "上櫃同碼股份有限公司",
                "UnifiedBusinessNo.": "77777772",
                "DateOfListing": "20260105",
            }
        ],
        tpex_quotes=[
            {"SecuritiesCompanyCode": "7777", "CompanyName": "上櫃同碼"}
        ],
        tpex_emerging_companies=[
            {
                "SecuritiesCompanyCode": "1111",
                "CompanyAbbreviation": "甲舊",
                "CompanyName": "甲股份有限公司",
                "UnifiedBusinessNo.": "12345678",
                "DateOfListing": "20200103",
            }
        ],
        tpex_delisted=[
            {
                "Code": "5555",
                "Company": "不同舊公司股份有限公司",
                "DelistingDate": "114-01-01",
                "Reason": "代號後續重用",
            }
        ],
        acquired_at="2026-07-24T03:00:00+00:00",
    )

    old_alias = platform.resolve_entity(
        "1111",
        source_id="tpex_openapi",
        identifier_type="exchange_code",
    )
    new_alias = platform.resolve_entity(
        "2222.tw",
        source_id="twse_openapi",
        identifier_type="display_symbol",
    )
    business_alias = platform.resolve_entity(
        "12-345-678",
        identifier_type="unified_business_no",
    )
    assert old_alias["status"] == new_alias["status"] == business_alias["status"] == "resolved"
    assert {
        old_alias["entity"]["entity_id"],
        new_alias["entity"]["entity_id"],
        business_alias["entity"]["entity_id"],
    } == {new_alias["entity"]["entity_id"]}
    assert ".TW" not in new_alias["entity"]["entity_id"]

    current_reuse = platform.resolve_entity("5555", identifier_type="exchange_code")
    historical_reuse = platform.resolve_entity(
        "5555",
        identifier_type="exchange_code",
        as_of="2024-12-31",
    )
    assert current_reuse["status"] == "resolved"
    assert current_reuse["candidate_count"] == 1
    assert current_reuse["warnings"] == []
    assert current_reuse["entity"]["lifecycle_status"] == "active"
    assert historical_reuse["status"] == "resolved"
    assert historical_reuse["entity"]["lifecycle_status"] == "delisted"

    same_code_two_venues = platform.resolve_entity(
        "7777",
        identifier_type="exchange_code",
    )
    assert same_code_two_venues["status"] == "ambiguous"
    assert same_code_two_venues["candidate_count"] == 2
    assert same_code_two_venues["entity"] is None
    etf_alias = platform.resolve_entity(
        "00888.TW",
        identifier_type="display_symbol",
    )
    assert etf_alias["status"] == "resolved"
    assert etf_alias["candidate_count"] == 1
    assert etf_alias["entity"]["entity_type"] == "etf"
    etf_entity_id = etf_alias["entity"]["entity_id"]
    company_only_resync = platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "00888",
                "公司簡稱": "測試基金",
                "公司名稱": "測試基金簡稱",
                "上市日期": "1150106",
            }
        ],
        twse_quotes=[{"Code": "00888", "Name": "測試基金"}],
        acquired_at="2026-07-24T04:00:00+00:00",
    )
    assert company_only_resync["status"] == "succeeded"
    assert platform.resolve_entity("00888.TW")["entity"]["entity_id"] == etf_entity_id
    placeholder_a = platform.resolve_entity("9101.TW")
    placeholder_b = platform.resolve_entity("9102.TW")
    assert placeholder_a["status"] == placeholder_b["status"] == "resolved"
    assert placeholder_a["entity"]["entity_id"] != placeholder_b["entity"]["entity_id"]

    registry = platform.entity_registry.status()
    assert registry["status"] == "passed"
    assert registry["cross_source_entity_count"] >= 1
    assert registry["missing_normalized_identifier_count"] == 0
    assert registry["invalid_interval_count"] == 0

    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    api_status = entity_registry_status()
    api_resolution = resolve_entity_identifier(
        identifier="2222.TW",
        source_id=None,
        identifier_type="display_symbol",
        as_of=None,
    )
    assert api_status["identifier_count"] > 0
    assert api_resolution["entity"]["entity_id"] == new_alias["entity"]["entity_id"]


def test_entity_registry_reconciles_v12_duplicate_aliases_without_deleting_history(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    legacy_id = "ENT-" + "1" * 32
    canonical_id = "ENT-" + "2" * 32
    platform.warehouse.upsert_entity(
        EntityRecord(
            entity_id=legacy_id,
            entity_type="etf",
            canonical_name="基金簡稱",
            market="taiwan",
            exchange="TWSE",
            lifecycle_status="active",
            metadata={
                "display_symbol": "0050.TW",
                "source_datasets": ["twse_companies_quotes"],
            },
        ),
        identifiers=(
            {
                "source_id": "twse_openapi",
                "identifier_type": "display_symbol",
                "identifier_value": "0050.TW",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "exchange_code",
                "identifier_value": "0050",
            },
        ),
    )
    platform.warehouse.upsert_entity(
        EntityRecord(
            entity_id=canonical_id,
            entity_type="etf",
            canonical_name="元大台灣卓越50證券投資信託基金",
            market="taiwan",
            exchange="TWSE",
            lifecycle_status="active",
            metadata={
                "display_symbol": "0050.TW",
                "source_datasets": ["twse_etfs"],
            },
        ),
        identifiers=(
            {
                "source_id": "twse_openapi",
                "identifier_type": "display_symbol",
                "identifier_value": "0050.TW",
                "valid_from": "2003-06-30",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "exchange_code",
                "identifier_value": "0050",
                "valid_from": "2003-06-30",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "unified_business_no",
                "identifier_value": "25699144",
                "valid_from": "2003-06-30",
            },
        ),
    )

    before = platform.resolve_entity("0050.TW")
    assert before["status"] == "ambiguous"
    repair = platform.warehouse.reconcile_entity_registry_aliases(
        effective_at="2026-07-24T06:00:00+00:00",
    )
    after = platform.resolve_entity("0050.TW")
    registry = platform.entity_registry.status()

    assert repair["superseded_alias_count"] == 2
    assert repair["created_merge_count"] == 1
    assert after["status"] == "resolved"
    assert after["entity"]["entity_id"] == canonical_id
    assert registry["status"] == "passed"
    assert registry["physical_entity_count"] == 2
    assert registry["entity_count"] == 1
    assert registry["superseded_identifier_count"] == 2
    assert registry["identity_merge_count"] == 1
    legacy_profile = platform.warehouse.entity_profile(legacy_id)
    assert legacy_profile is not None
    assert len(legacy_profile["identifiers"]) == 2
    assert all(item["superseded_at"] for item in legacy_profile["identifiers"])
    compatibility_sync = platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "0050",
                "公司簡稱": "基金簡稱",
                "公司名稱": "基金簡稱",
            }
        ],
        twse_quotes=[{"Code": "0050", "Name": "基金簡稱"}],
        acquired_at="2026-07-24T07:00:00+00:00",
    )
    canonical_profile = platform.warehouse.entity_profile(canonical_id)
    assert compatibility_sync["status"] == "succeeded"
    assert canonical_profile is not None
    assert len(canonical_profile["identifiers"]) == 3
    assert platform.entity_registry.status()["superseded_identifier_count"] == 2


def test_security_sync_splits_legacy_placeholder_entity_without_deleting_history(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    legacy_id = "ENT-" + "3" * 32
    platform.warehouse.upsert_entity(
        EntityRecord(
            entity_id=legacy_id,
            entity_type="stock",
            canonical_name="境外乙有限公司",
            market="taiwan",
            exchange="TWSE",
            lifecycle_status="active",
            metadata={"display_symbol": "9102.TW", "unified_business_no": "00000000"},
        ),
        identifiers=(
            {
                "source_id": "twse_openapi",
                "identifier_type": "display_symbol",
                "identifier_value": "9101.TW",
                "valid_from": "2020-01-01",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "exchange_code",
                "identifier_value": "9101",
                "valid_from": "2020-01-01",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "display_symbol",
                "identifier_value": "9102.TW",
                "valid_from": "2021-01-01",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "exchange_code",
                "identifier_value": "9102",
                "valid_from": "2021-01-01",
            },
            {
                "source_id": "twse_openapi",
                "identifier_type": "unified_business_no",
                "identifier_value": "00000000",
                "valid_from": "2020-01-01",
            },
        ),
    )
    repair = platform.warehouse.reconcile_entity_registry_aliases(
        effective_at="2026-07-24T05:00:00+00:00",
    )
    assert repair["superseded_placeholder_count"] == 1

    sync = platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "9101",
                "公司簡稱": "境外甲",
                "公司名稱": "境外甲有限公司",
                "營利事業統一編號": "00000000",
                "上市日期": "1090101",
            },
            {
                "公司代號": "9102",
                "公司簡稱": "境外乙",
                "公司名稱": "境外乙有限公司",
                "營利事業統一編號": "00000000",
                "上市日期": "1100101",
            },
        ],
        twse_quotes=[
            {"Code": "9101", "Name": "境外甲"},
            {"Code": "9102", "Name": "境外乙"},
        ],
        acquired_at="2026-07-24T06:00:00+00:00",
    )

    current_a = platform.resolve_entity("9101.TW")
    current_b = platform.resolve_entity("9102.TW")
    historical_a = platform.resolve_entity(
        "9101.TW",
        as_of="2025-01-01T00:00:00+00:00",
    )
    legacy_profile = platform.warehouse.entity_profile(legacy_id)

    assert sync["status"] == "succeeded"
    assert current_a["status"] == current_b["status"] == "resolved"
    assert current_a["entity"]["entity_id"] != legacy_id
    assert current_b["entity"]["entity_id"] == legacy_id
    assert historical_a["entity"]["entity_id"] == legacy_id
    assert legacy_profile is not None
    retired = [
        item
        for item in legacy_profile["identifiers"]
        if item["identifier_value"] in {"9101", "9101.TW"}
    ]
    assert len(retired) == 2
    assert all(item["superseded_by_entity_id"] == current_a["entity"]["entity_id"] for item in retired)
    assert all(item["superseded_at"] for item in retired)
    assert platform.entity_registry.status()["ambiguous_current_display_symbol_count"] == 0


def test_point_in_time_query_excludes_late_acquisition_and_preserves_revisions(tmp_path) -> None:
    platform = _platform(tmp_path)
    raw = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload={"close": 10},
        received_at="2026-07-02T01:00:00+00:00",
    )
    first = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-example",
        observation_key="2026-07-01",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            observed_at="2026-07-01",
            published_at="2026-07-01T09:00:00+00:00",
            available_at="2026-07-01T09:00:00+00:00",
            acquired_at="2026-07-02T01:00:00+00:00",
            effective_at="2026-07-01",
        ),
        payload={"close": 10},
        raw_payload_id=raw,
    )
    raw_revision = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload={"close": 11},
        received_at="2026-07-03T01:00:00+00:00",
    )
    second = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-example",
        observation_key="2026-07-01",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            observed_at="2026-07-01",
            published_at="2026-07-03T00:30:00+00:00",
            available_at="2026-07-03T00:30:00+00:00",
            acquired_at="2026-07-03T01:00:00+00:00",
            effective_at="2026-07-01",
        ),
        payload={"close": 11},
        raw_payload_id=raw_revision,
    )

    before_acquisition = platform.query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-01T23:59:59+00:00",
    )
    first_known = platform.query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-02T12:00:00+00:00",
    )
    revised = platform.query(
        dataset="prices_daily",
        entity_id="ENT-example",
        as_of="2026-07-04T00:00:00+00:00",
    )

    assert before_acquisition == []
    assert first_known[0].revision_id == first.revision_id
    assert first_known[0].payload["close"] == 10
    assert revised[0].revision_id == second.revision_id
    assert revised[0].supersedes_revision_id == first.revision_id
    assert revised[0].payload["close"] == 11
    close_trace = revised[0].field_provenance["/close"]
    assert close_trace.source_id == "twse_openapi"
    assert close_trace.temporal.acquired_at == "2026-07-03T01:00:00+00:00"
    assert close_trace.updated_at == revised[0].created_at
    assert close_trace.raw_payload_id == raw_revision
    assert close_trace.raw_json_pointer == "/close"
    assert close_trace.quality_status == "valid"
    assert close_trace.transformation_id == "stock_ai.normalizer.v1"


def test_revision_identity_ignores_fetch_timestamps_and_reuses_raw_capture(tmp_path) -> None:
    platform = _platform(tmp_path)
    common = {
        "stock_id": "2330",
        "date": "2026-06",
        "revenue": 200_000_000_000,
    }

    def ingest(acquired_at: str) -> list:
        records = [
            {
                **common,
                "available_at": acquired_at,
                "acquired_at": acquired_at,
            }
        ]
        return platform.ingest_records(
            source_id="twse_openapi",
            dataset="revenues_monthly",
            records=records,
            entity_id_for=lambda record: f"ENT-{record['stock_id']}",
            observation_key_for=lambda record: record["date"],
            available_at_for=lambda record: record["available_at"],
            effective_at_for=lambda record: record["date"] + "-01",
            fiscal_period_for=lambda record: record["date"],
            acquired_at=acquired_at,
        )

    first = ingest("2026-07-01T01:00:00+00:00")
    second = ingest("2026-07-02T01:00:00+00:00")

    assert first[0].revision_id == second[0].revision_id
    with sqlite3.connect(platform.warehouse.path) as conn:
        assert conn.execute(
            "select count(*) from data_revisions where dataset='revenues_monthly'"
        ).fetchone()[0] == 1
        assert conn.execute("select count(*) from raw_data_payloads").fetchone()[0] == 1
        assert conn.execute("select count(*) from raw_data_objects").fetchone()[0] == 1


def test_security_lifecycle_revision_ignores_live_quote_raw_row(tmp_path) -> None:
    platform = _platform(tmp_path)

    def sync(close: str, acquired_at: str) -> dict:
        return platform.sync_security_master_payloads(
            twse_companies=[
                {
                    "公司代號": "2330",
                    "公司簡稱": "台積電",
                    "公司名稱": "台灣積體電路製造股份有限公司",
                    "營利事業統一編號": "22099131",
                    "上市日期": "831105",
                }
            ],
            twse_quotes=[
                {
                    "Code": "2330",
                    "Name": "台積電",
                    "ClosingPrice": close,
                    "TradeVolume": "123456",
                }
            ],
            acquired_at=acquired_at,
        )

    first = sync("1000", "2026-07-24T03:00:00+00:00")
    second = sync("1010", "2026-07-25T03:00:00+00:00")

    assert first["revision_count"] == 1
    assert second["revision_count"] == 0
    assert second["reused_revision_count"] == 1
    with sqlite3.connect(platform.warehouse.path) as conn:
        assert conn.execute(
            "select count(*) from data_revisions where dataset='security_master'"
        ).fetchone()[0] == 1


def test_revision_history_snapshots_reconstruct_restatements_and_are_immutable(
    tmp_path,
    monkeypatch,
) -> None:
    platform = _platform(tmp_path)
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    original_payload = {
        "report_date": "2026-05-10",
        "period": "2026-03",
        "symbol": "9000.TW",
        "name": "版本驗證公司",
        "industry": "測試",
        "current_revenue": 100.0,
        "previous_revenue": 90.0,
        "last_year_revenue": 80.0,
        "mom_change_percent": 11.11,
        "yoy_change_percent": 25.0,
        "ytd_revenue": 300.0,
        "last_ytd_revenue": 240.0,
        "ytd_change_percent": 25.0,
        "note": None,
        "source": "MOPS revision fixture",
    }
    restated_payload = {
        **original_payload,
        "report_date": "2026-05-20",
        "current_revenue": 105.0,
        "mom_change_percent": 16.67,
        "yoy_change_percent": 31.25,
        "ytd_revenue": 305.0,
        "ytd_change_percent": 27.08,
    }
    first = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-history",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-10T08:00:00+00:00",
            available_at="2026-05-10T08:00:00+00:00",
            acquired_at="2026-05-11T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload=original_payload,
        raw_payload_id=None,
    )
    second = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-history",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-20T08:00:00+00:00",
            available_at="2026-05-20T08:00:00+00:00",
            acquired_at="2026-05-21T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload=restated_payload,
        raw_payload_id=None,
    )

    early = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    early_repeat = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    late = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-22T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    early_state = platform.warehouse.revision_snapshot(early["snapshot_id"])
    late_state = platform.warehouse.revision_snapshot(late["snapshot_id"])
    history = platform.revision_history(
        dataset="revenues_monthly",
        entity_id="ENT-history",
        observation_key="2026-03",
        source_id="mops",
    )

    assert early_repeat["snapshot_id"] == early["snapshot_id"]
    assert early["manifest_hash"] != late["manifest_hash"]
    assert early_state is not None and late_state is not None
    assert early_state["integrity"]["status"] == "passed"
    assert late_state["integrity"]["status"] == "passed"
    assert early_state["items"][0]["revision_id"] == first.revision_id
    assert early_state["items"][0]["payload"]["current_revenue"] == 100
    assert late_state["items"][0]["revision_id"] == second.revision_id
    assert late_state["items"][0]["payload"]["current_revenue"] == 105
    assert history["status"] == "passed"
    assert [item["revision"] for item in history["items"]] == [1, 2]
    assert history["items"][1]["supersedes_revision_id"] == first.revision_id

    with sqlite3.connect(platform.warehouse.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="data revisions are immutable"):
            conn.execute(
                "update data_revisions set payload_json='{}' where revision_id=?",
                (first.revision_id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="data revisions are immutable"):
            conn.execute(
                "delete from data_revisions where revision_id=?",
                (first.revision_id,),
            )
        conn.rollback()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="data revision snapshots are immutable",
        ):
            conn.execute(
                "update data_revision_snapshots set item_count=0 where snapshot_id=?",
                (early["snapshot_id"],),
            )
        conn.rollback()

    api_history = revision_history(
        dataset="revenues_monthly",
        entity_id="ENT-history",
        observation_key="2026-03",
        source_id="mops",
    )
    api_created = create_revision_snapshot(
        {
            "dataset": "revenues_monthly",
            "knowledge_at": "2026-05-12T00:00:00+00:00",
            "effective_at": "2026-04-30",
        }
    )
    api_snapshot = revision_snapshot(
        early["snapshot_id"],
        offset=0,
        limit=500,
    )
    status = platform.status()["warehouse"]["revision_history"]
    assert api_history["count"] == 2
    assert api_created["snapshot"]["snapshot_id"] == early["snapshot_id"]
    assert api_snapshot["items"][0]["payload"]["current_revenue"] == 100
    assert status["immutable"] is True
    assert status["corrected_observation_count"] == 1
    assert status["snapshot_count"] == 2
    assert status["chain_issue_count"] == 0
    assert status["snapshot_integrity_issue_count"] == 0


def test_bitemporal_query_blocks_unpublished_fiscal_data_and_late_restatements(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    first = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-bitemporal",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-10T08:00:00+00:00",
            available_at="2026-05-10T08:00:00+00:00",
            acquired_at="2026-05-11T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload={"period": "2026-03", "revenue": 100},
        raw_payload_id=None,
    )
    second = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-bitemporal",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-20T08:00:00+00:00",
            available_at="2026-05-20T08:00:00+00:00",
            acquired_at="2026-05-21T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload={"period": "2026-03", "revenue": 105},
        raw_payload_id=None,
    )

    assert platform.query(
        dataset="revenues_monthly",
        knowledge_at="2026-05-09T23:59:59+00:00",
        effective_at="2026-04-30",
    ) == []
    assert platform.query(
        dataset="revenues_monthly",
        knowledge_at="2026-05-10T12:00:00+00:00",
        effective_at="2026-04-30",
    ) == []
    assert platform.query(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-03-15",
    ) == []
    first_known = platform.query(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    restated = platform.query(
        dataset="revenues_monthly",
        knowledge_at="2026-05-22T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    compatibility = platform.query(
        dataset="revenues_monthly",
        as_of="2026-05-12T00:00:00+00:00",
    )

    assert first_known[0].revision_id == first.revision_id
    assert restated[0].revision_id == second.revision_id
    assert compatibility[0].revision_id == first.revision_id
    assert first_known[0].temporal.fiscal_period == "2026-03"
    assert first_known[0].field_provenance["/revenue"].temporal.time_basis == (
        "fiscal_period"
    )
    status = platform.warehouse.status()["tables"]
    assert status["fiscal_period_revisions"] == 2
    assert status["temporal_contract_violations"] == 0


def test_temporal_contract_rejects_incomplete_fiscal_period() -> None:
    with pytest.raises(ValueError, match="requires fiscal_period"):
        TemporalCoordinates(
            time_basis="fiscal_period",
            available_at="2026-05-10",
            acquired_at="2026-05-11",
            effective_at="2026-03-31",
        )


def test_data_envelope_traces_nested_values_and_allows_field_source_override(tmp_path) -> None:
    platform = _platform(tmp_path)
    temporal = TemporalCoordinates(
        observed_at="2026-07-24",
        published_at="2026-07-24T06:00:00+00:00",
        available_at="2026-07-24T06:00:00+00:00",
        acquired_at="2026-07-24T06:01:00+00:00",
        effective_at="2026-07-24",
    )
    raw = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload={"close": 100.0, "metrics": {"volume": 1234}},
        received_at=temporal.acquired_at,
    )

    envelope = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-field-trace",
        observation_key="2026-07-24",
        source_id="twse_openapi",
        temporal=temporal,
        payload={"close": 100.0, "metrics": {"volume": 1234}},
        raw_payload_id=raw,
        transformation_id="stock_ai.multi_source_projection.v1",
        field_provenance={
            "/close": {
                "source_id": "yahoo_finance",
                "temporal": temporal.model_dump(mode="json"),
                "updated_at": "2026-07-24T06:02:00+00:00",
                "raw_payload_id": raw,
                "raw_json_pointer": "/chart/result/0/indicators/quote/0/close/0",
                "quality_status": "warning",
                "quality_flags": ["cross_source_selected"],
                "transformation_id": "stock_ai.multi_source_projection.v1",
                "input_fields": ["yahoo:/chart/close", "twse:/close"],
            }
        },
    )

    assert set(envelope.field_provenance) == {"/close", "/metrics/volume"}
    assert envelope.field_provenance["/close"].source_id == "yahoo_finance"
    assert envelope.field_provenance["/close"].raw_json_pointer.endswith("/close/0")
    assert envelope.field_provenance["/close"].quality_flags == [
        "cross_source_selected"
    ]
    assert envelope.field_provenance["/metrics/volume"].source_id == "twse_openapi"
    assert envelope.field_provenance["/metrics/volume"].input_fields == [
        "/metrics/volume"
    ]
    assert platform.warehouse.status()["tables"]["untraced_revisions"] == 0
    assert platform.warehouse.status()["tables"]["traced_fields"] == 2


def test_daily_data_quality_reports_detect_all_required_issue_classes(
    tmp_path,
    monkeypatch,
) -> None:
    platform = _platform(tmp_path)
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )

    def write_price(
        *,
        entity_id: str,
        close: float | None,
        source_id: str = "twse_openapi",
        payload_trade_date: str = "2026-07-20",
        observation_key: str = "2026-07-20",
    ):
        payload: dict[str, object] = {"trade_date": payload_trade_date}
        if close is not None:
            payload["close"] = close
        return platform.warehouse.write_revision(
            dataset="prices_daily",
            entity_id=entity_id,
            observation_key=observation_key,
            source_id=source_id,
            temporal=TemporalCoordinates(
                time_basis="trade_date",
                trade_date="2026-07-20",
                observed_at="2026-07-20",
                published_at="2026-07-21T01:00:00+00:00",
                available_at="2026-07-21T01:00:00+00:00",
                acquired_at="2026-07-21T02:00:00+00:00",
                effective_at="2026-07-20",
            ),
            payload=payload,
            raw_payload_id=None,
        )

    for index, close in enumerate((100.0, 101.0, 99.0, 100.0, 102.0, 1000.0)):
        write_price(entity_id=f"ENT-quality-{index}", close=close)
    write_price(entity_id="ENT-quality-missing", close=None)
    write_price(
        entity_id="ENT-quality-time",
        close=100.0,
        payload_trade_date="2026-07-19",
    )
    write_price(entity_id="ENT-quality-duplicate", close=10.0)
    write_price(entity_id="ENT-quality-duplicate", close=11.0)
    write_price(entity_id="ENT-quality-duplicate", close=10.0)
    write_price(entity_id="ENT-quality-conflict", close=50.0)
    write_price(
        entity_id="ENT-quality-conflict",
        close=70.0,
        source_id="yahoo_finance",
    )
    conflict = platform.warehouse.reconcile(
        dataset="prices_daily",
        entity_id="ENT-quality-conflict",
        observation_key="2026-07-20",
        field_names=["close"],
        tolerance=0.1,
        as_of="2099-01-01T00:00:00+00:00",
    )
    assert conflict["status"] == "conflict"

    today = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    report = platform.daily_quality_report(
        dataset="prices_daily",
        report_date=today,
    )
    repeated = platform.daily_quality_report(
        dataset="prices_daily",
        report_date=today,
    )
    api_batch = daily_data_quality(
        {
            "datasets": ["prices_daily"],
            "report_date": today,
        }
    )
    api_list = quality_reports(
        dataset="prices_daily",
        report_date=today,
        limit=100,
    )
    api_detail = quality_report_detail(report["report_id"])

    assert report["schema_version"] == "stock_ai.data_quality_report.v2"
    assert report["status"] == "failed"
    assert report["missing_count"] >= 1
    assert report["anomaly_count"] >= 1
    assert report["time_misalignment_count"] >= 1
    assert report["duplicate_count"] >= 1
    assert report["conflict_count"] >= 1
    assert repeated["report_id"] == report["report_id"]
    assert api_batch["dataset_count"] == 1
    assert api_batch["reports"][0]["report_id"] == report["report_id"]
    assert api_list["count"] == 1
    assert api_detail["report_id"] == report["report_id"]
    assert {
        "missing",
        "anomaly",
        "time_misalignment",
        "duplicate",
        "source_conflict",
    }.issubset({issue["category"] for issue in api_detail["issues"]})
    quality_status = platform.status()["warehouse"]["data_quality"]
    assert quality_status["report_count"] == 1
    assert quality_status["issue_count"] == report["issue_count"]
    assert quality_status["latest_report"]["report_id"] == report["report_id"]
    with sqlite3.connect(platform.warehouse.path) as conn:
        assert conn.execute("select count(*) from data_quality_reports").fetchone()[0] == 1
        assert (
            conn.execute("select count(*) from data_quality_issues").fetchone()[0]
            == report["issue_count"]
        )


def test_reconciliation_records_conflicts_without_overwriting_sources(tmp_path) -> None:
    platform = _platform(tmp_path)
    for source_id, close in (("twse_openapi", 10.0), ("yahoo_finance", 10.5)):
        raw = platform.warehouse.record_raw_payload(
            source_id=source_id,
            payload={"close": close},
            received_at="2026-07-24T03:00:00+00:00",
        )
        platform.warehouse.write_revision(
            dataset="prices_daily",
            entity_id="ENT-example",
            observation_key="2026-07-24",
            source_id=source_id,
            temporal=TemporalCoordinates(
                observed_at="2026-07-24",
                available_at="2026-07-24T02:00:00+00:00",
                acquired_at="2026-07-24T03:00:00+00:00",
                effective_at="2026-07-24",
            ),
            payload={"close": close},
            raw_payload_id=raw,
            is_fallback=source_id == "yahoo_finance",
        )

    report = platform.warehouse.reconcile(
        dataset="prices_daily",
        entity_id="ENT-example",
        observation_key="2026-07-24",
        field_names=["close"],
        tolerance=0.1,
        as_of="2026-07-24T04:00:00+00:00",
    )

    assert report["status"] == "conflict"
    assert report["source_count"] == 2
    assert report["conflicts"][0]["left_value"] != report["conflicts"][0]["right_value"]
    assert platform.warehouse.quality_report("prices_daily")["conflict_count"] == 1


def test_reconciliation_engine_compares_price_financial_and_event_sources(
    monkeypatch,
    tmp_path,
) -> None:
    platform = _platform(tmp_path)

    def write(
        dataset: str,
        source_id: str,
        payload: dict[str, object],
        *,
        acquired_at: str,
        observation_key: str,
    ):
        return platform.warehouse.write_revision(
            dataset=dataset,
            entity_id="ENT-reconciliation",
            observation_key=observation_key,
            source_id=source_id,
            temporal=TemporalCoordinates(
                observed_at="2026-07-27",
                published_at="2026-07-27T01:00:00+00:00",
                available_at="2026-07-27T01:00:00+00:00",
                acquired_at=acquired_at,
                effective_at="2026-07-27",
            ),
            payload=payload,
            raw_payload_id=None,
        )

    write(
        "prices_daily",
        "twse_openapi",
        {"close": 100.0},
        acquired_at="2026-07-27T02:00:00+00:00",
        observation_key="2026-07-27",
    )
    write(
        "prices_daily",
        "yahoo_finance",
        {"close": 101.0},
        acquired_at="2026-07-27T02:01:00+00:00",
        observation_key="2026-07-27",
    )
    price = platform.reconcile_sources(
        dataset="prices_daily",
        entity_id="ENT-reconciliation",
        observation_key="2026-07-27",
        knowledge_at="2026-07-27T03:00:00+00:00",
    )
    assert price["status"] == "conflict"
    assert price["source_count"] == 2
    assert price["conflict_count"] == 1
    assert price["conflicts"][0]["comparison_method"] == "numeric"
    assert price["conflicts"][0]["relative_difference"] == pytest.approx(1 / 101)
    assert price["source_data_preserved"] is True

    for source_id, revenue in (
        ("twse_openapi", 1_000_000),
        ("yahoo_finance", 1_000_500),
    ):
        write(
            "revenues_monthly",
            source_id,
            {"current_revenue": revenue},
            acquired_at="2026-07-27T02:10:00+00:00",
            observation_key="2026-06",
        )
    financial = platform.reconcile_sources(
        dataset="revenues_monthly",
        entity_id="ENT-reconciliation",
        observation_key="2026-06",
        knowledge_at="2026-07-27T03:00:00+00:00",
    )
    assert financial["status"] == "consistent"
    assert financial["conflict_count"] == 0

    for source_id, title, event_time in (
        ("twse_openapi", "重大訊息：董事會決議", "2026-07-27T06:00:00+00:00"),
        ("yahoo_finance", "重大訊息 董事會決議", "2026-07-27T06:03:00+00:00"),
    ):
        write(
            "events",
            source_id,
            {
                "event_type": "material_information",
                "title": title,
                "event_time": event_time,
            },
            acquired_at="2026-07-27T07:00:00+00:00",
            observation_key="EVENT-1",
        )
    event = platform.reconcile_sources(
        dataset="events",
        entity_id="ENT-reconciliation",
        observation_key="EVENT-1",
        knowledge_at="2026-07-27T08:00:00+00:00",
    )
    assert event["status"] == "consistent"
    assert event["comparison_count"] == 3

    write(
        "events",
        "yahoo_finance",
        {
            "event_type": "earnings",
            "title": "重大訊息 董事會決議",
            "event_time": "2026-07-27T06:03:00+00:00",
        },
        acquired_at="2026-07-27T08:01:00+00:00",
        observation_key="EVENT-1",
    )
    event_conflict = platform.reconcile_sources(
        dataset="events",
        entity_id="ENT-reconciliation",
        observation_key="EVENT-1",
        knowledge_at="2026-07-27T09:00:00+00:00",
    )
    assert event_conflict["status"] == "conflict"
    assert [item["field_name"] for item in event_conflict["conflicts"]] == [
        "event_type"
    ]

    write(
        "prices_daily",
        "yahoo_finance",
        {"close": 100.05},
        acquired_at="2026-07-27T09:01:00+00:00",
        observation_key="2026-07-27",
    )
    resolved = platform.reconcile_sources(
        dataset="prices_daily",
        entity_id="ENT-reconciliation",
        observation_key="2026-07-27",
        knowledge_at="2026-07-27T10:00:00+00:00",
    )
    assert resolved["status"] == "consistent"
    assert resolved["resolved_count"] == 1
    assert platform.reconciliation_status()["open_conflict_count"] == 1
    assert platform.reconciliation_status()["resolved_conflict_count"] == 1
    assert len(
        platform.warehouse.revision_history(
            dataset="prices_daily",
            entity_id="ENT-reconciliation",
            observation_key="2026-07-27",
        )["items"]
    ) == 3

    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    api_report = run_reconciliation(
        {
            "dataset": "events",
            "entity_id": "ENT-reconciliation",
            "observation_key": "EVENT-1",
            "knowledge_at": "2026-07-27T09:00:00+00:00",
        }
    )
    assert api_report["status"] == "conflict"
    assert reconciliation_status()["source_data_preserved"] is True
    assert reconciliation_runs(limit=20)["count"] == 6
    assert reconciliation_run(api_report["run_id"])["item"]["status"] == "conflict"
    open_items = reconciliation_conflicts(
        dataset=None,
        status="open",
        limit=20,
    )["items"]
    assert len(open_items) == 1
    assert open_items[0]["field_name"] == "event_type"


def test_unified_data_api_functions_read_warehouse(monkeypatch, tmp_path) -> None:
    platform = _platform(tmp_path)
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )

    sources = data_sources()
    query = query_data(DataQuery(dataset="security_master"))

    assert sources["count"] == 15
    assert sources["dataset_count"] >= 29
    assert query["schema_version"] == "stock_ai.unified_data_query.v1"
    assert query["count"] == 0


def test_incremental_loader_skips_fresh_partition_and_resumes_cursor(tmp_path) -> None:
    platform = _platform(tmp_path)
    loader = IncrementalLoader(platform)
    fetched_cursors: list[str | None] = []
    persisted: list[dict] = []

    def fetch(cursor):
        fetched_cursors.append(cursor)
        return ([{"id": "one"}], "cursor-1", {"page_count": 1})

    first = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(records),
    )
    second = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(records),
    )
    forced = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(records),
        force=True,
    )

    assert first["status"] == "succeeded"
    assert second["status"] == "skipped_fresh"
    assert forced["previous_cursor"] == "cursor-1"
    assert fetched_cursors == [None, "cursor-1"]
    assert persisted == [{"id": "one"}, {"id": "one"}]


def test_cache_policy_enforces_ttl_invalidation_stale_window_and_singleflight(
    tmp_path,
    monkeypatch,
) -> None:
    platform = _platform(tmp_path)
    loader = IncrementalLoader(platform)
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    assert platform.cache_ttl("prices_intraday", source_id="twse_mis") == 5
    assert platform.cache_ttl("prices_daily", source_id="twse_openapi") == 900
    assert platform.cache_ttl("security_master", source_id="twse_openapi") == 21600

    platform.cache_policy_service.mark_refreshed(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        refreshed_at="2026-07-27T01:00:00+00:00",
    )
    stale = platform.cache_policy_service.decision(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        as_of="2026-07-27T01:00:06+00:00",
        record=False,
    )
    expired = platform.cache_policy_service.decision(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        as_of="2026-07-27T01:00:16+00:00",
        record=False,
    )
    assert stale["state"] == "stale_while_revalidate"
    assert stale["serve_stale"] is True
    assert expired["state"] == "expired"
    assert expired["serve_stale"] is False

    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls: list[str | None] = []
    persisted: list[dict[str, object]] = []

    def blocking_fetch(cursor):
        fetch_calls.append(cursor)
        fetch_started.set()
        assert release_fetch.wait(timeout=5)
        return ([{"id": "master-v1"}], "cursor-v1", {})

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            loader.run,
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="twse",
            fetch=blocking_fetch,
            persist=lambda records, _at: persisted.extend(records),
            as_of="2026-07-27T02:00:00+00:00",
        )
        assert fetch_started.wait(timeout=5)
        second_future = executor.submit(
            loader.run,
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="twse",
            fetch=blocking_fetch,
            persist=lambda records, _at: persisted.extend(records),
            as_of="2026-07-27T02:00:00+00:00",
        )
        second = second_future.result(timeout=5)
        release_fetch.set()
        first = first_future.result(timeout=5)

    fresh = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="twse",
        fetch=blocking_fetch,
        persist=lambda records, _at: persisted.extend(records),
        as_of="2026-07-27T02:01:00+00:00",
    )
    invalidated = invalidate_dataset_cache(
        "security_master",
        {
            "reason": "listing_event",
            "source_id": "twse_openapi",
            "partition_key": "twse",
            "invalidated_at": "2026-07-27T02:02:00+00:00",
        },
    )
    invalidated_status = dataset_cache_status(
        "security_master",
        as_of="2026-07-27T02:03:00+00:00",
    )
    refreshed = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="twse",
        fetch=lambda cursor: (
            fetch_calls.append(cursor) or [{"id": "master-v2"}],
            "cursor-v2",
            {},
        ),
        persist=lambda records, _at: persisted.extend(records),
        as_of="2026-07-27T02:03:00+00:00",
    )
    all_status = cache_status(
        dataset=None,
        as_of="2026-07-27T02:04:00+00:00",
    )

    assert first["status"] == "succeeded"
    assert second["status"] == "skipped_refresh_in_progress"
    assert fresh["status"] == "skipped_fresh"
    assert invalidated["count"] == 1
    assert invalidated_status["state_counts"]["invalidated"] == 1
    assert refreshed["status"] == "succeeded"
    assert fetch_calls == [None, "cursor-v1"]
    assert persisted == [{"id": "master-v1"}, {"id": "master-v2"}]
    assert all_status["active_refresh_count"] == 0
    assert all_status["invalidation_count"] == 1
    assert all_status["state_counts"]["fresh"] == 1
    assert all_status["state_counts"]["expired"] == 1
    with pytest.raises(ValueError, match="Invalid cache invalidation reason"):
        platform.invalidate_cache(
            dataset="security_master",
            reason="unreviewed_reason",
        )


def test_incremental_loader_failure_preserves_last_success_cursor(tmp_path) -> None:
    platform = _platform(tmp_path)
    loader = IncrementalLoader(platform)
    loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        fetch=lambda _cursor: ([{"id": "one"}], "cursor-ok", {}),
        persist=lambda _records, _acquired_at: None,
    )

    try:
        loader.run(
            source_id="twse_openapi",
            dataset="security_master",
            fetch=lambda cursor: (_ for _ in ()).throw(RuntimeError(f"failed after {cursor}")),
            persist=lambda _records, _acquired_at: None,
            force=True,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("loader failure was not propagated")

    checkpoint = platform.warehouse.get_checkpoint(
        source_id="twse_openapi",
        dataset="security_master",
    )
    assert checkpoint is not None
    assert checkpoint["status"] == "failed"
    assert checkpoint["cursor_value"] == "cursor-ok"
    assert checkpoint["error"]["type"] == "RuntimeError"
    assert checkpoint["metadata"]["resumable"] is True


def test_incremental_loader_commits_each_batch_and_resumes_without_refetch(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    loader = IncrementalLoader(platform)
    fetched_cursors: list[str | None] = []
    persisted: list[str] = []

    def fetch(cursor):
        fetched_cursors.append(cursor)
        pages = {
            None: ([{"id": "one"}], "cursor-1", {"has_more": True, "page": 1}),
            "cursor-1": (
                [{"id": "two"}],
                "cursor-2",
                {"has_more": True, "page": 2},
            ),
            "cursor-2": (
                [{"id": "three"}],
                "cursor-3",
                {"has_more": False, "page": 3},
            ),
        }
        return pages[cursor]

    first = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="paged",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(
            str(record["id"]) for record in records
        ),
        max_batches=2,
    )
    second = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="paged",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(
            str(record["id"]) for record in records
        ),
    )

    assert first["status"] == "paused"
    assert first["cursor"] == "cursor-2"
    assert first["batch_count"] == 2
    assert second["status"] == "succeeded"
    assert second["previous_cursor"] == "cursor-2"
    assert fetched_cursors == [None, "cursor-1", "cursor-2"]
    assert persisted == ["one", "two", "three"]

    checkpoint = platform.warehouse.get_checkpoint(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="paged",
    )
    assert checkpoint is not None
    assert checkpoint["status"] == "succeeded"
    assert checkpoint["cursor_value"] == "cursor-3"
    runs = platform.warehouse.list_ingestion_runs()
    assert [run["status"] for run in runs[:2]] == ["succeeded", "paused"]
    assert platform.warehouse.ingestion_run(first["run_id"])["batch_count"] == 2
    assert len(platform.warehouse.ingestion_run(first["run_id"])["batches"]) == 2


def test_incremental_loader_failure_resumes_from_last_committed_batch(
    tmp_path,
    monkeypatch,
) -> None:
    platform = _platform(tmp_path)
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )
    loader = IncrementalLoader(platform)
    fetched_cursors: list[str | None] = []
    persisted: list[str] = []
    fail_second_page = True

    def fetch(cursor):
        nonlocal fail_second_page
        fetched_cursors.append(cursor)
        if cursor is None:
            return ([{"id": "one"}], "cursor-1", {"has_more": True})
        if fail_second_page:
            fail_second_page = False
            raise RuntimeError("source interrupted")
        return ([{"id": "two"}], "cursor-2", {"has_more": False})

    with pytest.raises(RuntimeError, match="source interrupted"):
        loader.run(
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="interrupted",
            fetch=fetch,
            persist=lambda records, _acquired_at: persisted.extend(
                str(record["id"]) for record in records
            ),
        )

    resumed = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="interrupted",
        fetch=fetch,
        persist=lambda records, _acquired_at: persisted.extend(
            str(record["id"]) for record in records
        ),
    )

    assert resumed["status"] == "succeeded"
    assert resumed["previous_cursor"] == "cursor-1"
    assert fetched_cursors == [None, "cursor-1", "cursor-1"]
    assert persisted == ["one", "two"]
    status = platform.status()["warehouse"]["incremental_loader"]
    assert status["mode"] == "committed_cursor_delta"
    assert status["run_statuses"] == {"failed": 1, "succeeded": 1}
    assert ingestion_runs(limit=50)["count"] == 2
    assert ingestion_run(resumed["run_id"])["item"]["batch_count"] == 1


def test_incremental_loader_rejects_non_advancing_continuation_cursor(tmp_path) -> None:
    platform = _platform(tmp_path)
    loader = IncrementalLoader(platform)

    with pytest.raises(ValueError, match="without advancing"):
        loader.run(
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="bad-cursor",
            fetch=lambda cursor: ([], cursor, {"has_more": True}),
            persist=lambda _records, _acquired_at: None,
        )


def test_incremental_loader_marks_abandoned_run_interrupted_before_resume(
    tmp_path,
) -> None:
    platform = _platform(tmp_path)
    abandoned_run_id = platform.warehouse.start_ingestion_run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="restart",
        starting_cursor="cursor-7",
    )
    platform.warehouse.save_checkpoint(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="restart",
        cursor_value="cursor-7",
        status="running",
        metadata={"run_id": abandoned_run_id},
    )
    fetched_cursors: list[str | None] = []

    resumed = IncrementalLoader(platform).run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="restart",
        fetch=lambda cursor: (
            fetched_cursors.append(cursor)
            or ([{"id": "eight"}], "cursor-8", {"has_more": False})
        ),
        persist=lambda _records, _acquired_at: None,
    )

    assert resumed["status"] == "succeeded"
    assert fetched_cursors == ["cursor-7"]
    abandoned = platform.warehouse.ingestion_run(abandoned_run_id)
    assert abandoned is not None
    assert abandoned["status"] == "interrupted"
    assert abandoned["error"]["type"] == "Interrupted"


def test_official_security_loader_uses_six_resumable_partitions(monkeypatch, tmp_path) -> None:
    platform = _platform(tmp_path)
    module = "stock_ai.data_platform.security_loader"
    from stock_ai.data_platform import product_catalog

    def product_source(dataset_id):
        market = product_catalog.CATALOGUES[dataset_id][1]
        code = {"上市": "1111", "上櫃": "2222", "興櫃": "3333"}[market]
        section = "" if market == "興櫃" else "<tr><td colspan=7>股票</td></tr>"
        raw = (f"<h2>最近更新日期:2026/07/24</h2><table><tr>"
               "<td>有價證券代號及名稱</td><td>國際證券辨識號碼(ISIN Code)</td>"
               "<td>上市日</td><td>市場別</td><td>產業別</td><td>CFICode</td><td>備註</td></tr>"
               f"{section}<tr><td>{code}　甲</td><td>TW000{code}009</td><td>2026/01/02</td>"
               f"<td>{market}</td><td></td><td>ESVUFR</td><td></td></tr></table>").encode("cp950")
        url = product_catalog.source_endpoint(dataset_id)
        return {"raw": raw, "source_url": url, "effective_url": url, "http_status": 200,
                "requested_at": "2026-07-24T03:00:00+00:00", "acquired_at": "2026-07-24T03:00:00+00:00",
                "content_type": "text/html;charset=MS950"}

    monkeypatch.setattr(product_catalog, "fetch_product_catalogue", product_source)
    monkeypatch.setattr(
        f"{module}.twse_companies",
        lambda: [
            {
                "公司代號": "1111",
                "公司簡稱": "甲",
                "公司名稱": "甲股份有限公司",
                "上市日期": "1150102",
            }
        ],
    )
    monkeypatch.setattr(f"{module}.twse_quotes", lambda: [{"Code": "1111", "Name": "甲"}])
    for name in (
        "twse_etfs",
        "twse_warrants",
        "twse_indices",
        "twse_delisted",
        "tpex_companies",
        "tpex_quotes",
        "tpex_emerging_companies",
        "tpex_emerging_quotes",
        "tpex_warrants",
        "tpex_indices",
        "tpex_delisted",
    ):
        monkeypatch.setattr(f"{module}.{name}", lambda: [])

    first = OfficialSecurityMasterLoader(platform).run(
        as_of="2026-07-24T03:00:00+00:00"
    )
    second = OfficialSecurityMasterLoader(platform).run(
        as_of="2026-07-24T03:01:00+00:00"
    )

    assert [item["partition_key"] for item in first["partitions"]] == [
        "twse_official_master",
        "tpex_official_master",
        "tpex_delisted_history",
        "twse_isin_listed",
        "tpex_isin_otc",
        "tpex_isin_emerging",
    ]
    assert all(item["status"] == "succeeded" for item in first["partitions"])
    assert all(item["status"] == "skipped_fresh" for item in second["partitions"])
    assert first["entity_count"] == 3


def test_research_gateway_persists_price_history_with_canonical_identity(tmp_path) -> None:
    platform = _platform(tmp_path)
    gateway = UnifiedResearchDataGateway(platform)

    gateway._persist_price_history(
        symbol="2330.TW",
        market="TW",
        source_label="TWSE official close",
        records=[
            {
                "date": "2026-07-23",
                "open": 1000,
                "high": 1010,
                "low": 990,
                "close": 1005,
                "volume": 100,
            }
        ],
        is_fallback=False,
    )

    entity_id = stable_entity_id(
        market="taiwan",
        exchange="TWSE",
        source_code="2330",
    )
    rows = platform.query(dataset="prices_daily", entity_id=entity_id)
    assert len(rows) == 1
    assert rows[0].source_id == "twse_openapi"
    assert rows[0].payload["close"] == 1005
    assert rows[0].is_fallback is False
    assert rows[0].temporal.time_basis == "trade_date"
    assert rows[0].temporal.trade_date == "2026-07-23T00:00:00+00:00"
    assert platform.warehouse.lineage(rows[0].revision_id)["raw_payload"] is not None


def test_research_gateway_marks_official_close_as_advisory_fallback(monkeypatch) -> None:
    platform = SimpleNamespace(
        source_registry_service=SimpleNamespace(availability_audit={}),
        resolve_entity=lambda *_args, **_kwargs: {"status": "unresolved"},
    )
    gateway = UnifiedResearchDataGateway(platform)
    summary = SimpleNamespace(
        latest_price=SimpleNamespace(close=42.5, date="2026-08-28T00:00:00+00:00"),
        data_source="TWSE official close",
        data_timestamp="2026-08-28T00:00:00+00:00",
        official_close=True,
        events=[],
    )

    monkeypatch.setattr("stock_ai.services.get_market_detail_summary", lambda _symbol: summary)
    monkeypatch.setattr("stock_ai.services.get_price_history", lambda _symbol: [])
    monkeypatch.setattr("stock_ai.mvp_features.get_news_center", lambda **_kwargs: {"items": []})
    monkeypatch.setattr("stock_ai.phase1_data.list_margin_trading", lambda **_kwargs: [])
    monkeypatch.setattr("stock_ai.phase1_data.list_monthly_revenues", lambda **_kwargs: [])
    monkeypatch.setattr(gateway, "_persist_price_history", lambda **_kwargs: None)

    bundle = gateway.load(symbol="2887.TW", market="TW")

    assert bundle["price"] == 42.5
    assert bundle["price_is_fallback"] is True
    assert bundle["raw"]["summary_mode"] == "official_close_research_fallback"
