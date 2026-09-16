"""Explicit campaign stops supersede older Host authorization; all stores are temporary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from open_stock_ai.execution import autonomous_campaign as campaign_module
from open_stock_ai.execution.trading_plan import utc_time
from stock_ai import autonomous_trading_api as api
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.autonomous_model_review import AutonomousModelReview
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from test_autonomous_campaign import setup as campaign_setup
from test_autonomous_model_review import RuntimeFixture
from test_autonomy_instrument_scope import context


@pytest.fixture
def clock(monkeypatch):
    current = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0] if tz else current[0].replace(tzinfo=None)
    monkeypatch.setattr(campaign_module, "datetime", Clock)
    monkeypatch.setattr("stock_ai.agent_run_store._now", lambda: current[0].isoformat())
    return current


def test_initial_disabled_state_is_not_a_user_stop_and_survives_restart(tmp_path, clock):
    campaign, _ = campaign_setup(tmp_path)
    assert campaign.status()["enabled"] is False
    assert campaign.status()["last_disabled_at"] is None
    authorized = clock[0]
    restarted, _ = campaign_setup(tmp_path, shared=campaign.plans.store)
    assert restarted.status()["last_disabled_at"] is None
    assert restarted.configure(enabled=True, authorized_at=authorized)["enabled"] is True
    assert restarted.status()["last_disabled_at"] is None


def test_explicit_stop_rejects_older_or_equal_authority_but_accepts_a_new_user_run(tmp_path, clock):
    campaign, _ = campaign_setup(tmp_path)
    old = clock[0]
    campaign.configure(enabled=True, authorized_at=old)
    clock[0] += timedelta(seconds=1)
    stopped = campaign.configure(enabled=False)
    assert stopped["last_disabled_at"] == clock[0].isoformat()
    restarted, _ = campaign_setup(tmp_path, shared=campaign.plans.store)
    for unauthorized in (old, clock[0]):
        with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
            restarted.configure(enabled=True, authorized_at=unauthorized)
    assert restarted.status()["enabled"] is False
    clock[0] += timedelta(seconds=1)
    assert restarted.configure(enabled=True, authorized_at=clock[0])["enabled"] is True
    # A new enable never erases the stop history or restores old run authority.
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        campaign.assert_activation_authorized(authorized_at=old)


@pytest.mark.parametrize("authorized", [None, "", "not-a-time", "2026-09-11T00:00:00"])
def test_missing_or_invalid_host_run_time_cannot_authorize(tmp_path, authorized):
    campaign, _ = campaign_setup(tmp_path)
    with pytest.raises(PermissionError, match="host_timestamp_required"):
        campaign.assert_activation_authorized(authorized_at=authorized)


def test_final_enable_reads_the_stop_inside_the_sqlite_write_transaction(tmp_path, clock):
    campaign, _ = campaign_setup(tmp_path)
    authority = clock[0]
    campaign.assert_activation_authorized(authorized_at=authority)
    entered = Event()
    def enable():
        entered.set()
        return campaign.configure(enabled=True, authorized_at=authority)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with campaign.plans.store._connect() as stopping:
            stopping.execute("begin immediate")
            # Simulate the concurrent user stop that wins the writer lock after precheck.
            stopping.execute("update autonomous_campaign_state set enabled=0,last_disabled_at=? where account_id=?",
                             ((authority + timedelta(seconds=1)).isoformat(), campaign.broker.account_id))
            future = pool.submit(enable)
            assert entered.wait(timeout=2)
            stopping.commit()
        with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
            future.result(timeout=5)
    assert campaign.status()["enabled"] is False


def test_task_suspend_resume_preserves_authority_without_creating_campaign_stop(tmp_path, clock):
    campaign, _ = campaign_setup(tmp_path)
    runs = AgentRunStore(tmp_path / "host-runs.sqlite")
    run = runs.create_run("AR-original", {"autonomy": "paper_execute"})
    runs.pause_run(run["run_id"], status="suspended")
    clock[0] += timedelta(seconds=1)
    assert runs.mark_resuming(run["run_id"])
    resumed = runs.get_run(run["run_id"])
    assert resumed["created_at"] == run["created_at"]
    assert campaign.configure(enabled=True, authorized_at=resumed["created_at"])["enabled"] is True
    assert campaign.status()["last_disabled_at"] is None
    campaign.configure(enabled=False)
    runs.pause_run(run["run_id"], status="suspended")
    clock[0] += timedelta(seconds=1)
    assert runs.mark_resuming(run["run_id"])
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        campaign.configure(enabled=True, authorized_at=runs.get_run(run["run_id"])["created_at"])
    new_run = runs.create_run("AR-new-user-request", {"autonomy": "paper_execute"})
    assert campaign.configure(enabled=True, authorized_at=new_run["created_at"])["enabled"] is True


@pytest.mark.parametrize("tool", ["autonomy.activate", "autonomy.propose_plan"])
def test_tool_uses_persisted_run_time_not_model_arguments_or_context(tmp_path, monkeypatch, clock, tool):
    campaign, _ = campaign_setup(tmp_path)
    runtime = RuntimeFixture(tmp_path / "host-runs.sqlite")
    run = runtime.store.create_run("offline-scoped", {"autonomy": "paper_execute"})
    clock[0] += timedelta(seconds=1)
    campaign.configure(enabled=False)
    runtime.store.pause_run(run["run_id"], status="suspended")
    assert runtime.store.mark_resuming(run["run_id"])
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _: SimpleNamespace(runtime=runtime))
    def forbidden(*args, **kwargs):
        pytest.fail("Old authorization must not reach the plan builder")
    monkeypatch.setattr("open_stock_ai.execution.agent_campaign_actions.propose_plan", forbidden)
    ctx = context(())
    forged = (clock[0] + timedelta(days=1)).isoformat()
    ctx.state["authorized_at"] = forged
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        asyncio.run(AutonomousTradingToolProvider().execute(tool,
            {"cycle_id": "c", "symbol": "2330.TW", "authorized_at": forged}, ctx))
    assert not campaign.status()["enabled"]


def test_stop_after_plan_creation_prevents_enable_review_and_success_receipt(tmp_path, monkeypatch, clock):
    campaign, _ = campaign_setup(tmp_path)
    runtime = RuntimeFixture(tmp_path / "host-runs.sqlite")
    runtime.store.create_run("offline-scoped", {"autonomy": "paper_execute"})
    calls = []
    async def create_plans(**kwargs):
        calls.append("waiting_plan_created")
        clock[0] += timedelta(seconds=1)
        campaign.configure(enabled=False)
        return {"plans": [{"plan_id": "waiting-only"}], "skipped": []}
    def forbidden(*args, **kwargs):
        pytest.fail("A rejected activation must not enable model review or retain a success receipt")
    campaign.create_plans = create_plans
    campaign.cycle = lambda cycle_id: {"cycle_id": cycle_id, "created_at": datetime.now(timezone.utc).isoformat()}
    campaign._retain = forbidden
    review = SimpleNamespace(runtime=runtime, register_current_review=lambda **kwargs: calls.append("review_registered"), configure=forbidden)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _: review)
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.activate",
            {"cycle_id": "c", "use_candidate_plans": True}, context(())))
    assert calls == ["waiting_plan_created", "review_registered"]
    assert campaign.status()["enabled"] is False


def test_review_enable_rechecks_stop_after_campaign_was_enabled(tmp_path, clock):
    campaign, _ = campaign_setup(tmp_path)
    runtime = RuntimeFixture(tmp_path / "host-runs.sqlite")
    review = AutonomousModelReview(campaign=campaign, runtime=runtime)
    authority = clock[0]
    campaign.configure(enabled=True, authorized_at=authority)
    clock[0] += timedelta(seconds=1)
    campaign.configure(enabled=False)
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        review.configure(enabled=True, authorized_at=authority)
    assert review.status()["enabled"] is False
    assert campaign.status()["enabled"] is False
    clock[0] += timedelta(seconds=1)
    campaign.configure(enabled=True, authorized_at=clock[0])
    assert review.configure(enabled=True, authorized_at=clock[0])["enabled"] is True


@pytest.mark.parametrize("failure", ["initialize", "configure"])
def test_control_stop_precedes_failure_in_review_control(monkeypatch, failure):
    state = {"enabled": True}
    service = SimpleNamespace(configure=lambda *, enabled: state.update(enabled=enabled) or dict(state))
    monkeypatch.setattr(api, "get_autonomous_campaign", lambda: service)
    def fail(*args, **kwargs):
        assert state["enabled"] is False
        raise RuntimeError("offline review failed")
    monkeypatch.setattr(api, "get_autonomous_model_review", fail if failure == "initialize" else lambda _: SimpleNamespace(configure=fail))
    with pytest.raises(RuntimeError, match="offline review failed"):
        api.autonomy_control(api.AutonomyControl(enabled=False))
    assert state["enabled"] is False


def test_http_activate_cannot_overwrite_a_stop_while_creating_plans(tmp_path, monkeypatch, clock):
    campaign, _ = campaign_setup(tmp_path)
    campaign.cycle = lambda cycle_id: {"cycle_id": cycle_id, "created_at": datetime.now(timezone.utc).isoformat()}
    async def create_plans(**kwargs):
        clock[0] = datetime.now(timezone.utc) + timedelta(seconds=1)
        campaign.configure(enabled=False)
        return {"plans": [], "skipped": []}
    campaign.create_plans = create_plans
    monkeypatch.setattr(api, "get_autonomous_campaign", lambda: campaign)
    def forbidden(*args, **kwargs):
        pytest.fail("A newer stop must reject before model review initialization")
    monkeypatch.setattr(api, "get_autonomous_model_review", forbidden)
    with pytest.raises(PermissionError, match="disabled_after_run_authorization"):
        asyncio.run(api.autonomy_activate(api.ActivateCycleRequest(cycle_id="c", use_candidate_plans=True)))
    assert campaign.status()["enabled"] is False
