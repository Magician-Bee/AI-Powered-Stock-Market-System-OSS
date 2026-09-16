"""Bounded research selection and independent candidate diagnostics; no live feeds/models."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import threading
from types import SimpleNamespace

import pytest

from open_stock_ai.execution import autonomous_campaign as campaign_module
from open_stock_ai.execution.autonomous_campaign import validate_research_symbols
from open_stock_ai.execution.agent_campaign_actions import propose_plan
from open_stock_ai.execution.trading_plan import content_hash
from test_agent_campaign_actions import CONTEXT, setup
from test_autonomous_campaign import NOW, history_fixture


@pytest.mark.parametrize("limit", [True, False, 0, 21, 1.0, "1", None])
def test_research_limit_is_strict_before_any_scan_or_io(limit):
    with pytest.raises(ValueError, match="deep_limit_between_1_and_20"):
        validate_research_symbols(None, deep_limit=limit)


@pytest.mark.parametrize("symbols", [[], (), "2330.TW", ["2330.tw"], [" 2330.TW"], ["AAPL"],
                                    ["123.TW"], ["1234567.TWO"], [None], ["2330.TW", "2330.TW"],
                                    [f"{2300+i}.TW" for i in range(21)]])
def test_research_symbols_reject_invalid_whole_batches(symbols):
    with pytest.raises(ValueError):
        validate_research_symbols(symbols, deep_limit=20)


def test_research_helper_preserves_request_order_and_rejects_truncation():
    symbols = ["8299.TWO", "2330.TW", "006201.TWO"]
    assert validate_research_symbols(None, deep_limit=20) is None
    assert validate_research_symbols(symbols, deep_limit=3) == tuple(symbols)
    assert symbols == ["8299.TWO", "2330.TW", "006201.TWO"]
    with pytest.raises(ValueError, match="exceed_deep_limit"):
        validate_research_symbols(symbols, deep_limit=2)


def test_on_demand_uses_full_ordinary_universe_without_bulk_quote_or_rotation_rank(tmp_path):
    symbols = tuple(f"{2300+i}.TW" for i in range(25))
    calls = []
    def history(symbol, _):
        calls.append(symbol)
        return history_fixture(symbol)
    service, _ = setup(tmp_path, symbols=symbols, history=history)
    original = service.scanner
    async def sparse_scan():
        scanned = await original()
        for row in scanned["all_features"]:
            if row["symbol"] == "2324.TW":
                row.update(close=None, data_as_of=None)
        scanned["features"] = [row for row in scanned["features"] if row["symbol"] != "2324.TW"]
        return scanned
    service.scanner = sparse_scan
    with service.plans.store._connect() as conn:
        conn.execute("update autonomous_campaign_state set research_cursor=7")
    cycle = asyncio.run(service.research(now=NOW, deep_limit=2, symbols=["2324.TW", "2301.TW"]))
    assert calls == ["2324.TW", "2301.TW"]
    assert [row["symbol"] for row in cycle["results"]] == calls
    assert cycle["results"][0]["feature"]["close"] is None
    assert cycle["requested_symbols"] == calls and cycle["selection"]["mode"] == "on_demand"
    assert cycle["deep_success_count"] == 2 and cycle["deep_success_count_scope"] == "verified_history_retained"
    assert cycle["ordinary_stock_count"] == 25
    assert cycle["research_family"]["current_universe_symbols"] == list(symbols)
    assert cycle["research_family"]["evaluation_family_size"] == 50
    assert len(service._evidence(cycle["bulk_evidence_id"], "market_screen")["features"]) == 26
    assert service.status()["research_cursor"] == 7
    assert service.status()["deep_research_coverage"]["symbols_succeeded"] == 2
    assert not service.plans.list(account_id=service.broker.account_id) and service.broker.submissions == []
    assert service.cycle(cycle["cycle_id"]) == cycle


@pytest.mark.parametrize("fault", ["unknown", "duplicate_row", "conflicting_venue", "exchange_mismatch"])
def test_invalid_requested_identity_rejects_entire_batch_before_fetch(tmp_path, fault):
    calls = []
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"), history=lambda symbol, _: calls.append(symbol))
    original = service.scanner
    async def conflicting_scan():
        scanned = await original()
        row = next(row for row in scanned["all_features"] if row["symbol"] == "2317.TW")
        if fault == "duplicate_row":
            scanned["all_features"].append(deepcopy(row))
        elif fault == "conflicting_venue":
            scanned["all_features"].append({**row, "symbol": "2317.TWO", "exchange": "TPEx"})
        elif fault == "exchange_mismatch":
            row["exchange"] = "TPEx"
        return scanned
    service.scanner = conflicting_scan
    target = "9999.TW" if fault == "unknown" else "2317.TW"
    with pytest.raises(ValueError, match="requested_research_symbol"):
        asyncio.run(service.research(now=NOW, symbols=["2330.TW", target]))
    assert calls == []
    with service.plans.store._connect() as conn:
        assert conn.execute("select count(*) from autonomous_research_cycles").fetchone()[0] == 0
        assert conn.execute("select count(*) from autonomous_evidence").fetchone()[0] == 0


@pytest.mark.parametrize("fault,reason", [("short", "insufficient_completed_history"), ("source", "identity_or_provenance"),
    ("symbol", "identity_or_provenance"), ("future", "future_bar"), ("chronology", "strictly_chronological"),
    ("hash", "data_hash_mismatch"), ("normalized_hash", "normalized_hash_mismatch")])
def test_requested_history_hard_gates_still_prevent_proposals(tmp_path, fault, reason):
    history = history_fixture("2330.TW")
    if fault == "short":
        history["rows"] = history["rows"][-61:]
    elif fault == "source":
        history["data_evidence"]["source_provenance_verified"] = False
    elif fault == "symbol":
        history["data_evidence"]["symbol"] = "2317.TW"
    elif fault == "future":
        history["rows"][-1]["timestamp"] = (NOW+timedelta(days=1)).isoformat()
    elif fault == "chronology":
        history["rows"][1]["timestamp"] = history["rows"][0]["timestamp"]
    elif fault == "hash":
        history["data_evidence"]["data_sha256"] = "0"*64
    else:
        history["data_evidence"]["normalized_data_sha256"] = "0"*64
    service, _ = setup(tmp_path, symbols=("2330.TW",), history=lambda *_: history)
    async def scenario():
        cycle = await service.research(now=NOW, symbols=["2330.TW"])
        assert cycle["results"] == [] and cycle["deep_success_count"] == 0
        assert reason in cycle["errors"][0]["error"]
        assert cycle["candidate_evaluation_success_count"] == cycle["candidate_evaluation_error_count"] == 0
        with pytest.raises(ValueError, match="requires_retained_symbol_history"):
            await propose_plan(service, {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "quantity_shares": 1,
                "stop_loss": 95, "rationale": "Must reject invalid fixture history"}, host_context=CONTEXT, now=NOW)
    asyncio.run(scenario())


@pytest.mark.parametrize("all_fail", [False, True])
def test_candidate_failures_keep_verified_history_and_allow_discretionary_proposal(tmp_path, monkeypatch, all_fail):
    original = campaign_module.evaluate_candidate
    calls, history_calls = [], []
    def evaluated(strategy, *args, **kwargs):
        calls.append(strategy.candidate_id)
        if all_fail or len(calls) % 2 == 1:
            raise ValueError("offline candidate evaluation failed")
        return original(strategy, *args, **kwargs)
    monkeypatch.setattr(campaign_module, "evaluate_candidate", evaluated)
    def history(symbol, _):
        history_calls.append(symbol)
        return history_fixture(symbol)
    service, _ = setup(tmp_path, symbols=("2330.TW",), history=history)
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1, symbols=["2330.TW"])
        row = cycle["results"][0]
        assert cycle["deep_success_count"] == 1 and cycle["errors"] == []
        assert row["candidate_evaluation_status"] == ("failed" if all_fail else "partial")
        assert cycle["candidate_evaluation_error_count"] == (2 if all_fail else 1)
        assert cycle["candidate_evaluation_success_count"] == (0 if all_fail else 1)
        assert all(error["stage"] == "candidate_evaluation" for error in row["evaluation_errors"])
        assert all(candidate.get("qualification_id") for candidate in row["candidates"])
        if all_fail:
            automatic = await service.create_plans(cycle_id=cycle["cycle_id"], now=NOW)
            assert automatic["plans"] == [] and automatic["skipped"][0]["reason"] == "no_frozen_candidate_entry"
        args = {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "quantity_shares": 1,
                "stop_loss": 95, "rationale": "Independent offline research proposal"}
        plan = await propose_plan(service, args, host_context=CONTEXT, now=NOW)
        assert plan["definition"]["qualification_id"] is None
        assert plan["definition"]["metadata"]["positive_ev_qualified"] is False
        assert plan["definition"]["evidence_ids"][0] == row["history_id"]
        retry = await propose_plan(service, args, host_context=CONTEXT, now=NOW+timedelta(seconds=1))
        assert retry["definition_hash"] == plan["definition_hash"]
        await service.research(now=NOW+timedelta(seconds=2), deep_limit=1, symbols=["2330.TW"])
        with service.plans.store._connect() as conn:
            coverage = conn.execute("select success_count,last_error from autonomous_deep_research_coverage").fetchone()
        assert tuple(coverage) == (2, None)
    asyncio.run(scenario())
    assert history_calls == ["2330.TW"]  # A strategy failure does not invalidate the current history cache.
    assert len(calls) == 4 and service.broker.submissions == []


@pytest.mark.parametrize("stage", ["signal_generation", "qualification_retention"])
def test_generation_or_retention_failure_does_not_create_placeholder_candidates(tmp_path, monkeypatch, stage):
    service, _ = setup(tmp_path, symbols=("2330.TW",))
    strategies = campaign_module.frozen_candidates()
    def failed(*args, **kwargs):
        raise ValueError("offline stage failure")
    if stage == "signal_generation":
        first = strategies[0]
        monkeypatch.setattr(campaign_module, "frozen_candidates", lambda: (
            SimpleNamespace(candidate_id=first.candidate_id, strategy_version_hash=first.strategy_version_hash, generate_signal=failed),
            strategies[1]))
    else:
        retain = service._retain
        count = 0
        def selected_failure(kind, payload):
            nonlocal count
            if kind == "qualification":
                count += 1
                if count == 1:
                    failed()
            return retain(kind, payload)
        service._retain = selected_failure
    cycle = asyncio.run(service.research(now=NOW, symbols=["2330.TW"]))
    row = cycle["results"][0]
    assert row["candidate_evaluation_status"] == "partial" and cycle["errors"] == []
    assert len(row["candidates"]) == len(row["evaluation_errors"]) == 1
    assert row["evaluation_errors"][0]["stage"] == stage
    assert row["candidates"][0]["strategy_id"] == strategies[1].candidate_id
    qualification = service._evidence(row["candidates"][0]["qualification_id"], "qualification")
    assert qualification["positive_ev_qualified"] is False


def test_cached_candidate_cpu_work_yields_and_cancellation_stops_remaining_work(tmp_path, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, history_calls = [], []
    original = campaign_module.evaluate_candidate
    def slow(strategy, *args, **kwargs):
        calls.append(strategy.candidate_id)
        started.set()
        assert release.wait(3), "event loop was blocked by candidate CPU work"
        try:
            return original(strategy, *args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(campaign_module, "evaluate_candidate", slow)
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"), history=lambda symbol, _: history_calls.append(symbol))
    service._retain("price_history", history_fixture("2330.TW"))
    service._retain("price_history", history_fixture("2317.TW"))
    async def scenario():
        task = asyncio.create_task(service.research(now=NOW, symbols=["2330.TW", "2317.TW"]))
        try:
            async def wait_started():
                while not started.is_set():
                    await asyncio.sleep(.001)
            await asyncio.wait_for(wait_started(), timeout=1)
            # Actual management can run while pure candidate evaluation waits.
            assert (await asyncio.wait_for(service.manage(now=NOW), timeout=1))["errors"] == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 2)
        with service.plans.store._connect() as conn:
            assert conn.execute("select count(*) from autonomous_research_cycles").fetchone()[0] == 0
            assert conn.execute("select count(*) from autonomous_evidence where kind='qualification'").fetchone()[0] == 0
    asyncio.run(scenario())
    assert len(calls) == 1 and history_calls == []
