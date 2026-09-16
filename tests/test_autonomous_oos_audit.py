from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from open_stock_ai.research.autonomous_oos_audit import audit_research_cycle
from open_stock_ai.strategy.provenance import content_hash


def _evidence_id(account_id, kind, payload):
    return "AE-" + content_hash({"account_id": account_id, "kind": kind, "payload": payload})


def _database(path):
    account_id = "autonomous-paper-v1"
    created = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)
    bars = [
        {"timestamp": (created - timedelta(days=6-index)).isoformat(),
         "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        for index in range(6)
    ]
    history = {"rows": bars, "data_evidence": {"symbol": "2330.TW", "data_sha256": content_hash(bars)}}
    history_id = _evidence_id(account_id, "price_history", history)
    costs = {"broker_commission_bps": 14.25, "broker_minimum_commission_twd": 20,
             "spread_bps": 5, "historical_impact_sensitivity_bps": 15,
             "adverse_execution_floor_bps": 25}
    partition = lambda: {
        "bar_count": 2, "net_expectancy": {"closed_trade_count": 1, "mean_net_return_pct": 1.0},
        "performance": {"compounded_return_pct": 0.1, "max_drawdown_pct": 0.05},
        "fill_count": 2, "total_fees": 20.0, "accounting_verified": True,
    }
    qualification = {
        "schema_version": "open_stock_ai.candle_qualification.v1", "symbol": "2330.TW",
        "candidate_id": "tw_candle_breakout_20_60_v1", "strategy_version_hash": "a" * 64,
        "data_version_hash": content_hash(bars), "valid_row_count": 6,
        "research_paper_candidate_eligible": True, "positive_ev_qualified": False,
        "evaluation_manifest": {"train": [0, 2], "validation": [2, 4], "holdout": [4, 6],
            "split_basis": "chronological_indices_before_evaluation", "evaluation_family_size": 2,
            "cost_assumptions": costs},
        "partitions": {"train": partition(), "validation": partition(), "holdout": partition()},
        "reasons": ["holdout_insufficient_bars"],
    }
    qualification["receipt_sha256"] = content_hash(qualification)
    qualification_id = _evidence_id(account_id, "qualification", qualification)
    cycle = {
        "schema_version": "open_stock_ai.autonomous_research_cycle.v1", "account_id": account_id,
        "created_at": created.isoformat(), "results": [{"symbol": "2330.TW", "history_id": history_id,
            "candidates": [{"strategy_id": qualification["candidate_id"], "qualification_id": qualification_id}]}],
    }
    cycle_id = "AC-" + content_hash(cycle)
    cycle["cycle_id"] = cycle_id
    connection = sqlite3.connect(path)
    connection.executescript("""
        create table autonomous_research_cycles(cycle_id text,account_id text,created_at text,payload_json text);
        create table autonomous_evidence(evidence_id text,account_id text,kind text,payload_json text);
    """)
    connection.execute("insert into autonomous_research_cycles values (?,?,?,?)",
                       (cycle_id, account_id, created.isoformat(), json.dumps(cycle)))
    connection.executemany("insert into autonomous_evidence values (?,?,?,?)", [
        (history_id, account_id, "price_history", json.dumps(history)),
        (qualification_id, account_id, "qualification", json.dumps(qualification)),
    ])
    connection.commit()
    connection.close()
    return cycle_id, qualification_id


def test_oos_audit_verifies_retained_identity_temporal_split_costs_and_accounting(tmp_path):
    database = tmp_path / "runtime.sqlite"
    cycle_id, _ = _database(database)
    result = audit_research_cycle(database, cycle_id=cycle_id)
    assert result["integrity_passed"] is True
    assert result["qualification_receipt_count"] == 1
    assert result["conclusion"] == {
        "historical_oos_executed": True,
        "positive_expectancy_established": False,
        "selection_permitted_from_holdout": False,
        "forward_trial_still_required": True,
        "boundary": "exploratory_per_symbol_oos; holdout_results_must_not_choose_the_forward_policy",
    }
    assert result["phases"]["holdout"]["closed_trade_count_sum_across_independent_ledgers"] == 1


def test_oos_audit_rejects_changed_qualification_receipt(tmp_path):
    database = tmp_path / "runtime.sqlite"
    cycle_id, qualification_id = _database(database)
    connection = sqlite3.connect(database)
    payload = json.loads(connection.execute(
        "select payload_json from autonomous_evidence where evidence_id=?", (qualification_id,),
    ).fetchone()[0])
    payload["positive_ev_qualified"] = True
    connection.execute("update autonomous_evidence set payload_json=? where evidence_id=?",
                       (json.dumps(payload), qualification_id))
    connection.commit()
    connection.close()
    result = audit_research_cycle(database, cycle_id=cycle_id)
    assert result["integrity_passed"] is False
    assert any("qualification_identity_mismatch" in item for item in result["integrity_violations"])
    assert any("qualification_receipt_hash_mismatch" in item for item in result["integrity_violations"])
