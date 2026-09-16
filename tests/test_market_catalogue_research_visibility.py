"""Catalogue research visibility never grants lifecycle or quote eligibility."""
from copy import deepcopy
import socket

import pytest

from stock_ai.market_intelligence import feature_store
from stock_ai.market_intelligence.broad_scanner import BroadScanner
from stock_ai.market_intelligence.product_projection import product_assessment
from stock_ai.market_intelligence.service import MarketIntelligenceService
from stock_ai.market_intelligence.snapshot_store import SnapshotStore
from stock_ai.models import SecurityMasterItem
from product_classification_fixtures import product_fixture


def listing(symbol="2330.TW", *, status="active", entity_id=None, identifier_status="resolved"):
    product = product_fixture(symbol)
    return SecurityMasterItem(
        symbol=symbol, entity_id=entity_id or product["entity_id"], name="isolated catalogue",
        market="taiwan", exchange="TWSE", listing_type="listed", entity_type="security",
        lifecycle_status="pre_listing" if status == "listed_pending_quote" else status,
        trading_status=status, listing_identifier_status=identifier_status,
        product_classification=product["product_classification"], source="isolated fixture",
    )


@pytest.fixture
def sources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("catalogue visibility fixtures must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    master, quote_codes, calls, factor_symbols = [], [], [], []
    def read_master(**kwargs):
        assert kwargs == {"market": "taiwan", "limit": 100000, "include_lifecycle": True}
        return master[:kwargs["limit"]]
    monkeypatch.setattr(feature_store, "list_securities_master", read_master)
    def quote(venue):
        calls.append(venue)
        return [{"Code": code, "Date": "2026-09-12", "ClosingPrice": "100", "OpeningPrice": "99",
                 "HighestPrice": "101", "LowestPrice": "98", "Change": "1", "TradeVolume": "100000",
                 "TradeValue": "10000000", "Transaction": "500"} for code in quote_codes] if venue == "TWSE" else []
    monkeypatch.setattr(feature_store, "twse_quotes", lambda: quote("TWSE"))
    monkeypatch.setattr(feature_store, "tpex_quotes", lambda: quote("TPEx"))
    def optional(**kwargs):
        assert kwargs["allow_network"] is False
        return []
    monkeypatch.setattr(feature_store, "list_monthly_revenues", optional)
    monkeypatch.setattr(feature_store, "list_institutional_flows", optional)
    original_attach = feature_store.attach_current_source_inputs
    def attach(features, **kwargs):
        factor_symbols.extend(feature["symbol"] for feature in features)
        return original_attach(features, **kwargs)
    monkeypatch.setattr(feature_store, "attach_current_source_inputs", attach)
    return master, quote_codes, calls, factor_symbols


def workspace(tmp_path):
    return MarketIntelligenceService(store=SnapshotStore(tmp_path / "research.sqlite"),
        scanner=BroadScanner(feature_loader=feature_store.load_all_taiwan_features,
                             feature_enricher=lambda rows: rows, position_loader=lambda: {}))


@pytest.mark.parametrize("status", ["unknown", "pre_listing", "listed_pending_quote", "suspended"])
def test_nonactive_catalogue_rows_stay_visible_without_same_code_market_or_entry(sources, tmp_path, status):
    master, quote_codes, calls, factor_symbols = sources
    master.extend([listing(), listing("1234.TW", status=status)])
    quote_codes.extend(["2330", "1234"])
    service = workspace(tmp_path)
    saved = service.build_snapshot()
    restored = service.snapshot(saved.snapshot_id)
    assert calls == ["TWSE", "TPEx"]
    assert factor_symbols == ["2330.TW"]
    assert set(restored.candidate_details) == {"2330.TW", "1234.TW"}
    candidate = restored.candidate_details["1234.TW"]
    assert candidate.entity_id == master[1].entity_id
    assert candidate.lifecycle_status == ("pre_listing" if status == "listed_pending_quote" else status)
    assert candidate.latest_price is None and candidate.volume == 0
    assert candidate.data_quality.status == "insufficient" and candidate.data_quality.data_as_of is None
    assert candidate.data_quality.source_observation["primary_quote_binding"]["reason"] == "research_listing_not_active"
    assert candidate.evidence[0].observed_at is None and candidate.evidence[0].quality == "invalid"
    assert candidate.market_category != "BUY_NOW" and candidate.host_risk_status == "blocked"
    assert candidate.product_entry_assessment["allowed"] is False
    assert restored.candidate_details["2330.TW"].latest_price == 100


def test_historical_expired_owner_does_not_hide_future_issued_research_row(sources):
    master, quote_codes, calls, factor_symbols = sources
    master.extend([listing("030012.TW", status="expired", entity_id="ENT-" + "1" * 32),
                   listing("030012.TW", status="listed_pending_quote", entity_id="ENT-" + "2" * 32),
                   listing("9999.TW", status="delisted")])
    quote_codes.append("030012")
    features = feature_store.load_all_taiwan_features()
    assert len(features) == 1
    assert features[0]["entity_id"] == "ENT-" + "2" * 32
    assert features[0]["lifecycle_status"] == "pre_listing" and features[0]["close"] is None
    assert product_assessment(features[0])["allowed"] is False
    assert factor_symbols == [] and calls == ["TWSE", "TPEx"]


def test_open_old_and_future_new_issue_become_one_explicit_ambiguous_row(sources, tmp_path):
    master, quote_codes, _calls, factor_symbols = sources
    master.extend([listing("030012.TW", entity_id="ENT-" + "1" * 32),
                   listing("030012.TW", status="pre_listing", entity_id="ENT-" + "2" * 32)])
    quote_codes.append("030012")
    forward = feature_store.load_all_taiwan_features()[0]
    master.reverse()
    reverse = feature_store.load_all_taiwan_features()[0]
    for feature in (forward, reverse):
        assert feature["entity_id"] is None and feature["lifecycle_status"] == "unknown"
        assert feature["close"] is None and feature["data_as_of"] is None
        assert feature["product_classification"]["status"] == "conflict"
        observation = feature["data_quality"]["source_observation"]["primary_quote_binding"]
        assert observation["candidate_count"] == 2
        assert [candidate["entity_id"] for candidate in observation["candidates"]] == ["ENT-" + "1" * 32, "ENT-" + "2" * 32]
        assert product_assessment(feature)["allowed"] is False
    assert factor_symbols == []
    service = workspace(tmp_path)
    snapshot = service.build_snapshot()
    restored = service.snapshot(snapshot.snapshot_id)
    candidate = restored.candidate_details["030012.TW"]
    assert candidate.product_entry_assessment["allowed"] is False
    assert candidate.data_quality.source_observation["primary_quote_binding"]["candidate_count"] == 2


def test_identical_current_entity_rows_deduplicate_without_losing_its_quote(sources):
    master, quote_codes, _calls, factor_symbols = sources
    item = listing()
    master.extend([item, deepcopy(item)])
    quote_codes.append("2330")
    rows = feature_store.load_all_taiwan_features()
    assert len(rows) == 1 and rows[0]["entity_id"] == item.entity_id
    assert rows[0]["close"] == 100 and product_assessment(rows[0])["allowed"] is True
    assert factor_symbols == ["2330.TW"]


def test_explicit_unresolved_listing_does_not_use_observed_same_code_quote(sources):
    master, quote_codes, _calls, factor_symbols = sources
    master.append(listing(identifier_status="unavailable_or_ambiguous"))
    quote_codes.append("2330")
    feature = feature_store.load_all_taiwan_features()[0]
    assert feature["close"] is None
    assert feature["lifecycle_status"] == "unknown"
    assert product_assessment(feature)["allowed"] is False
    assert feature["data_quality"]["source_observation"]["primary_quote_binding"]["reason"] == "research_listing_identity_unverified"
    assert factor_symbols == []


def test_emerging_listing_does_not_join_otc_same_symbol_bulk_quote(sources, monkeypatch):
    master, _quote_codes, _calls, factor_symbols = sources
    master.append(listing("1234.TWO").model_copy(update={"exchange": "TPEx-ESB"}))
    monkeypatch.setattr(feature_store, "tpex_quotes", lambda: [
        {"SecuritiesCompanyCode": "1234", "Date": "2026-09-12", "Close": "100", "TradingShares": "1000"}])
    feature = feature_store.load_all_taiwan_features()[0]
    assert feature["close"] is None and feature["data_as_of"] is None
    assert feature["data_quality"]["source_observation"]["primary_quote_binding"]["reason"] == "research_listing_quote_venue_unsupported"
    assert factor_symbols == []


def test_research_inventory_includes_rows_after_old_5000_limit_without_per_symbol_fetches(sources):
    master, _quote_codes, calls, _factor_symbols = sources
    base = listing(status="unknown")
    master.extend(base.model_copy(update={"symbol": f"{100000 + index}.TW",
        "entity_id": "ENT-" + f"{index:032x}"}) for index in range(5001))
    features = feature_store.load_all_taiwan_features()
    assert len(features) == 5001 and features[-1]["symbol"] == "105000.TW"
    assert all(feature["close"] is None for feature in features)
    assert calls == ["TWSE", "TPEx"]
