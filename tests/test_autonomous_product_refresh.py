"""Explicit research refresh wiring using isolated sources, clocks and stores."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
import socket
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from stock_ai import autonomous_trading_service as wiring
from stock_ai.data_platform import security_loader, service as data_service
from stock_ai.market_intelligence import broad_scanner
from open_stock_ai.execution import autonomous_campaign as campaign_module
from open_stock_ai.execution.agent_campaign_actions import propose_plan
from test_agent_campaign_actions import CONTEXT
from test_agent_campaign_actions import setup as campaign_setup
from test_autonomous_campaign import NOW as RESEARCH_NOW
from test_autonomous_trading_plans import NOW, market, setup as execution_setup


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("product refresh tests must not open real transports")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(data_service, "get_market_data_platform", lambda: SimpleNamespace())


def install(monkeypatch, *, run, scanned=None):
    calls = []
    class Loader:
        def __init__(self, platform):
            assert platform is not None
        def run(self, *, force, stop_event):
            assert force is False and isinstance(stop_event, Event)
            calls.append(("refresh", get_ident()))
            return run(stop_event)
    class Scanner:
        def __init__(self, *, position_loader):
            assert position_loader() == {}
        def scan(self):
            calls.append(("scan", get_ident()))
            return deepcopy(scanned if scanned is not None else {"features": [], "all_features": []})
    monkeypatch.setattr(security_loader, "OfficialSecurityMasterLoader", Loader)
    monkeypatch.setattr(broad_scanner, "BroadScanner", Scanner)
    return calls


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "skipped_fresh", "refresh_in_progress"])
def test_research_refresh_precedes_scan_and_retains_bounded_partition_diagnostics(monkeypatch, status):
    result = {"status": status, "partitions": [
        {"source_id": "twse_openapi", "partition_key": "twse_official_master", "status": "skipped_fresh",
         "ttl_seconds": 86400, "cursor": "b" * 64},
        {"source_id": "twse_isin", "partition_key": "twse_isin_listed", "status": "succeeded", "run_id": "IR-fixture",
         "classification": {"raw_payload_id": "RAW-fixture", "raw_sha256": "a" * 64, "row_count": 47000,
                            "sync": {"updated_count": 2, "unmatched_bindings": ["unmatched"] * 47000,
                                     "ambiguous_bindings": ["ambiguous"] * 1000}}},
        {"source_id": "twse_isin", "partition_key": "tpex_isin_otc", "status": "failed",
         "error": {"type": "ValueError", "message": "isolated_parse_error:" + "x" * 2000},
         "classification": {"raw_payload_id": "RAW-rejected", "raw_sha256": "c" * 64}},
    ]}
    calls = install(monkeypatch, run=lambda _: result)
    response = asyncio.run(wiring._scan())
    summary = response["security_master_refresh"]
    assert [name for name, _ in calls] == ["refresh", "scan"]
    assert all(thread != get_ident() for _, thread in calls)
    assert summary["status"] == status and summary["force"] is False
    assert summary["partition_status_counts"] == {"skipped_fresh": 1, "succeeded": 1, "failed": 1}
    assert summary["partitions"][1]["classification"]["raw_payload_id"] == "RAW-fixture"
    assert summary["partitions"][1]["classification"]["sync"] == {
        "updated_count": 2, "unmatched_bindings_count": 47000, "ambiguous_bindings_count": 1000}
    assert summary["partitions"][2]["classification"]["raw_payload_id"] == "RAW-rejected"
    assert summary["partitions"][2]["error"]["message"].startswith("isolated_parse_error:")
    update_slo = summary["update_slo"]
    unsigned_policy = {key: value for key, value in update_slo["policy"].items() if key != "policy_id"}
    assert update_slo["policy"]["policy_id"] == "AMUSLO-" + wiring.content_hash(unsigned_policy)
    assert unsigned_policy == wiring._SECURITY_MASTER_UPDATE_SLO_POLICY
    assert update_slo["declared_at"] <= update_slo["completed_at"] <= update_slo["deadline_at"]
    assert update_slo["policy"]["max_completion_seconds"] == 300.0
    assert update_slo["policy"]["max_attempt_failure_rate"] == 0.0
    assert len(json.dumps(summary)) < 4000


def test_refresh_failure_uses_cached_research_without_claiming_refresh_success(monkeypatch):
    def failed(_):
        raise OSError("isolated refresh unavailable")
    calls = install(monkeypatch, run=failed)
    result = asyncio.run(wiring._scan())
    assert [name for name, _ in calls] == ["refresh", "scan"]
    assert result["security_master_refresh"]["status"] == "failed"
    assert result["security_master_refresh"]["error"]["type"] == "OSError"
    assert result["security_master_refresh"]["partition_count"] == 0


def test_refresh_evidence_is_retained_and_expired_cached_product_cannot_generate_stock_candidate(tmp_path, monkeypatch):
    campaign, _ = campaign_setup(tmp_path, symbols=("2330.TW",))
    scanned = asyncio.run(campaign.scanner())
    for key in ("features", "all_features"):
        for row in scanned[key]:
            receipt = row["product_classification"]
            receipt["acquired_at"] = (RESEARCH_NOW - timedelta(days=8)).isoformat()
            receipt["source_updated_on"] = (RESEARCH_NOW - timedelta(days=8)).date().isoformat()
    install(monkeypatch, run=lambda _: {"status": "partial", "partitions": [{
        "source_id": "twse_isin", "partition_key": "twse_isin_listed", "status": "failed",
        "error": {"type": "TimeoutError", "message": "isolated transport timeout"},
        "classification": {"raw_payload_id": "RAW-failed-source", "raw_sha256": "d" * 64}}]}, scanned=scanned)
    campaign.scanner = wiring._scan
    cycle = asyncio.run(campaign.research(now=RESEARCH_NOW, symbols=["2330.TW"]))
    assert cycle["security_master_refresh_status"] == "partial"
    assert cycle["deep_success_count"] == 1 and cycle["results"][0]["candidates"] == []
    assert "product_classification_stale" in cycle["results"][0]["candidate_evaluation_reasons"]
    retained = campaign._evidence(cycle["bulk_evidence_id"], "market_screen")
    assert retained["security_master_refresh"]["partitions"][0]["classification"]["raw_payload_id"] == "RAW-failed-source"
    assert not campaign.plans.list(account_id=campaign.broker.account_id)


@pytest.mark.parametrize("explicit_now", [False, True])
def test_research_clock_covers_catalogue_acquired_during_live_scan(tmp_path, monkeypatch, explicit_now):
    campaign, _ = campaign_setup(tmp_path, symbols=("2330.TW",))
    scanned = asyncio.run(campaign.scanner())
    acquired = RESEARCH_NOW + timedelta(seconds=2)
    for key in ("features", "all_features"):
        for row in scanned[key]:
            row["product_classification"]["acquired_at"] = acquired.isoformat()

    class HostClock(datetime):
        instant = RESEARCH_NOW
        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

    def refresh(_):
        HostClock.instant = acquired
        return {"status": "succeeded", "partitions": []}

    install(monkeypatch, run=refresh, scanned=scanned)
    monkeypatch.setattr(campaign_module, "datetime", HostClock)
    campaign.scanner = wiring._scan
    history_times = []
    original_history = campaign.history_loader
    async def history(symbol, now):
        history_times.append(now)
        return await original_history(symbol, now)
    campaign.history_loader = history

    async def scenario():
        cycle = await campaign.research(symbols=["2330.TW"], **({"now": RESEARCH_NOW} if explicit_now else {}))
        cutoff = RESEARCH_NOW if explicit_now else acquired
        assert cycle["created_at"] == cycle["research_family"]["registered_at"] == cutoff.isoformat()
        assert history_times == [cutoff]
        result = cycle["results"][0]
        assert result["product_admission"]["classification"]["acquired_at"] == acquired.isoformat()
        if explicit_now:
            assert cycle["ordinary_stock_count"] == 0 and not result["candidates"]
            assert "product_classification_from_future" in result["candidate_evaluation_reasons"]
        else:
            assert cycle["ordinary_stock_count"] == 1 and result["candidates"]
            assert result["product_admission"]["allowed"] is True
            plan = await propose_plan(campaign, {
                "cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "position_size_pct": 3,
                "stop_loss": 95, "target_price": 130, "rationale": "Offline post-refresh clock regression",
            }, host_context=CONTEXT, now=cutoff)
            assert plan["definition"]["metadata"]["product_admission"]["allowed"] is True
            assert plan["definition"]["metadata"]["cycle_id"] == cycle["cycle_id"]
    asyncio.run(scenario())


def test_slow_refresh_worker_does_not_block_position_protection(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    def slow(_):
        entered.set()
        assert release.wait(5)
        return {"status": "skipped_fresh", "partitions": []}
    calls = install(monkeypatch, run=slow)
    _, broker, pid, executor = execution_setup(tmp_path)
    async def scenario():
        await executor.tick(pid, market=market(), now=NOW)
        research = asyncio.create_task(wiring._scan())
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            later = NOW + timedelta(seconds=1)
            protected = await asyncio.wait_for(executor.tick(pid, market=market(94, later), now=later), timeout=1)
            assert protected["state"]["exit_intent"]["side"] == "sell"
            assert len(broker.submissions) == 2 and calls[0][0] == "refresh" and len(calls) == 1
        finally:
            release.set()
            await research
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_stage", ["catalogue", "master"])
def test_cancellation_finishes_current_native_partition_but_starts_no_later_partition_or_scan(monkeypatch, cancel_stage):
    entered, release, finished = Event(), Event(), Event()
    partitions, scans = [], []
    class Incremental:
        def __init__(self, _):
            pass
        def run(self, **kwargs):
            assert kwargs["force"] is False
            partitions.append(kwargs["partition_key"])
            entered.set()
            assert release.wait(5)
            # Model the native partition's successful checkpoint/lease cleanup.
            finished.set()
            return {"status": "succeeded", "partition_key": kwargs["partition_key"]}
    monkeypatch.setattr(security_loader, "IncrementalLoader", Incremental)
    if cancel_stage == "catalogue":
        from stock_ai.data_platform import product_catalog
        monkeypatch.setattr(product_catalog, "IncrementalLoader", Incremental)
    else:
        monkeypatch.setattr(security_loader, "OfficialProductClassificationLoader",
                            lambda _: SimpleNamespace(run=lambda **kwargs: []))
    monkeypatch.setattr(broad_scanner, "BroadScanner", lambda **_: scans.append("started"))
    async def scenario():
        research = asyncio.create_task(wiring._scan())
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(.001)
        research.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await research
        finally:
            release.set()
        async with asyncio.timeout(2):
            while not finished.is_set():
                await asyncio.sleep(.001)
    asyncio.run(scenario())  # Joins the executor, including the cancelled worker.
    assert partitions == ["twse_isin_listed" if cancel_stage == "catalogue" else "twse_official_master"]
    assert not scans


def test_status_local_master_read_and_manage_do_not_trigger_source_refresh(tmp_path, monkeypatch):
    campaign, _ = campaign_setup(tmp_path)
    calls = install(monkeypatch, run=lambda _: pytest.fail("read/management must not refresh"))
    monkeypatch.setattr(wiring, "get_autonomous_model_review", lambda _: SimpleNamespace(status=lambda: {}))
    from stock_ai import phase1_data
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: SimpleNamespace(securities=lambda **_: []))
    assert phase1_data.list_securities_master(include_lifecycle=True) == []
    assert asyncio.run(wiring.autonomous_status_snapshot(campaign))["account_id"] == campaign.broker.account_id
    assert asyncio.run(campaign.manage(now=RESEARCH_NOW))["errors"] == []
    assert calls == []
