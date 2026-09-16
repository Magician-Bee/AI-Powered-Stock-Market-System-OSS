from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.trading_plan import ExitOrderPolicy, TradingPlan, content_hash
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from product_admission_fixtures import admitted_product, product_resolver


NOW = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)


def plan(**kwargs):
    kwargs["metadata"] = {"product_admission": admitted_product(kwargs.get("symbol", "2330.TW"), now=NOW),
                          **kwargs.get("metadata", {})}
    return TradingPlan(**{
        "symbol": "2330.TW", "strategy_id": "candle", "strategy_version": "a" * 64,
        "evidence_ids": ("history-1",), "reference_price": 100, "position_size_pct": 1,
        "stop_loss": 95, "target_price": 110, "quantity_shares": 10, "cash_budget": 1050,
        **kwargs,
    })


def market(price=100, now=NOW, **kwargs):
    return {
        "symbol": "2330.TW", "price": price, "source_timestamp": now.isoformat(),
        "source_envelope": quote_envelope(
            provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
            quote_kind="last_trade", exchange_timestamp=now.isoformat(), received_at=now.isoformat(),
            max_age_seconds=60, authorized=True, realtime=True, delayed=False, official_close=False,
            trading_state="trading",
        ), **kwargs,
    }


def evidence(_):
    return {
        "required_evidence": ["price_history", "cost_model"],
        "receipts": {"price_history": order_risk_evidence_receipt(
            kind="price_history", payload={"symbol": "2330.TW", "completed_bars": 100},
            source="explicit_test_fixture", passed=True,
        )},
        "experiment_limits": {"max_order_notional_pct": 5, "max_total_exposure_pct": 20},
    }


class BrokerFixture:
    """Deterministic broker failures/fills, never offered as market evidence."""
    account_id, mode = "plan-test", "paper"

    def __init__(self):
        self.orders, self.submissions, self.observations = {}, [], []
        self.position, self.cash = 0, 100_000
        self.fill_fraction, self.raise_after_accept = 1, False
        self.cost = 1

    async def account(self, *, now):
        return {"total_equity": self.cash + self.position * 100, "available_cash": self.cash,
                "cash_balance": self.cash, "today_pnl": 0, "peak_equity": 100_000,
                "positions": [{"symbol": "2330.TW", "quantity": self.position, "market_value": self.position * 100}] if self.position else []}

    async def observe(self, market):
        self.observations.append(market)

    async def order(self, order_id):
        return self.orders.get(order_id)

    async def preview(self, intent, market):
        total = intent["quantity_shares"] * market["price"] + (self.cost if intent["side"] == "buy" else -self.cost)
        return {"can_submit": True, "estimated_costs": {"estimated_total": total},
                "cost_evidence": {"execution_evidence_eligible": False}}

    async def submit(self, intent, market):
        self.submissions.append(intent.copy())
        qty = int(intent["quantity_shares"] * self.fill_fraction)
        self.position += qty if intent["side"] == "buy" else -qty
        self.cash += (-qty * market["price"] if intent["side"] == "buy" else qty * market["price"]) - self.cost
        result = {"order_id": intent["order_id"], "status": "filled" if self.fill_fraction == 1 else "partially_filled",
                  "filled_quantity": qty, "remaining_quantity": intent["quantity_shares"]-qty,
                  "is_open": self.fill_fraction < 1}
        self.orders[intent["order_id"]] = result
        if self.raise_after_accept:
            raise TimeoutError("accepted_but_response_lost")
        return result

    async def cancel(self, order_id, *, reason):
        self.orders[order_id] = {**self.orders[order_id], "status": "canceled", "is_open": False}
        return self.orders[order_id]


def setup(tmp_path, definition=None):
    store = TradingPlanStore(SQLiteStore(tmp_path / "plans.db"))
    broker = BrokerFixture()
    record = store.create(account_id=broker.account_id, plan=definition or plan(), idempotency_key="decision-1", now=NOW)
    executor = TradingPlanExecutor(store=store, broker=broker, risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    return store, broker, record["plan_id"], executor


def tick(executor, plan_id, *, price=100, now=NOW, quote=None):
    return asyncio.run(executor.tick(plan_id, market=quote or market(price, now), now=now))


def test_one_flow_waits_enters_recovers_exits_and_reconciles(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(not_before=(NOW+timedelta(days=2)).isoformat(), entry_condition="price_at_or_above", trigger_price=101))
    assert tick(executor, pid)["state"]["wait_reason"] == "waiting_time"
    due = NOW + timedelta(days=2)
    assert tick(executor, pid, now=due)["state"]["wait_reason"] == "waiting_price"
    accepted = tick(executor, pid, price=101, now=due)
    assert accepted["state"]["status"] == "entry_submitted", accepted["state"]
    assert broker.submissions[0]["quantity_shares"] == 10
    # Reconstruct the service from its SQLite journal, no model call required.
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker, risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    assert tick(restarted, pid, price=102, now=due+timedelta(seconds=1))["state"]["status"] == "open"
    assert len(broker.submissions) == 1
    assert tick(restarted, pid, price=110, now=due+timedelta(hours=1))["state"]["status"] == "exit_submitted"
    closed = tick(restarted, pid, price=110, now=due+timedelta(hours=1, seconds=1))
    assert closed["state"]["status"] == "closed"
    assert broker.position == 0 and broker.cash == 100_088
    assert len(broker.submissions) == 2
    assert [x["event_type"] for x in store.events(pid)].count("plan.intent_persisted") == 2


def test_response_lost_after_broker_acceptance_reconciles_without_resubmit(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.raise_after_accept = True
    with pytest.raises(TimeoutError):
        tick(executor, pid)
    assert store.get(pid)["state"]["status"] == "entry_dispatching"
    broker.raise_after_accept = False
    assert tick(executor, pid)["state"]["status"] == "open"
    assert len(broker.submissions) == 1


@pytest.mark.parametrize("condition,trigger,stop,target", [
    ("price_at_or_above", 110, 105, 120),
    ("price_at_or_below", 90, 85, 95),
])
def test_future_entry_bracket_survives_wait_restart_and_protects_actual_position(
    tmp_path, condition, trigger, stop, target,
):
    due = NOW + timedelta(days=3)
    definition = plan(reference_price=trigger, stop_loss=stop, target_price=target,
                      cash_budget=trigger * 10 + 25, entry_condition=condition, trigger_price=trigger,
                      not_before=due.isoformat(), expires_at=(due + timedelta(hours=2)).isoformat())
    store, broker, pid, executor = setup(tmp_path, definition)
    assert tick(executor, pid, price=100)["state"]["wait_reason"] == "waiting_time"
    assert tick(executor, pid, price=100, now=due)["state"]["wait_reason"] == "waiting_price"
    assert broker.submissions == []
    resumed = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                  risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    assert tick(resumed, pid, price=trigger, now=due)["state"]["status"] == "entry_submitted"
    assert tick(resumed, pid, price=trigger, now=due + timedelta(seconds=1))["state"]["status"] == "open"
    assert len(broker.submissions) == 1 and broker.position == 10
    assert tick(resumed, pid, price=stop, now=due + timedelta(seconds=2))["state"]["status"] == "exit_submitted"
    assert tick(resumed, pid, price=stop, now=due + timedelta(seconds=3))["state"]["status"] == "closed"
    assert broker.position == 0
    assert [item["side"] for item in broker.submissions] == ["buy", "sell"]


def test_missing_uncertain_receipt_waits_and_does_not_retry(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.raise_after_accept = True
    with pytest.raises(TimeoutError):
        tick(executor, pid)
    broker.orders.clear()
    for _ in range(2):
        assert tick(executor, pid)["state"]["status"] == "reconciliation_required"
    assert len(broker.submissions) == 1


def test_stop_cancels_partial_entry_before_selling_only_filled_quantity(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = 0.5
    tick(executor, pid)
    broker.fill_fraction = 1
    result = tick(executor, pid, price=94, now=NOW+timedelta(seconds=1))
    assert result["state"]["status"] == "exit_submitted", result["state"]
    assert broker.orders[broker.submissions[0]["order_id"]]["status"] == "canceled"
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert len(broker.observations) == 1  # invalidated entry got no extra matching tick
    assert tick(executor, pid, price=94, now=NOW+timedelta(seconds=2))["state"]["status"] == "closed"


def test_partial_exit_then_expiry_retries_only_residual(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    broker.fill_fraction = 0.5
    tick(executor, pid, price=110, now=NOW+timedelta(seconds=1))
    first_exit = broker.submissions[-1]["order_id"]
    broker.orders[first_exit].update(status="expired", is_open=False)
    broker.fill_fraction = 1
    result = tick(executor, pid, price=109, now=NOW+timedelta(seconds=2))
    assert result["state"]["status"] == "exit_submitted", result["state"]
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert broker.submissions[-1]["order_id"] != first_exit
    result = tick(executor, pid, price=109, now=NOW+timedelta(seconds=3))
    assert result["state"]["status"] == "closed" and broker.position == 0


def test_stale_quote_blocks_execution_but_time_expiry_still_finishes(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(expires_at=(NOW+timedelta(minutes=2)).isoformat()))
    stale = market(now=NOW-timedelta(minutes=5))
    assert tick(executor, pid, quote=stale)["state"]["wait_reason"] == "quote_ineligible"
    assert tick(executor, pid, now=NOW+timedelta(minutes=3), quote=stale)["state"]["status"] == "expired"
    assert not broker.submissions


def test_quantity_never_grows_and_price_gap_cannot_exceed_frozen_budget(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    result = tick(executor, pid, price=106)
    assert result["state"]["wait_reason"] == "broker_preview_or_frozen_budget"
    assert not broker.submissions


def test_idempotency_conflicts_and_account_leases_are_durable(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    assert store.create(account_id=broker.account_id, plan=plan(), idempotency_key="decision-1")["plan_id"] == pid
    with pytest.raises(ValueError, match="idempotency_conflict"):
        store.create(account_id=broker.account_id, plan=replace(plan(), quantity_shares=9), idempotency_key="decision-1")
    with store.account_lease(broker.account_id, now=NOW):
        assert tick(executor, pid)["status"] == "account_busy"
    assert not broker.submissions


def test_same_tick_does_not_reconsume_liquidity_and_holding_deadline_exits(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(max_holding_seconds=60))
    tick(executor, pid)
    tick(executor, pid)
    assert len(broker.observations) == 1
    result = tick(executor, pid, now=NOW+timedelta(seconds=61))
    assert result["state"]["exit_reason"] == "holding_period_expired"
    assert broker.submissions[-1]["side"] == "sell"


def test_expired_partial_entry_cancels_during_quote_outage_without_selling_stale(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(expires_at=(NOW+timedelta(seconds=30)).isoformat()))
    broker.fill_fraction = .5
    tick(executor, pid)
    result = tick(executor, pid, now=NOW+timedelta(seconds=61), quote=market())
    assert broker.orders[broker.submissions[0]['order_id']]['is_open'] is False
    assert result['state']['remaining_quantity'] == 5
    assert result['state']['wait_reason'] == 'quote_ineligible'
    assert len(broker.submissions) == 1
    broker.fill_fraction = 1
    result = tick(executor, pid, price=94, now=NOW+timedelta(seconds=62))
    assert result['state']['status'] == 'exit_submitted'
    assert broker.submissions[-1]['quantity_shares'] == 5


def test_stale_price_does_not_cancel_valid_entry_as_a_stop(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    tick(executor, pid)
    result = tick(executor, pid, now=NOW+timedelta(seconds=61), quote=market(90))
    assert result['state']['wait_reason'] == 'quote_ineligible'
    assert broker.orders[broker.submissions[0]['order_id']]['is_open']
    assert len(broker.submissions) == 1


def test_pending_cancel_receipt_prevents_exit_until_final_fills_are_known(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    tick(executor, pid)
    original_cancel = broker.cancel
    async def pending(order_id, *, reason):
        return broker.orders[order_id]
    broker.cancel = pending
    result = tick(executor, pid, price=94, now=NOW+timedelta(seconds=1))
    assert result['state']['wait_reason'] == 'awaiting_entry_cancellation'
    assert len(broker.submissions) == 1
    broker.cancel = original_cancel
    broker.fill_fraction = 1
    assert tick(executor, pid, price=94, now=NOW+timedelta(seconds=2))['state']['status'] == 'exit_submitted'
    assert broker.submissions[-1]['quantity_shares'] == 5


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('order_id', 'some-other-order', 'broker_receipt_id_mismatch'),
    ('account_id', 'other-account', 'broker_receipt_account_mismatch'),
    ('symbol', '2317.TW', 'broker_receipt_symbol_mismatch'),
    ('filled_quantity', float('nan'), 'invalid_broker_cumulative_fills'),
    ('filled_quantity', 11, 'invalid_broker_cumulative_fills'),
    ('filled_quantity', 9.5, 'invalid_broker_cumulative_fills'),
])
def test_inconsistent_broker_receipt_requires_reconciliation_without_new_orders(tmp_path, field, value, reason):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    broker.orders[broker.submissions[0]['order_id']][field] = value
    result = tick(executor, pid, price=94, now=NOW+timedelta(seconds=1))
    assert result['state']['status'] == 'reconciliation_required'
    assert result['state']['wait_reason'] == reason
    assert len(broker.submissions) == 1


def test_receipt_disappearing_after_market_observation_cannot_create_replacement_buy(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    async def observe(_):
        broker.orders.clear()
    broker.observe = observe
    result = tick(executor, pid, price=102, now=NOW+timedelta(seconds=1))
    assert result['state']['status'] == 'reconciliation_required'
    assert len(broker.submissions) == 1


def test_restarted_lost_response_does_not_reset_holding_deadline(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(max_holding_seconds=60))
    broker.raise_after_accept = True
    with pytest.raises(TimeoutError):
        tick(executor, pid)
    broker.raise_after_accept = False
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker, risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    result = tick(restarted, pid, now=NOW+timedelta(seconds=61))
    assert result['state']['entered_at'] == NOW.isoformat()
    assert result['state']['exit_reason'] == 'holding_period_expired'
    assert broker.submissions[-1]['side'] == 'sell'


def test_refetched_quote_cannot_reconsume_liquidity_but_new_exchange_observation_can(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    tick(executor, pid)
    refreshed = market(now=NOW+timedelta(seconds=1))
    refreshed['source_envelope'] = market()['source_envelope']
    tick(executor, pid, now=NOW+timedelta(seconds=1), quote=refreshed)
    assert len(broker.observations) == 1
    new_observation = market(now=NOW+timedelta(seconds=2))
    new_observation['source_timestamp'] = NOW.isoformat()
    tick(executor, pid, now=NOW+timedelta(seconds=2), quote=new_observation)
    assert len(broker.observations) == 2


def test_partial_exit_retry_waits_for_a_new_quote_nonce(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    broker.fill_fraction = .5
    exit_quote = market(110, NOW+timedelta(seconds=1))
    tick(executor, pid, now=NOW+timedelta(seconds=1), quote=exit_quote)
    first_exit = broker.submissions[-1]['order_id']
    broker.orders[first_exit].update(status='expired', is_open=False)
    broker.fill_fraction = 1
    repeated = tick(executor, pid, now=NOW+timedelta(seconds=2), quote=exit_quote)
    assert repeated['state']['wait_reason'] == 'awaiting_new_market_observation'
    assert len(broker.submissions) == 2
    result = tick(executor, pid, price=110, now=NOW+timedelta(seconds=3))
    assert result['state']['status'] == 'exit_submitted'
    assert broker.submissions[-1]['quantity_shares'] == 5
    assert broker.submissions[-1]['order_id'] != first_exit


def test_taiwan_partial_entry_residual_exits_as_valid_board_then_odd_lot_children(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(reference_price=1, stop_loss=.95, target_price=1.1, quantity_shares=2000, cash_budget=2050))
    broker.fill_fraction = .75
    tick(executor, pid, price=1)
    broker.fill_fraction = 1
    first = tick(executor, pid, price=.94, now=NOW+timedelta(seconds=1))
    assert first['state']['status'] == 'exit_submitted', first['state']
    assert broker.submissions[-1]['quantity_shares'] == 1000
    second = tick(executor, pid, price=.94, now=NOW+timedelta(seconds=2))
    assert second['state']['status'] == 'exit_submitted', second['state']
    assert broker.submissions[-1]['quantity_shares'] == 500
    assert len({item['order_id'] for item in broker.submissions}) == 3
    assert tick(executor, pid, price=.94, now=NOW+timedelta(seconds=3))['state']['status'] == 'closed'
    assert broker.position == 0


def test_unknown_acceptance_blocks_other_new_entries_only_in_same_account(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    broker.raise_after_accept = True
    with pytest.raises(TimeoutError):
        tick(executor, pid)
    broker.orders.clear()
    other = store.create(account_id=broker.account_id, plan=plan(symbol='2317.TW'), idempotency_key='second-symbol', now=NOW)
    result = tick(executor, other['plan_id'], quote={**market(), 'symbol': '2317.TW'})
    assert result['state']['wait_reason'] == 'account_reconciliation_required'
    assert len(broker.submissions) == 1
    assert store.unresolved_dispatches(account_id='other-account', excluding_plan_id='none') == []


def test_unknown_other_dispatch_does_not_block_reduce_only_exit(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    other = store.create(account_id=broker.account_id, plan=plan(symbol='2317.TW'), idempotency_key='second-symbol', now=NOW)
    with store.lease(other['plan_id'], now=NOW) as owner:
        store.save_state(other['plan_id'], state={**other['state'], 'status': 'entry_dispatching', 'entry_order_id': 'uncertain'}, revision=other['revision'], event_type='test.uncertain', now=NOW, lease_owner=owner)
    assert tick(executor, pid, price=94, now=NOW+timedelta(seconds=1))['state']['status'] == 'exit_submitted'
    assert broker.submissions[-1]['reduce_only'] is True


def test_absolute_exit_deadline_is_durable_and_not_a_model_metadata_request(tmp_path):
    deadline = NOW+timedelta(seconds=61)
    definition = plan(exit_not_after=deadline.isoformat(), metadata={'exit_reason': 'model_wants_to_sell_now'})
    store, broker, pid, executor = setup(tmp_path, definition)
    tick(executor, pid)
    assert tick(executor, pid, now=NOW+timedelta(seconds=1))['state']['status'] == 'open'
    result = tick(executor, pid, now=deadline)
    assert result['state']['exit_reason'] == 'absolute_exit_deadline'
    assert broker.submissions[-1]['side'] == 'sell'
    assert definition.entry_status(price=100, now=deadline) == 'expired'
    with pytest.raises(ValueError, match='timezone'):
        plan(exit_not_after='2026-09-30T13:25:00')


def test_expired_lease_cannot_save_and_manual_mixed_lot_or_nonfinite_deadline_rejected(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    record = store.get(pid)
    with store.lease(pid, now=NOW, seconds=1) as owner:
        with pytest.raises(RuntimeError, match='concurrent_update'):
            store.save_state(pid, state=record['state'], revision=record['revision'], event_type='test.stale', now=NOW+timedelta(seconds=2), lease_owner=owner)
    with pytest.raises(ValueError, match='single_venue_lot'):
        plan(quantity_shares=1500, cash_budget=150100)
    with pytest.raises(ValueError, match='holding_period'):
        plan(max_holding_seconds=float('nan'))


@pytest.mark.parametrize("response_lost", [False, True])
def test_stop_survives_pending_entry_cancel_restart_and_price_rebound(tmp_path, response_lost):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    tick(executor, pid)
    original_cancel = broker.cancel

    async def interrupted_cancel(order_id, *, reason):
        if response_lost:
            await original_cancel(order_id, reason=reason)
            raise TimeoutError("cancel_accepted_response_lost")
        return broker.orders[order_id]

    broker.cancel = interrupted_cancel
    if response_lost:
        with pytest.raises(TimeoutError):
            tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    else:
        pending = tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
        assert pending["state"]["wait_reason"] == "awaiting_entry_cancellation"
    persisted = store.get(pid)["state"]
    assert persisted["exit_reason"] == "stop_loss"
    assert persisted["remaining_quantity"] == 5
    broker.cancel, broker.fill_fraction = original_cancel, 1
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    result = tick(restarted, pid, price=100, now=NOW + timedelta(seconds=2))
    assert result["state"]["status"] == "exit_submitted"
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert len(broker.submissions) == 2


def test_legacy_exit_timeout_reports_remaining_risk_without_authorizing_a_new_price(tmp_path):
    store, broker, pid, executor = setup(tmp_path)
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    result = tick(restarted, pid, price=90, now=NOW + timedelta(seconds=61))
    assert result["state"]["wait_reason"] == "exit_fill_timeout"
    assert result["state"]["exit_alert"]["remaining_quantity"] == 5
    assert len(broker.submissions) == 2
    first_exit = broker.submissions[-1]["order_id"]
    assert broker.orders[first_exit]["is_open"]
    broker.orders[first_exit].update(status="expired", is_open=False)
    tick(restarted, pid, price=89, now=NOW + timedelta(seconds=62))
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert broker.submissions[-1]["limit_price"] == 94


def test_exit_cancel_confirmation_and_late_partial_fill_survive_restart(tmp_path):
    definition = plan(exit_order_policy=ExitOrderPolicy(60, 2, 90))
    store, broker, pid, executor = setup(tmp_path, definition)
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    first_exit = broker.submissions[-1]["order_id"]
    original_cancel, cancellations = broker.cancel, []

    async def pending_cancel(order_id, *, reason):
        cancellations.append(order_id)
        return broker.orders[order_id]

    broker.cancel = pending_cancel
    pending = tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
    assert pending["state"]["wait_reason"] == "awaiting_exit_cancellation"
    assert len(broker.submissions) == 2
    # Two more shares fill before the cancellation becomes terminal.
    broker.orders[first_exit].update(filled_quantity=7, remaining_quantity=3)
    broker.position -= 2
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    pending = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=62))
    assert pending["state"]["remaining_quantity"] == 3
    assert pending["state"]["exit_alert"]["remaining_quantity"] == 3
    assert cancellations == [first_exit]
    assert len(broker.submissions) == 2
    asyncio.run(original_cancel(first_exit, reason="confirmed"))
    broker.fill_fraction = 1
    result = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=63))
    assert result["state"]["exit_replacement_count"] == 1
    assert broker.submissions[-1]["quantity_shares"] == 3
    assert broker.submissions[-1]["limit_price"] == 91
    assert broker.submissions[-1]["order_id"] != first_exit
    assert tick(restarted, pid, price=91, now=NOW + timedelta(seconds=64))["state"]["status"] == "closed"
    assert broker.position == 0


@pytest.mark.parametrize("accepted", [False, True])
def test_exit_cancel_lost_receipt_never_duplicates_an_open_order(tmp_path, accepted):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 2, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    original_cancel = broker.cancel

    async def lost_receipt(order_id, *, reason):
        if accepted:
            await original_cancel(order_id, reason=reason)
            raise TimeoutError("cancel_receipt_lost")
        return None

    broker.cancel = lost_receipt
    if accepted:
        with pytest.raises(TimeoutError):
            tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
    else:
        result = tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
        assert result["state"]["status"] == "reconciliation_required"
    assert store.get(pid)["state"]["exit_cancel_intent"]
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    broker.fill_fraction = 1
    result = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=62))
    assert len(broker.submissions) == (3 if accepted else 2)
    if accepted:
        assert broker.submissions[-1]["quantity_shares"] == 5
    else:
        assert result["state"]["wait_reason"] == "awaiting_exit_cancellation"


@pytest.mark.parametrize("failure", ["crash_before_dispatch", "transport_not_delivered"])
def test_undelivered_exit_cancel_recovers_same_order_after_durable_wait(tmp_path, monkeypatch, failure):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 1, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    first_exit = broker.submissions[-1]["order_id"]
    original_cancel, save_state, cancellation_calls = broker.cancel, store.save_state, []
    delivery_available = False

    async def cancel(order_id, *, reason):
        cancellation_calls.append(order_id)
        if not delivery_available:
            raise TimeoutError("cancel_transport_not_delivered")
        return await original_cancel(order_id, reason=reason)

    def save_then_crash(*args, **kwargs):
        saved = save_state(*args, **kwargs)
        if kwargs["event_type"] == "plan.exit_cancel_intent_persisted":
            raise RuntimeError("crash_before_cancel_dispatch")
        return saved

    broker.cancel = cancel
    if failure == "crash_before_dispatch":
        monkeypatch.setattr(store, "save_state", save_then_crash)
        with pytest.raises(RuntimeError, match="crash_before_cancel_dispatch"):
            tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
        assert cancellation_calls == []
        monkeypatch.setattr(store, "save_state", save_state)
    else:
        with pytest.raises(TimeoutError, match="cancel_transport_not_delivered"):
            tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
        assert cancellation_calls == [first_exit]
    cancellation = store.get(pid)["state"]["exit_cancel_intent"]
    assert cancellation["attempt_count"] == 1
    assert cancellation["last_attempt_at"] == (NOW + timedelta(seconds=61)).isoformat()
    assert broker.orders[first_exit]["is_open"]
    delivery_available, broker.fill_fraction = True, 1
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    previous_calls = len(cancellation_calls)
    pending = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=120))
    assert pending["state"]["wait_reason"] == "awaiting_exit_cancellation"
    assert len(cancellation_calls) == previous_calls
    assert len(broker.submissions) == 2
    recovered = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=121))
    assert len(cancellation_calls) == previous_calls + 1
    assert set(cancellation_calls) == {first_exit}
    assert broker.orders[first_exit]["is_open"] is False
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert recovered["state"]["exit_replacement_count"] == 1
    attempts = [event["payload"]["exit_cancel_intent"]["attempt_count"]
                for event in store.events(pid) if event["event_type"] == "plan.exit_cancel_intent_persisted"]
    assert attempts == [1, 2]
    assert tick(restarted, pid, price=91, now=NOW + timedelta(seconds=122))["state"]["status"] == "closed"
    assert broker.position == 0 and len(broker.submissions) == 3


def test_pending_exit_cancel_retries_are_spaced_bounded_and_reconciled_after_exhaustion(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 1, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    first_exit = broker.submissions[-1]["order_id"]
    original_cancel, cancellations = broker.cancel, []

    async def pending_cancel(order_id, *, reason):
        cancellations.append(order_id)
        return broker.orders[order_id]

    broker.cancel = pending_cancel
    for seconds, expected_calls in ((61, 1), (62, 1), (120, 1), (121, 2), (122, 2), (180, 2), (181, 3), (300, 3)):
        restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                        risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
        result = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=seconds))
        assert cancellations == [first_exit] * expected_calls
        assert result["state"]["exit_cancel_intent"]["attempt_count"] == expected_calls
        assert result["state"]["remaining_quantity"] == 5
        assert len(broker.submissions) == 2
        if expected_calls == 3:
            assert result["state"]["wait_reason"] == "exit_cancellation_limit_reached"
            assert result["state"]["exit_alert"]["cancel_attempt_count"] == 3
    asyncio.run(original_cancel(first_exit, reason="late_confirmation"))
    broker.fill_fraction = 1
    result = tick(restarted, pid, price=91, now=NOW + timedelta(seconds=301))
    assert result["state"]["exit_replacement_count"] == 1
    assert broker.submissions[-1]["quantity_shares"] == 5
    assert len(cancellations) == 3 and len(broker.submissions) == 3


def test_exit_cancel_delivery_retry_survives_quote_outage_without_submitting_stale_exit(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 1, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    original_cancel, cancellations = broker.cancel, []

    async def cancel(order_id, *, reason):
        cancellations.append(order_id)
        if len(cancellations) == 1:
            return broker.orders[order_id]
        return await original_cancel(order_id, reason=reason)

    broker.cancel = cancel
    tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
    result = tick(executor, pid, quote=market(91), now=NOW + timedelta(seconds=121))
    assert len(cancellations) == 2 and len(set(cancellations)) == 1
    assert result["state"]["wait_reason"] == "quote_ineligible"
    assert result["state"]["remaining_quantity"] == 5
    assert result["state"]["exit_order_id"] is None
    assert len(broker.submissions) == 2


def test_exit_full_fill_during_cancel_closes_without_replacement(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 2, 90)))
    tick(executor, pid)
    broker.fill_fraction = 0
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))

    async def filled_before_cancel(order_id, *, reason):
        broker.position = 0
        broker.orders[order_id].update(status="filled", filled_quantity=10, remaining_quantity=0, is_open=False)
        return broker.orders[order_id]

    broker.cancel = filled_before_cancel
    result = tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
    assert result["state"]["status"] == "closed"
    assert result["state"]["remaining_quantity"] == 0
    assert len(broker.submissions) == 2


def test_replacement_acceptance_lost_across_restart_keeps_one_order_and_budget(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 1, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    broker.fill_fraction, broker.raise_after_accept = 1, True
    with pytest.raises(TimeoutError, match="accepted_but_response_lost"):
        tick(executor, pid, price=92, now=NOW + timedelta(seconds=61))
    saved = store.get(pid)["state"]
    assert saved["status"] == "exit_dispatching"
    assert saved["exit_replacement_count"] == 1
    assert saved["sold_before_retry"] == 5
    broker.raise_after_accept = False
    restarted = TradingPlanExecutor(store=TradingPlanStore(store.store), broker=broker,
                                    risk=RiskEngine(), evidence_resolver=evidence, product_resolver=product_resolver)
    result = tick(restarted, pid, price=91, now=NOW + timedelta(days=1))
    assert result["state"]["status"] == "closed"
    assert result["state"]["exit_replacement_count"] == 1
    assert broker.position == 0
    assert len(broker.submissions) == 3


def test_quote_outage_during_exit_wait_preserves_order_and_reports_risk(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 2, 90)))
    tick(executor, pid)
    broker.fill_fraction = .5
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    result = tick(executor, pid, quote=market(90), now=NOW + timedelta(seconds=61))
    assert result["state"]["wait_reason"] == "exit_quote_ineligible"
    assert result["state"]["exit_alert"]["remaining_quantity"] == 5
    assert broker.orders[broker.submissions[-1]["order_id"]]["is_open"]
    assert len(broker.submissions) == 2


def test_exit_policy_floor_and_replacement_budget_bound_unfilled_risk(tmp_path):
    store, broker, pid, executor = setup(tmp_path, plan(exit_order_policy=ExitOrderPolicy(60, 1, 90)))
    tick(executor, pid)
    broker.fill_fraction = 0
    tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    result = tick(executor, pid, price=89, now=NOW + timedelta(seconds=61))
    assert result["state"]["wait_reason"] == "exit_price_floor_reached"
    assert result["state"]["remaining_quantity"] == 10
    assert len(broker.submissions) == 2
    result = tick(executor, pid, price=90, now=NOW + timedelta(seconds=62))
    assert result["state"]["exit_replacement_count"] == 1
    assert broker.submissions[-1]["limit_price"] == 90
    result = tick(executor, pid, price=90, now=NOW + timedelta(seconds=122))
    assert result["state"]["wait_reason"] == "exit_replacement_limit_reached"
    assert len(broker.submissions) == 3
    broker.orders[broker.submissions[-1]["order_id"]].update(status="expired", is_open=False)
    result = tick(executor, pid, price=91, now=NOW + timedelta(seconds=123))
    assert result["state"]["wait_reason"] == "exit_replacement_limit_reached"
    assert result["state"]["status"] == "open"
    assert result["state"]["exit_order_id"] is None
    assert len(broker.submissions) == 3


def test_exit_policy_loading_preserves_legacy_definition_hash_and_is_explicit(tmp_path):
    legacy = plan().to_dict()
    assert "exit_order_policy" not in legacy
    definition = TradingPlan.from_dict(legacy)
    store, broker, pid, executor = setup(tmp_path, definition)
    assert store.get(pid)["definition_hash"] == content_hash(legacy)
    tick(executor, pid)
    assert TradingPlan.from_dict(store.get(pid)["definition"]).to_dict() == legacy
    assert store.create(account_id=broker.account_id, plan=definition, idempotency_key="decision-1")["plan_id"] == pid
    policy = {"wait_seconds": 60, "max_replacements": 2, "minimum_limit_price": 90}
    configured = TradingPlan.from_dict({**legacy, "exit_order_policy": policy})
    assert configured.to_dict()["exit_order_policy"] == policy
    assert content_hash(configured.to_dict()) != content_hash(legacy)
    with pytest.raises(ValueError, match="idempotency_conflict"):
        store.create(account_id=broker.account_id, plan=configured, idempotency_key="decision-1")


@pytest.mark.parametrize("policy", [
    {"wait_seconds": True, "max_replacements": 1, "minimum_limit_price": 90},
    {"wait_seconds": 0, "max_replacements": 1, "minimum_limit_price": 90},
    {"wait_seconds": 60, "max_replacements": 0, "minimum_limit_price": 90},
    {"wait_seconds": 60, "max_replacements": 11, "minimum_limit_price": 90},
    {"wait_seconds": 60, "max_replacements": 1, "minimum_limit_price": float("nan")},
    {"wait_seconds": 60, "max_replacements": 1, "minimum_limit_price": True},
    {"wait_seconds": 60, "max_replacements": 1, "minimum_limit_price": "90"},
    {"wait_seconds": 60, "max_replacements": 1, "minimum_limit_price": None},
])
def test_exit_policy_rejects_invalid_bounds(policy):
    with pytest.raises(ValueError, match="invalid_exit_order"):
        plan(exit_order_policy=policy)


def test_exit_policy_passes_floor_to_entry_risk_without_moving_stop_trigger(tmp_path):
    definition = plan(exit_order_policy=ExitOrderPolicy(60, 2, 90))
    store, broker, pid, executor = setup(tmp_path, definition)
    entered = tick(executor, pid)
    assert entered["state"]["entry_intent"]["stop_loss"] == 95
    assert entered["state"]["entry_intent"]["exit_minimum_limit_price"] == 90
    result = tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    assert result["state"]["exit_reason"] == "stop_loss"
    assert broker.submissions[-1]["exit_minimum_limit_price"] == 90
    assert broker.submissions[-1]["stop_loss"] == 95
    with pytest.raises(ValueError, match="exit_order_price_floor_above_stop_loss"):
        plan(exit_order_policy=ExitOrderPolicy(60, 2, 96))
