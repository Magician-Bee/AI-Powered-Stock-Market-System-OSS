from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.strategy.strategy_engine import StrategyEngine
from open_stock_ai.strategy.strategy_registry import StrategyArtifactRegistry
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest


def test_baseline_strategy_artifact_is_persisted_once_and_cannot_be_rewritten(tmp_path):
    store = SQLiteStore(tmp_path / "strategy.sqlite")
    registry = StrategyArtifactRegistry(store=store)

    first = registry.baseline_artifact()
    second = registry.baseline_artifact()

    assert first["artifact_kind"] == "baseline_rule"
    assert first["approval_status"] == "baseline_only"
    assert first["production_eligible"] is False
    assert first["registry"]["saved"] is True
    assert second["registry"]["already_exists"] is True
    assert len(store.strategy_artifacts()) == 1
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="strategy artifacts are immutable"):
            conn.execute("update strategy_artifacts set approval_status='approved' where artifact_id=?", (first["artifact_id"],))


def test_default_strategy_signal_carries_explicit_baseline_artifact(tmp_path):
    engine = StrategyEngine(artifact_registry=StrategyArtifactRegistry(store=SQLiteStore(tmp_path / "strategy.sqlite")))
    signal = engine.generate_signal(
        StockRequest(symbol="2330.TW", market="TW"),
        MarketSnapshot(symbol="2330.TW", market="TW", price=100.0),
        IntelligenceResult(
            symbol="2330.TW", market="TW", summary="baseline",
            technical_view="uptrend", fundamental_view="positive",
        ),
    )

    artifact = signal.decision_schema["strategy_artifact"]
    assert signal.rule_set_id == "open_stock_ai.strategy_v2"
    assert artifact["strategy_id"] == signal.rule_set_id
    assert artifact["artifact_kind"] == "baseline_rule"
    assert artifact["promotion_boundary"] == "requires_exact_evaluation_shadow_and_human_approval"
