"""Offline proposal-to-accounting integration through production paper components.

Scanner/history/calendar and Codex run/model identity are explicit fixtures from
the existing campaign tests. Quotes are synthetic MIS-shaped board trades from
the proxy tests, not network observations. The real opted-in odd-lot proxy owns
partial matching; no auction receipt, fill, position or cash row is fabricated.
This proves plumbing/accounting, not model judgement, live execution or EV.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import socket

import pytest

from open_stock_ai.execution.agent_campaign_actions import propose_plan
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.storage.sqlite_store import SQLiteStore
from test_agent_campaign_actions import CONTEXT
from test_autonomous_campaign import NOW, setup as campaign_setup
from test_autonomous_trading_plans import market as executable_market_fixture
from test_paper_odd_lot_proxy import _market as proxy_market_fixture


ACCOUNT_ID = "offline-agent-partial-round-trip"
INITIAL_CASH = 100_000


def _campaign(tmp_path, database, clock):
    # Every restart reconstructs OMS, broker, campaign, executor and plan store
    # from the same temporary SQLite file; no in-memory broker survives it.
    storage = SQLiteStore(database)
    oms = PaperOMS(storage, account_id=ACCOUNT_ID, initial_cash=INITIAL_CASH,
                   commission_bps=14.25, minimum_commission=20,
                   sell_tax_bps=30, slippage_bps=5, read_environment=False)
    broker = PaperBrokerSimulator(storage, oms, odd_lot_execution_model=OddLotBoardProxyModel())
    service, _ = campaign_setup(tmp_path, broker=PaperBrokerPort(broker), shared=storage, symbols=("2330.TW",))
    service.costs = {"commission_bps": 14.25, "minimum_commission": 20,
                     "sell_tax_bps": 30, "slippage_bps": 5, "market_impact_bps": 20}

    async def quotes(symbol):
        assert symbol == "2330.TW"
        market = proxy_market_fixture(at=clock["now"], price=clock["price"], size=1000)
        # The executor's intraday gate requires an authorized source. This is
        # the existing synthetic authorization contract, not a real entitlement.
        market["source_envelope"] = executable_market_fixture(clock["price"], clock["now"])["source_envelope"]
        assert "odd_lot_auction_matched" not in market
        assert "paper_training_fill_override" not in market
        return market

    service.quote_loader = quotes
    return service


@pytest.mark.parametrize("exit_reason", ["stop_loss", "holding_period_expired"])
def test_agent_proposal_partial_fill_protection_and_restarted_outcomes(tmp_path, monkeypatch, exit_reason):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("offline_agent_pipeline_must_not_open_network")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    clock = {"now": NOW, "price": 106}
    for cls in (PaperOMS, PaperBrokerSimulator, PaperTrainingLab):
        monkeypatch.setattr(cls, "_now", lambda _self: clock["now"].isoformat())
    database = tmp_path / "agent-pipeline.sqlite"
    service = _campaign(tmp_path, database, clock)
    context = deepcopy(CONTEXT)  # Fixed model/run metadata, never a model call.
    due = (NOW + timedelta(days=3)).replace(hour=2, minute=0)  # Monday 10:00 Taipei.

    async def manage(campaign, *, at, price):
        clock.update(now=at, price=price)
        result = await campaign.manage(now=at)
        assert result["errors"] == [], result
        assert result["model_calls"] == 0
        return result

    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        assert cycle["deep_success_count"] == 1 and cycle["errors"] == []
        assert all(not candidate["positive_ev_qualified"] for candidate in cycle["results"][0]["candidates"])
        proposal = {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "quantity_shares": 40,
                    "stop_loss": 95, "target_price": 130, "max_holding_seconds": 60,
                    "not_before": due.isoformat(), "expires_at": (due + timedelta(hours=2)).isoformat(),
                    "rationale": "Offline discretionary fixture: bound exposure and exit on stop or holding deadline."}
        record = await propose_plan(service, proposal, host_context=context, now=NOW)
        plan_id = record["plan_id"]
        definition = record["definition"]
        assert definition["strategy_id"] == "agent_discretionary_proposal_v1"
        assert definition["quantity_shares"] == 40 and definition["qualification_id"] is None
        assert definition["metadata"]["agent_context"] == context
        assert definition["metadata"]["positive_ev_qualified"] is False
        assert cycle["results"][0]["history_id"] in definition["evidence_ids"]
        assert service.cycle(cycle["cycle_id"])["cycle_id"] == cycle["cycle_id"]
        service.configure(enabled=True)

        # A same-price limit cannot pay the proxy's adverse execution price.
        await manage(service, at=due, price=106)
        submitted = service.plans.get(plan_id)
        assert submitted["state"]["status"] == "entry_submitted", submitted["state"]
        entry_id = submitted["state"]["entry_order_id"]
        assert submitted["state"]["entry_receipt"]["filled_quantity"] == 0
        assert len(service.broker.broker.recent_orders()) == 1

        # A later better quote permits only 1% of 1,000 observed fixture shares.
        first_fill_at = due + timedelta(seconds=5)
        await manage(service, at=first_fill_at, price=105)
        partial = service.plans.get(plan_id)
        entry = await service.broker.order(entry_id)
        assert entry["status"] == "partially_filled"
        assert entry["filled_quantity"] == 10 and entry["remaining_quantity"] == 30
        assert partial["state"]["filled_quantity"] == 10
        assert partial["state"]["holding_time_basis"] == "broker_first_fill"
        assert partial["state"]["entered_at"] == first_fill_at.isoformat()
        receipt_events = service.broker.broker.get_order(entry_id)["events"]
        simulations = [json.loads(event["payload_json"]).get("execution_model", {}).get("paper_odd_lot_simulation")
                       for event in receipt_events]
        assert any(item and item["is_simulated"] and not item["execution_evidence_eligible"] for item in simulations)
        await manage(service, at=first_fill_at, price=105)
        assert len(await service.broker.fills([entry_id])) == 1

        # Recover the partial order, held quantity, deadline and proposal from disk.
        recovered = _campaign(tmp_path, database, clock)
        assert recovered.plans.get(plan_id)["definition_hash"] == record["definition_hash"]
        retry = await propose_plan(recovered, proposal, host_context=context, now=first_fill_at)
        assert retry["plan_id"] == plan_id
        await manage(recovered, at=first_fill_at, price=105)
        assert (await recovered.broker.order(entry_id))["filled_quantity"] == 10
        assert recovered.plans.get(plan_id)["state"]["entered_at"] == first_fill_at.isoformat()

        protection_at = due + timedelta(seconds=10 if exit_reason == "stop_loss" else 66)
        protection_price = 94 if exit_reason == "stop_loss" else 105
        await manage(recovered, at=protection_at, price=protection_price)
        exiting = recovered.plans.get(plan_id)
        assert exiting["state"]["exit_reason"] == exit_reason
        assert exiting["state"]["exit_intent"]["quantity_shares"] == 10
        cancelled = await recovered.broker.order(entry_id)
        assert cancelled["status"] == "canceled" and not cancelled["is_open"]
        assert cancelled["filled_quantity"] == 10  # Protection did not refill the 30-share residual.
        exit_id = exiting["state"]["exit_order_id"]
        assert (await recovered.broker.order(exit_id))["filled_quantity"] == 0
        assert len(recovered.broker.broker.recent_orders()) == 2

        # The sell also waits for a quote whose adverse price respects its limit.
        closed_at = protection_at + timedelta(seconds=5)
        await manage(recovered, at=closed_at, price=protection_price + .5)
        closed = recovered.plans.get(plan_id)
        assert closed["status"] == "closed" and closed["state"]["remaining_quantity"] == 0
        fills = await recovered.broker.fills([entry_id, exit_id])
        assert len(fills) == 2
        assert [(fill["side"], fill["quantity"]) for fill in fills] == [("buy", 10), ("sell", 10)]
        assert all(fill["account_id"] == ACCOUNT_ID for fill in fills)
        commission = sum(fill["commission"] for fill in fills)
        tax = sum(fill["tax"] for fill in fills)
        net_pnl = sum(fill["net_cash_delta"] for fill in fills)
        assert commission > 0 and tax > 0 and net_pnl < 0
        account = await recovered.broker.account(now=closed_at)
        assert not any(position["quantity"] for position in account["positions"])
        assert account["cash_balance"] == pytest.approx(INITIAL_CASH + net_pnl, abs=.01)
        # manage(), not the test, must have recorded the account-bound outcome.
        outcomes = recovered.outcomes.list(account_id=ACCOUNT_ID)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome["plan_id"] == plan_id and outcome["quantity"] == 10
        assert outcome["net_pnl"] == pytest.approx(net_pnl)
        assert outcome["commission"] == commission and outcome["tax"] == tax
        assert outcome["exit_reason"] == exit_reason
        assert not outcome["positive_ev_qualified"] and not outcome["execution_evidence_eligible"]
        assert recovered.outcomes.summary(account_id="another-account")["closed_trade_count"] == 0

        # Another full component reconstruction and repeated management must
        # preserve the two orders/two fills/one immutable outcome exactly.
        restarted = _campaign(tmp_path, database, clock)
        for offset in (0, 5):
            await manage(restarted, at=closed_at + timedelta(seconds=offset), price=protection_price + .5)
        assert {order["order_id"] for order in restarted.broker.broker.recent_orders()} == {entry_id, exit_id}
        assert await restarted.broker.fills([entry_id, exit_id]) == fills
        assert restarted.outcomes.list(account_id=ACCOUNT_ID) == outcomes
        assert restarted.outcomes.summary(account_id=ACCOUNT_ID)["closed_trade_count"] == 1
        assert (await restarted.broker.account(now=clock["now"]))["cash_balance"] == account["cash_balance"]

    asyncio.run(scenario())
