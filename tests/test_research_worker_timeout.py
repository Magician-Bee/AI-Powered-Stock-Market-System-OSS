"""Host research timeouts reach the supervisor without broadening other workers."""
import asyncio
import json
import sqlite3

import pytest

from open_stock_ai.agent_runtime.orchestrator import _worker_type
from open_stock_ai.agent_runtime.workers import WorkerSupervisor, profile_for
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider


def research_spec():
    return next(row for row in AutonomousTradingToolProvider().manifest() if row["name"] == "autonomy.research")


@pytest.mark.parametrize("change,expected", [
    ({}, "read_only_research"),
    ({"long_running": False}, "in_process"),
    ({"long_running": "true"}, "in_process"),
    ({"category": "market"}, "in_process"),
    ({"category": "paper"}, "in_process"),
    ({"risk_class": "financial_paper"}, "in_process"),
    ({"mutating": True}, "in_process"),
    ({"destructive": True}, "in_process"),
    ({"side_effects": ["send_order"]}, "in_process"),
    ({"required_permissions": ["paper_execute"]}, "in_process"),
    ({"requires_paper_execution": True}, "in_process"),
    ({"requires_project_execution": True}, "in_process"),
    ({"requires_external_execution": True}, "in_process"),
    ({"requires_full_execution": True}, "in_process"),
    ({"execution_backend": "broker"}, "broker"),
    ({"category": "project"}, "project"),
    ({"category": "external"}, "external"),
])
def test_only_host_declared_read_only_long_research_uses_the_new_profile(change, expected):
    assert _worker_type({**research_spec(), **change}) == expected
    assert profile_for("in_process").maximum_timeout_seconds == 120
    assert profile_for("broker").maximum_timeout_seconds == 900
    assert profile_for("project").maximum_timeout_seconds == 600


def supervisor(tmp_path):
    path = tmp_path / "workers.sqlite"
    AgentRunStore(path).create_run("AR-offline", {"objective": "Offline research timeout proof", "symbols": [],
        "driver_id": "codex", "autonomy": "advisory", "max_steps": 1})
    return WorkerSupervisor(path, socket_path=tmp_path / "unused.sock")


def row_for(supervised):
    with sqlite3.connect(supervised.path) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("select * from agent_workers where run_id='AR-offline'").fetchone())
    row["payload"] = json.loads(row.pop("payload_json"))
    return row


async def execute(supervised, handler, *, requested=None):
    spec = research_spec()
    return await supervised.execute(run_id="AR-offline", step_id="research", worker_type=_worker_type(spec),
        timeout_seconds=spec["timeout_seconds"] if requested is None else requested,
        payload={"tool": spec["name"], "arguments": {"deep_limit": 6}, "audit_arguments": {"deep_limit": 6}},
        handler=handler)


@pytest.mark.parametrize("requested,effective", [(None, 600), (40, 40), (3600, 600)])
def test_real_manifest_budget_reaches_wait_for_and_persisted_worker_record(tmp_path, monkeypatch, requested, effective):
    supervised = supervisor(tmp_path)
    observed = []
    real_wait_for = asyncio.wait_for

    async def inspect_wait(awaitable, timeout):
        observed.append(timeout)
        # Inspect the real supervision budget without spending minutes on a test.
        return await real_wait_for(awaitable, timeout=2)

    monkeypatch.setattr("open_stock_ai.agent_runtime.workers.supervisor.asyncio.wait_for", inspect_wait)
    async def handler(request):
        assert request.timeout_seconds == effective
        return {"bounded_offline_result": True}
    worker_id, result = asyncio.run(execute(supervised, handler, requested=requested))
    row = row_for(supervised)
    assert observed == [effective] and result == {"bounded_offline_result": True}
    assert row["worker_id"] == worker_id and row["status"] == "completed" and row["pid"] is None
    assert row["worker_type"] == "read_only_research"
    assert row["payload"]["requested_timeout_seconds"] == (600 if requested is None else requested)
    assert row["payload"]["effective_timeout_seconds"] == effective
    assert row["payload"]["worker_profile"]["maximum_timeout_seconds"] == 600
    assert row["payload"]["arguments"] == {"deep_limit": 6}


def test_long_research_cancels_immediately_and_persists_cancelled_state(tmp_path):
    supervised = supervisor(tmp_path)
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def pending(request):
            assert request.timeout_seconds == 600
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(execute(supervised, pending))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await supervised.cancel_run("AR-offline") == 1
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and not supervised.active_workers(run_id="AR-offline")
    asyncio.run(scenario())
    row = row_for(supervised)
    assert row["status"] == "cancelled" and json.loads(row["error_json"])["type"] == "CancelledError"
    assert row["payload"]["effective_timeout_seconds"] == 600
    assert row["payload"]["worker_profile"]["supports_cancellation"] is True


def test_research_timeout_remains_bounded_and_cleans_up_handler(tmp_path, monkeypatch):
    supervised = supervisor(tmp_path)
    stopped = []
    async def expire(awaitable, timeout):
        assert timeout == 600
        await asyncio.sleep(0)
        awaitable.cancel()
        await asyncio.gather(awaitable, return_exceptions=True)
        raise TimeoutError("offline simulated deadline")
    monkeypatch.setattr("open_stock_ai.agent_runtime.workers.supervisor.asyncio.wait_for", expire)
    async def pending(_request):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)
    with pytest.raises(TimeoutError, match="offline simulated deadline"):
        asyncio.run(execute(supervised, pending))
    row = row_for(supervised)
    assert row["status"] == "failed" and json.loads(row["error_json"])["type"] == "TimeoutError"
    assert stopped == [True] and not supervised._tasks
