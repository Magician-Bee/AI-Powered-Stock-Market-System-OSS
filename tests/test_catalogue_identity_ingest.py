"""Synthetic retained catalogues exercise the real identity/revision writer."""
import json
import socket

import pytest

from stock_ai.data_platform import warehouse as warehouse_module
from stock_ai.data_platform.contracts import DataQuery, EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from test_catalogue_issue_identity import ACQUIRED, retain_catalogue


CODE = "085974"
NAME = "離線名錄權證"
ISIN = "TW26Z0859747"


def stock(**changes):
    return {"code": "2330", "name": "離線股票", "isin": "TW0002330008",
            "listed_on": "1994/09/05", "section": "股票", "cfi": "ESVUFR", **changes}


def warrant(**changes):
    return {"code": CODE, "name": NAME, "isin": ISIN,
            "listed_on": "2026/09/01", "section": "上市認購(售)權證", "cfi": "RWCCCC", **changes}


@pytest.fixture
def platform(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("isolated catalogue ingest must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: ACQUIRED)
    return MarketDataPlatform(database_path=tmp_path / "catalogue.sqlite")


def ingest(platform, *, as_of=ACQUIRED):
    return platform.warehouse.sync_catalogue_identities(as_of=as_of, code_version="isolated-catalogue-test")


def listed_report(result):
    return next(row for row in result["partitions"] if row["source_dataset"] == "twse_isin_listed")


def inventory(platform):
    return {row["metadata"]["display_symbol"]: row for row in platform.warehouse.identity_inventory()}


def revisions(platform):
    with platform.warehouse._connect() as conn:
        return [dict(row) for row in conn.execute("select * from data_revisions order by entity_id,revision")]


def test_catalogue_imports_reviewed_types_as_unknown_or_prelisting_without_fabricated_fields(platform):
    rows = [stock(),
            stock(code="7777", name="離線未上市", isin="TW0007777009", listed_on="2026/09/14"),
            {"code": "00400A", "name": "離線基金", "isin": "TW00000400A3", "listed_on": "2026/09/01", "section": "ETF", "cfi": "CEOJEU"},
            {"code": "020000", "name": "離線ETN", "isin": "TW0000200005", "listed_on": "2026/09/01", "section": "ETN", "cfi": "CMXXXU"},
            warrant()]
    retained = retain_catalogue(platform, rows=rows)
    result = ingest(platform)
    assert listed_report(result)["status"] == "succeeded"
    assert result["created_count"] == 5
    items = inventory(platform)
    assert {key: item["entity_type"] for key, item in items.items()} == {
        "2330.TW": "stock", "7777.TW": "stock", "00400A.TW": "etf", "020000.TW": "security", CODE + ".TW": "warrant"}
    assert items["7777.TW"]["lifecycle_status"] == "pre_listing"
    assert items["7777.TW"]["listed_at"] == "2026-09-13T16:00:00+00:00"
    by_symbol = {row["symbol"]: row for row in retained}
    for symbol, item in items.items():
        if symbol != "7777.TW":
            assert item["lifecycle_status"] == "unknown"
        assert item["metadata"]["catalogue_only"] is True
        assert item["metadata"]["quote_present"] is False
        assert item["metadata"]["expires_at"] is None and item["delisted_at"] is None
        assert item["metadata"]["product_classifications"]["TWSE:" + symbol] == by_symbol[symbol]
        assert {link["identifier_type"] for link in item["identifiers"]} == {"isin", "exchange_code", "display_symbol"}
        assert all(link["source_id"] == "twse_isin" and link["valid_to"] is None for link in item["identifiers"])
    assert "expires_at" in items[CODE + ".TW"]["metadata"]["catalogue_missing_fields"]


def test_catalogue_revisions_reach_exact_raw_and_cannot_claim_preacquisition_knowledge(platform):
    receipts = retain_catalogue(platform, rows=[stock()])
    ingest(platform)
    entity = inventory(platform)["2330.TW"]
    rows = revisions(platform)
    assert len(rows) == 1
    revision = rows[0]
    assert revision["raw_payload_id"] == receipts[0]["raw_payload_id"]
    assert revision["source_id"] == "twse_isin"
    assert revision["available_at"] == revision["acquired_at"] == ACQUIRED
    assert revision["published_at"] is None
    assert revision["effective_at"] == "1994-09-04T16:00:00+00:00"
    flags = json.loads(revision["quality_flags_json"])
    assert {"publication_time_unavailable", "catalogue_identity_only", "active_lifecycle_unavailable"} <= set(flags)
    availability = json.loads(revision["availability_contract_snapshot_json"])
    assert availability["contract"]["historical_pit_eligible"] is False
    assert availability["contract"]["basis"] == "acquisition_only"
    provenance = json.loads(revision["field_provenance_json"])
    assert provenance and all(value["source_id"] == "twse_isin" and value["raw_payload_id"] == revision["raw_payload_id"] for value in provenance.values())
    trace = platform.warehouse.lineage(revision["revision_id"])
    assert trace["raw_payload"]["integrity_status"] == "passed"
    assert trace["graph"]["completeness"]["status"] == "complete"
    assert trace["edges"][0]["raw_payload_id"] == revision["raw_payload_id"]
    assert platform.warehouse.query(DataQuery(dataset="security_master", entity_id=entity["entity_id"],
        knowledge_at="2026-09-12T11:59:59+00:00", effective_at=ACQUIRED)) == []
    assert len(platform.warehouse.query(DataQuery(dataset="security_master", entity_id=entity["entity_id"], as_of=ACQUIRED))) == 1


def test_repeated_ingestion_reuses_entity_identifiers_revisions_and_events(platform):
    retain_catalogue(platform, rows=[stock(), warrant()])
    first = ingest(platform)
    before = inventory(platform)
    revision_ids = [row["revision_id"] for row in revisions(platform)]
    with platform.warehouse._connect() as conn:
        event_count = conn.execute("select count(*) from entity_lifecycle_events").fetchone()[0]
    second = ingest(platform)
    assert first["created_count"] == 2 and second["created_count"] == 0
    assert second["refreshed_count"] == 2
    assert second["write"]["created_revision_count"] == 0
    assert second["write"]["created_event_count"] == 0
    assert inventory(platform) == before
    assert [row["revision_id"] for row in revisions(platform)] == revision_ids
    with platform.warehouse._connect() as conn:
        assert conn.execute("select count(*) from entity_lifecycle_events").fetchone()[0] == event_count


@pytest.mark.parametrize("failure", ["checkpoint_failed", "stale_acquisition", "stale_source", "future_acquisition"])
def test_unavailable_catalogue_does_not_create_unverified_identity(platform, failure):
    acquired = "2026-09-13T16:00:00+00:00" if failure == "stale_source" else ACQUIRED
    retain_catalogue(platform, rows=[stock(), warrant()], acquired_at=acquired)
    as_of = acquired
    if failure == "checkpoint_failed":
        with platform.warehouse._connect() as conn:
            conn.execute("update data_ingestion_checkpoints set status='failed' where partition_key='twse_isin_listed'")
    elif failure == "stale_acquisition":
        as_of = "2026-09-12T18:00:01+00:00"
    elif failure == "future_acquisition":
        as_of = "2026-09-12T11:59:59+00:00"
    result = ingest(platform, as_of=as_of)
    assert listed_report(result)["status"] == "unavailable"
    assert listed_report(result)["reasons"]
    assert result["created_count"] == 0 and inventory(platform) == {} and revisions(platform) == []
    receipt = platform.warehouse.get_checkpoint(source_id="twse_isin", dataset="security_master", partition_key="twse_isin_listed:identity")
    assert receipt["status"] == "unavailable"


def test_active_lifecycle_source_owner_is_not_downgraded_by_catalogue(platform):
    platform.sync_security_master_payloads(twse_companies=[{"公司代號": "2330", "公司簡稱": "離線股票", "公司名稱": "離線股票股份有限公司"}],
        twse_quotes=[{"Code": "2330", "Name": "離線股票"}], acquired_at=ACQUIRED)
    existing = inventory(platform)["2330.TW"]
    before = platform.warehouse.entity_profile(existing["entity_id"])
    retain_catalogue(platform, rows=[stock()])
    result = ingest(platform)
    assert result["existing_count"] == 1 and result["created_count"] == 0
    assert platform.warehouse.entity_profile(existing["entity_id"]) == before
    assert inventory(platform)["2330.TW"]["lifecycle_status"] == "active"


def test_exact_isin_owner_wins_over_historical_same_code_without_isin(platform):
    current_id = "ENT-" + "b" * 32
    old_id = "ENT-" + "c" * 32
    platform.warehouse.upsert_entity(EntityRecord(entity_id=current_id, entity_type="stock",
        canonical_name="離線股票", market="taiwan", exchange="TWSE", lifecycle_status="active",
        listed_at="1994-09-04T16:00:00+00:00", metadata={"display_symbol": "2330.TW"}),
        identifiers=[
            {"source_id": "twse_openapi", "identifier_type": "exchange_code", "identifier_value": "2330",
             "valid_from": "1994-09-04T16:00:00+00:00", "valid_to": None},
            {"source_id": "twse_isin", "identifier_type": "isin", "identifier_value": "TW0002330008",
             "valid_from": "1994-09-04T16:00:00+00:00", "valid_to": None},
        ])
    platform.warehouse.upsert_entity(EntityRecord(entity_id=old_id, entity_type="stock",
        canonical_name="離線舊股票", market="taiwan", exchange="TWSE", lifecycle_status="delisted",
        delisted_at="2002-11-04T00:00:00+00:00", metadata={"display_symbol": "2330.TW"}),
        identifiers=[{"source_id": "twse_openapi", "identifier_type": "exchange_code",
                      "identifier_value": "2330", "valid_from": None,
                      "valid_to": "2002-11-04T00:00:00+00:00"}])
    before_current = platform.warehouse.entity_profile(current_id)
    before_old = platform.warehouse.entity_profile(old_id)
    retain_catalogue(platform, rows=[stock()])
    result = ingest(platform)
    assert listed_report(result)["status"] == "succeeded"
    assert result["existing_count"] == 1 and result["created_count"] == 0
    assert platform.warehouse.entity_profile(current_id) == before_current
    assert platform.warehouse.entity_profile(old_id) == before_old


def test_expired_old_warrant_is_preserved_and_future_new_isin_has_separate_identity(platform):
    old_id = "ENT-" + "a" * 32
    platform.warehouse.upsert_entity(EntityRecord(entity_id=old_id, entity_type="warrant", canonical_name="離線舊發行",
        market="taiwan", exchange="TWSE", lifecycle_status="expired", listed_at="2025-01-01", delisted_at="2026-07-01",
        metadata={"expires_at": "2026-07-01", "display_symbol": CODE + ".TW", "source_datasets": ["twse_warrants"]}),
        identifiers=[{"source_id": "twse_openapi", "identifier_type": kind, "identifier_value": value,
            "valid_from": "2025-01-01", "valid_to": "2026-07-01", "metadata": {"venue": "TWSE"}}
            for kind, value in (("exchange_code", CODE), ("display_symbol", CODE + ".TW"))])
    before = platform.warehouse.entity_profile(old_id)
    retain_catalogue(platform, rows=[stock(), warrant(listed_on="2026/09/14")])
    result = ingest(platform)
    assert listed_report(result)["unresolved"] == []
    warrants = [row for row in platform.warehouse.identity_inventory() if row["entity_type"] == "warrant"]
    assert len(warrants) == 2
    new = next(row for row in warrants if row["entity_id"] != old_id)
    assert new["lifecycle_status"] == "pre_listing"
    assert new["metadata"]["issuance_identity"]["isin"] == ISIN
    assert platform.warehouse.entity_profile(old_id) == before

    # Registry search is investigative rather than a point-in-time resolver:
    # an exact reused code must expose both issuances without picking one.
    matches = platform.warehouse.list_entities(query=CODE, limit=10)
    assert {row["entity_id"] for row in matches} == {old_id, new["entity_id"]}


def test_lifecycle_expiry_closes_only_same_issue_catalogue_code_aliases_and_ingest_cannot_reopen(platform):
    retain_catalogue(platform, rows=[stock(), warrant()])
    ingest(platform)
    entity_id = inventory(platform)[CODE + ".TW"]["entity_id"]
    result = platform.sync_security_master_payloads(twse_warrants=[{"權證代號": CODE, "權證簡稱": NAME,
        "履約開始日": "1150912", "履約截止日": "1150912"}], acquired_at=ACQUIRED)
    assert result["status"] == "succeeded"
    profile = platform.warehouse.entity_profile(entity_id)
    assert profile["lifecycle_status"] == "active"
    for link in profile["identifiers"]:
        if link["source_id"] == "twse_isin":
            assert link["valid_to"] == (None if link["identifier_type"] == "isin" else "2026-09-12T16:00:00+00:00")
    assert platform.resolve_entity(CODE + ".TW", as_of="2026-09-12T15:59:59+00:00")["entity"]["entity_id"] == entity_id
    assert platform.resolve_entity(CODE + ".TW", as_of="2026-09-12T16:00:00+00:00")["status"] == "unavailable"
    ingest(platform, as_of="2026-09-12T12:01:00+00:00")
    assert platform.warehouse.entity_profile(entity_id) == profile


def test_listing_day_rollover_keeps_identity_unknown_until_lifecycle_source(platform):
    retain_catalogue(platform, rows=[stock(listed_on="2026/09/13")], acquired_at="2026-09-12T15:00:00+00:00")
    ingest(platform, as_of="2026-09-12T15:59:59+00:00")
    before = inventory(platform)["2330.TW"]
    assert before["lifecycle_status"] == "pre_listing"
    ingest(platform, as_of="2026-09-12T16:00:00+00:00")
    after = inventory(platform)["2330.TW"]
    assert after["entity_id"] == before["entity_id"]
    assert after["lifecycle_status"] == "unknown" and after["metadata"]["quote_present"] is False


def test_cross_venue_source_code_date_conflicts_reject_all_owners_but_third_venue_commits(platform):
    retain_catalogue(platform, dataset_id="twse_isin_listed", rows=[stock(code="1234", isin="TW0001234000", listed_on="2026/09/01")])
    retain_catalogue(platform, dataset_id="tpex_isin_otc", rows=[stock(code="1234", isin="TW0001234001", listed_on="2026/09/01")])
    retain_catalogue(platform, dataset_id="tpex_isin_emerging", rows=[stock(code="6950", name="離線興櫃", isin="TW0006950009", listed_on="2026/09/01")])
    result = ingest(platform)
    assert result["status"] == "partial" and result["created_count"] == 1
    assert set(inventory(platform)) == {"6950.TWO"}
    assert inventory(platform)["6950.TWO"]["exchange"] == "TPEx-ESB"
    by_dataset = {row["source_dataset"]: row for row in result["partitions"]}
    for dataset in ("twse_isin_listed", "tpex_isin_otc"):
        report = by_dataset[dataset]
        assert report["status"] == "partial" and report["created_count"] == 0
        assert len(report["unresolved"]) == 1
        assert report["unresolved"][0]["code"] == "1234"
        assert report["unresolved"][0]["reason"] == "catalogue_identifier_interval_conflict"
        checkpoint = platform.warehouse.get_checkpoint(source_id="twse_isin", dataset="security_master", partition_key=dataset + ":identity")
        assert checkpoint["status"] == "partial"
        assert checkpoint["metadata"]["unresolved"] == report["unresolved"]
    assert by_dataset["tpex_isin_emerging"]["status"] == "succeeded"
    assert len(revisions(platform)) == 1
