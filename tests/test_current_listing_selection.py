"""Current-listing selection must not rewrite source-qualified alias history."""
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace

import pytest

from stock_ai.data_platform import warehouse as warehouse_module
from stock_ai import phase1_data
from stock_ai.data_platform.contracts import EntityRecord, SourceDefinition
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id


NOW = datetime(2026, 9, 12, 4, tzinfo=timezone.utc)


@pytest.fixture
def listing_store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("listing selection fixtures must not use a real transport")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: clock.now.isoformat())
    platform = MarketDataPlatform(database_path=tmp_path / "current-listing.sqlite")
    for source in ("offline_a", "offline_b", "offline_c"):
        platform.warehouse.register_source(SourceDefinition(
            source_id=source, display_name=source, authority="isolated fixture",
            license_status="fixture only", update_frequency_seconds=3600,
            reliability_tier=1, priority=1, domains=["security_master"],
        ))
    return platform, clock


def entity(symbol, *, exchange="TWSE", market="taiwan", kind="stock", status="active", name=None):
    return EntityRecord(
        entity_id=stable_entity_id(market=market, exchange=exchange, source_code=symbol),
        entity_type=kind, canonical_name=name or symbol, market=market,
        exchange=exchange, lifecycle_status=status,
        metadata={"display_symbol": symbol, "fixture_only": True},
    )


def alias(symbol, *, venue="TWSE", source="offline_a", start="2020-01-01T00:00:00+00:00", end=None):
    return {"source_id": source, "identifier_type": "display_symbol", "identifier_value": symbol,
            "valid_from": start, "valid_to": end, "metadata": {"venue": venue}}


def persisted_identifiers(platform):
    with platform.warehouse._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "select * from entity_identifiers order by source_id,identifier_type,identifier_value,valid_from"
        )]


@pytest.mark.parametrize("old_end", ["2025-01-01T00:00:00+00:00", None])
@pytest.mark.parametrize("legacy_metadata", [True, False])
def test_transferred_listing_uses_current_venue_despite_newer_legacy_source(listing_store, old_end, legacy_metadata):
    platform, clock = listing_store
    old = entity("1234.TWO", exchange="TPEx")
    clock.now = NOW - timedelta(days=30)
    platform.warehouse.upsert_entity(old, identifiers=[alias("1234.TWO", venue="TPEx", end=old_end)])
    current = old.model_copy(update={"exchange": "TWSE", "metadata": {
        **old.metadata, "display_symbol": "1234.TW", "venue_history": ["TPEx", "TWSE"],
    }})
    clock.now = NOW - timedelta(days=2)
    platform.warehouse.upsert_entity(current, identifiers=[alias("1234.TW", start="2025-01-01T00:00:00+00:00")])
    # A later source import must not turn this same issuer back into its old venue.
    clock.now = NOW - timedelta(days=1)
    late_alias = alias("1234.TWO", venue="TPEx", source="offline_b", end=old_end)
    if not legacy_metadata:
        late_alias["metadata"] = {}
    platform.warehouse.upsert_entity(current, identifiers=[late_alias])
    clock.now = NOW
    before = persisted_identifiers(platform)
    listed = platform.warehouse.list_entities(market="taiwan", limit=1)
    assert listed[0]["entity_id"] == old.entity_id
    assert listed[0]["display_symbol"] == "1234.TW"
    public = platform.securities(market="TWSE", limit=1)
    assert [(row["entity_id"], row["symbol"]) for row in public] == [(old.entity_id, "1234.TW")]
    assert public[0]["trading_status"] == "active"
    assert platform.securities(market="TPEx", limit=1) == []
    assert persisted_identifiers(platform) == before
    assert {row["identifier_value"] for row in platform.warehouse.entity_profile(old.entity_id)["identifiers"]} == {
        "1234.TW", "1234.TWO",
    }


@pytest.mark.parametrize("status", ["active", "suspended", "unknown"])
def test_current_lifecycle_ignores_future_and_expired_same_venue_aliases(listing_store, status):
    platform, clock = listing_store
    item = entity("1234.TW", status=status)
    clock.now = NOW - timedelta(days=3)
    platform.warehouse.upsert_entity(item, identifiers=[alias("1234.TW")])
    clock.now = NOW - timedelta(days=2)
    platform.warehouse.upsert_entity(item, identifiers=[alias("1235.TW", end="2025-01-01T00:00:00+00:00")])
    clock.now = NOW - timedelta(days=1)
    platform.warehouse.upsert_entity(item, identifiers=[alias("1236.TW", start="2099-01-01T00:00:00+00:00")])
    clock.now = NOW
    before = persisted_identifiers(platform)
    assert platform.warehouse.list_entities()[0]["display_symbol"] == "1234.TW"
    assert platform.securities()[0]["symbol"] == "1234.TW"
    assert persisted_identifiers(platform) == before


@pytest.mark.parametrize("status,start,end", [
    ("delisted", "2020-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    ("expired", "2020-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
    ("pre_listing", "2099-01-01T00:00:00+00:00", None),
])
def test_historical_and_prelisting_identities_remain_researchable(listing_store, status, start, end):
    platform, _ = listing_store
    item = entity("1234.TW", status=status)
    platform.warehouse.upsert_entity(item, identifiers=[alias("1234.TW", start=start, end=end)])
    before = persisted_identifiers(platform)
    assert platform.warehouse.list_entities()[0]["display_symbol"] == "1234.TW"
    public = platform.securities()[0]
    assert public["symbol"] == "1234.TW"
    assert public["trading_status"] == ("listed_pending_quote" if status == "pre_listing" else status)
    assert public["lifecycle_status"] == status
    assert public["entity_id"] == item.entity_id
    assert public["product_classification"]["status"] != "verified"
    assert persisted_identifiers(platform) == before


@pytest.mark.parametrize("case", ["expired", "future", "ambiguous"])
def test_active_without_unique_valid_listing_is_only_an_unknown_research_label(listing_store, case):
    platform, _ = listing_store
    item = entity("1234.TW")
    identifiers = (
        [alias("1234.TW", end="2025-01-01T00:00:00+00:00")] if case == "expired" else
        [alias("1234.TW", start="2099-01-01T00:00:00+00:00")] if case == "future" else
        [alias("1234.TW"), alias("1235.TW", source="offline_b")]
    )
    platform.warehouse.upsert_entity(item, identifiers=identifiers)
    before = persisted_identifiers(platform)
    assert platform.warehouse.list_entities()[0]["display_symbol"] is None
    public = platform.securities()[0]
    assert public["symbol"] == "1234.TW"  # Preserved metadata label is not executable identity.
    assert public["listing_identifier_status"] == "unavailable_or_ambiguous"
    assert public["trading_status"] == "unknown" and public["lifecycle_status"] == "active"
    assert public["product_classification"]["status"] == "unknown"
    assert persisted_identifiers(platform) == before


def test_same_current_symbol_from_multiple_sources_is_not_ambiguous(listing_store):
    platform, _ = listing_store
    item = entity("1234.TW")
    platform.warehouse.upsert_entity(item, identifiers=[alias("1234.TW"), alias("1234.TW", source="offline_b")])
    assert platform.warehouse.list_entities()[0]["display_symbol"] == "1234.TW"
    assert platform.securities()[0]["trading_status"] == "active"


def test_normalized_equivalent_display_symbols_dedupe_without_rewriting_raw_aliases(listing_store):
    platform, _ = listing_store
    item = entity("1234.TW")
    platform.warehouse.upsert_entity(item, identifiers=[
        alias("1234.TW"), alias("  1234.tw  ", source="offline_b"),
    ])
    before = persisted_identifiers(platform)
    identifiers = platform.warehouse.entity_profile(item.entity_id)["identifiers"]
    assert {row["identifier_value"] for row in identifiers} == {"1234.TW", "  1234.tw  "}
    assert {row["normalized_value"] for row in identifiers} == {"1234.TW"}
    assert platform.warehouse.list_entities()[0]["display_symbol"] == "1234.TW"
    public = platform.securities()[0]
    assert public["symbol"] == "1234.TW" and public["listing_identifier_status"] == "resolved"
    assert public["trading_status"] == "active"
    assert persisted_identifiers(platform) == before


def test_active_identifier_expires_at_exact_boundary_while_new_interval_is_valid(listing_store):
    platform, _ = listing_store
    ended, started = entity("1234.TW"), entity("5678.TW")
    platform.warehouse.upsert_entity(ended, identifiers=[alias("1234.TW", end=NOW.isoformat())])
    platform.warehouse.upsert_entity(started, identifiers=[alias("5678.TW", start=NOW.isoformat())])
    before = persisted_identifiers(platform)
    listings = {row["entity_id"]: row for row in platform.warehouse.list_entities()}
    assert listings[ended.entity_id]["display_symbol"] is None
    assert listings[started.entity_id]["display_symbol"] == "5678.TW"
    public = {row["entity_id"]: row for row in platform.securities()}
    assert public[ended.entity_id]["trading_status"] == "unknown"
    assert public[started.entity_id]["trading_status"] == "active"
    assert persisted_identifiers(platform) == before


def test_missing_display_label_does_not_drop_other_security_master_rows(listing_store, monkeypatch):
    platform, _ = listing_store
    missing = entity("1234.TW", name="A missing listing label").model_copy(update={"metadata": {"fixture_only": True}})
    good = entity("5678.TW", name="B valid listing")
    platform.warehouse.upsert_entity(missing)
    platform.warehouse.upsert_entity(good, identifiers=[alias("5678.TW")])
    before = persisted_identifiers(platform)
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    rows = phase1_data.list_securities_master(market="taiwan", limit=2, include_lifecycle=True)
    assert len(rows) == 2
    public = {row.entity_id: row for row in rows}
    assert public[good.entity_id].symbol == "5678.TW" and public[good.entity_id].trading_status == "active"
    assert public[missing.entity_id].symbol == "" and public[missing.entity_id].trading_status == "unknown"
    assert public[missing.entity_id].product_classification["status"] == "unknown"
    assert persisted_identifiers(platform) == before


@pytest.mark.parametrize("market,expected_symbol", [
    ("taiwan", "2000.TWO"), ("TWSE", "1000.TW"), ("listed", "1000.TW"),
    ("TPEx", "2000.TWO"), ("otc", "2000.TWO"), ("emerging", "3000.TWO"),
    ("stock", "2000.TWO"), ("etf", "0050.TW"), ("warrant", "010001.TW"), ("index", "^TWII"),
])
def test_security_market_venue_and_type_filters_apply_before_limit(listing_store, market, expected_symbol):
    platform, _ = listing_store
    for symbol, venue, kind, region in (
        ("AAA.US", "AAA", "stock", "united_states"),
        ("AETF.US", "AAA", "etf", "united_states"),
        ("AWAR.US", "AAA", "warrant", "united_states"),
        ("AIDX.US", "AAA", "index", "united_states"),
        ("1000.TW", "TWSE", "stock", "taiwan"),
        ("2000.TWO", "TPEx", "stock", "taiwan"),
        ("3000.TWO", "TPEx-ESB", "stock", "taiwan"),
        ("0050.TW", "TWSE", "etf", "taiwan"),
        ("010001.TW", "TWSE", "warrant", "taiwan"),
        ("^TWII", "TWSE", "index", "taiwan"),
    ):
        item = entity(symbol, exchange=venue, kind=kind, market=region)
        platform.warehouse.upsert_entity(item, identifiers=[alias(symbol, venue=venue)])
    assert platform.warehouse.list_entities(limit=1)[0]["market"] == "united_states"
    result = platform.securities(market=market, limit=1)
    assert len(result) == 1 and result[0]["symbol"] == expected_symbol
    assert result[0]["market"] == "taiwan"


def test_warehouse_exchange_filter_precedes_limit(listing_store):
    platform, _ = listing_store
    for symbol, venue in (("2000.TWO", "TPEx"), ("1000.TW", "TWSE")):
        platform.warehouse.upsert_entity(entity(symbol, exchange=venue), identifiers=[alias(symbol, venue=venue)])
    rows = platform.warehouse.list_entities(market="taiwan", exchange="TWSE", limit=1)
    assert [(row["exchange"], row["display_symbol"]) for row in rows] == [("TWSE", "1000.TW")]
