"""Per-security coverage ledger contracts; all evidence is isolated fixture data."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading

import pytest

from open_stock_ai.execution.research_coverage import COVERAGE_DOMAINS
from test_autonomous_campaign import NOW, history_fixture, setup
from product_admission_fixtures import product_feature


def load_coverage_audit():
    path = Path(__file__).parents[1] / "scripts" / "audit_autonomous_research_coverage.py"
    spec = importlib.util.spec_from_file_location("audit_autonomous_research_coverage", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def feature(symbol: str, *, entity_id: str | None = None) -> dict:
    row = {
        **product_feature(symbol, now=NOW),
        "name": symbol,
        "industry": "fixture-industry",
        "close": 106.0,
        "data_as_of": NOW.date().isoformat(),
        "trade_value": 1000.0,
        "source": "official-fixture",
        "data_quality": {
            "status": "partial",
            "score": 0.75,
            "source": "official-fixture",
            "quality_flags": ["cross_source_observation_not_available"],
            "source_observation": {},
        },
        "evidence": [{"evidence_id": f"quote:{symbol}:{NOW.date().isoformat()}"}],
        "scanner_factor_inputs": {
            "fundamental": {
                "value": 60.0, "status": "partial", "source": "official-revenue-fixture",
                "domain": "revenue", "dataset": "revenues_monthly",
                "available_at": (NOW - timedelta(days=10)).isoformat(),
                "historical_pit_eligible": False,
                "reason": "current_source_export_not_historical_pit_certified",
            },
            "valuation": {
                "value": 55.0, "status": "ready", "source": "warehouse-fixture",
                "domain": "financials", "dataset": "valuation_metrics",
                "revision_id": "REV-financial", "available_at": (NOW - timedelta(days=20)).isoformat(),
                "historical_pit_eligible": True,
            },
            "chip": {
                "value": 70.0, "status": "ready", "source": "warehouse-fixture",
                "domain": "flows", "dataset": "institutional_flows_daily",
                "revision_id": "REV-flow", "available_at": (NOW - timedelta(days=1)).isoformat(),
                "historical_pit_eligible": True,
            },
            "event": {
                "value": 50.0, "status": "ready", "source": "warehouse-fixture",
                "domain": "events", "dataset": "news_events",
                "revision_id": "REV-event", "available_at": (NOW - timedelta(days=1)).isoformat(),
                "historical_pit_eligible": True,
            },
        },
    }
    if entity_id is not None:
        row["entity_id"] = entity_id
    return row


def test_research_ledgers_every_universe_entity_not_only_the_selected_subset(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"))
    rows = [feature("2330.TW"), feature("2317.TW"), feature("0050.TW")]

    async def scanner():
        return {"features": deepcopy(rows), "all_features": deepcopy(rows)}

    service.scanner = scanner
    cycle = asyncio.run(service.research(now=NOW, deep_limit=1))
    summary = service.status()["security_research_coverage"]

    assert cycle["deep_selected_count"] == cycle["deep_success_count"] == 1
    assert summary["status"] == "available"
    assert summary["security_count"] == 3
    assert summary["universe_expires_at"]
    assert summary["deep_research_status_counts"] == {"current": 1, "never_researched": 2}
    assert summary["complete_deep_coverage"] is False
    assert summary["all_domains_current"] is False
    assert summary["data_domain_counts"]["intraday_price"]["unavailable"] == 3

    result = service.coverage_ledger.query(limit=10)
    assert [item["symbol"] for item in result["items"]] == ["0050.TW", "2317.TW", "2330.TW"]
    assert all(set(item["data_domains"]) == set(COVERAGE_DOMAINS) for item in result["items"])
    selected = next(item for item in result["items"] if item["deep_research_status"] == "current")
    assert selected["data_domains"]["price_history"]["availability"] == "ready"
    assert selected["data_domains"]["price_history"]["freshness"] == "current"
    assert selected["last_deep_research_cycle_id"] == cycle["cycle_id"]
    never = next(item for item in result["items"] if item["deep_research_status"] == "never_researched")
    assert never["data_domains"]["price_history"]["reason"] == "deep_price_history_never_researched"


def test_domain_statuses_keep_partial_current_stale_and_missing_distinct(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    row = feature("2330.TW")
    row["scanner_factor_inputs"]["valuation"]["available_at"] = (NOW - timedelta(days=121)).isoformat()
    row["scanner_factor_inputs"].pop("event")

    service.coverage_ledger.sync(
        [row], observed_at=NOW,
        product_admissions={"2330.TW": {"allowed": True, "reasons": []}},
        source_cycle_id=None,
    )
    item = service.coverage_ledger.query(symbols=["2330.TW"])["items"][0]

    daily = item["data_domains"]["daily_price"]
    assert daily["availability"] == "partial" and daily["freshness"] == "current"
    assert daily["needs_update"] is True and daily["expires_at"] and daily["next_update_at"]
    financials = item["data_domains"]["financials"]
    assert financials["availability"] == "ready" and financials["freshness"] == "stale"
    assert item["data_domains"]["news_events"]["availability"] == "unavailable"
    assert item["data_domains"]["industry"]["availability"] == "ready"

    missing_events = service.coverage_ledger.query(domain="news_events", needs_update=True)
    assert [entry["symbol"] for entry in missing_events["items"]] == ["2330.TW"]
    assert set(missing_events["items"][0]["data_domains"]) == {"news_events"}
    assert service.coverage_ledger.query(domain="industry", needs_update=True)["items"] == []


def test_ambiguous_reused_symbol_expands_each_entity_and_does_not_borrow_history(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    symbol = "085974.TWO"
    service._retain("price_history", history_fixture(symbol))
    with service.plans.store._connect() as conn:
        conn.execute(
            """insert into autonomous_deep_research_coverage(
                   account_id,symbol,last_selected_at,selection_count,last_success_at,success_count,last_error
               ) values (?,?,?,?,?,?,?)""",
            (service.broker.account_id, symbol, NOW.isoformat(), 1, NOW.isoformat(), 1, None),
        )
        conn.commit()
    row = feature("2330.TW")
    row.update(
        symbol=symbol,
        entity_id=None,
        exchange="unknown",
        lifecycle_status="unknown",
        product_classification={"product_type": "unknown"},
    )
    row["data_quality"]["source_observation"] = {"primary_quote_binding": {
        "reason": "ambiguous_research_listing_identity",
        "candidate_count": 2,
        "candidates": [
            {"entity_id": "WARRANT-NEW", "exchange": "TPEx", "trading_status": "pre_listing"},
            {"entity_id": "WARRANT-OLD", "exchange": "TPEx", "trading_status": "expired"},
        ],
    }}
    service.coverage_ledger.sync(
        [row], observed_at=NOW,
        product_admissions={symbol: {"allowed": False, "reasons": ["product_classification_conflict"]}},
        source_cycle_id="AC-ambiguous",
    )

    items = service.coverage_ledger.query(symbols=[symbol])["items"]
    assert {item["entity_id"] for item in items} == {"WARRANT-NEW", "WARRANT-OLD"}
    assert {item["lifecycle_status"] for item in items} == {"pre_listing", "expired"}
    assert all(item["deep_research_status"] == "not_attributed" for item in items)
    assert all(item["data_domains"]["identity"]["availability"] == "conflict" for item in items)
    assert all(item["data_domains"]["price_history"]["availability"] == "conflict" for item in items)
    for domain in ("daily_price", "financials", "revenue", "ownership_flows", "news_events", "industry"):
        assert all(item["data_domains"][domain]["availability"] == "conflict" for item in items)
        assert all(item["data_domains"][domain]["needs_update"] is True for item in items)
    assert all(item["new_entry_eligible"] is False for item in items)


def test_single_new_entity_never_inherits_an_old_issuance_history_by_symbol(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    symbol = "2330.TW"
    old = feature(symbol)
    old_entity = old["entity_id"]

    async def old_scanner():
        return {"features": [deepcopy(old)], "all_features": [deepcopy(old)]}

    service.scanner = old_scanner
    cycle = asyncio.run(service.research(now=NOW, deep_limit=1))
    old_item = service.coverage_ledger.query(symbols=[symbol])["items"][0]
    assert old_item["entity_id"] == old_entity
    assert old_item["deep_research_status"] == "current"
    assert old_item["last_deep_research_cycle_id"] == cycle["cycle_id"]

    new = feature(symbol, entity_id="ENT-" + "1" * 32)
    service.coverage_ledger.sync(
        [new], observed_at=NOW + timedelta(minutes=1),
        product_admissions={symbol: {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )
    current = service.coverage_ledger.query(symbols=[symbol])["items"]
    assert len(current) == 1 and current[0]["entity_id"] == "ENT-" + "1" * 32
    assert current[0]["deep_research_status"] == "not_attributed"
    assert current[0]["last_deep_research_evidence_id"] is None
    assert current[0]["data_domains"]["price_history"]["availability"] == "unavailable"
    assert current[0]["last_deep_research_error"] == "legacy_symbol_research_not_attributable_to_entity"


def test_latest_universe_retires_absent_rows_and_survives_campaign_restart(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"))
    rows = [feature("2330.TW"), feature("2317.TW")]
    admissions = {row["symbol"]: {"allowed": True, "reasons": []} for row in rows}
    first = service.coverage_ledger.sync(rows, observed_at=NOW, product_admissions=admissions, source_cycle_id=None)
    second = service.coverage_ledger.sync(rows[:1], observed_at=NOW + timedelta(minutes=1),
                                          product_admissions=admissions, source_cycle_id=None)
    assert first["security_count"] == 2 and second["security_count"] == 1
    assert [item["symbol"] for item in service.coverage_ledger.query()["items"]] == ["2330.TW"]
    with service.plans.store._connect() as conn:
        retired = conn.execute(
            """select in_latest_universe from autonomous_security_research_coverage
               where account_id=? and symbol='2317.TW'""",
            (service.broker.account_id,),
        ).fetchone()
    assert retired[0] == 0

    restarted, _ = setup(tmp_path / "restart", shared=service.plans.store)
    assert restarted.status()["security_research_coverage"]["snapshot_id"] == second["snapshot_id"]
    assert restarted.coverage_ledger.query()["items"][0]["symbol"] == "2330.TW"


def test_late_older_sync_cannot_replace_a_newer_current_universe(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    newer = service.coverage_ledger.sync(
        [feature("2317.TW")], observed_at=NOW + timedelta(minutes=2),
        product_admissions={"2317.TW": {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )
    returned = service.coverage_ledger.sync(
        [feature("2330.TW")], observed_at=NOW,
        product_admissions={"2330.TW": {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )

    assert returned["snapshot_id"] == newer["snapshot_id"]
    assert returned["observed_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert [item["symbol"] for item in service.coverage_ledger.query()["items"]] == ["2317.TW"]


def test_query_filters_page_stably_and_rejects_ambiguous_requests(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    rows = [feature(symbol) for symbol in ("2301.TW", "2302.TW", "2303.TW")]
    service.coverage_ledger.sync(
        rows, observed_at=NOW,
        product_admissions={row["symbol"]: {"allowed": True, "reasons": []} for row in rows},
        source_cycle_id=None,
    )
    first = service.coverage_ledger.query(deep_status="never_researched", new_entry_eligible=True, limit=2)
    second = service.coverage_ledger.query(deep_status="never_researched", new_entry_eligible=True,
                                           after=first["next_after"], limit=2)
    assert [item["symbol"] for item in first["items"]] == ["2301.TW", "2302.TW"]
    assert [item["symbol"] for item in second["items"]] == ["2303.TW"]
    assert first["next_after"] and second["next_after"] is None
    for kwargs, reason in (
        ({"limit": 0}, "limit"),
        ({"symbols": ["AAPL"]}, "symbols"),
        ({"deep_status": "success"}, "deep_status"),
        ({"domain": "quotes"}, "domain"),
        ({"needs_update": True}, "requires_domain"),
    ):
        with pytest.raises(ValueError, match=reason):
            service.coverage_ledger.query(**kwargs)


def test_snapshot_payloads_are_json_and_have_stable_hashes(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    summary = service.coverage_ledger.sync(
        [feature("2330.TW")], observed_at=NOW,
        product_admissions={"2330.TW": {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )
    with service.plans.store._connect() as conn:
        row = conn.execute(
            """select summary_json,snapshot_sha256 from autonomous_security_research_coverage_snapshots
               where snapshot_id=?""",
            (summary["snapshot_id"],),
        ).fetchone()
    decoded = json.loads(row[0])
    assert decoded["security_count"] == 1
    assert len(row[1]) == 64 and summary["snapshot_id"].startswith("ACV-")


def test_empty_or_duplicate_universe_fails_before_replacing_last_valid_snapshot(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    row = feature("2330.TW")
    accepted = service.coverage_ledger.sync(
        [row], observed_at=NOW,
        product_admissions={"2330.TW": {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )
    with pytest.raises(ValueError, match="official_universe_empty"):
        service.coverage_ledger.sync([], observed_at=NOW + timedelta(minutes=1),
                                     product_admissions={}, source_cycle_id=None)
    duplicate = feature("2317.TW", entity_id=row["entity_id"])
    with pytest.raises(ValueError, match="duplicate_entity_identity"):
        service.coverage_ledger.sync(
            [row, duplicate], observed_at=NOW + timedelta(minutes=2),
            product_admissions={row["symbol"]: {"allowed": True, "reasons": []},
                                duplicate["symbol"]: {"allowed": True, "reasons": []}},
            source_cycle_id=None,
        )
    assert service.coverage_ledger.latest_summary()["snapshot_id"] == accepted["snapshot_id"]
    assert [item["symbol"] for item in service.coverage_ledger.query()["items"]] == ["2330.TW"]


def test_exact_duplicate_scanner_row_is_idempotently_coalesced(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    row = feature("2330.TW")

    summary = service.coverage_ledger.sync(
        [row, deepcopy(row)], observed_at=NOW,
        product_admissions={"2330.TW": {"allowed": True, "reasons": []}}, source_cycle_id=None,
    )

    assert summary["security_count"] == 1
    assert [item["symbol"] for item in service.coverage_ledger.query()["items"]] == ["2330.TW"]


def test_large_coverage_projection_yields_to_position_management(tmp_path, monkeypatch):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    started, release = threading.Event(), threading.Event()
    original = service.coverage_ledger.sync

    def slow_sync(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.coverage_ledger, "sync", slow_sync)

    async def scenario():
        research = asyncio.create_task(service.research(now=NOW, deep_limit=1))
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=2.5)
            managed = await asyncio.wait_for(service.manage(now=NOW), timeout=1)
            assert managed["errors"] == []
        finally:
            release.set()
        await research

    asyncio.run(scenario())


def test_read_only_coverage_audit_recomputes_hashes_counts_and_references(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"))
    cycle = asyncio.run(service.research(now=NOW, deep_limit=1))

    result = load_coverage_audit().audit(
        tmp_path / "campaign.db", account_id=service.broker.account_id,
    )

    assert result["passed"] is True
    assert result["access"] == {
        "sqlite_mode": "ro", "query_only": True, "transaction": "BEGIN", "immutable": False,
    }
    assert result["source_cycle_id"] == cycle["cycle_id"]
    assert result["current_row_count"] == result["summary"]["security_count"] == 3
    assert result["summary"]["universe_expires_at"] == "2026-09-11T07:30:00+00:00"
    assert result["saved_summary_matches"] is True
    assert result["row_snapshot_hash_mismatch_count"] == 0
    assert result["missing_cycle_reference_count"] == 0
    assert result["missing_evidence_reference_count"] == 0
    assert result["problem_counts"] == {}


def test_read_only_coverage_audit_cli_writes_a_verifiable_receipt(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    asyncio.run(service.research(now=NOW, deep_limit=1))
    script = Path(__file__).parents[1] / "scripts" / "audit_autonomous_research_coverage.py"
    receipt = tmp_path / "coverage-audit.json"

    completed = subprocess.run(
        [sys.executable, str(script), "--database", str(tmp_path / "campaign.db"),
         "--account-id", service.broker.account_id, "--output", str(receipt)],
        capture_output=True, text=True, timeout=20,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    stdout = json.loads(completed.stdout)
    saved = json.loads(receipt.read_text())
    assert stdout == saved
    assert saved["passed"] is True
    assert saved["schema_version"] == "open_stock_ai.security_research_coverage_audit.v1"
    digest = saved.pop("receipt_sha256")
    assert digest == load_coverage_audit()._sha(saved)


@pytest.mark.parametrize(
    ("mutation", "expected_problem"),
    (
        ("update autonomous_security_research_coverage set needs_update_mask=0",
         "coverage_needs_update_mask_mismatch"),
        ("update autonomous_security_research_coverage set data_domains_blob=x'00'",
         "coverage_payload_decode_failed"),
        ("update autonomous_security_research_coverage set snapshot_sha256='tampered'",
         "coverage_snapshot_hash_mismatch"),
    ),
)
def test_read_only_coverage_audit_rejects_tampered_storage(tmp_path, mutation, expected_problem):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    asyncio.run(service.research(now=NOW, deep_limit=1))
    tampered = tmp_path / (expected_problem + ".db")
    shutil.copy2(tmp_path / "campaign.db", tampered)
    with sqlite3.connect(tampered) as conn:
        conn.execute(mutation)
        conn.commit()

    result = load_coverage_audit().audit(tampered, account_id=service.broker.account_id)

    assert result["passed"] is False
    assert expected_problem in result["problem_counts"]


def test_read_only_coverage_audit_rejects_missing_snapshot_cycle_reference(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    asyncio.run(service.research(now=NOW, deep_limit=1))
    database = tmp_path / "campaign.db"
    with sqlite3.connect(database) as conn:
        conn.execute("delete from autonomous_research_cycles")
        conn.commit()

    result = load_coverage_audit().audit(database, account_id=service.broker.account_id)

    assert result["passed"] is False
    assert result["problem_counts"]["snapshot_source_cycle_reference_missing"] == 1
