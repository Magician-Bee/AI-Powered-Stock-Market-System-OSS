"""Source-qualified warrant code reuse preserves separate issued contracts."""
from datetime import datetime, timezone
import socket
from types import SimpleNamespace

import pytest

from stock_ai.data_platform import warehouse as warehouse_module
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform


NOW = datetime(2026, 9, 12, 4, tzinfo=timezone.utc)
OLD_ID = "ENT-" + "1" * 32
NEW_ID = "ENT-" + "2" * 32
SYMBOL = "030012.TW"


@pytest.fixture
def registry_store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("warrant registry tests must not use network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: clock.now.isoformat())
    path = tmp_path / "warrant-registry.sqlite"
    return MarketDataPlatform(database_path=path), clock, path


def issue(entity_id, *, status="active", isin=None, kind="warrant"):
    return EntityRecord(
        entity_id=entity_id, entity_type=kind, canonical_name=f"test issue {entity_id[-1]}",
        market="taiwan", exchange="TWSE", lifecycle_status=status,
        metadata={"display_symbol": SYMBOL, "issuance_isin": isin,
                  "source_datasets": ["twse_warrants"], "fixture_only": True},
    )


def aliases(*, start=None, end=None):
    return [
        {"source_id": "twse_openapi", "identifier_type": kind, "identifier_value": value,
         "valid_from": start, "valid_to": end, "metadata": {"venue": "TWSE", "listing_type": "warrant"}}
        for kind, value in (("exchange_code", "030012"), ("display_symbol", SYMBOL))
    ]


def write(platform, entity, identifiers, *, method="batch"):
    if method == "upsert":
        return platform.warehouse.upsert_entity(entity, identifiers=identifiers)
    return platform.warehouse.write_security_lifecycle_batch(
        entities=[entity], identifiers=[{**row, "entity_id": entity.entity_id} for row in identifiers],
        revisions=[], events=[], raw_payload_ids={}, acquired_at=NOW.isoformat(), code_version="isolated-fixture",
    )


def identifiers(platform):
    with platform.warehouse._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "select * from entity_identifiers order by source_id,identifier_type,identifier_value,valid_from"
        )]


@pytest.mark.parametrize("method", ["upsert", "batch"])
def test_real_listing_start_preserves_blank_legacy_alias_and_historical_entity(registry_store, method):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(end="2026-01-06"), method=method)
    old_profile = platform.warehouse.entity_profile(OLD_ID)
    write(platform, issue(NEW_ID, isin="TW030012NEW0"), aliases(start="2026-09-01", end="2027-01-06"), method=method)
    before = identifiers(platform)
    restarted = MarketDataPlatform(database_path=path)

    current = restarted.resolve_entity(SYMBOL)
    assert current["entity"]["entity_id"] == NEW_ID
    assert current["candidate_count"] == 1
    assert restarted.resolve_entity(SYMBOL, as_of="2025-12-01")["entity"]["entity_id"] == OLD_ID
    assert restarted.resolve_entity(SYMBOL, as_of="2026-01-06")["status"] == "unavailable"
    assert restarted.resolve_entity(SYMBOL, as_of="2026-09-01")["entity"]["entity_id"] == NEW_ID
    assert restarted.resolve_entity(OLD_ID)["entity"] == old_profile
    assert identifiers(restarted) == before
    assert {row["entity_id"] for row in restarted.securities(market="warrant")} == {OLD_ID, NEW_ID}
    # Refreshing the same issued contract is idempotent and cannot move its start.
    write(restarted, issue(NEW_ID, isin="TW030012NEW0"), aliases(start="2026-09-01", end="2027-01-06"), method=method)
    assert identifiers(restarted) == before


def test_prelisting_reused_code_does_not_resolve_as_current_but_remains_researchable(registry_store):
    platform, clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(end="2026-01-06"))
    write(platform, issue(NEW_ID, status="pre_listing", isin="TW030012NEW0"),
          aliases(start="2026-09-15", end="2027-01-06"))
    restarted = MarketDataPlatform(database_path=path)
    assert restarted.resolve_entity(SYMBOL)["status"] == "unavailable"
    assert {row["entity_id"] for row in restarted.securities(market="warrant")} == {OLD_ID, NEW_ID}
    assert restarted.resolve_entity(SYMBOL, as_of="2026-09-15")["entity"]["entity_id"] == NEW_ID
    clock.now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert restarted.resolve_entity(SYMBOL)["entity"]["entity_id"] == NEW_ID


@pytest.mark.parametrize("old_isin,new_isin,old_kind", [
    (None, "TW030012NEW0", "warrant"),
    ("TW030012OLD0", "TW030012NEW0", "warrant"),
    ("TW030012SAME", "TW030012SAME", "warrant"),
    (None, "TW030012NEW0", "stock"),
])
def test_open_legacy_alias_never_merges_or_selects_a_distinct_warrant_entity(registry_store, old_isin, new_isin, old_kind):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired", isin=old_isin, kind=old_kind), aliases())
    write(platform, issue(NEW_ID, isin=new_isin), aliases(start="2026-09-01", end="2027-01-06"))
    before = identifiers(platform)
    reconciliation = platform.warehouse.reconcile_entity_registry_aliases(effective_at=NOW.isoformat())
    assert reconciliation["retained_warrant_ambiguity_count"] == 1
    assert reconciliation["created_merge_count"] == reconciliation["superseded_alias_count"] == 0
    restarted = MarketDataPlatform(database_path=path)
    for kwargs in ({}, {"source_id": "twse_openapi"}, {"as_of": NOW.isoformat()}):
        result = restarted.resolve_entity(SYMBOL, **kwargs)
        assert result["status"] == "ambiguous"
        assert result["entity"] is None
        assert {row["entity_id"] for row in result["candidates"]} == {OLD_ID, NEW_ID}
    assert restarted.resolve_entity(SYMBOL, as_of="2025-12-01")["entity"]["entity_id"] == OLD_ID
    assert restarted.resolve_entity(OLD_ID)["entity"]["entity_id"] == OLD_ID
    assert identifiers(restarted) == before
    with restarted.warehouse._connect() as conn:
        assert conn.execute("select count(*) from entity_identity_merges").fetchone()[0] == 0


@pytest.mark.parametrize("method", ["upsert", "batch"])
def test_writers_keep_strict_cross_owner_key_rejection_and_atomic_rollback(registry_store, method):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(end="2026-01-06"), method=method)
    before = identifiers(platform)
    with pytest.raises(ValueError, match="already assigned to another entity"):
        write(platform, issue(NEW_ID, isin="TW030012NEW0"), aliases(end="2027-01-06"), method=method)
    restarted = MarketDataPlatform(database_path=path)
    assert restarted.warehouse.entity_profile(NEW_ID) is None
    assert identifiers(restarted) == before


@pytest.mark.parametrize("bad_entity_id", [OLD_ID, NEW_ID])
def test_legacy_exercise_interval_repair_preserves_colliding_blank_key(registry_store, bad_entity_id):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(end="2025-01-06"))
    write(platform, issue(bad_entity_id, status="expired"), aliases(start="2026-01-01", end="2026-01-06"))
    # V12 imported exercise start as listing time. Public writers now reject
    # this shape, so recreate only that legacy invalid row in the isolated DB.
    with platform.warehouse._connect() as conn:
        conn.execute("update entity_identifiers set valid_from=valid_to where valid_from!=''")
        conn.commit()
    before = identifiers(platform)
    result = platform.warehouse.reconcile_entity_registry_aliases(effective_at=NOW.isoformat())
    assert result["status"] == "partial"
    assert result["unresolved_identifier_interval_count"] == 2
    assert result["repaired_identifier_interval_count"] == 0
    restarted = MarketDataPlatform(database_path=path)
    assert identifiers(restarted) == before
    assert restarted.resolve_entity(SYMBOL, as_of="2024-12-01")["entity"]["entity_id"] == OLD_ID


def test_noncolliding_legacy_exercise_interval_still_repairs_without_new_ownership(registry_store):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(start="2026-01-01", end="2026-01-06"))
    with platform.warehouse._connect() as conn:
        conn.execute("update entity_identifiers set valid_from=valid_to")
        conn.commit()
    result = platform.warehouse.reconcile_entity_registry_aliases(effective_at=NOW.isoformat())
    assert result["repaired_identifier_interval_count"] == 2
    assert result["unresolved_identifier_interval_count"] == 0
    restarted = MarketDataPlatform(database_path=path)
    profile = restarted.warehouse.entity_profile(OLD_ID)
    assert all(row["valid_from"] is None for row in profile["identifiers"])
    assert all(row["metadata"]["valid_from_repair"] == "twse_exercise_start_not_listing" for row in profile["identifiers"])
    assert restarted.resolve_entity(SYMBOL, as_of="2025-12-01")["entity"]["entity_id"] == OLD_ID
    assert restarted.resolve_entity(SYMBOL)["status"] == "unavailable"


def test_legacy_repair_reserves_blank_keys_within_the_same_transaction(registry_store):
    platform, _clock, path = registry_store
    write(platform, issue(OLD_ID, status="expired"), aliases(start="2024-01-01", end="2025-01-06"))
    write(platform, issue(NEW_ID, status="expired"), aliases(start="2025-02-01", end="2026-01-06"))
    with platform.warehouse._connect() as conn:
        conn.execute("update entity_identifiers set valid_from=valid_to")
        conn.commit()
    result = platform.warehouse.reconcile_entity_registry_aliases(effective_at=NOW.isoformat())
    assert result["status"] == "partial"
    assert result["repaired_identifier_interval_count"] == 2
    assert result["unresolved_identifier_interval_count"] == 2
    before_restart = identifiers(platform)
    restarted = MarketDataPlatform(database_path=path)
    assert identifiers(restarted) == before_restart
    assert len(before_restart) == 4
    for entity_id in (OLD_ID, NEW_ID):
        profile = restarted.warehouse.entity_profile(entity_id)
        assert len(profile["identifiers"]) == 2
        assert all(row["superseded_at"] is None for row in profile["identifiers"])
