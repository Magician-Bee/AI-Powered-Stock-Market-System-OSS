"""Isolated issuance fixtures test source binding through the real lifecycle writer."""
import socket

import pytest

from stock_ai.data_platform import warehouse as warehouse_module
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from test_catalogue_issue_identity import retain_catalogue

ACQUIRED = "2026-09-12T12:00:00+00:00"
OLD_ID = "ENT-" + "a" * 32
CODE = "085974"
NAME = "離線新發行"
ISIN = "TW26Z0859747"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("isolated warrant tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: ACQUIRED)
    return MarketDataPlatform(database_path=tmp_path / "warrant.sqlite")


def catalogue(platform, *, name=NAME, listed="2026/09/14", isin=ISIN, venue="TWSE", **kwargs):
    return retain_catalogue(platform, dataset_id="twse_isin_listed" if venue == "TWSE" else "tpex_isin_otc",
        rows=[{"code": CODE, "name": name, "isin": isin, "listed_on": listed, **kwargs}], acquired_at=ACQUIRED)


def payload(*, name=NAME, expiry="1160314", venue="TWSE", listed="20260914"):
    return {"twse_warrants": [{"權證代號": CODE, "權證簡稱": name, "履約開始日": expiry,
                              "履約截止日": expiry}]} if venue == "TWSE" else {
        "tpex_warrants": [{"Code": CODE, "Name": name, "ListedDate": listed, "ExpiryDate": expiry}]}


def legacy(platform, *, name="離線舊發行", expiry="2026-07-01", entity_id=OLD_ID, venue="TWSE"):
    suffix = ".TW" if venue == "TWSE" else ".TWO"
    entity = EntityRecord(entity_id=entity_id, entity_type="warrant", canonical_name=name,
        market="taiwan", exchange=venue, lifecycle_status="expired",
        delisted_at="2026-07-01", metadata={"expires_at": expiry, "display_symbol": CODE + suffix,
                                         "source_datasets": ["twse_warrants"], "fixture_only": True})
    platform.warehouse.upsert_entity(entity, identifiers=[
        {"source_id": "twse_openapi" if venue == "TWSE" else "tpex_openapi", "identifier_type": kind,
         "identifier_value": value, "valid_to": "2026-07-01", "metadata": {"venue": venue}}
        for kind, value in (("exchange_code", CODE), ("display_symbol", CODE + suffix))])
    return platform.warehouse.entity_profile(entity_id)


def test_code_reuse_new_issue_preserves_old_profile_and_restarts_with_future_date(platform):
    old = legacy(platform)
    catalogue(platform)
    result = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert result["status"] == "succeeded"
    assert result["by_status"] == {"pre_listing": 1}
    assert platform.warehouse.entity_profile(OLD_ID) == old
    new = next(r for r in platform.warehouse.identity_inventory() if r["entity_id"] != OLD_ID)
    assert new["entity_id"] != OLD_ID
    assert new["metadata"]["quote_present"] is False
    assert new["metadata"]["issuance_identity"]["isin"] == ISIN
    assert new["listed_at"] == "2026-09-13T16:00:00+00:00"
    assert new["metadata"]["product_classifications"][f"TWSE:{CODE}.TW"]["product_type"] == "warrant"
    profile = platform.warehouse.entity_profile(new["entity_id"])
    assert {i["source_id"] for i in profile["identifiers"] if i["identifier_type"] == "isin"} == {"twse_isin"}
    restarted = MarketDataPlatform(database_path=platform.warehouse.path)
    assert restarted.warehouse.entity_profile(OLD_ID) == old
    assert restarted.resolve_entity(f"{CODE}.TW")["status"] == "unavailable"
    assert restarted.resolve_entity(f"{CODE}.TW", as_of="2026-09-13T16:00:00+00:00")["entity"]["entity_id"] == new["entity_id"]
    replay = restarted.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert replay["revision_count"] == 0
    assert len(restarted.warehouse.identity_inventory()) == 2


def test_legacy_extension_adopts_same_entity_and_uses_real_listing_date(platform):
    old = legacy(platform, name=NAME, expiry="2026-09-02")
    catalogue(platform, listed="2014/07/31")
    result = platform.sync_security_master_payloads(**payload(expiry="1160302"), acquired_at=ACQUIRED)
    assert result["status"] == "succeeded"
    assert len(platform.warehouse.identity_inventory()) == 1
    current = platform.warehouse.entity_profile(OLD_ID)
    assert current["canonical_name"] == old["canonical_name"]
    assert current["lifecycle_status"] == "active"
    assert current["listed_at"] == "2014-07-30T16:00:00+00:00"
    assert current["metadata"]["expires_at"].startswith("2027-03-02")
    assert current["metadata"]["quote_present"] is False
    assert any(i["valid_from"] is None for i in current["identifiers"])
    assert platform.resolve_entity(f"{CODE}.TW", as_of="2027-03-02T15:59:59+00:00")["entity"]["entity_id"] == OLD_ID
    assert platform.resolve_entity(f"{CODE}.TW", as_of="2027-03-02T16:00:00+00:00")["status"] == "unavailable"


def test_known_isin_preserves_identity_across_name_and_expiry_corrections(platform):
    catalogue(platform, listed="2026/09/01")
    platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    entity_id = platform.warehouse.identity_inventory()[0]["entity_id"]
    catalogue(platform, name="離線更正名稱", listed="2026/09/01")
    result = platform.sync_security_master_payloads(**payload(name="離線更正名稱", expiry="1160901"), acquired_at=ACQUIRED)
    assert result["status"] == "succeeded"
    assert [e["entity_id"] for e in platform.warehouse.identity_inventory()] == [entity_id]
    assert platform.warehouse.entity_profile(entity_id)["canonical_name"] == "離線更正名稱"


def test_new_capture_of_same_issue_updates_proof_without_duplicate_business_revision(platform):
    catalogue(platform, listed="2026/09/01")
    first = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    entity_id = platform.warehouse.identity_inventory()[0]["entity_id"]
    old_proof = platform.warehouse.entity_profile(entity_id)["metadata"]["issuance_identity"]
    later = "2026-09-12T12:01:00+00:00"
    retain_catalogue(platform, rows=[{"code": CODE, "name": NAME, "isin": ISIN, "listed_on": "2026/09/01"}], acquired_at=later)
    refreshed = platform.sync_security_master_payloads(**payload(), acquired_at=later)
    new_proof = platform.warehouse.entity_profile(entity_id)["metadata"]["issuance_identity"]
    assert first["revision_count"] == 1
    assert refreshed["revision_count"] == 0 and refreshed["reused_revision_count"] == 1
    assert new_proof["raw_payload_id"] != old_proof["raw_payload_id"]
    from stock_ai.data_platform.warehouse import content_hash
    assert new_proof["receipt_sha256"] == content_hash({k: v for k, v in new_proof.items() if k != "receipt_sha256"})


@pytest.mark.parametrize("name,expiry", [(NAME, None), (NAME, "zzzz"), ("無法證明的更名", "2027-03-01")])
def test_legacy_unknown_or_overlapping_name_conflict_stays_unresolved(platform, name, expiry):
    old = legacy(platform, name=name, expiry=expiry)
    # Remove the secondary end-date when checking truly unknown identity bounds.
    if expiry is None:
        with platform.warehouse._connect() as conn:
            conn.execute("update market_entities set delisted_at=null where entity_id=?", (OLD_ID,))
            conn.commit()
        old = platform.warehouse.entity_profile(OLD_ID)
    catalogue(platform)
    result = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert result["status"] == "partial"
    assert result["identity_resolution"]["unresolved"][0]["reason"] == "warrant_legacy_issuance_unresolved"
    assert platform.warehouse.entity_profile(OLD_ID) == old
    assert len(platform.warehouse.identity_inventory()) == 1


@pytest.mark.parametrize("change,reason", [
    ({"name": "來源不同名稱"}, "warrant_catalogue_name_conflict"),
    ({"expiry": "1150901"}, "warrant_expiry_missing_or_before_listing"),
])
def test_conflicting_source_row_retains_raw_and_checkpoint_without_new_entity(platform, change, reason):
    catalogue(platform)
    result = platform.sync_security_master_payloads(**payload(**change), acquired_at=ACQUIRED)
    assert result["status"] == "partial" and result["count"] == 0
    assert result["identity_resolution"]["unresolved"][0]["reason"] == reason
    checkpoint = platform.warehouse.get_checkpoint(source_id="twse_openapi", dataset="security_master", partition_key="twse_warrants")
    assert checkpoint["status"] == "partial"
    assert checkpoint["metadata"]["unresolved_identities"][0]["reason"] == reason
    assert checkpoint["metadata"]["raw_payload_id"].startswith("RAW-")
    assert platform.warehouse.identity_inventory() == []


def test_missing_proof_does_not_cache_unresolved_and_later_verified_proof_recovers(platform):
    first = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert first["status"] == "partial"
    catalogue(platform)
    second = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert second["status"] == "succeeded" and second["count"] == 1


def test_expired_archive_row_without_current_issuance_is_retained_without_blocking_current_baseline(platform):
    result = platform.sync_security_master_payloads(**payload(expiry="1150901"), acquired_at=ACQUIRED)
    resolution = result["identity_resolution"]
    assert result["status"] == "succeeded_with_historical_gaps"
    assert resolution["current_baseline_complete"] is True
    assert resolution["unresolved_count"] == 0 and resolution["unresolved"] == []
    assert resolution["historical_unresolved_count"] == resolution["total_unresolved_count"] == 1
    assert resolution["historical_unresolved"] == [{
        "source_dataset": "twse_warrants", "venue": "TWSE", "code": CODE,
        "symbol": CODE + ".TW", "reason": "verified_warrant_issuance_unavailable",
    }]
    checkpoint = platform.warehouse.get_checkpoint(
        source_id="twse_openapi", dataset="security_master", partition_key="twse_warrants"
    )
    assert checkpoint["status"] == "partial"
    assert checkpoint["metadata"]["unresolved_identity_count"] == 1
    assert platform.warehouse.identity_inventory() == []


def test_wrong_product_type_cannot_promote_warrant_to_ordinary_entry_classification(platform):
    catalogue(platform, section="股票", cfi="ESVUFR")
    result = platform.sync_security_master_payloads(**payload(), acquired_at=ACQUIRED)
    assert result["count"] == 0
    assert result["identity_resolution"]["unresolved"][0]["reason"] == "warrant_catalogue_product_type_conflict"


def test_tpex_true_listing_date_must_agree_and_future_has_no_quote(platform):
    catalogue(platform, venue="TPEx")
    mismatch = platform.sync_security_master_payloads(**payload(venue="TPEx", listed="20260913"), acquired_at=ACQUIRED)
    assert mismatch["identity_resolution"]["unresolved"][0]["reason"] == "warrant_catalogue_listing_date_conflict"
    result = platform.sync_security_master_payloads(**payload(venue="TPEx"), acquired_at=ACQUIRED)
    assert result["by_status"] == {"pre_listing": 1}
    assert platform.warehouse.identity_inventory()[0]["metadata"]["quote_present"] is False


def test_same_code_and_name_across_venues_get_separate_issuance_ids(platform):
    catalogue(platform, listed="2026/09/01")
    catalogue(platform, listed="2026/09/01", venue="TPEx", isin="TW26Z0859748")
    result = platform.sync_security_master_payloads(**payload(), **payload(venue="TPEx", listed="20260901"), acquired_at=ACQUIRED)
    assert result["count"] == 2
    assert len({e["entity_id"] for e in platform.warehouse.identity_inventory()}) == 2


def test_market_day_boundary_recomputes_prelisting_without_changing_issue_id(platform):
    catalogue(platform, listed="2026/09/13")
    first = platform.sync_security_master_payloads(**payload(), acquired_at="2026-09-12T15:59:59+00:00")
    entity_id = platform.warehouse.identity_inventory()[0]["entity_id"]
    second = platform.sync_security_master_payloads(**payload(), acquired_at="2026-09-12T16:00:00+00:00")
    assert first["by_status"] == {"pre_listing": 1}
    assert second["by_status"] == {"active": 1}
    assert platform.warehouse.identity_inventory()[0]["entity_id"] == entity_id
