from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.automation import (
    AutomationController, AutomationIntent, AutomationKind, AutomationStore,
    CostPolicy, NotificationManager, NotificationPolicy,
)
from open_stock_ai.agent_runtime.automation.controller import _decision_changed
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime, _automation_confidence

NOW = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)


def _setup(tmp_path, *, maximum=2):
    store = AutomationStore(tmp_path / 'automation.sqlite')
    controller = AutomationController(store, notifications=NotificationManager(store, {}), reanalyze=lambda *_: {'action': 'hold'})
    intent = AutomationIntent(
        goal='watch test symbol', user_id='test-user', session_id='test-session', symbol='2330.TW',
        kind=AutomationKind.CONDITION_WATCH, trigger={'type': 'price_crossing', 'field': 'price'},
        decision_logic={'field': 'price', 'operator': 'gte', 'value': 100},
        analysis=({'type': 'strategy_reanalysis'},), actions=({'type': 'notify'},),
        notification_policy=NotificationPolicy(channels=('in_app',), cooldown_seconds=0),
        cost_policy=CostPolicy(max_reanalysis_per_day=maximum),
    )
    activated = controller.confirm_and_activate(intent, user_confirmed=True, now=NOW)
    return store, controller, activated.automation['automation_id']


def _used(store, automation_id):
    with sqlite3.connect(store.path) as conn:
        return conn.execute('select budget_day, count(*) from agent_automation_reanalysis_budget where automation_id=? group by budget_day order by budget_day', (automation_id,)).fetchall()


@pytest.mark.parametrize(('decision_confidence', 'expected'), [(0, 0), (1, .01), (20, .2), (55, .55), (80, .8), (100, 1), (101, 1), (-1, 0), ('NaN', 0), ('inf', 0), ('invalid', 0), (None, 0)])
def test_agent_decision_confidence_is_percentage(decision_confidence, expected):
    assert _automation_confidence({'decision': {'confidence': decision_confidence}}) == expected


@pytest.mark.parametrize(('value', 'expected'), [(.8, .8), (80, .8), (1, 1), ('NaN', 0)])
def test_legacy_automation_confidence_fraction(value, expected):
    assert _automation_confidence({'confidence': value}) == expected


def test_structured_reanalysis_preserves_action_and_same_occurrence_identity(monkeypatch):
    monkeypatch.delenv('STOCK_AI_AGENT_BACKGROUND_PAUSED', raising=False)
    requests = []
    result = {'status': 'completed', 'summary': 'advisory only', 'decision': {'action': 'buy', 'symbol': '2330.TW', 'confidence': 55}}
    async def create_run(**kwargs):
        requests.append(kwargs)
        return {'run_id': 'child-one'}
    async def wait(_):
        return result
    runtime = SimpleNamespace(create_run=create_run, wait=wait, snapshot=lambda _: {'evidence': [{'evidence_id': 'price-one'}]})
    intent = SimpleNamespace(goal='watch', symbol='2330.TW', session_id='owner')
    async def scenario():
        first = await DurableAgentRuntime._automation_reanalyze(runtime, intent, {'source_event_id': 'bar-one'}, None)
        result['decision'] = {'action': 'sell', 'symbol': '2330.TW', 'confidence': 55}
        second = await DurableAgentRuntime._automation_reanalyze(runtime, intent, {'source_event_id': 'bar-one', 'restart_recovery': True}, first)
        assert first['recommendation'] == first['action'] == 'buy'
        assert first['confidence'] == .55
        assert first['evidence_ids'] == ['price-one']
        assert second['action'] == 'sell' and _decision_changed(first, second)
        assert requests[0]['idempotency_key'] == requests[1]['idempotency_key']
        assert requests[0]['session_id'] == 'owner'
        assert requests[0]['autonomy'] == 'advisory'
        result['status'] = 'max_steps_reached'
        with pytest.raises(RuntimeError, match='did not complete'):
            await DurableAgentRuntime._automation_reanalyze(runtime, intent, {'event_id': 'bar-two'}, first)
    asyncio.run(scenario())


def test_meaningful_change_uses_action_and_confidence_not_summary_wording():
    original = {'action': 'buy', 'symbol': '2330.TW', 'confidence': .55, 'conclusion': 'first wording'}
    assert not _decision_changed(original, {**original, 'conclusion': 'paraphrased wording'})
    assert _decision_changed(original, {**original, 'action': 'sell'})
    assert _decision_changed(original, {**original, 'confidence': .7})
    assert not _decision_changed(original, {**original, 'confidence': .69})
    assert not _decision_changed(original, {**original, 'confidence': 55})
    assert _decision_changed({'conclusion': 'watch'}, {'conclusion': 'review now'})


def test_daily_cap_survives_reopen_deduplicates_event_and_resets_on_utc_day(tmp_path):
    store, controller, automation_id = _setup(tmp_path)
    calls = []
    def analyze(*_):
        calls.append(True)
        return {'action': 'hold'}
    filtered = controller.handle_trigger(automation_id, {'event_id': 'below', 'price': 99}, reanalyze=analyze, now=NOW)
    assert not filtered.reanalyzed and _used(store, automation_id) == []
    for event_id in ('one', 'two'):
        assert controller.handle_trigger(automation_id, {'event_id': event_id, 'price': 101}, reanalyze=analyze, now=NOW).reanalyzed
    reopened = AutomationStore(store.path)
    restarted = AutomationController(reopened, notifications=NotificationManager(reopened, {}))
    duplicate = restarted.handle_trigger(automation_id, {'event_id': 'one', 'price': 101}, reanalyze=analyze, now=NOW)
    limited = restarted.handle_trigger(automation_id, {'event_id': 'three', 'price': 101}, reanalyze=analyze, now=NOW)
    assert not duplicate.reanalyzed and not limited.reanalyzed
    assert limited.reason == 'daily_reanalysis_limit_reached'
    assert reopened.get_execution(limited.execution_id)['output']['used'] == 2
    assert reopened.get_automation(automation_id)['state'] == 'active'
    assert len(calls) == 2 and _used(reopened, automation_id) == [('2026-09-11', 2)]
    # Local midnight at UTC+8 is still the same UTC budget day.
    still_today = NOW.replace(hour=0, tzinfo=timezone(timedelta(hours=8))) + timedelta(days=1)
    assert not restarted.handle_trigger(automation_id, {'event_id': 'local-midnight', 'price': 101}, reanalyze=analyze, now=still_today).reanalyzed
    assert restarted.handle_trigger(automation_id, {'event_id': 'next-day', 'price': 101}, reanalyze=analyze, now=NOW + timedelta(days=1)).reanalyzed
    assert len(calls) == 3 and _used(reopened, automation_id) == [('2026-09-11', 2), ('2026-09-12', 1)]


def test_paused_trigger_skips_ai_and_completion_does_not_unpause(tmp_path):
    store, controller, automation_id = _setup(tmp_path)
    controller.pause(automation_id, now=NOW)
    def forbidden(*_):
        raise AssertionError('paused automation called AI')
    outcome = controller.handle_trigger(automation_id, {'event_id': 'paused', 'price': 101}, reanalyze=forbidden, now=NOW)
    assert not outcome.reanalyzed and outcome.reason == 'automation_paused'
    assert store.get_execution(outcome.execution_id)['status'] == 'filtered'
    assert _used(store, automation_id) == []
    controller.resume(automation_id, now=NOW)
    def pause_during_analysis(*_):
        controller.pause(automation_id, now=NOW)
        return {'action': 'hold'}
    schedule = store.list_schedules(automation_id)[0]
    controller.reanalyze = pause_during_analysis
    controller.handle_scheduler_callback(schedule['schedule_id'], {'event_id': 'in-flight', 'price': 101}, now=NOW)
    assert store.get_automation(automation_id)['state'] == 'paused'
    assert store.get_schedule(schedule['schedule_id'])['status'] == 'paused'


def test_failed_attempt_is_charged_and_budget_upgrade_backfills_existing_receipts(tmp_path):
    store, controller, automation_id = _setup(tmp_path, maximum=1)
    def fail(*_):
        raise RuntimeError('provider failed')
    with pytest.raises(RuntimeError, match='provider failed'):
        controller.handle_trigger(automation_id, {'event_id': 'failed', 'price': 101}, reanalyze=fail, now=NOW)
    assert _used(store, automation_id) == [('2026-09-11', 1)]
    # Simulate a pre-budget database whose failed receipt already records spend.
    with sqlite3.connect(store.path) as conn:
        conn.execute('delete from agent_automation_reanalysis_budget')
    reopened = AutomationStore(store.path)
    assert _used(reopened, automation_id) == [('2026-09-11', 1)]
    reopened.set_state(automation_id, 'active', now=NOW)
    restarted = AutomationController(reopened)
    outcome = restarted.handle_trigger(automation_id, {'event_id': 'after-failure', 'price': 101}, reanalyze=fail, now=NOW)
    assert not outcome.reanalyzed and outcome.reason == 'daily_reanalysis_limit_reached'


def test_atomic_budget_and_source_event_claims_across_store_instances(tmp_path):
    store, _, automation_id = _setup(tmp_path, maximum=1)
    barrier = Barrier(8)
    def contend(index):
        local = AutomationStore(store.path)
        execution_id = local.begin_execution(automation_id, stage='trigger_pipeline', source_event_id='same-event', now=NOW)
        barrier.wait(timeout=10)
        reservation = local.reserve_reanalysis(automation_id, execution_id, max_per_day=1, now=NOW)
        return execution_id, reservation
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(contend, range(8)))
    assert len({execution_id for execution_id, _ in results}) == 1
    assert sum(result['admitted'] for _, result in results) == 1
    assert _used(store, automation_id) == [('2026-09-11', 1)]
    store.finish_reanalysis_state(automation_id, 'active', now=NOW)
    execution = store.begin_execution(automation_id, stage='trigger_pipeline', source_event_id='new-event', now=NOW)
    assert store.reserve_reanalysis(automation_id, execution, max_per_day=1, now=NOW)['reason'] == 'daily_reanalysis_limit_reached'


def test_async_reanalysis_uses_shared_budget_and_concurrent_event_does_not_call_ai(tmp_path):
    store, controller, automation_id = _setup(tmp_path, maximum=1)
    calls = []
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def analyze(*_):
            calls.append(True)
            started.set()
            await release.wait()
            return {'action': 'hold'}
        first_task = asyncio.create_task(controller.handle_trigger_async(automation_id, {'event_id': 'first', 'price': 101}, reanalyze=analyze, now=NOW))
        await started.wait()
        duplicate = await controller.handle_trigger_async(automation_id, {'event_id': 'first', 'price': 101}, reanalyze=analyze, now=NOW)
        concurrent = await controller.handle_trigger_async(automation_id, {'event_id': 'second', 'price': 101}, reanalyze=analyze, now=NOW)
        assert not duplicate.reanalyzed and not concurrent.reanalyzed
        release.set()
        assert (await first_task).reanalyzed
        limited = await controller.handle_trigger_async(automation_id, {'event_id': 'second', 'price': 101}, reanalyze=analyze, now=NOW)
        assert limited.reason == 'daily_reanalysis_limit_reached'
    asyncio.run(scenario())
    assert len(calls) == 1 and _used(store, automation_id) == [('2026-09-11', 1)]


def test_unspecified_cost_policy_retains_default_24():
    assert CostPolicy.from_value({}).max_reanalysis_per_day == 24


def test_schedule_same_key_survives_reopen_pause_and_concurrent_registration(tmp_path):
    service = SimpleNamespace(default_driver='codex', drivers={'codex': object()})
    path = tmp_path / 'runs.sqlite'
    runtime = DurableAgentRuntime(service_provider=lambda: service, store=AgentRunStore(path))
    payload = {'name': 'test clock', 'objective': 'test clock', 'dedup_key': 'same-goal', 'next_run_at': NOW.isoformat(), 'session_id': 'owner'}
    original = runtime.create_schedule(payload)
    runtime.disable_schedule(original['schedule_id'])
    reopened = DurableAgentRuntime(service_provider=lambda: service, store=AgentRunStore(path))
    repeated = reopened.create_schedule(payload)
    assert repeated['schedule_id'] == original['schedule_id'] and not repeated['enabled']
    def register(_):
        return reopened.create_schedule({**payload, 'dedup_key': 'concurrent-new'})['schedule_id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(register, range(8)))
    assert len(set(ids)) == 1
    assert len(runtime.list_schedules()) == 2


def test_paused_one_shot_keeps_due_occurrence_across_reopen_and_resume(tmp_path, monkeypatch):
    from dataclasses import replace
    from open_stock_ai.agent_runtime.automation import ProductTimePoller
    store, controller, existing_id = _setup(tmp_path)
    base = AutomationIntent.from_dict(store.latest_version(existing_id)['intent'])
    monkeypatch.setattr(controller.compiler, '_backend', lambda intent: 'internal_scheduler')
    once = replace(base, goal='one-shot review', user_id='once-user', kind=AutomationKind.ONE_SHOT,
                   trigger={'type': 'one_shot', 'run_at': (NOW + timedelta(minutes=1)).isoformat()}, decision_logic={})
    created = controller.confirm_and_activate(once, user_confirmed=True, now=NOW)
    automation_id = created.automation['automation_id']
    schedule_id = created.activation.backend_reference
    controller.pause(automation_id, now=NOW)
    # Migrate a legacy pause that did not also update its internal schedule.
    with sqlite3.connect(store.path) as conn:
        conn.execute("update agent_automation_schedules set status='active' where schedule_id=?", (schedule_id,))
    reopened = AutomationStore(store.path)
    calls = []
    restarted = AutomationController(reopened, reanalyze=lambda *_: calls.append(True) or {'action': 'hold'})
    poller = ProductTimePoller(reopened, restarted.handle_scheduler_callback)
    assert poller.poll(now=NOW + timedelta(minutes=2)).claimed == 0
    assert calls == [] and reopened.get_schedule(schedule_id)['next_due_at'] is not None
    restarted.resume(automation_id, now=NOW + timedelta(minutes=2))
    assert poller.poll(now=NOW + timedelta(minutes=2)).completed == 1
    assert len(calls) == 1 and reopened.get_schedule(schedule_id)['status'] == 'completed'
    assert poller.poll(now=NOW + timedelta(minutes=3)).claimed == 0
