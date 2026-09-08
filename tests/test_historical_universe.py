from __future__ import annotations

from open_stock_ai.research.historical_universe import historical_universe_at
from stock_ai.data_platform.service import MarketDataPlatform


def _record(
    entity_id: str,
    symbol: str,
    *,
    effective_from: str,
    effective_until: str | None = None,
    available_at: str = "2020-01-01T00:00:00+00:00",
    ingested_at: str = "2020-01-01T00:00:00+00:00",
    effective_until_available_at: str | None = None,
    effective_until_ingested_at: str | None = None,
) -> dict[str, object]:
    return {
        "entity_id": entity_id,
        "symbol": symbol,
        "entity_type": "stock",
        "listing_type": "listed",
        "effective_from": effective_from,
        "effective_until": effective_until,
        "effective_until_available_at": effective_until_available_at,
        "effective_until_ingested_at": effective_until_ingested_at,
        "available_at": available_at,
        "ingested_at": ingested_at,
        "historical_pit_eligible": True,
        "universe_scope_id": "taiwan-listed-and-otc-securities",
        "universe_coverage_complete": True,
        "source_revision_id": f"lifecycle-{entity_id}",
    }


def test_historical_universe_restores_delisted_and_current_members_by_effective_date():
    records = [
        _record(
            "EQ-OLD",
            "1111.TW",
            effective_from="2020-01-01T00:00:00+00:00",
            effective_until="2023-01-01T00:00:00+00:00",
            effective_until_available_at="2022-12-01T00:00:00+00:00",
            effective_until_ingested_at="2022-12-01T00:00:00+00:00",
        ),
        _record("EQ-NEW", "2222.TW", effective_from="2022-01-01T00:00:00+00:00"),
    ]

    before_delisting = historical_universe_at(records, as_of="2022-06-01T00:00:00+00:00", required_entity_id="EQ-OLD")
    after_delisting = historical_universe_at(records, as_of="2024-06-01T00:00:00+00:00", required_entity_id="EQ-OLD")

    assert before_delisting["passed"] is True
    assert [item["symbol"] for item in before_delisting["members"]] == ["1111.TW", "2222.TW"]
    assert before_delisting["members"][0]["effective_until"] is None
    assert before_delisting["hidden_future_end_event_count"] == 1
    assert after_delisting["passed"] is False
    assert [item["symbol"] for item in after_delisting["members"]] == ["2222.TW"]
    assert "historical_universe_required_entity_not_member" in after_delisting["blockers"]


def test_historical_universe_requires_end_event_availability_and_complete_scope():
    missing_end_lineage = _record(
        "EQ-OLD",
        "1111.TW",
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2023-01-01T00:00:00+00:00",
    )
    incomplete_scope = _record(
        "EQ-NEW",
        "2222.TW",
        effective_from="2020-01-01T00:00:00+00:00",
    )
    incomplete_scope["universe_coverage_complete"] = False

    end_receipt = historical_universe_at(
        [missing_end_lineage],
        as_of="2022-06-01T00:00:00+00:00",
        required_entity_id="EQ-OLD",
    )
    scope_receipt = historical_universe_at(
        [incomplete_scope],
        as_of="2022-06-01T00:00:00+00:00",
        required_entity_id="EQ-NEW",
    )

    assert end_receipt["passed"] is False
    assert "historical_universe_effective_until_availability_missing:0" in end_receipt["blockers"]
    assert scope_receipt["passed"] is False
    assert "historical_universe_coverage_incomplete:0" in scope_receipt["blockers"]


def test_historical_universe_rejects_currently_backfilled_membership_as_past_knowledge():
    receipt = historical_universe_at(
        [
            _record(
                "EQ-OLD",
                "1111.TW",
                effective_from="2020-01-01T00:00:00+00:00",
                available_at="2026-01-01T00:00:00+00:00",
                ingested_at="2026-01-01T00:00:00+00:00",
            )
        ],
        as_of="2022-06-01T00:00:00+00:00",
        required_entity_id="EQ-OLD",
    )

    assert receipt["passed"] is False
    assert receipt["excluded_after_knowledge_cutoff"] == 1
    assert "historical_universe_no_known_membership_records" in receipt["blockers"]
    assert "historical_universe_required_entity_not_member" in receipt["blockers"]


def test_future_membership_does_not_leak_or_block_on_later_lifecycle_details():
    future = _record(
        "EQ-FUTURE",
        "3333.TW",
        effective_from="2026-01-01T00:00:00+00:00",
        effective_until="2027-01-01T00:00:00+00:00",
        available_at="2026-01-01T00:00:00+00:00",
        ingested_at="2026-01-01T00:00:00+00:00",
    )

    receipt = historical_universe_at(
        [future],
        as_of="2022-06-01T00:00:00+00:00",
    )

    assert receipt["passed"] is False
    assert receipt["excluded_after_knowledge_cutoff"] == 1
    assert not any("effective_until_availability_missing" in item for item in receipt["blockers"])


def test_warehouse_lifecycle_projection_preserves_unknown_publication_time_as_unavailable(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market-data.sqlite")
    platform.sync_security_master_payloads(
        twse_companies=[
            {
                "公司代號": "1111",
                "公司簡稱": "測試股",
                "公司名稱": "測試股份有限公司",
                "上市日期": "1090102",
            }
        ],
        twse_quotes=[{"Code": "1111", "Name": "測試股"}],
        acquired_at="2026-01-01T00:00:00+00:00",
    )

    records = platform.warehouse.historical_universe_records(knowledge_at="2026-01-02T00:00:00+00:00")
    receipt = historical_universe_at(
        records,
        as_of="2020-06-01T00:00:00+00:00",
        required_entity_id=records[0]["entity_id"],
    )

    assert records[0]["available_at"] is None
    assert records[0]["historical_pit_eligible"] is False
    assert receipt["passed"] is False
    assert any("historical_universe_availability_missing" in item for item in receipt["blockers"])
