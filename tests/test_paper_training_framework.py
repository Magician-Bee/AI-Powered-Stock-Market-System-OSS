from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.governance.artifact_rollback import ApprovedArtifactRollbackRegistry
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.learning.promotion_gate import PromotionGate
from open_stock_ai.research.regime_robustness import evaluate_regime_robustness
from open_stock_ai.research.benchmark_metrics import evaluate_benchmark_metrics
from open_stock_ai.research.monte_carlo import simulate_tail_risk


TEST_SYMBOL = "2330.TW"


def _regime_observations(count: int) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    close = 100.0
    rows = []
    for index in range(count):
        bull_phase = (index // 20) % 2 == 0
        move = (1.018 if index % 3 == 0 else 1.004) if bull_phase else (0.986 if index % 3 == 0 else 0.996)
        close *= move
        timestamp = start + timedelta(days=index)
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "close": close,
                "volume": 500 if index % 4 == 0 else 1400 + (index % 5) * 40,
                "earnings_event": (
                    {"event_type": "earnings", "available_at": timestamp.isoformat()}
                    if index % 10 == 0
                    else None
                ),
            }
        )
    return rows


def _historical_universe_evidence(count: int) -> tuple[dict[str, object], list[dict[str, object]]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {
            "as_of": (start + timedelta(days=index)).isoformat(),
            "passed": True,
            "point_in_time_verified": True,
            "required_entity_in_universe": True,
            "universe_scope_id": "taiwan-listed-and-otc-securities",
            "universe_coverage_complete": True,
            "manifest_hash": hashlib.sha256(f"universe-{index}".encode("utf-8")).hexdigest(),
        }
        for index in range(count)
    ]
    return (
        {
            "schema_version": "open_stock_ai.historical_universe.v1",
            "passed": True,
            "point_in_time_verified": True,
            "universe_coverage_complete": True,
            "universe_scope_ids": ["taiwan-listed-and-otc-securities"],
            "row_count": count,
        },
        rows,
    )


def _benchmark_and_cash_evidence(count: int) -> tuple[list[str], list[dict[str, object]], list[dict[str, object]]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    revision = "c" * 64
    timestamps = [(start + timedelta(days=index)).isoformat() for index in range(count)]
    benchmark = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.001 + (index % 5) * 0.0001,
            "benchmark_id": "TWII",
            "source_id": "twse_official_index",
            "dataset_id": "twse_index_daily",
            "revision_id": revision,
        }
        for index, timestamp in enumerate(timestamps)
    ]
    cash = [
        {
            "timestamp": timestamp,
            "available_at": timestamp,
            "return": 0.00004,
            "cash_rate_id": "twd_overnight_cash_rate",
            "source_id": "central_bank_taiwan",
            "dataset_id": "twd_cash_rate_daily",
            "revision_id": "d" * 64,
        }
        for timestamp in timestamps
    ]
    return timestamps, benchmark, cash


def build_lab(tmp_path, monkeypatch) -> PaperTrainingLab:
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "training-test")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "10000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_LOT_SIZE", "1")
    store = SQLiteStore(db_path=tmp_path / "paper-training.sqlite")
    oms = PaperOMS(store=store)
    return PaperTrainingLab(store=store, oms=oms)


def test_virtual_capital_gains_when_verified_market_mark_rises(tmp_path, monkeypatch):
    lab = build_lab(tmp_path, monkeypatch)
    reset = lab.reset_account(10_000)
    episode_id = reset["episode"]["episode_id"]

    fill = lab.oms.submit_and_fill(
        {
            "order_id": "TRAIN-BUY-1",
            "symbol": TEST_SYMBOL,
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 10,
            "risk_approved": True,
        }
    )
    assert fill["filled"] is True
    assert fill["fill"]["quantity"] == 10
    assert fill["portfolio"]["total_equity"] == 10_000

    marked = lab.apply_market_mark(
        symbol=TEST_SYMBOL,
        market="TW",
        price=110,
        price_source="verified_test_exchange_last_trade",
        source_timestamp="2026-07-15T01:00:00+00:00",
        is_realtime=True,
        is_fallback=False,
        episode_id=episode_id,
    )

    account = marked["account"]
    assert account["cash_balance"] == 9_000
    assert account["holdings_market_value"] == 1_100
    assert account["total_equity"] == 10_100
    assert account["unrealized_pnl"] == 100
    assert account["total_return_pct"] == 1.0
    assert account["positions"][0]["symbol"] == TEST_SYMBOL
    assert account["positions"][0]["price_source"] == "verified_test_exchange_last_trade"

    evaluation = lab.evaluate_episode(episode_id)
    assert evaluation["reward"] == 100
    assert evaluation["return_pct"] == 1.0


def test_learning_episode_keeps_losses_and_reflections(tmp_path, monkeypatch):
    lab = build_lab(tmp_path, monkeypatch)
    reset = lab.reset_account(10_000)
    episode_id = reset["episode"]["episode_id"]
    lab.oms.submit_and_fill(
        {
            "order_id": "TRAIN-BUY-LOSS",
            "symbol": TEST_SYMBOL,
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 10,
            "risk_approved": True,
        }
    )
    lab.apply_market_mark(
        symbol=TEST_SYMBOL,
        market="TW",
        price=90,
        price_source="verified_test_exchange_close",
        source_timestamp="2026-07-16T05:30:00+00:00",
        is_realtime=False,
        is_fallback=True,
        episode_id=episode_id,
    )
    evaluation = lab.evaluate_episode(episode_id, close=True)
    reflection = lab.save_reflection(
        episode_id=episode_id,
        summary="The entry lost value after the verified market close.",
        lessons=["Do not hide losing outcomes"],
        next_rules=["Recheck volume and event risk before the next experiment"],
    )
    learning = lab.learning_summary()

    assert evaluation["reward"] == -100
    assert learning["negative_episode_count"] == 1
    assert learning["total_reward"] == -100
    assert reflection["lessons"] == ["Do not hide losing outcomes"]
    assert reflection["policy_proposal"]["status"] == "proposed"
    assert learning["reflections"][0]["next_rules"] == [
        "Recheck volume and event risk before the next experiment"
    ]


def test_schema_v3_contains_verified_marks_and_learning_tables(tmp_path, monkeypatch):
    lab = build_lab(tmp_path, monkeypatch)
    with sqlite3.connect(lab.store.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
        }
        user_version = conn.execute("pragma user_version").fetchone()[0]

    assert user_version >= 3
    assert {
        "paper_price_marks",
        "agent_learning_episodes",
        "agent_learning_events",
        "agent_reflections",
        "policy_proposals",
        "strategy_versions",
    }.issubset(tables)


def test_policy_proposal_requires_exact_evaluation_shadow_and_human_promotion(tmp_path, monkeypatch):
    lab = build_lab(tmp_path, monkeypatch)
    episode_id = lab.reset_account(10_000)["episode"]["episode_id"]
    proposal = lab.save_reflection(
        episode_id=episode_id,
        summary="Try a stricter entry filter.",
        lessons=["Avoid weak evidence"],
        next_rules=["Require confirmed volume"],
    )["policy_proposal"]
    period_returns = [0.004 + (index % 7) * 0.0004 for index in range(80)]
    regime_observations = _regime_observations(len(period_returns))
    historical_universe, universe_membership_rows = _historical_universe_evidence(len(period_returns))
    return_timestamps, benchmark_observations, risk_free_observations = _benchmark_and_cash_evidence(len(period_returns))
    evidence = {
        "strategy_replay_exact": True,
        "empirical_valid": True,
        "lookahead_safe": True,
        "transaction_costs_included": True,
        "slippage_included": True,
        "execution_latency_bars": 1,
        "walk_forward": {"passed": True},
        "out_of_sample": {"passed": True},
        "purged_cross_validation": {"passed": True},
        "accounting_identity": [{"residual": 0.0}],
        "fill_ledger": [{
            "feature_available_time": "2026-01-01T00:00:00+00:00",
            "decision_time": "2026-01-01T00:00:00+00:00",
            "order_time": "2026-01-01T00:00:00+00:00",
            "broker_receive_time": "2026-01-02T00:00:00+00:00",
            "fill_time": "2026-01-02T00:00:00+00:00",
        }],
        "live_execution_evidence_eligible": False,
        "period_returns": period_returns,
        "return_timestamps": return_timestamps,
        "benchmark_observations": benchmark_observations,
        "risk_free_observations": risk_free_observations,
        "benchmark_metrics": evaluate_benchmark_metrics(
            period_returns,
            benchmark_observations,
            risk_free_observations,
            evaluation_timestamps=return_timestamps,
        ),
        "monte_carlo_tail_risk": simulate_tail_risk(period_returns),
        "regime_observations": regime_observations,
        "regime_robustness": evaluate_regime_robustness(period_returns, regime_observations),
        "historical_universe": historical_universe,
        "universe_membership_rows": universe_membership_rows,
        "strategy_version_hash": "a" * 64,
        "data_version_hash": "b" * 64,
    }
    rollback = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(lab.store.path))
    gate = PromotionGate(lab.store, artifact_rollback=rollback)

    evaluated = gate.evaluate(proposal["proposal_id"], evidence, {"approved": True})
    assert evaluated["proposal"]["status"] == "evaluated"
    assert gate.begin_shadow(proposal["proposal_id"])["status"] == "shadow"
    shadow = gate.record_shadow(
        proposal["proposal_id"],
        sample_size=20,
        return_pct=2.5,
        max_drawdown_pct=4.0,
        risk_violations=0,
    )
    assert shadow["shadow"]["passed"] is True
    with pytest.raises(PermissionError, match="human_approval"):
        gate.promote(proposal["proposal_id"], approved_by="agent")

    original_save_artifact = lab.store.save_strategy_artifact
    monkeypatch.setattr(
        lab.store,
        "save_strategy_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("artifact_store_failed")),
    )
    with pytest.raises(RuntimeError, match="artifact_store_failed"):
        gate.promote(proposal["proposal_id"], approved_by="human-reviewer")
    assert gate.proposals.get(proposal["proposal_id"])["status"] == "shadow"
    with sqlite3.connect(lab.store.path) as conn:
        assert conn.execute("select count(*) from strategy_versions").fetchone()[0] == 0
        assert conn.execute("select count(*) from strategy_artifacts").fetchone()[0] == 0
    monkeypatch.setattr(lab.store, "save_strategy_artifact", original_save_artifact)

    promoted = gate.promote(proposal["proposal_id"], approved_by="human-reviewer")
    assert promoted["active"] is True
    assert promoted["proposal"]["status"] == "promoted"
    artifact = promoted["strategy_artifact"]
    assert artifact["artifact_kind"] == "approved_rule"
    assert artifact["approval_status"] == "human_approved_paper"
    assert artifact["production_eligible"] is True
    assert artifact["execution_evidence_eligible"] is False
    assert artifact["approval_receipt"]["version_id"] == promoted["version_id"]
    assert artifact["registry"]["saved"] is True
    assert promoted["governance"]["artifact_rollback_wired"] is True
    assert promoted["governance"]["active_artifact_id"] == artifact["artifact_id"]
    restarted = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(lab.store.path))
    assert restarted.current_artifact_id == artifact["artifact_id"]
    assert lab.store.strategy_artifacts(artifact["strategy_id"])[0]["artifact_id"] == artifact["artifact_id"]
