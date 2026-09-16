"""Real loader orchestration with synthetic retained sources and no network."""
from copy import deepcopy
import json
import socket

import pytest

from stock_ai.data_platform import product_catalog, security_loader, warehouse as warehouse_module
from stock_ai.data_platform.service import MarketDataPlatform
from test_catalogue_issue_identity import ACQUIRED, retain_catalogue


CODE = "085974"
NAME = "離線權證發行"
ISIN = "TW26Z0859747"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("loader fixture must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: ACQUIRED)
    platform = MarketDataPlatform(database_path=tmp_path / "target.sqlite")
    seed = MarketDataPlatform(database_path=tmp_path / "synthetic-captures.sqlite")
    captures = {}
    for dataset in product_catalog.CATALOGUES:
        rows = ([{"code": CODE, "name": NAME, "isin": ISIN, "listed_on": "2026/09/01"}]
                if dataset == "twse_isin_listed" else None)
        receipts = retain_catalogue(seed, dataset_id=dataset, rows=rows)
        retained = seed.warehouse.raw_payload(receipts[0]["raw_payload_id"], include_body=True)
        url = product_catalog.source_endpoint(dataset)
        captures[dataset] = {"raw": retained["body"], "source_url": url, "effective_url": url,
            "requested_at": ACQUIRED, "acquired_at": ACQUIRED, "http_status": 200,
            "content_type": "text/html;charset=MS950"}
    payloads = {name: [] for name in (
        "twse_companies", "twse_quotes", "twse_etfs", "twse_warrants", "twse_indices", "twse_delisted",
        "tpex_companies", "tpex_quotes", "tpex_emerging_companies", "tpex_emerging_quotes", "tpex_warrants",
        "tpex_indices", "tpex_delisted")}
    payloads.update(
        twse_companies=[{"公司代號": "2330", "公司簡稱": "離線股票", "公司名稱": "離線股票股份有限公司", "上市日期": "0830905"}],
        twse_quotes=[{"Code": "2330", "Name": "離線股票"}],
        twse_warrants=[{"權證代號": CODE, "權證簡稱": NAME, "履約開始日": "1160901", "履約截止日": "1160901"}],
        tpex_companies=[{"SecuritiesCompanyCode": "8299", "CompanyAbbreviation": "離線上櫃", "CompanyName": "離線上櫃股份有限公司", "DateOfListing": "2004/12/06"}],
        tpex_quotes=[{"SecuritiesCompanyCode": "8299", "CompanyName": "離線上櫃"}],
    )
    events = []
    def fetch_catalogue(dataset):
        events.append("catalogue:" + dataset)
        return deepcopy(captures[dataset])
    monkeypatch.setattr(product_catalog, "fetch_product_catalogue", fetch_catalogue)
    for name in payloads:
        def fetch_source(name=name):
            events.append("source:" + name)
            return deepcopy(payloads[name])
        monkeypatch.setattr(security_loader, name, fetch_source)
    return platform, captures, payloads, events


def partitions(result):
    return {row["partition_key"]: row for row in result["partitions"]}


def checkpoint(platform, partition, source="twse_openapi"):
    return platform.warehouse.get_checkpoint(source_id=source, dataset="security_master", partition_key=partition)


def test_actual_catalogue_precedes_warrant_persist_and_creates_bound_issue(setup, monkeypatch):
    platform, _captures, _payloads, events = setup
    original = platform.sync_security_master_payloads
    persisted = []
    def sync(**kwargs):
        if kwargs.get("twse_warrants"):
            assert all(checkpoint(platform, dataset, "twse_isin")["status"] == "succeeded"
                       for dataset in product_catalog.CATALOGUES)
            persisted.append("warrant")
        return original(**kwargs)
    monkeypatch.setattr(platform, "sync_security_master_payloads", sync)
    result = security_loader.OfficialSecurityMasterLoader(platform).run(as_of=ACQUIRED)
    assert result["status"] == "succeeded"
    assert events[:3] == ["catalogue:" + dataset for dataset in product_catalog.CATALOGUES]
    assert persisted == ["warrant"]
    item = next(row for row in platform.warehouse.identity_inventory() if row["entity_type"] == "warrant")
    assert item["metadata"]["issuance_identity"]["isin"] == ISIN
    assert item["metadata"]["quote_present"] is False
    assert item["metadata"]["product_classifications"]["TWSE:" + CODE + ".TW"]["raw_payload_id"].startswith("RAW-")
    ordinary = next(row for row in platform.warehouse.identity_inventory()
                    if row["metadata"]["display_symbol"] == "2330.TW")
    assert ordinary["metadata"]["product_classifications"]["TWSE:2330.TW"]["product_type"] == "ordinary_stock"


def test_fresh_retained_catalogues_bind_first_master_load_without_downloading(setup):
    platform, _captures, _payloads, events = setup
    assert all(row["status"] == "succeeded" for row in product_catalog.OfficialProductClassificationLoader(platform).run(as_of=ACQUIRED))
    events.clear()
    result = security_loader.OfficialSecurityMasterLoader(platform).run(as_of=ACQUIRED)
    by_partition = partitions(result)
    assert all(by_partition[dataset]["status"] == "skipped_fresh" for dataset in product_catalog.CATALOGUES)
    assert all(not event.startswith("catalogue:") for event in events)
    assert by_partition["twse_official_master"]["sync"]["identity_resolution"]["resolved_warrant_count"] == 1


def test_expired_archive_gap_does_not_fail_current_master_partition(setup):
    platform, captures, payloads, _events = setup
    # The daily catalogue contains a different current issuance; the warrant
    # archive still carries one already-expired code whose reused identity
    # cannot safely be inferred without historical issuance proof.
    captures["twse_isin_listed"]["raw"] = captures["twse_isin_listed"]["raw"].replace(
        CODE.encode(), b"085975"
    )
    payloads["twse_warrants"][0]["履約開始日"] = "1150901"
    payloads["twse_warrants"][0]["履約截止日"] = "1150901"
    result = security_loader.OfficialSecurityMasterLoader(platform).run(as_of=ACQUIRED)
    master = partitions(result)["twse_official_master"]
    resolution = master["sync"]["identity_resolution"]
    assert result["status"] == master["status"] == "succeeded"
    assert master["sync"]["status"] == "succeeded_with_historical_gaps"
    assert resolution["current_baseline_complete"] is True
    assert resolution["unresolved_count"] == 0
    assert resolution["historical_unresolved_count"] == 1
    assert checkpoint(platform, "twse_warrants")["status"] == "partial"
    assert checkpoint(platform, "twse_official_master")["status"] == "succeeded"


@pytest.mark.parametrize("failure", ["listing_date", "http_status"])
def test_bad_catalogue_retains_unresolved_rows_without_certifying_cache_and_other_rows_update(setup, failure):
    platform, captures, _payloads, events = setup
    if failure == "listing_date":
        captures["twse_isin_listed"]["raw"] = captures["twse_isin_listed"]["raw"].replace(b"2026/09/01", b"2026/02/30")
    else:
        captures["twse_isin_listed"]["http_status"] = 503
    loader = security_loader.OfficialSecurityMasterLoader(platform)
    result = loader.run(as_of=ACQUIRED)
    by_partition = partitions(result)
    assert result["status"] == "partial"
    assert by_partition["twse_official_master"]["status"] == "failed"
    assert by_partition["twse_official_master"]["sync"]["status"] == "partial"
    failed_run_id = by_partition["twse_official_master"]["run_id"]
    assert failed_run_id == checkpoint(platform, "twse_official_master")["metadata"]["run_id"]
    assert by_partition["twse_official_master"]["dataset"] == "security_master"
    assert platform.warehouse.ingestion_run(failed_run_id)["status"] == "failed"
    assert by_partition["tpex_official_master"]["status"] == "succeeded"
    inventory = platform.warehouse.identity_inventory()
    assert {row["metadata"]["display_symbol"] for row in inventory} == {"2330.TW", "8299.TWO", "7001.TWO"}
    assert all(row["entity_type"] == "stock" for row in inventory)
    assert all(row["lifecycle_status"] == "active" for row in inventory
               if row["metadata"]["display_symbol"] != "7001.TWO")
    emerging = next(row for row in inventory if row["metadata"]["display_symbol"] == "7001.TWO")
    assert emerging["lifecycle_status"] == "unknown"
    assert emerging["metadata"]["catalogue_only"] is True
    rejected = checkpoint(platform, "twse_warrants")
    assert rejected["status"] == "partial"
    assert rejected["metadata"]["unresolved_identity_count"] == 1
    assert rejected["metadata"]["unresolved_identities"][0]["code"] == CODE
    assert rejected["metadata"]["unresolved_identities"][0]["reason"] == "verified_warrant_issuance_unavailable"
    retained = platform.warehouse.raw_payload(rejected["metadata"]["raw_payload_id"], include_body=True)
    assert retained["integrity_status"] == "passed"
    assert json.loads(retained["body"])[0]["權證代號"] == CODE
    aggregate = checkpoint(platform, "twse_official_master")
    assert aggregate["status"] == "failed" and aggregate["last_success_at"] is None
    entry = platform.warehouse.cache_entry(source_id="twse_openapi", dataset="security_master", partition_key="twse_official_master")
    assert entry["refreshed_at"] is None and entry["fresh_until"] is None
    events.clear()
    retry = partitions(loader.run(as_of="2026-09-12T12:01:00+00:00"))
    assert retry["twse_official_master"]["status"] == "failed"
    assert "source:twse_warrants" in events
    assert retry["tpex_official_master"]["status"] == "skipped_fresh"
    assert checkpoint(platform, "twse_warrants")["metadata"]["unresolved_identity_count"] == 1


@pytest.mark.parametrize("same_body", [False, True], ids=["error_page_to_catalogue", "same_bytes_status_recovery"])
def test_failed_catalogue_recovers_with_normal_retry_without_forcing_identity(setup, same_body):
    platform, captures, _payloads, _events = setup
    successful_body = captures["twse_isin_listed"]["raw"]
    captures["twse_isin_listed"]["http_status"] = 503
    if not same_body:
        captures["twse_isin_listed"]["raw"] = b"<html>synthetic upstream unavailable</html>"
    loader = security_loader.OfficialSecurityMasterLoader(platform)
    assert loader.run(as_of=ACQUIRED)["status"] == "partial"
    failed_checkpoint = checkpoint(platform, "twse_official_master")
    failed_run_id = failed_checkpoint["metadata"]["run_id"]
    failed_raw_id = checkpoint(platform, "twse_warrants")["metadata"]["raw_payload_id"]
    failed_run = platform.warehouse.ingestion_run(failed_run_id)
    assert failed_run["status"] == "failed"
    assert len(failed_run["batches"]) == 1 and failed_run["batches"][0]["status"] == "failed"
    for metadata in (failed_run["metadata"], failed_run["batches"][0]["metadata"], failed_checkpoint["metadata"]):
        assert metadata["raw_payload_ids"]["twse_warrants"] == failed_raw_id
        assert metadata["identity_resolution"]["unresolved_count"] == 1
        assert metadata["identity_resolution"]["unresolved"] == [{"source_dataset": "twse_warrants",
            "venue": "TWSE", "code": CODE, "symbol": CODE + ".TW", "reason": "verified_warrant_issuance_unavailable"}]
    captures["twse_isin_listed"]["http_status"] = 200
    captures["twse_isin_listed"]["raw"] = successful_body
    # A successful subsequent request is a new capture, never a mutation of
    # the same immutable failed response's acquisition identity.
    captures["twse_isin_listed"]["requested_at"] = "2026-09-12T12:01:00+00:00"
    captures["twse_isin_listed"]["acquired_at"] = "2026-09-12T12:01:00+00:00"
    result = loader.run(as_of="2026-09-12T12:01:00+00:00")
    assert result["status"] == "succeeded"
    assert partitions(result)["twse_official_master"]["sync"]["identity_resolution"]["unresolved_count"] == 0
    assert checkpoint(platform, "twse_warrants")["status"] == "succeeded"
    assert sum(row["entity_type"] == "warrant" for row in platform.warehouse.identity_inventory()) == 1
    assert checkpoint(platform, "twse_official_master")["metadata"]["run_id"] != failed_run_id
    assert platform.warehouse.ingestion_run(failed_run_id) == failed_run
    assert platform.warehouse.raw_payload(failed_raw_id)["integrity_status"] == "passed"


def test_failed_forced_refresh_cannot_hide_unresolved_group_behind_previous_fresh_cache(setup):
    platform, _captures, payloads, events = setup
    loader = security_loader.OfficialSecurityMasterLoader(platform)
    assert loader.run(as_of=ACQUIRED)["status"] == "succeeded"
    payloads["twse_warrants"][0]["權證簡稱"] = "不同發行無法綁定"
    failure = loader.run(force=True, as_of="2026-09-12T12:01:00+00:00")
    assert failure["status"] == "partial"
    assert checkpoint(platform, "twse_warrants")["metadata"]["unresolved_identity_count"] == 1
    previous = platform.warehouse.cache_entry(source_id="twse_openapi", dataset="security_master", partition_key="twse_official_master")
    assert previous["refreshed_at"] == ACQUIRED  # Failed attempt must not advance success time.
    assert previous["invalidated_at"] == "2026-09-12T12:01:00+00:00"
    assert platform.cache_policy_service.decision(source_id="twse_openapi", dataset="security_master",
        partition_key="twse_official_master", as_of="2026-09-12T12:02:00+00:00", record=False)["state"] == "invalidated"
    events.clear()
    retry = partitions(loader.run(as_of="2026-09-12T12:02:00+00:00"))
    assert retry["twse_official_master"]["status"] == "failed"
    assert "source:twse_warrants" in events


def test_forced_catalogue_failure_invalidates_old_success_and_normal_retry_recovers(setup):
    platform, captures, _payloads, events = setup
    loader = security_loader.OfficialSecurityMasterLoader(platform)
    assert loader.run(as_of=ACQUIRED)["status"] == "succeeded"
    original_body = captures["twse_isin_listed"]["raw"]
    captures["twse_isin_listed"].update(raw=b"<html>synthetic failed refresh</html>", http_status=503,
        requested_at="2026-09-12T12:01:00+00:00", acquired_at="2026-09-12T12:01:00+00:00")
    assert loader.run(force=True, as_of="2026-09-12T12:01:00+00:00")["status"] == "partial"
    assert checkpoint(platform, "twse_isin_listed", "twse_isin")["status"] == "failed"
    cache = platform.warehouse.cache_entry(source_id="twse_isin", dataset="security_master", partition_key="twse_isin_listed")
    assert cache["refreshed_at"] == ACQUIRED
    decision = platform.cache_policy_service.decision(source_id="twse_isin", dataset="security_master",
        partition_key="twse_isin_listed", as_of="2026-09-12T12:02:00+00:00", record=False)
    captures["twse_isin_listed"].update(raw=original_body, http_status=200,
        requested_at="2026-09-12T12:02:00+00:00", acquired_at="2026-09-12T12:02:00+00:00")
    events.clear()
    retry = loader.run(as_of="2026-09-12T12:02:00+00:00")
    assert partitions(retry)["twse_isin_listed"]["status"] == "succeeded"
    assert "catalogue:twse_isin_listed" in events
    assert cache["invalidated_at"] == "2026-09-12T12:01:00+00:00"
    assert decision["state"] == "invalidated"
    assert retry["status"] == "succeeded"
    assert partitions(retry)["twse_official_master"]["sync"]["identity_resolution"]["unresolved_count"] == 0
