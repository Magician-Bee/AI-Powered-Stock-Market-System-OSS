from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.data.execution_quote import execution_eligibility, quote_envelope
from stock_ai.agent_tools import StockAgentToolRegistry

NOW = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
ORDER = {'symbol': '2330.TW', 'side': 'buy', 'quantity_shares': 10}


def _market(*, received_at=NOW, exchange_timestamp=None):
    envelope = quote_envelope(
        provider_id='licensed_realtime', connector_id='stock_ai.realtime_quotes.fetch_licensed_quote',
        quote_kind='last_trade', exchange_timestamp=(exchange_timestamp or NOW).isoformat(),
        received_at=received_at.isoformat(), max_age_seconds=120, authorized=True,
        realtime=True, delayed=False, official_close=False, trading_state='trading',
    )
    return {
        'symbol': '2330.TW', 'market': 'TW', 'exchange': 'TWSE', 'price': 100.0,
        'source_kind': 'realtime_last_trade', 'source_timestamp': (exchange_timestamp or NOW).isoformat(),
        'source_envelope': envelope, 'execution_eligibility': execution_eligibility(envelope, horizon='intraday', now=received_at),
        'is_realtime': True, 'is_fallback': False, 'trading_state': 'trading',
        'limit_up': 110.0, 'limit_down': 90.0, 'buy_liquidity_confirmed': True,
        'sell_liquidity_confirmed': True, 'exchange_rules_enforced': True, 'settlement_rules_enforced': True,
    }


def _setup(monkeypatch):
    registry = StockAgentToolRegistry()
    context = AgentRunContext(run_id='preview-identity', autonomy='paper_execute', symbols=('2330.TW',), allow_paper_orders=True)
    preview = {'can_submit': True, 'market': _market()}
    submitted = []
    monkeypatch.setattr('stock_ai.agent_tools.paper_training_preview', lambda request: deepcopy(preview))
    monkeypatch.setattr('stock_ai.agent_tools.paper_training_order', lambda request: submitted.append(request) or {'status': 'filled', 'order_id': 'host-paper-order'})
    asyncio.run(registry.execute('paper.preview_order', dict(ORDER), context))
    return registry, context, preview, submitted


def test_same_trade_refetched_with_new_receive_time_can_submit(monkeypatch):
    registry, context, preview, submitted = _setup(monkeypatch)
    original_receipt = next(iter(context.state['paper_previews'].values())).copy()
    preview['market'] = _market(received_at=NOW + timedelta(seconds=1))
    assert preview['market']['source_envelope']['signature'] != original_receipt['source_signature']
    result = asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert result['order_id'] == 'host-paper-order'
    assert len(submitted) == 1 and submitted[0].quantity_shares == 10
    assert next(iter(context.state['paper_previews'].values()))['source_signature'] == original_receipt['source_signature']


@pytest.mark.parametrize(('field', 'value'), [
    ('price', 101.0), ('symbol', '2317.TW'), ('exchange', 'TPEX'),
    ('trading_state', 'halted'), ('limit_up', 111.0), ('buy_liquidity_confirmed', False),
    ('source_timestamp', (NOW + timedelta(seconds=1)).isoformat()),
])
def test_changed_execution_facts_require_new_exact_preview(monkeypatch, field, value):
    registry, context, preview, submitted = _setup(monkeypatch)
    preview['market'][field] = value
    with pytest.raises(PermissionError, match='verified market price changed'):
        asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert submitted == []
    asyncio.run(registry.execute('paper.preview_order', dict(ORDER), context))
    assert asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))['status'] == 'filled'


@pytest.mark.parametrize(('field', 'value'), [
    ('authorized', False), ('trading_state', 'halted'), ('provider_id', 'another-provider'),
    ('exchange_timestamp', (NOW + timedelta(seconds=1)).isoformat()),
])
def test_changed_source_contract_requires_new_preview(monkeypatch, field, value):
    registry, context, preview, submitted = _setup(monkeypatch)
    preview['market']['source_envelope'][field] = value
    with pytest.raises(PermissionError, match='verified market price changed'):
        asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert submitted == []


def test_refetch_does_not_make_expired_exchange_quote_eligible(monkeypatch):
    registry, context, preview, submitted = _setup(monkeypatch)
    preview['market'] = _market(received_at=NOW + timedelta(seconds=121))
    assert not preview['market']['execution_eligibility']['execution_eligible']
    preview['can_submit'] = False
    with pytest.raises(PermissionError):
        asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert submitted == []


def test_same_quote_still_rechecks_current_broker_validation(monkeypatch):
    registry, context, preview, submitted = _setup(monkeypatch)
    preview['market'] = _market(received_at=NOW + timedelta(seconds=1))
    preview['can_submit'] = False
    with pytest.raises(PermissionError, match='Paper Broker validation rejected'):
        asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert submitted == []


def test_order_mutation_and_legacy_receipts_remain_strict(monkeypatch):
    registry, context, preview, submitted = _setup(monkeypatch)
    with pytest.raises(PermissionError, match='exact paper order must be previewed'):
        asyncio.run(registry.execute('paper.submit_order', {**ORDER, 'quantity_shares': 11}, context))
    receipt = next(iter(context.state['paper_previews'].values()))
    receipt.pop('market_signature')
    preview['market'] = _market(received_at=NOW + timedelta(seconds=1))
    with pytest.raises(PermissionError, match='verified market price changed'):
        asyncio.run(registry.execute('paper.submit_order', dict(ORDER), context))
    assert submitted == []
