"""Missing-month fixtures retain verified bars without certifying full OOS data."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from open_stock_ai.execution.agent_plan_proposal import build_agent_plan_proposal
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.research.official_candle_loader import load_official_candles
from product_admission_fixtures import product_snapshot
from test_agent_plan_proposal import fixture as proposal_fixture
from test_autonomous_campaign import NOW, setup


COUNTS = {"202604": 20, "202605": 20, "202606": 21, "202607": 22, "202608": 21, "202609": 8}


def payload(month, *, code="1303"):
    day = date(int(month[:4]), int(month[4:]), 1)
    rows = []
    while len(rows) < COUNTS[month]:
        if day.weekday() < 5:
            rows.append([f"{day.year-1911}/{day.month:02}/{day.day:02}", "1000", "100000", "100", "101", "99", "100"])
        day += timedelta(days=1)
    return {"stat": "OK", "date": month+"01", "title": f"{code} fixture 各日成交資訊", "data": rows}


def load(tmp_path, *, corrupt_month=None, foreign_host=False):
    def fetch(url, timeout):
        month = parse_qs(urlparse(url).query)["date"][0][:6]
        if month not in COUNTS:
            raise OSError("explicit_fixture_missing_archived_month")
        raw = json.dumps(payload(month, code="2303" if month == corrupt_month else "1303")).encode()
        return (raw, "https://example.com/history") if foreign_host else raw

    return load_official_candles("1303.TW", start="2025-09-01", end="2026-09-10",
                                 max_months=14, cache_dir=tmp_path, fetch_bytes=fetch)


def normalized(history):
    result = deepcopy(history)
    result["rows"] = [{**row, "timestamp": datetime.fromisoformat(row["date"]).replace(
        hour=13, minute=30, tzinfo=ZoneInfo("Asia/Taipei")).isoformat()} for row in result["rows"]]
    result["data_evidence"]["data_sha256"] = content_hash(result["rows"])
    result["data_evidence"]["fixture_only_not_market_or_ev_proof"] = True
    return result


def test_missing_months_do_not_revoke_verified_returned_source_identity(tmp_path):
    result = load(tmp_path)
    source = result["data_evidence"]
    assert len(result["rows"]) == 112
    assert source["source_provenance_verified"] and source["instrument_identity_verified"]
    assert source["coverage_complete"] is False
    assert source["verified_start"] == "2026-04-01" and source["verified_end"] == "2026-09-10"
    assert source["rejected_months"] == ["202509", "202510", "202511", "202512", "202601", "202602", "202603"]
    assert source["verification_rejected_months"] == []
    rejected = [row for row in result["source_request_receipts"] if row["status"] == "rejected"]
    assert all(row["failure_kind"] == "coverage_gap" and row["reason"] for row in rejected)


@pytest.mark.parametrize("fault", ["identity", "hash", "host"])
def test_actual_verification_failures_cannot_be_downgraded_to_coverage_gaps(tmp_path, fault):
    if fault == "hash":
        load(tmp_path)
        path = tmp_path/"1303.TW-202605.json"
        cached = json.loads(path.read_text())
        cached["raw_sha256"] = "0"*64
        path.write_text(json.dumps(cached))
    result = load(tmp_path, corrupt_month="202605" if fault == "identity" else None, foreign_host=fault == "host")
    source = result["data_evidence"]
    assert not source["source_provenance_verified"] and not source["instrument_identity_verified"]
    assert source["verification_rejected_months"]
    assert any(row.get("failure_kind") == "verification_failure" for row in result["source_request_receipts"])


def test_112_verified_partial_bars_retain_diagnostics_and_build_discretionary_plan(tmp_path):
    history = normalized(load(tmp_path/"cache"))
    campaign, _ = setup(tmp_path, symbols=("1303.TW",), history=lambda *_: history)
    cycle = asyncio.run(campaign.research(now=NOW, deep_limit=1))
    assert cycle["deep_success_count"] == 1 and cycle["errors"] == []
    result = cycle["results"][0]
    assert result["bar_count"] == 112 and result["coverage_complete"] is False
    attempt = campaign._evidence(result["source_attempt_id"], "history_source_attempt")
    assert attempt["source_request_receipts"] == history["source_request_receipts"]
    assert all(not c["positive_ev_qualified"] and not c["research_paper_candidate_eligible"] for c in result["candidates"])
    for candidate in result["candidates"]:
        qualification = campaign._evidence(candidate["qualification_id"], "qualification")
        assert "declared_source_range_incomplete" in qualification["input_reasons"]
        assert qualification["partitions"] == {}  # incomplete requested range is never an OOS evaluation
    retained = {identifier: {"kind": kind, "payload": campaign._evidence(identifier, kind)} for identifier, kind in [
        (result["history_id"], "price_history"), (cycle["bulk_evidence_id"], "market_screen")]}
    account = {**asyncio.run(campaign.broker.account(now=NOW)), "account_id": campaign.broker.account_id}
    plan = build_agent_plan_proposal(
        {"cycle_id": cycle["cycle_id"], "symbol": "1303.TW", "quantity_shares": 10,
         "stop_loss": 95, "target_price": 110, "rationale": "Explicit offline bounded partial-history proposal"},
        cycle=cycle, retained_evidence=retained, account_summary=account,
        host_context=proposal_fixture()[1]["host_context"], now=NOW,
        product_snapshot=product_snapshot("1303.TW", now=NOW))
    assert plan.quantity_shares == 10 and plan.metadata["eligibility"] == "bounded_experiment"
    assert plan.qualification_id is None and plan.metadata["positive_ev_qualified"] is False
    saved = campaign.plans.create(account_id=campaign.broker.account_id, plan=plan, idempotency_key="partial-test", now=NOW)
    assert saved["definition"]["evidence_ids"] == list(plan.evidence_ids)


@pytest.mark.parametrize("fault,reason", [("short", "insufficient_completed_history"),
                                         ("chronology", "history_not_strictly_chronological"),
                                         ("hash", "history_data_hash_mismatch"),
                                         ("identity", "history_identity_or_provenance_not_verified")])
def test_campaign_rejection_still_retains_the_source_attempt(tmp_path, fault, reason):
    history = normalized(load(tmp_path/"cache"))
    if fault == "short":
        history["rows"] = history["rows"][-61:]
        history["data_evidence"]["data_sha256"] = content_hash(history["rows"])
    elif fault == "chronology":
        history["rows"][0], history["rows"][1] = history["rows"][1], history["rows"][0]
    elif fault == "hash":
        history["data_evidence"]["data_sha256"] = "0"*64
    else:
        history["data_evidence"]["symbol"] = "2303.TW"
    campaign, _ = setup(tmp_path, symbols=("1303.TW",), history=lambda *_: history)
    cycle = asyncio.run(campaign.research(now=NOW, deep_limit=1))
    assert cycle["deep_success_count"] == 0
    error = cycle["errors"][0]
    assert reason in error["error"]
    assert "history_id" not in error
    attempt = campaign._evidence(error["source_attempt_id"], "history_source_attempt")
    assert attempt["source_request_receipts"] == history["source_request_receipts"]
