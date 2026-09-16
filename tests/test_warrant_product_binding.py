"""A retained catalogue row cannot classify another issuance sharing its code."""
from copy import deepcopy
from datetime import datetime
import socket

import pytest

from open_stock_ai.execution.product_admission import assess_new_entry_product
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from test_catalogue_issue_identity import retain_catalogue, ACQUIRED


NOW = datetime.fromisoformat(ACQUIRED)
ENTITY_ID = "ENT-" + "a" * 32
SYMBOL = "030012.TW"
OLD_ISIN = "TW2600300121"
NEW_ISIN = "TW2700300121"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("warrant binding tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return MarketDataPlatform(database_path=tmp_path / "warrant-product.sqlite")


def old_listing(platform, *, isin=OLD_ISIN):
    entity = EntityRecord(
        entity_id=ENTITY_ID, entity_type="warrant", canonical_name="original issued warrant",
        market="taiwan", exchange="TWSE", lifecycle_status="active",
        metadata={"display_symbol": SYMBOL, "source_datasets": ["twse_warrants"],
                  "issuance_identity": {"isin": isin} if isin else None},
    )
    platform.warehouse.upsert_entity(entity, identifiers=[{
        "source_id": "twse_openapi", "identifier_type": "display_symbol", "identifier_value": SYMBOL,
        "valid_from": "2026-01-01", "valid_to": "2027-01-01", "metadata": {"venue": "TWSE"},
    }])


def catalogue(platform, *, isin=OLD_ISIN, ordinary=False):
    receipts = retain_catalogue(platform, rows=[{
        "code": "030012", "name": "catalogue issued contract", "isin": isin,
        "listed_on": "2026/09/14" if isin != OLD_ISIN else "2026/01/01",
        "section": "股票" if ordinary else "上市認購(售)權證",
        "cfi": "ESVUFR" if ordinary else "RWCCCC",
    }])
    platform.sync_product_classifications(receipts, acquired_at=ACQUIRED)
    return next(receipt for receipt in receipts if receipt["symbol"] == SYMBOL)


def resolutions(platform):
    yield platform.resolve_product_classification(symbol=SYMBOL, market="TWSE", now=NOW)
    yield platform.warehouse.product_classifications_for_listings(listings=[("TWSE", SYMBOL)], now=NOW)[("TWSE", SYMBOL)]
    restarted = MarketDataPlatform(database_path=platform.warehouse.path)
    yield restarted.resolve_product_classification(symbol=SYMBOL, market="taiwan", now=NOW)


def test_future_issued_isin_cannot_be_attached_to_current_old_warrant(platform):
    old_listing(platform)
    raw_receipt = catalogue(platform, isin=NEW_ISIN)
    before = platform.warehouse.entity_profile(ENTITY_ID)
    assert before["metadata"]["product_classifications"]["TWSE:" + SYMBOL] == raw_receipt
    for resolution in resolutions(platform):
        assert resolution["entity_id"] == ENTITY_ID
        assert resolution["lifecycle_status"] == "active"
        assert resolution["classification"]["status"] == "conflict"
        assert resolution["classification"]["product_type"] == "unknown"
        assert resolution["classification"]["reasons"] == ["warrant_issuance_isin_binding_mismatch"]
        gate = assess_new_entry_product(symbol=SYMBOL, market="TWSE", now=NOW,
            expected_entity_id=ENTITY_ID, product_snapshot=resolution)
        assert gate["allowed"] is False and gate["classification_verified"] is False
    assert platform.warehouse.entity_profile(ENTITY_ID) == before


@pytest.mark.parametrize("known_isin", [OLD_ISIN, None])
def test_verified_ordinary_catalogue_cannot_enable_entry_on_warrant_owner(platform, known_isin):
    old_listing(platform, isin=known_isin)
    catalogue(platform, isin=NEW_ISIN, ordinary=True)
    before = platform.warehouse.entity_profile(ENTITY_ID)
    for resolution in resolutions(platform):
        assert resolution["classification"]["status"] == "conflict"
        assert resolution["classification"]["reasons"] == ["warrant_product_type_binding_mismatch"]
        gate = assess_new_entry_product(symbol=SYMBOL, market="TWSE", now=NOW,
            expected_entity_id=ENTITY_ID, product_snapshot=resolution)
        assert gate["allowed"] is False and gate["classification_verified"] is False
    assert platform.warehouse.entity_profile(ENTITY_ID) == before


def test_same_issued_warrant_keeps_verified_source_and_unsupported_entry_status(platform):
    old_listing(platform)
    receipt = catalogue(platform)
    for resolution in resolutions(platform):
        assert resolution["classification"] == receipt
        gate = assess_new_entry_product(symbol=SYMBOL, market="TWSE", now=NOW,
            expected_entity_id=ENTITY_ID, product_snapshot=resolution)
        assert gate["classification_verified"] is True
        assert gate["allowed"] is False
        assert "product_type_not_supported" in gate["reasons"]


def test_matching_owner_does_not_substitute_for_raw_receipt_verification(platform):
    old_listing(platform)
    receipt = catalogue(platform)
    changed = deepcopy(receipt)
    changed["product_type"] = "ordinary_stock"
    platform.sync_product_classifications([changed], acquired_at=ACQUIRED)
    for resolution in resolutions(platform):
        assert resolution["classification"]["status"] == "unknown"
        assert resolution["classification"]["reasons"] == ["product_raw_row_binding_mismatch"]


def test_missing_classification_remains_unknown_without_inventing_issuance_conflict(platform):
    old_listing(platform)
    for resolution in resolutions(platform):
        assert resolution["classification"]["status"] == "unknown"
        assert resolution["classification"]["reasons"] == ["product_classification_unavailable"]
