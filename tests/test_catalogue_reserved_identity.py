"""Superseded aliases reserve ownership without identifying a new issuance."""
import socket

import pytest

from stock_ai.data_platform import warehouse as warehouse_module
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from test_catalogue_issue_identity import ACQUIRED, retain_catalogue


CODE = "085974"
ISIN = "TW26Z0859747"
OLD_ID = "ENT-" + "a" * 32
LISTED_AT = "2026-08-31T16:00:00+00:00"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("reserved identity tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(warehouse_module, "utc_now", lambda: ACQUIRED)
    return MarketDataPlatform(database_path=tmp_path / "reserved.sqlite")


def reserve_old_alias(platform, *, kind, valid_from=LISTED_AT):
    value = {"exchange_code": CODE, "display_symbol": CODE + ".TW", "isin": ISIN}[kind]
    platform.warehouse.upsert_entity(EntityRecord(
        entity_id=OLD_ID, entity_type="warrant", canonical_name="isolated historical owner",
        market="taiwan", exchange="TWSE", lifecycle_status="expired",
        metadata={"expires_at": "2026-09-01"},
    ), identifiers=[{"source_id": "twse_isin", "identifier_type": kind,
        "identifier_value": value, "valid_from": valid_from,
        "valid_to": "2026-09-01T16:00:00+00:00", "metadata": {"venue": "TWSE"}}])
    # Reproduce the persisted state left by historical alias reconciliation.
    # The public upsert supplies the entity/key; only this isolated fixture
    # marks the alias superseded, without changing its immutable owner.
    with platform.warehouse._connect() as conn:
        conn.execute("update entity_identifiers set superseded_at=?, is_primary=0 where entity_id=?",
                     (ACQUIRED, OLD_ID))
    return platform.warehouse.entity_profile(OLD_ID)


def retain_new_issue(platform):
    # The retained fixture includes an independent ordinary stock (2330).
    retain_catalogue(platform, rows=[{"code": CODE, "name": "isolated new issue", "isin": ISIN,
        "listed_on": "2026/09/01", "section": "上市認購(售)權證", "cfi": "RWCCCC"}])


def ingest(platform):
    return platform.warehouse.sync_catalogue_identities(as_of=ACQUIRED, code_version="isolated-reserved-test")


@pytest.mark.parametrize("kind", ["exchange_code", "display_symbol", "isin"])
def test_reserved_collision_is_partial_and_does_not_rollback_other_symbols_across_restart(platform, kind):
    before = reserve_old_alias(platform, kind=kind)
    retain_new_issue(platform)
    first = ingest(platform)
    partition = next(row for row in first["partitions"] if row["source_dataset"] == "twse_isin_listed")
    assert partition["status"] == "partial" and partition["created_count"] == 1
    assert partition["unresolved"] == [{"venue": "TWSE", "code": CODE, "symbol": CODE + ".TW",
        "reason": "catalogue_identifier_interval_conflict"}]
    assert platform.warehouse.entity_profile(OLD_ID) == before
    rows = platform.warehouse.identity_inventory()
    assert len(rows) == 2
    stock = next(row for row in rows if row["entity_id"] != OLD_ID)
    assert stock["metadata"]["display_symbol"] == "2330.TW"
    assert stock["lifecycle_status"] == "unknown"
    restarted = MarketDataPlatform(database_path=platform.warehouse.path)
    second = ingest(restarted)
    assert second["created_count"] == 0 and second["refreshed_count"] == 1
    assert second["write"]["created_revision_count"] == 0
    assert second["write"]["created_event_count"] == 0
    assert restarted.warehouse.entity_profile(OLD_ID) == before
    assert {row["entity_id"] for row in restarted.warehouse.identity_inventory()} == {OLD_ID, stock["entity_id"]}


def test_superseded_older_interval_is_not_adopted_as_the_new_issuance(platform):
    before = reserve_old_alias(platform, kind="exchange_code", valid_from="2025-01-01T00:00:00+00:00")
    retain_new_issue(platform)
    result = ingest(platform)
    partition = next(row for row in result["partitions"] if row["source_dataset"] == "twse_isin_listed")
    assert partition["status"] == "succeeded" and partition["unresolved"] == []
    assert result["created_count"] == 2
    assert platform.warehouse.entity_profile(OLD_ID) == before
    new = next(row for row in platform.warehouse.identity_inventory()
               if row["entity_id"] != OLD_ID and row["entity_type"] == "warrant")
    assert new["metadata"]["issuance_identity"]["isin"] == ISIN
    assert new["lifecycle_status"] == "unknown"
