"""Executor checks against the real paper broker/OMS using isolated quotes and DBs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import ExitOrderPolicy, TradingPlan
from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor, _preview_cost_components
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from product_admission_fixtures import admitted_product, product_resolver


NOW = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)


def plan(**kwargs):
    return TradingPlan(symbol="2330.TW", strategy_id="exit-test", strategy_version="a" * 64,
        evidence_ids=("fixture-history",), reference_price=100, position_size_pct=1,
        stop_loss=kwargs.pop("stop_loss", 95), target_price=110, quantity_shares=10,
        cash_budget=1050, metadata={"product_admission": admitted_product("2330.TW", now=NOW)}, **kwargs)


class PaperHarness:
    def __init__(self, root, definition):
        self.now = NOW
        harness = self

        class ClockOMS(PaperOMS):
            def _now(self):
                return harness.now.isoformat()

        class ClockBroker(PaperBrokerSimulator):
            def _now(self):
                return harness.now.isoformat()

        store = SQLiteStore(root / "exit-policy.sqlite")
        self.oms = ClockOMS(store, account_id="isolated-exit-test", initial_cash=100_000,
            commission_bps=14.25, minimum_commission=20, sell_tax_bps=30, read_environment=False)
        self.broker = ClockBroker(store, self.oms)
        self.port, self.plans = PaperBrokerPort(self.broker), TradingPlanStore(store)
        self.pid = self.plans.create(account_id=self.port.account_id, plan=definition,
            idempotency_key="isolated-decision", now=self.now)["plan_id"]
        self.executor = TradingPlanExecutor(store=self.plans, broker=self.port, risk=RiskEngine(),
            evidence_resolver=lambda _: {
                "required_evidence": ["price_history", "cost_model"],
                "receipts": {"price_history": order_risk_evidence_receipt(kind="price_history",
                    payload={"symbol": "2330.TW", "completed_bars": 100}, source="explicit_test_fixture", passed=True)},
                "experiment_limits": {"max_order_notional_pct": 5, "max_total_exposure_pct": 20},
            }, product_resolver=product_resolver)

    def quote(self, price, **kwargs):
        return {"symbol": "2330.TW", "price": price, "market": "TW", "source_timestamp": self.now.isoformat(),
            "source_envelope": quote_envelope(provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
                quote_kind="last_trade", exchange_timestamp=self.now.isoformat(), received_at=self.now.isoformat(),
                max_age_seconds=60, authorized=True, realtime=True, delayed=False, official_close=False, trading_state="trading"),
            "price_source": "explicit_test_fixture", "is_realtime": True, "is_fallback": False,
            "odd_lot_auction_matched": True, "exchange_rules_enforced": True, "limit_up": 110, "limit_down": 80, **kwargs}

    async def tick(self, price, seconds, **kwargs):
        self.now = NOW + timedelta(seconds=seconds)
        return await self.executor.tick(self.pid, market=self.quote(price, **kwargs), now=self.now)


def test_off_tick_exit_floor_rounds_up_preserves_policy_and_real_oms_can_exit(tmp_path):
    definition = plan(exit_order_policy=ExitOrderPolicy(60, 2, 94.99))
    harness = PaperHarness(tmp_path, definition)

    async def exercise():
        assert (await harness.tick(100, 0))["state"]["status"] == "entry_submitted"
        submitted = await harness.tick(94.9, 1)
        assert submitted["state"]["status"] == "exit_submitted"
        assert submitted["state"]["exit_intent"]["limit_price"] == 95
        assert submitted["state"]["exit_intent"]["exit_minimum_limit_price"] == 94.99
        assert submitted["state"]["remaining_quantity"] == 10
        assert submitted["state"]["exit_receipt"]["is_open"] is True
        assert submitted["definition"]["exit_order_policy"]["minimum_limit_price"] == 94.99
        assert (await harness.tick(95, 2))["state"]["status"] == "closed"
        assert harness.oms.portfolio_summary()["positions"] == []

    asyncio.run(exercise())
    with pytest.raises(ValueError, match="exit_order_executable_floor_above_stop_loss"):
        plan(stop_loss=94.99, exit_order_policy=ExitOrderPolicy(60, 2, 94.98))


@pytest.mark.parametrize("observed_price", [89, 91.7])
def test_resting_legacy_exit_price_difference_never_cancels_real_broker_fees(tmp_path, observed_price):
    harness = PaperHarness(tmp_path, plan())

    async def exercise():
        await harness.tick(100, 0)
        first = await harness.tick(94, 1, volume_cap=0)
        await harness.port.cancel(first["state"]["exit_order_id"], reason="fixture_expired_commitment")
        result = await harness.tick(observed_price, 2, volume_cap=0)
        assert result["state"]["exit_intent"]["limit_price"] == 94
        assert result["state"]["exit_intent"]["reference_price"] == observed_price
        cost_gate = next(gate for gate in result["state"]["last_risk"]["gate_checks"] if gate["code"] == "order_cost_binding")
        assert cost_gate["passed"] is True
        assert cost_gate["observed"] == pytest.approx(22.82)
        assert result["state"]["last_cost_estimate"]["estimated_fees"] == pytest.approx(22.82)
        assert result["state"]["last_cost_estimate"]["estimated_adverse_price_cost"] == 0
        assert result["state"]["remaining_quantity"] == 10

    asyncio.run(exercise())


def test_preview_cost_keeps_real_buy_slippage_separate_from_commission(tmp_path):
    harness = PaperHarness(tmp_path, plan())
    harness.oms.slippage_bps = 100
    preview = harness.broker.preview({"symbol": "2330.TW", "market": "TW", "side": "buy",
        "quantity_shares": 10, "limit_price": 101, "order_type": "limit", "time_in_force": "rod",
        "lot_type": "odd_lot", "session": "regular"}, harness.quote(100))
    assert preview["estimated_fill_price"] == 101
    costs = _preview_cost_components(preview, quantity=10, price=100, side="buy")
    assert costs["estimated_fees"] == pytest.approx(20)
    assert costs["estimated_adverse_price_cost"] == pytest.approx(10)
    assert costs["estimated_total_cost"] == pytest.approx(30)
    assert harness.oms.portfolio_summary()["cash_balance"] == 100_000


def test_rejected_exit_preview_exposes_remaining_risk_and_exchange_blockers(tmp_path):
    harness = PaperHarness(tmp_path, plan())

    async def exercise():
        await harness.tick(100, 0)
        first = await harness.tick(94, 1, volume_cap=0)
        await harness.port.cancel(first["state"]["exit_order_id"], reason="fixture_prior_day_expiry")
        result = await harness.tick(100, 2, limit_down=95, volume_cap=0)
        assert result["state"]["status"] == "open"
        assert result["state"]["exit_order_id"] is None
        assert result["state"]["exit_alert"]["requires_attention"] is True
        assert result["state"]["exit_alert"]["remaining_quantity"] == 10
        assert result["state"]["exit_alert"]["limit_price"] == 94
        assert result["state"]["exit_alert"]["reason"] == "broker_preview_or_frozen_budget"
        blockers = result["state"]["last_preview"]["market_rules"]["blockers"]
        assert any(item["code"] == "limit_price_outside_daily_price_limit" for item in blockers)
        assert "limit_price_outside_daily_price_limit" in result["state"]["exit_alert"]["preview_blockers"]

    asyncio.run(exercise())
