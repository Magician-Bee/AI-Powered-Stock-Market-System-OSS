"""Offline wiring/state/account tests. Fixture returns never qualify positive EV."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

import pytest

from open_stock_ai.execution.autonomous_campaign import AutonomousCampaign
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from test_autonomous_trading_plans import BrokerFixture, market
from product_admission_fixtures import product_feature, product_resolver

NOW = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)


class CalendarFixture:
    def next_trading_day(self, day):
        day += timedelta(days=1)
        while day.weekday() > 4:
            day += timedelta(days=1)
        return day
    def day_status(self, day):
        return {'trading_day': day.weekday() < 5}


def history_fixture(symbol, *, now=NOW):
    days, day = [], now.date()
    while len(days) < 120:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    rows = [{'timestamp': datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=5, minute=30).isoformat(),
             'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 2_000_000} for day in reversed(days)]
    rows[-1].update(close=106, high=107, volume=3_000_000)
    return {'rows': rows, 'data_evidence': {
        'symbol': symbol, 'source_kind': 'exchange_official', 'source_id': 'offline_exchange_contract_fixture',
        'source_provenance_verified': True, 'instrument_identity_verified': True, 'coverage_complete': True,
        'fixture_only_not_market_or_ev_proof': True,
        'corporate_actions_verified': False, 'historical_vintage_verified': False, 'execution_costs_verified': False,
        'data_sha256': content_hash(rows), 'normalized_data_sha256': content_hash(rows),
    }}


def setup(tmp_path, *, broker=None, symbols=('2330.TW', '2317.TW'), shared=None, history=None,
          history_lookback_days=0):
    store = shared or SQLiteStore(tmp_path / 'campaign.db')
    broker = broker or BrokerFixture()
    broker.account_id = getattr(broker, 'account_id', 'campaign-test')
    features = [{**product_feature(symbol, now=NOW), 'close': 106, 'data_as_of': NOW.date().isoformat(), 'trade_value': 1000-index} for index, symbol in enumerate(symbols)]
    features.append({**product_feature('0050.TW', now=NOW), 'close': 200, 'data_as_of': NOW.date().isoformat(), 'is_etf': True})
    async def scanner():
        return {'features': deepcopy(features), 'all_features': deepcopy(features)}
    async def loader(symbol, now):
        return history(symbol, now) if history else history_fixture(symbol)
    quote_state = {'price': 106, 'now': NOW}
    async def quotes(symbol):
        return {**market(quote_state['price'], quote_state['now']), 'symbol': symbol, 'market': 'TW',
                'is_realtime': True, 'is_fallback': False, 'price_source': 'offline_broker_contract_fixture',
                'odd_lot_auction_matched': True}
    service = AutonomousCampaign(plans=TradingPlanStore(store), broker=broker, risk=RiskEngine(), scanner=scanner,
                                 history_loader=loader, quote_loader=quotes, calendar=CalendarFixture(),
                                 history_lookback_days=history_lookback_days, product_resolver=product_resolver)
    return service, quote_state


def test_research_reports_bounded_coverage_no_ev_and_same_bar_reuses_frozen_plan(tmp_path):
    service, _ = setup(tmp_path)
    async def scenario():
        first = await service.research(now=NOW, deep_limit=1)
        assert (first['universe_count'], first['ordinary_stock_count'], first['deep_selected_count']) == (3, 2, 1)
        assert first['deep_success_count'] == 1 and first['model_calls'] == 0
        assert not any(c['positive_ev_qualified'] for c in first['results'][0]['candidates'])
        first_plan = (await service.create_plans(cycle_id=first['cycle_id'], now=NOW))['plans'][0]
        # A later same-bar research receipt has different cycle metadata.
        await service.research(now=NOW+timedelta(seconds=1), deep_limit=1)
        repeated = await service.research(now=NOW+timedelta(seconds=2), deep_limit=1)
        result = await service.create_plans(cycle_id=repeated['cycle_id'], now=NOW+timedelta(seconds=2))
        assert result['reused_plan_ids'] == [first_plan['plan_id']]
        assert result['plans'][0]['definition_hash'] == first_plan['definition_hash']
        assert result['plans'][0]['definition']['metadata']['cycle_id'] == first['cycle_id']
        assert service.status()['positive_ev_qualified'] is False
    asyncio.run(scenario())


def test_default_research_rotation_deep_checks_twenty_usable_stocks(tmp_path):
    symbols = tuple(f'{2300+index}.TW' for index in range(25))
    service, _ = setup(tmp_path, symbols=symbols)

    first = asyncio.run(service.research(now=NOW))
    first_symbols = {item['symbol'] for item in first['results']}
    first_coverage = service.status()['deep_research_coverage']
    original_scanner = service.scanner

    async def reordered_scanner():
        scanned = await original_scanner()
        for index, item in enumerate(reversed(scanned['features'])):
            item['trade_value'] = 10_000-index
        return scanned

    service.scanner = reordered_scanner
    second = asyncio.run(service.research(now=NOW+timedelta(seconds=1)))
    second_symbols = {item['symbol'] for item in second['results']}

    assert first['ordinary_stock_count'] == 25
    assert first['deep_selected_count'] == first['deep_success_count'] == 20
    assert first_coverage == {
        'symbols_attempted': 20,
        'symbols_succeeded': 20,
        'total_attempts': 20,
        'selection_policy': 'bounded_failed_retry_then_never_or_least_recently_selected',
    }
    assert second['deep_selected_count'] == second['deep_success_count'] == 20
    assert first_symbols | second_symbols == set(symbols)
    assert '0050.TW' not in first_symbols | second_symbols
    assert len(first_symbols & second_symbols) == 15
    assert service.status()['deep_research_coverage']['symbols_succeeded'] == 25
    assert service.status()['deep_research_coverage']['total_attempts'] == 40


def test_non_stock_product_requires_explicit_on_demand_deep_research(tmp_path):
    service, _ = setup(tmp_path, symbols=('2330.TW',))

    rotated = asyncio.run(service.research(now=NOW, deep_limit=2))
    requested = asyncio.run(service.research(now=NOW+timedelta(seconds=1), deep_limit=1, symbols=['0050.TW']))

    assert rotated['ordinary_usable_bulk_count'] == 1
    assert [row['symbol'] for row in rotated['results']] == ['2330.TW']
    assert requested['selection']['mode'] == 'on_demand'
    assert requested['deep_success_count'] == 1
    assert requested['results'][0]['symbol'] == '0050.TW'
    assert requested['results'][0]['product_admission']['allowed'] is False
    assert requested['results'][0]['candidate_evaluation_status'] == 'not_evaluated_unsupported_product'


def test_failed_history_retries_use_only_quarter_of_next_rotation(tmp_path):
    symbols = tuple(f'{2300+index}.TW' for index in range(50))
    calls = {}

    def transient(symbol, _):
        calls[symbol] = calls.get(symbol, 0)+1
        if calls[symbol] == 1:
            raise OSError('transient_official_source_fixture')
        return history_fixture(symbol)

    service, _ = setup(tmp_path, symbols=symbols, history=transient)
    first = asyncio.run(service.research(now=NOW))
    second = asyncio.run(service.research(now=NOW+timedelta(days=1)))
    first_symbols = {row['symbol'] for row in first['errors']}
    second_symbols = {row['symbol'] for row in second['results']} | {row['symbol'] for row in second['errors']}

    assert first['deep_success_count'] == 0 and len(first_symbols) == 20
    assert second['deep_selected_count'] == 20 and second['deep_success_count'] == 5
    assert len(first_symbols & second_symbols) == 5
    assert len(second_symbols-first_symbols) == 15


def test_evidence_and_cycles_are_account_scoped_even_for_identical_fixture_inputs(tmp_path):
    service, _ = setup(tmp_path)
    other_broker = BrokerFixture()
    other_broker.account_id = 'other-account'
    other, _ = setup(tmp_path, shared=service.plans.store, broker=other_broker)
    async def scenario():
        left = await service.research(now=NOW, deep_limit=1)
        right = await other.research(now=NOW, deep_limit=1)
        assert left['cycle_id'] != right['cycle_id']
        assert left['bulk_evidence_id'] != right['bulk_evidence_id']
        assert other.cycle(right['cycle_id'])['account_id'] == 'other-account'
        assert other._evidence(right['results'][0]['history_id'])['rows']
        with pytest.raises(ValueError, match='not_found'):
            other.cycle(left['cycle_id'])
    asyncio.run(scenario())


def test_cycle_corruption_future_data_and_expiry_fail_closed(tmp_path):
    service, _ = setup(tmp_path)
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        with pytest.raises(ValueError, match='requires_refresh'):
            await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW-timedelta(seconds=1))
        with pytest.raises(ValueError, match='requires_refresh'):
            await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW+timedelta(days=2))
        with service.plans.store._connect() as conn:
            conn.execute("update autonomous_research_cycles set payload_json='{}' where cycle_id=?", (cycle['cycle_id'],))
        with pytest.raises(ValueError, match='corrupted'):
            service.cycle(cycle['cycle_id'])
    asyncio.run(scenario())
    def future_history(symbol, _):
        result = history_fixture(symbol)
        result['rows'][0]['timestamp'] = (NOW+timedelta(days=1)).isoformat()
        return result
    future, _ = setup(tmp_path / 'future', history=future_history)
    result = asyncio.run(future.research(now=NOW, deep_limit=1))
    assert result['deep_success_count'] == 0 and 'future_bar' in result['errors'][0]['error']


def test_multi_plan_allocations_reserve_cash_and_exposure_before_any_submission(tmp_path):
    symbols = tuple(f'{2300+index}.TW' for index in range(6))
    service, _ = setup(tmp_path, symbols=symbols)
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=6)
        result = await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW)
        assert len(result['plans']) == 4
        assert sum(p['definition']['cash_budget'] for p in result['plans']) <= 20_000
        repeated = await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW)
        assert len(repeated['plans']) == 4 and len(repeated['reused_plan_ids']) == 4
        assert not service.broker.submissions
    asyncio.run(scenario())


def test_real_paper_port_candidate_to_plan_to_receipt_to_closed_reconciles_costs(tmp_path, monkeypatch):
    from open_stock_ai.execution.paper_training import PaperTrainingLab
    clock = {'now': NOW}
    monkeypatch.setattr(PaperOMS, '_now', lambda self: clock['now'].isoformat())
    monkeypatch.setattr(PaperBrokerSimulator, '_now', lambda self: clock['now'].isoformat())
    monkeypatch.setattr(PaperTrainingLab, '_now', lambda self: clock['now'].isoformat())
    storage = SQLiteStore(tmp_path / 'real-paper.db')
    oms = PaperOMS(storage, account_id='offline-plan-flow', initial_cash=1_000_000, commission_bps=14.25,
                   minimum_commission=20, sell_tax_bps=30, slippage_bps=5, read_environment=False)
    port = PaperBrokerPort(PaperBrokerSimulator(storage, oms))
    service, quote = setup(tmp_path, broker=port, shared=storage, symbols=('2330.TW',))
    clock = quote
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        assert cycle['results'][0]['candidates'][0]['positive_ev_qualified'] is False
        created = await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW)
        record = created['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        quote.update(now=due, price=106)
        first = await service.manage(now=due)
        assert first['errors'] == []
        entered = service.plans.get(record['plan_id'])
        assert entered['state']['status'] == 'entry_submitted', entered['state']
        assert entered['state']['entry_receipt']['filled_quantity'] > 0
        # Disabling stops new exposure but must retain management of this position.
        service.configure(enabled=False)
        quote.update(now=due+timedelta(seconds=1), price=math.ceil(record['definition']['target_price']*2)/2)
        managed = await service.manage(now=quote['now'])
        assert managed['errors'] == []
        assert service.plans.get(record['plan_id'])['state']['status'] == 'exit_submitted'
        quote['now'] += timedelta(seconds=1)
        await service.manage(now=quote['now'])
        assert service.plans.get(record['plan_id'])['state']['status'] == 'closed'
        account = await port.account(now=quote['now'])
        fills = port.broker.recent_fills()
        assert len(fills) == 2
        assert sum(f['commission'] for f in fills) > 0 and sum(f['tax'] for f in fills) > 0
        assert account['cash_balance'] == pytest.approx(1_000_000+sum(f['net_cash_delta'] for f in fills), abs=.01)
        assert not any(p['quantity'] for p in account['positions'])
        assert service.status()['positive_ev_qualified'] is False
    asyncio.run(scenario())


def test_disabled_campaign_cancels_remaining_buy_but_manages_filled_shares(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        service.broker.fill_fraction = .5
        quote['now'] = due
        await service.manage(now=due)
        service.configure(enabled=False)
        quote['now'] += timedelta(seconds=1)
        result = await service.manage(now=quote['now'])
        assert result['errors'] == []
        entry = service.broker.orders[service.broker.submissions[0]['order_id']]
        assert entry['is_open'] is False
        assert len(service.broker.submissions) == 1
        assert service.plans.get(record['plan_id'])['state']['remaining_quantity'] > 0
    asyncio.run(scenario())


def test_postclose_attempts_are_persistent_bounded_and_completed_day_does_not_repeat(tmp_path):
    service, _ = setup(tmp_path)
    service.configure(enabled=True)
    assert service.claim_postclose_research(now=NOW)
    service.finish_postclose_research(error='network fixture failed', now=NOW)
    reopened, _ = setup(tmp_path, shared=service.plans.store)
    assert not reopened.claim_postclose_research(now=NOW+timedelta(minutes=30))
    assert reopened.claim_postclose_research(now=NOW+timedelta(hours=1))
    assert reopened.claim_postclose_research(now=NOW+timedelta(hours=2))
    assert not reopened.claim_postclose_research(now=NOW+timedelta(hours=3))
    monday = NOW+timedelta(days=3)
    assert reopened.claim_postclose_research(now=monday)
    reopened.finish_postclose_research(now=monday)
    assert not reopened.claim_postclose_research(now=monday+timedelta(hours=2))


def test_service_normalization_preserves_raw_hash_and_rehashes_verified_transformation(monkeypatch):
    from stock_ai import autonomous_trading_service as module
    original = {'rows': [{'date': '2026-09-10', 'open': 1, 'high': 2, 'low': 1, 'close': 2, 'volume': 1000}], 'data_evidence': {}}
    original['data_evidence']['data_sha256'] = content_hash(original['rows'])
    calls = []
    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(original)
    monkeypatch.setattr(module, 'load_official_candles', load)
    result = asyncio.run(module._history('2330.TW', NOW))
    assert calls[0][1]['start'] == '2022-09-01'
    assert calls[0][1]['end'] == '2026-09-11'
    assert calls[0][1]['max_months'] == 50
    assert result['data_evidence']['raw_source_data_sha256'] == original['data_evidence']['data_sha256']
    assert result['data_evidence']['data_sha256'] == content_hash(result['rows'])
    original['data_evidence']['data_sha256'] = 'tampered'
    with pytest.raises(ValueError, match='hash_mismatch'):
        asyncio.run(module._history('2330.TW', NOW))


def test_loop_cancellation_during_poll_sleep_cancels_research_child(monkeypatch):
    from stock_ai import autonomous_trading_service as module
    async def scenario():
        sleeping, researched, child_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def manage():
            return {'errors': []}
        fake = SimpleNamespace(manage=manage, claim_postclose_research=lambda: True)
        async def research(_):
            researched.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_cancelled.set()
        async def sleep(_):
            sleeping.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(module, '_SERVICE', fake)
        monkeypatch.setattr(module, '_autonomous_postclose_research', research)
        monkeypatch.setattr(module.asyncio, 'sleep', sleep)
        task = asyncio.create_task(module.autonomous_trading_loop())
        await sleeping.wait()
        await researched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child_cancelled.is_set()
    asyncio.run(scenario())


def test_quote_transport_failure_still_cancels_disabled_pending_buy(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        service.broker.fill_fraction = .5
        quote['now'] = due
        await service.manage(now=due)
        service.configure(enabled=False)
        async def unavailable(_):
            raise ConnectionError('explicit offline outage fixture')
        service.quote_loader = unavailable
        result = await service.manage(now=due+timedelta(seconds=1))
        assert result['errors'][0]['stage'] == 'quote'
        assert result['results'][0]['state']['wait_reason'] == 'quote_ineligible'
        assert not service.broker.orders[service.broker.submissions[0]['order_id']]['is_open']
        assert len(service.broker.submissions) == 1
    asyncio.run(scenario())


def test_hung_quote_still_reconciles_and_cancels_disabled_partial_entry(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    service.quote_timeout_seconds = .02

    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        service.broker.fill_fraction = .5
        quote['now'] = due
        await service.manage(now=due)
        service.configure(enabled=False)
        cancelled = asyncio.Event()

        async def hung(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        service.quote_loader = hung
        result = await asyncio.wait_for(service.manage(now=due+timedelta(seconds=1)), timeout=2)
        assert cancelled.is_set()
        assert result['errors'][0]['stage'] == 'quote'
        assert result['errors'][0]['timeout_seconds'] == .02
        assert result['results'][0]['state']['remaining_quantity'] > 0
        assert result['results'][0]['state']['wait_reason'] == 'quote_ineligible'
        assert not service.broker.orders[service.broker.submissions[0]['order_id']]['is_open']
        assert len(service.broker.submissions) == 1

    asyncio.run(scenario())


def test_management_reconciles_existing_orders_before_older_new_entry(tmp_path):
    service, quote = setup(tmp_path)

    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=2)
        records = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans']
        assert len(records) == 2
        waiting, submitted = records
        due = datetime.fromisoformat(submitted['definition']['not_before']).astimezone(timezone.utc)
        service.configure(enabled=True)
        quote['now'] = due
        await service.manage(now=due, entry_symbols=frozenset({submitted['symbol']}))
        seen = []
        original_quote = service.quote_loader

        async def observed_quote(symbol):
            seen.append(symbol)
            return await original_quote(symbol)

        service.quote_loader = observed_quote
        quote['now'] += timedelta(seconds=1)
        await service.manage(now=quote['now'])
        assert seen == [submitted['symbol'], waiting['symbol']]

    asyncio.run(scenario())


def test_management_cancellation_propagates_without_becoming_quote_failure(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))

    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def hung(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        service.quote_loader = hung
        task = asyncio.create_task(service.manage(now=due))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert service.broker.submissions == []

    asyncio.run(scenario())


def test_live_management_refreshes_clock_after_slow_quote_before_next_plan(tmp_path, monkeypatch):
    from open_stock_ai.execution import autonomous_campaign as module
    service, quote = setup(tmp_path)

    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=2)
        records = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans']
        due = datetime.fromisoformat(records[0]['definition']['not_before']).astimezone(timezone.utc)
        service.configure(enabled=True)
        clock = {'now': due}

        class HostClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock['now'] if tz else clock['now'].replace(tzinfo=None)

        monkeypatch.setattr(module, 'datetime', HostClock)
        original_quote = service.quote_loader
        observed_symbols = []

        async def delayed(symbol):
            observed_symbols.append(symbol)
            if len(observed_symbols) == 1:
                clock['now'] += timedelta(seconds=6)
                raise TimeoutError('offline delayed first source')
            quote['now'] = clock['now']
            return await original_quote(symbol)

        service.quote_loader = delayed
        result = await service.manage()
        assert len(result['errors']) == 1 and result['errors'][0]['stage'] == 'quote'
        assert len(observed_symbols) == 2
        entered_record = next(record for record in records if record['symbol'] == observed_symbols[1])
        entered = service.plans.get(entered_record['plan_id'])
        assert entered['state']['status'] == 'entry_submitted', entered['state']
        assert entered['state']['entry_receipt']['filled_quantity'] > 0
        assert entered['state']['entry_dispatched_at'] == clock['now'].isoformat()

    asyncio.run(scenario())


def test_daily_frozen_strategy_exit_is_persisted_and_unchanged_bar_never_refetches(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    history_calls = []
    next_history = history_fixture('2330.TW', now=NOW+timedelta(days=3))
    next_history['rows'][-1].update(open=100, high=101, close=99, low=98)
    next_history['data_evidence']['data_sha256'] = content_hash(next_history['rows'])
    next_history['data_evidence']['normalized_data_sha256'] = content_hash(next_history['rows'])
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        quote['now'] = due
        await service.manage(now=due)
        async def fresh(symbol, now):
            history_calls.append(symbol)
            return deepcopy(next_history)
        service.history_loader = fresh
        review_time = NOW+timedelta(days=3)
        review = await service.review_positions(now=review_time)
        assert review['errors'] == [] and review['reviewed'][0]['reason'] == 'close_below_slow_mean'
        request = service.plans.exit_request(record['plan_id'], strategy_version=record['definition']['strategy_version'])
        assert request['evidence_id'].startswith('AE-')
        assert request['observed_bar_at'] == next_history['rows'][-1]['timestamp']
        repeat = await service.review_positions(now=review_time+timedelta(seconds=1))
        assert not repeat['reviewed'] and history_calls == ['2330.TW']
        # Even a later research pass reuses the retained completed bars.
        await service.research(now=review_time+timedelta(seconds=2), deep_limit=1)
        assert history_calls == ['2330.TW']
        quote.update(now=review_time+timedelta(seconds=3), price=105)
        result = await service.manage(now=quote['now'])
        assert result['errors'] == []
        assert service.plans.get(record['plan_id'])['state']['exit_reason'] == 'close_below_slow_mean'
        assert service.broker.submissions[-1]['side'] == 'sell'
    asyncio.run(scenario())


def test_current_short_cache_is_upgraded_once_to_configured_history_window(tmp_path):
    initial = history_fixture('2330.TW')
    initial['data_evidence'].update(requested_start='2026-03-01', requested_end='2026-09-11')
    calls = []

    def source(symbol, _):
        calls.append(symbol)
        result = deepcopy(initial)
        if len(calls) > 1:
            result['data_evidence']['requested_start'] = '2022-09-01'
        return result

    service, _ = setup(tmp_path, symbols=('2330.TW',), history=source, history_lookback_days=4*365)

    async def scenario():
        await service.research(now=NOW, deep_limit=1)
        await service.research(now=NOW+timedelta(seconds=1), deep_limit=1)
        await service.research(now=NOW+timedelta(seconds=2), deep_limit=1)

    asyncio.run(scenario())
    assert calls == ['2330.TW', '2330.TW']


def test_changed_strategy_version_never_reinterprets_old_position(tmp_path, monkeypatch):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        quote['now'] = due
        await service.manage(now=due)
        changed = SimpleNamespace(candidate_id=record['definition']['strategy_id'], strategy_version_hash=lambda: 'new-version')
        monkeypatch.setattr('open_stock_ai.execution.autonomous_campaign.frozen_candidates', lambda: (changed,))
        async def forbidden(*_):
            raise AssertionError('changed strategy must not fetch or reinterpret old plan')
        service.history_loader = forbidden
        result = await service.review_positions(now=NOW+timedelta(days=3))
        assert result['errors'] == [] and not result['reviewed']
        assert result['skipped'][0]['reason'] == 'strategy_version_requires_revalidation'
        assert service.plans.exit_request(record['plan_id'], strategy_version=record['definition']['strategy_version']) is None
    asyncio.run(scenario())


def test_daily_hold_review_does_not_create_exit_request(tmp_path):
    service, quote = setup(tmp_path, symbols=('2330.TW',))
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        record = (await service.create_plans(cycle_id=cycle['cycle_id'], now=NOW))['plans'][0]
        service.configure(enabled=True)
        due = datetime.fromisoformat(record['definition']['not_before']).astimezone(timezone.utc)
        quote['now'] = due
        await service.manage(now=due)
        async def fresh(symbol, _):
            return history_fixture(symbol, now=NOW+timedelta(days=3))
        service.history_loader = fresh
        result = await service.review_positions(now=NOW+timedelta(days=3))
        assert result['errors'] == [] and result['reviewed'][0]['action'] == 'hold'
        assert service.plans.exit_request(record['plan_id'], strategy_version=record['definition']['strategy_version']) is None
    asyncio.run(scenario())
