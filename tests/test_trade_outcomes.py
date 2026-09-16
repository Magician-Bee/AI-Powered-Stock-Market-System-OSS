"""Accounting integration using explicit offline market fixtures, not EV evidence."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import sqlite3

import pytest

from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.execution.trade_outcomes import AutonomousOutcomeLedger
from open_stock_ai.storage.sqlite_store import SQLiteStore
from test_autonomous_campaign import NOW, setup


def closed_flow(tmp_path, monkeypatch):
    clock = {"now": NOW}
    for cls in (PaperOMS, PaperBrokerSimulator, PaperTrainingLab):
        monkeypatch.setattr(cls, "_now", lambda self: clock["now"].isoformat())
    storage = SQLiteStore(tmp_path / "outcomes.db")
    oms = PaperOMS(storage, account_id="offline-outcome", commission_bps=14.25, minimum_commission=20,
                   sell_tax_bps=30, slippage_bps=5, read_environment=False)
    port = PaperBrokerPort(PaperBrokerSimulator(storage, oms))
    campaign, quote = setup(tmp_path, broker=port, shared=storage, symbols=("2330.TW",))
    clock = quote

    async def run():
        cycle = await campaign.research(now=NOW, deep_limit=1)
        record = (await campaign.create_plans(cycle_id=cycle["cycle_id"], now=NOW))["plans"][0]
        campaign.configure(enabled=True)
        due = datetime.fromisoformat(record["definition"]["not_before"]).astimezone(timezone.utc)
        quote.update(now=due, price=106)
        assert not (await campaign.manage(now=due))["errors"]
        quote.update(now=due+timedelta(seconds=1), price=math.ceil(record["definition"]["target_price"]*2)/2)
        assert not (await campaign.manage(now=quote["now"]))["errors"]
        quote["now"] += timedelta(seconds=1)
        assert not (await campaign.manage(now=quote["now"]))["errors"]
        final = campaign.plans.get(record["plan_id"])
        ids = {event["payload"][phase]["order_id"] for event in campaign.plans.events(record["plan_id"])
               for phase in ("entry_intent", "exit_intent") if event["payload"].get(phase)}
        fills = await port.fills(sorted(ids))
        return campaign, final, fills
    return asyncio.run(run())


def test_closed_plan_reward_comes_from_its_fills_and_is_idempotent(tmp_path, monkeypatch):
    service, plan, fills = closed_flow(tmp_path, monkeypatch)
    ledger = AutonomousOutcomeLedger(service.plans.store)
    first = ledger.record(plan=plan, fills=fills, mode="paper")
    assert first["net_pnl"] == pytest.approx(sum(f["net_cash_delta"] for f in fills))
    assert first["commission"] > 0 and first["tax"] > 0
    assert first["slippage_cost_already_in_fill_prices"] >= 0
    assert first["positive_ev_qualified"] is False and first["execution_evidence_eligible"] is False
    assert ledger.record(plan=plan, fills=fills, mode="paper") == first
    assert ledger.summary(account_id=plan["account_id"])["closed_trade_count"] == 1
    assert ledger.summary(account_id="other-account")["closed_trade_count"] == 0


def test_broker_provenance_view_preserves_v1_accounting_receipt_and_source_rows(tmp_path, monkeypatch):
    service, plan, fills = closed_flow(tmp_path, monkeypatch)
    ledger = AutonomousOutcomeLedger(service.plans.store)
    before = ledger.list(account_id=plan["account_id"])[0]
    assert all(fill["fill_evidence_verification"]["integrity_status"] == "verified" for fill in fills)
    with service.plans.store._connect() as conn:
        conn.row_factory = sqlite3.Row
        raw = [dict(row) for row in conn.execute("select * from paper_fills where account_id=?", (plan["account_id"],))]
        metadata = list(conn.execute("select metadata_json from cash_ledger where order_id is not null"))
    assert ledger.record(plan=plan, fills=raw, mode="paper") == before
    assert ledger.record(plan=plan, fills=fills, mode="paper") == before
    assert all("fill_evidence" not in fill for fill in before["fills"])
    with service.plans.store._connect() as conn:
        conn.row_factory = sqlite3.Row
        assert list(conn.execute("select metadata_json from cash_ledger where order_id is not null")) == metadata


def test_unclosed_duplicate_wrong_account_and_bad_cashflow_are_not_learning_rewards(tmp_path, monkeypatch):
    service, plan, fills = closed_flow(tmp_path, monkeypatch)
    ledger = AutonomousOutcomeLedger(service.plans.store)
    with pytest.raises(ValueError, match="closed_reconciled"):
        ledger.record(plan={**plan, "state": {**plan["state"], "status": "open"}}, fills=fills, mode="paper")
    with pytest.raises(ValueError, match="duplicate"):
        ledger.record(plan=plan, fills=fills+fills[:1], mode="paper")
    wrong = deepcopy(fills)
    wrong[0]["account_id"] = "other-account"
    with pytest.raises(ValueError, match="scope_mismatch"):
        ledger.record(plan=plan, fills=wrong, mode="paper")
    wrong = deepcopy(fills)
    wrong[0]["net_cash_delta"] += 100
    with pytest.raises(ValueError, match="cashflow_mismatch"):
        ledger.record(plan=plan, fills=wrong, mode="paper")
    wrong = deepcopy(fills)
    wrong[0]["order_id"] = "a_different_round_trip_in_the_same_stock"
    with pytest.raises(ValueError, match="dispatch_journal"):
        ledger.record(plan=plan, fills=wrong, mode="paper")
