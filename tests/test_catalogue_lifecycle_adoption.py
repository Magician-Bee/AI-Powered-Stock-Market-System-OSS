"""Offline retained-source fixtures; catalogue/API identity adoption only."""
from copy import deepcopy
from datetime import datetime, timedelta
import socket

import pytest

from stock_ai.data_platform.catalogue_identity import retained_catalogue_issue_bindings
from stock_ai.data_platform.security_lifecycle import consolidate_security_snapshots, normalize_security_payloads
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.warehouse import content_hash
from test_catalogue_issue_identity import ACQUIRED, retain_catalogue


OWNER = "ENT-" + "a" * 32


@pytest.fixture
def issue_factory(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("catalogue adoption tests cannot use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    platform = MarketDataPlatform(database_path=tmp_path / "catalogue-adoption.sqlite")

    def build(*, code="6950", name="科科科技-KY", isin="KYG5275M1015", dataset="tpex_isin_emerging", kind="ordinary_stock", listed="2026/08/31"):
        section, cfi = {"ordinary_stock": ("股票", "ESVUFR"), "etf": ("ETF", "CEOIBU"),
                        "other": ("受益證券-資產基礎證券", "DAFUFR"), "warrant": ("上市認購(售)權證", "RWSCCE")}[kind]
        retain_catalogue(platform, dataset_id=dataset, rows=[{"code": code, "name": name, "isin": isin,
                         "listed_on": listed, "section": section, "cfi": cfi}])
        result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
        venue = {"tpex_isin_emerging": "TPEx-ESB", "twse_isin_listed": "TWSE", "tpex_isin_otc": "TPEx"}[dataset]
        return result["bindings"][(venue, code)]
    return build


def owner(issue, *, entity_id=OWNER, kind=None):
    kind = kind or {"ordinary_stock": "stock", "etf": "etf", "warrant": "warrant"}.get(issue["product_type"], "security")
    return {"entity_id": entity_id, "entity_type": kind, "canonical_name": issue["source_name"],
            "exchange": issue["venue"], "lifecycle_status": "unknown", "listed_at": issue["listed_at"], "delisted_at": None,
            "metadata": {"identity_origin": "official_catalogue", "catalogue_only": True,
                         "issuance_identity": deepcopy(issue), "source_code": issue["code"],
                         "display_symbol": issue["symbol"], "source_datasets": [issue["source_dataset"]],
                         "preserved_metadata": "fixture"},
            "identifiers": [{"identifier_type": kind, "identifier_value": value, "source_id": "twse_isin"}
                            for kind, value in (("exchange_code", issue["code"]), ("display_symbol", issue["symbol"]), ("isin", issue["isin"]))]}


def api_payload(issue, *, legal_name="科科科技股份有限公司"):
    code = issue["code"]
    if issue["venue"] == "TWSE":
        return {"twse_companies": [{"公司代號": code, "公司名稱": legal_name, "公司簡稱": issue["source_name"],
                                    "營利事業統一編號": "12345678", "上市日期": issue["listed_on"]}],
                "twse_quotes": [{"Code": code, "Name": issue["source_name"]}]}
    prefix = "tpex_emerging" if issue["venue"] == "TPEx-ESB" else "tpex"
    return {f"{prefix}_companies": [{"SecuritiesCompanyCode": code, "CompanyName": legal_name,
             "CompanyAbbreviation": issue["source_name"], "UnifiedBusinessNo.": "12345678", "DateOfListing": issue["listed_on"]}],
            f"{prefix}_quotes": [{"SecuritiesCompanyCode": code, "CompanyName": issue["source_name"]}]}


def run(issue, inventory, *, bindings=None, as_of=ACQUIRED, payload=None):
    if bindings is None:
        bindings = {(issue["venue"], issue["code"]): issue}
    snapshots = normalize_security_payloads(payload or api_payload(issue), acquired_at=as_of, issue_bindings=bindings)
    return consolidate_security_snapshots(snapshots, acquired_at=as_of, existing_inventory=inventory)


def test_real_short_and_legal_name_shapes_adopt_same_catalogue_stock_id(issue_factory):
    issue = issue_factory(); existing = owner(issue); before = deepcopy(existing)
    result = run(issue, [existing]); entity, = result["entities"]
    assert not result["unresolved"] and entity.entity_id == OWNER
    assert entity.canonical_name == "科科科技股份有限公司" and entity.entity_type == "stock"
    assert entity.lifecycle_status == "active"
    assert entity.listed_at == issue["listed_at"]
    assert entity.metadata["identity_origin"] == "official_catalogue" and entity.metadata["catalogue_only"] is False
    assert entity.metadata["preserved_metadata"] == "fixture" and existing == before
    assert entity.metadata["issuance_identity"] == issue
    assert content_hash({k: v for k, v in entity.metadata["issuance_identity"].items() if k != "receipt_sha256"}) == issue["receipt_sha256"]
    assert result["revisions"][0]["payload"]["metadata"]["issuance_identity"] == issue
    assert any(r["identifier_type"] == "isin" and r["identifier_value"] == issue["isin"] and r["entity_id"] == OWNER for r in result["identifiers"])
    assert all(r["valid_from"] == issue["listed_at"] for r in result["identifiers"])


def test_etf_adopts_security_id_without_using_shared_issuer_number(issue_factory):
    issue = issue_factory(code="00838B", name="永豐7-10年中國債", isin="TW00000838B2", dataset="tpex_isin_otc", kind="etf", listed="2019/06/13")
    result = run(issue, [owner(issue)], payload=api_payload(issue, legal_name="永豐證券投資信託股份有限公司"))
    entity, = result["entities"]
    assert entity.entity_id == OWNER and entity.entity_type == "etf"
    assert entity.metadata["catalogue_only"] is False
    assert not any(r["identifier_type"] == "unified_business_no" for r in result["identifiers"])


def test_reused_code_conflicting_isin_never_overwrites_catalogue_owner(issue_factory):
    old = issue_factory(); existing = owner(old)
    new = issue_factory(isin="TW0006950001")
    before = deepcopy(existing); result = run(new, [existing])
    assert result["entities"] == [] and existing == before
    assert result["unresolved"][0]["reason"] == "catalogue_issuance_isin_conflict"


def test_multiple_same_isin_catalogue_owners_are_ambiguous(issue_factory):
    issue = issue_factory()
    result = run(issue, [owner(issue), owner(issue, entity_id="ENT-" + "b" * 32)])
    assert result["entities"] == []
    assert result["unresolved"][0]["reason"] == "catalogue_issuance_owner_ambiguous"


def test_different_venue_cannot_adopt_catalogue_owner_by_name_or_business(issue_factory):
    old = issue_factory(); existing = owner(old)
    existing["canonical_name"] = "科科科技股份有限公司"
    existing["identifiers"].append({"identifier_type": "unified_business_no", "identifier_value": "12345678"})
    current = issue_factory(dataset="twse_isin_listed")
    result = run(current, [existing]); entity, = result["entities"]
    assert entity.entity_id != OWNER and entity.exchange == "TWSE"
    assert "identity_origin" not in entity.metadata


@pytest.mark.parametrize("failure", ["missing", "stale", "failed", "hash", "incomplete"])
def test_unverified_proof_cannot_fall_back_to_name_adoption(issue_factory, failure):
    issue = issue_factory(); existing = owner(issue)
    existing["canonical_name"] = "科科科技股份有限公司"
    binding = deepcopy(issue); now = ACQUIRED
    if failure == "stale":
        now = (datetime.fromisoformat(ACQUIRED) + timedelta(hours=6, seconds=1)).isoformat()
    elif failure == "failed":
        binding["product_classification"]["status"] = "unknown"
    elif failure == "hash":
        binding["receipt_sha256"] = "0" * 64
    elif failure == "incomplete":
        binding.pop("product_classification")
    bindings = {} if failure == "missing" else {(issue["venue"], issue["code"]): binding}
    result = run(issue, [existing], bindings=bindings, as_of=now)
    assert result["entities"] == [] and len(result["unresolved"]) == 1


def test_generic_catalogue_security_cannot_become_api_guessed_stock(issue_factory):
    issue = issue_factory(code="01111S", name="081中租賃A", isin="TW00001111S7", dataset="tpex_isin_otc", kind="other", listed="2019/12/11")
    existing = owner(issue); before = deepcopy(existing)
    result = run(issue, [existing], payload=api_payload(issue, legal_name=issue["source_name"]))
    assert result["entities"] == [] and existing == before
    assert result["unresolved"][0]["reason"] == "catalogue_issuance_product_type_conflict"


def test_future_catalogue_listing_cannot_become_active_from_api_quote(issue_factory):
    issue = issue_factory(listed="2026/09/14")
    result = run(issue, [owner(issue)]); entity, = result["entities"]
    assert entity.entity_id == OWNER and entity.lifecycle_status == "pre_listing"
    assert entity.metadata["quote_present"] is False


def test_conflicting_api_original_listing_date_stays_unresolved(issue_factory):
    issue = issue_factory(); payload = api_payload(issue)
    payload["tpex_emerging_companies"][0]["DateOfListing"] = "2025-08-31"
    result = run(issue, [owner(issue)], payload=payload)
    assert not result["entities"]
    assert result["unresolved"][0]["reason"] == "catalogue_issuance_listing_date_conflict"


def test_existing_legacy_stock_keeps_its_matching_path_without_catalogue_proof(issue_factory):
    issue = issue_factory(); existing = owner(issue)
    existing["metadata"].pop("identity_origin"); existing["metadata"].pop("catalogue_only")
    existing["canonical_name"] = "科科科技股份有限公司"
    result = run(issue, [existing], bindings={}); entity, = result["entities"]
    assert entity.entity_id == OWNER and not result["unresolved"]


def test_warrant_api_adoption_clears_catalogue_only_and_emits_real_expiry_end(issue_factory):
    issue = issue_factory(code="03007X", name="元展07", isin="TW14Z03007X5", dataset="twse_isin_listed", kind="warrant", listed="2014/07/31")
    existing = owner(issue)
    payload = {"twse_warrants": [{"權證代號": "03007X", "權證簡稱": "元展07", "履約截止日": "1160302", "權證類型": "認購"}]}
    result = run(issue, [existing], payload=payload); entity, = result["entities"]
    assert entity.entity_id == OWNER and entity.metadata["catalogue_only"] is False
    assert entity.metadata["identity_origin"] == "official_catalogue"
    aliases = [r for r in result["identifiers"] if r["identifier_type"] in {"exchange_code", "display_symbol"}]
    assert len(aliases) == 2 and all(r["valid_to"] == "2027-03-02T16:00:00+00:00" for r in aliases)
    assert next(r for r in result["identifiers"] if r["identifier_type"] == "isin")["valid_to"] is None
