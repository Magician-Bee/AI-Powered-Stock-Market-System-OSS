#!/usr/bin/env python3
from __future__ import annotations

"""Run one isolated autonomous paper-trading lifecycle acceptance scenario.

The scenario uses deterministic fixture candles and quotes so it can exercise
the real research, plan, risk, OMS, broker, reconciliation and outcome-ledger
code without a model call or a live broker.  Its receipt is engineering proof
of reachability only and is never market or positive-expectancy evidence.
"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.autonomous_campaign import AutonomousCampaign
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore


START = datetime(2026, 9, 11, 7, 0, tzinfo=timezone.utc)
_CLOCK = {"now": START}


class _ClockedPaperOMS(PaperOMS):
    def _now(self) -> str:
        return _CLOCK["now"].isoformat()


class _ClockedPaperBroker(PaperBrokerSimulator):
    def _now(self) -> str:
        return _CLOCK["now"].isoformat()


class _Calendar:
    def next_trading_day(self, day):
        day += timedelta(days=1)
        while day.weekday() > 4:
            day += timedelta(days=1)
        return day

    def day_status(self, day):
        return {"trading_day": day.weekday() < 5}


def _history(symbol: str) -> dict[str, Any]:
    days = []
    day = START.date()
    while len(days) < 180:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    rows = [
        {
            "timestamp": datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).replace(hour=5, minute=30).isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 2_000_000.0,
        }
        for value in reversed(days)
    ]
    rows[-1].update(open=104.0, high=107.0, low=103.0, close=106.0, volume=3_000_000.0)
    return {
        "rows": rows,
        "data_evidence": {
            "symbol": symbol,
            "source_kind": "exchange_official",
            "source_id": "isolated_closed_loop_fixture",
            "source_provenance_verified": True,
            "instrument_identity_verified": True,
            "coverage_complete": True,
            "fixture_only_not_market_or_ev_proof": True,
            "corporate_actions_verified": False,
            "historical_vintage_verified": False,
            "execution_costs_verified": False,
            "data_sha256": content_hash(rows),
            "normalized_data_sha256": content_hash(rows),
        },
    }


def _market(symbol: str, price: float, now: datetime) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market": "TW",
        "price": float(price),
        "source_timestamp": now.isoformat(),
        "is_realtime": True,
        "is_fallback": False,
        "price_source": "isolated_closed_loop_fixture",
        "odd_lot_auction_matched": True,
        "fixture_only_not_market_or_ev_proof": True,
        "source_envelope": quote_envelope(
            provider_id="twse_mis",
            # Exercise the real TWSE MIS execution-contract branch.  The
            # surrounding payload and final receipt retain the fixture marker.
            connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
            quote_kind="last_trade",
            exchange_timestamp=now.isoformat(),
            received_at=now.isoformat(),
            max_age_seconds=60,
            authorized=True,
            realtime=True,
            delayed=False,
            official_close=False,
            trading_state="trading",
        ),
    }


def _fixture_product_snapshot(*, symbol: str, market: str, now: datetime) -> dict[str, Any]:
    """Explicit injected Host fixture; never consult the production warehouse."""
    if symbol != "2330.TW" or market.upper() not in {"TW", "TAIWAN", "TWSE"}:
        return {"classification": {"status": "unknown", "product_type": "unknown"}}
    row = {
        "cells": ["2330　OFFLINE FIXTURE", "TW0002330008", "2020/01/01", "上市", "fixture", "ESVUFR", ""],
        "section": "股票", "fixture_only_not_market_or_ev_proof": True,
    }
    return {
        "entity_id": "ENT-" + uuid5(NAMESPACE_URL, "isolated-closed-loop-product:" + symbol).hex,
        "lifecycle_status": "active",
        "classification": {
            "schema_version": "stock_ai.product_classification.v1",
            "status": "verified", "product_type": "ordinary_stock", "symbol": symbol,
            "venue": "TWSE", "market_segment": "ordinary", "isin": "TW0002330008", "cfi_code": "ESVUFR",
            "source_id": "twse_isin", "source_dataset": "twse_isin_listed",
            "source_url": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
            "acquired_at": START.isoformat(), "source_updated_on": START.date().isoformat(),
            "raw_sha256": content_hash({"fixture_catalogue": row}),
            "row_sha256": content_hash(row), "source_row": row,
            "fixture_only_not_market_or_ev_proof": True,
        },
    }


def _commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


async def _run(database: Path) -> dict[str, Any]:
    _CLOCK["now"] = START
    store = SQLiteStore(database)
    oms = _ClockedPaperOMS(
        store,
        account_id="closed-loop-acceptance-paper",
        initial_cash=1_000_000,
        commission_bps=14.25,
        minimum_commission=20,
        sell_tax_bps=30,
        slippage_bps=5,
        read_environment=False,
    )
    broker = PaperBrokerPort(_ClockedPaperBroker(store, oms))
    product = _fixture_product_snapshot(symbol="2330.TW", market="TW", now=START)
    features = [{
        "symbol": "2330.TW", "close": 106.0, "data_as_of": START.date().isoformat(),
        "trade_value": 50_000_000_000.0, "exchange": "TWSE",
        "entity_id": product["entity_id"], "lifecycle_status": product["lifecycle_status"],
        "product_classification": product["classification"],
    }]

    async def scanner():
        return {"features": deepcopy(features), "all_features": deepcopy(features)}

    async def history_loader(symbol: str, now: datetime):
        return _history(symbol)

    quote_state = {"now": START, "price": 106.0}

    async def quote_loader(symbol: str):
        return _market(symbol, quote_state["price"], quote_state["now"])

    campaign = AutonomousCampaign(
        plans=TradingPlanStore(store), broker=broker, risk=RiskEngine(),
        scanner=scanner, history_loader=history_loader, quote_loader=quote_loader,
        product_resolver=_fixture_product_snapshot,
        calendar=_Calendar(),
        costs={"commission_bps": 14.25, "minimum_commission": 20,
               "slippage_bps": 5, "market_impact_bps": 20, "sell_tax_bps": 30},
    )
    stages: list[dict[str, Any]] = []
    cycle = await campaign.research(now=START, deep_limit=1)
    stages.append({"stage": "analysis", "cycle_id": cycle["cycle_id"],
                   "deep_success_count": cycle["deep_success_count"]})
    created = await campaign.create_plans(cycle_id=cycle["cycle_id"], now=START)
    if len(created.get("plans") or []) != 1:
        raise RuntimeError(f"closed_loop_expected_one_plan:{created}")
    plan = created["plans"][0]
    stages.append({"stage": "plan", "plan_id": plan["plan_id"],
                   "definition_hash": plan["definition_hash"]})
    campaign.configure(enabled=True)
    due = datetime.fromisoformat(plan["definition"]["not_before"]).astimezone(timezone.utc)
    _CLOCK["now"] = due
    quote_state.update(now=due, price=106.0)
    submitted = await campaign.manage(now=due)
    entry_state = campaign.plans.get(plan["plan_id"])["state"]
    stages.append({"stage": "entry_submission", "status": entry_state["status"],
                   "order_id": entry_state.get("entry_order_id"), "errors": submitted["errors"]})

    target = math.ceil(float(plan["definition"]["target_price"]) * 2.0) / 2.0
    _CLOCK["now"] = due + timedelta(seconds=1)
    quote_state.update(now=_CLOCK["now"], price=target)
    exit_submission = await campaign.manage(now=_CLOCK["now"])
    exit_state = campaign.plans.get(plan["plan_id"])["state"]
    stages.append({"stage": "position_management_and_exit_submission", "status": exit_state["status"],
                   "order_id": exit_state.get("exit_order_id"), "errors": exit_submission["errors"]})

    _CLOCK["now"] = due + timedelta(seconds=2)
    quote_state["now"] = _CLOCK["now"]
    close_pass = await campaign.manage(now=_CLOCK["now"])
    closed_plan = campaign.plans.get(plan["plan_id"])
    stages.append({"stage": "exit_fill_and_reconciliation", "status": closed_plan["state"]["status"],
                   "errors": close_pass["errors"]})
    repeat = await campaign.manage(now=_CLOCK["now"] + timedelta(seconds=1))
    account = await broker.account(now=_CLOCK["now"] + timedelta(seconds=1))
    fills = broker.broker.recent_fills()
    learning = campaign.status()["learning"]
    outcomes = learning.get("outcomes") or []
    outcome = outcomes[0] if len(outcomes) == 1 else {}
    outcome_hash_valid = bool(outcome) and content_hash({
        key: value for key, value in outcome.items() if key != "receipt_sha256"
    }) == outcome.get("receipt_sha256")
    order_ids = [fill.get("order_id") for fill in fills]
    chronological_fills = sorted(fills, key=lambda fill: (str(fill.get("created_at") or ""), str(fill.get("fill_id") or "")))
    checks = {
        "paper_account_isolated": account.get("account_id") == "closed-loop-acceptance-paper",
        "one_plan_created": len(created["plans"]) == 1,
        "entry_submitted": entry_state.get("status") == "entry_submitted",
        "exit_submitted_after_position_management": exit_state.get("status") == "exit_submitted",
        "plan_closed_and_flat": closed_plan["state"].get("status") == "closed"
            and float(closed_plan["state"].get("remaining_quantity") or 0) == 0,
        "two_real_paper_fills": len(fills) == 2
            and [fill.get("side") for fill in chronological_fills] == ["buy", "sell"],
        "distinct_idempotent_orders": len(set(order_ids)) == 2,
        "costs_charged": sum(float(fill.get("commission") or 0) for fill in fills) > 0
            and sum(float(fill.get("tax") or 0) for fill in fills) > 0
            and sum(float(fill.get("slippage_cost") or 0) for fill in fills) > 0,
        "account_reconciled_flat": account.get("position_count") == 0,
        "one_learning_outcome": learning.get("closed_trade_count") == 1,
        "outcome_hash_valid": outcome_hash_valid,
        "outcome_matches_account_cashflow": bool(outcome)
            and abs(float(outcome.get("net_pnl") or 0) - (float(account["cash_balance"]) - 1_000_000.0)) <= 0.011,
        "repeat_reconciliation_idempotent": not repeat["errors"]
            and campaign.status()["learning"].get("closed_trade_count") == 1,
        "no_live_execution": broker.mode == "paper",
        "not_positive_ev_evidence": outcome.get("positive_ev_qualified") is False,
    }
    receipt: dict[str, Any] = {
        "schema_version": "open_stock_ai.autonomous_closed_loop_acceptance.v1",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _commit(),
        "fixture_only_not_market_or_ev_proof": True,
        "database": str(database.resolve()),
        "account_id": account.get("account_id"),
        "cycle_id": cycle["cycle_id"],
        "plan_id": plan["plan_id"],
        "stages": stages,
        "fills": fills,
        "outcome": outcome,
        "final_account": {key: account.get(key) for key in (
            "cash_balance", "settled_cash_balance", "available_cash", "total_equity",
            "position_count", "order_count", "fill_count", "realized_pnl",
        )},
        "checks": checks,
        "passed": all(checks.values()),
        "scope": "isolated_deterministic_engineering_reachability; no_model_call; no_live_broker; no_profitability_claim",
    }
    receipt["receipt_sha256"] = content_hash(receipt)
    return receipt


def run_verification(database: Path) -> dict[str, Any]:
    return asyncio.run(_run(database))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database", type=Path)
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.database:
        if args.database.exists():
            raise ValueError("acceptance_database_must_not_exist")
        args.database.parent.mkdir(parents=True, exist_ok=True)
        receipt = run_verification(args.database)
    else:
        with TemporaryDirectory(prefix="stock-ai-closed-loop-") as folder:
            receipt = run_verification(Path(folder) / "acceptance.sqlite")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema_version": receipt["schema_version"],
        "passed": receipt["passed"],
        "cycle_id": receipt["cycle_id"],
        "plan_id": receipt["plan_id"],
        "stages": receipt["stages"],
        "final_account": receipt["final_account"],
        "checks": receipt["checks"],
        "scope": receipt["scope"],
        "receipt_sha256": receipt["receipt_sha256"],
    }, ensure_ascii=False, indent=2))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
