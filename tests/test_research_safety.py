from __future__ import annotations

import json
from dataclasses import dataclass

from open_stock_ai.data.data_quality import source_envelope
from open_stock_ai.external_sources.registry import EXPECTED_REPOSITORY_LOCKS, ExternalProjectRegistry
from open_stock_ai.pipeline import SignalPipeline
from open_stock_ai.research.backtest_research import BacktestResearch
from open_stock_ai.research.factor_research import FactorResearch
from open_stock_ai.types import (
    ExecutionResult,
    IntelligenceResult,
    MarketSnapshot,
    ResearchResult,
    RiskDecision,
    StockRequest,
    TradingSignal,
)


def _signal(action: str = "buy") -> TradingSignal:
    return TradingSignal(
        symbol="2330.TW",
        market="TW",
        action=action,  # type: ignore[arg-type]
        confidence=0.82,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        target_price=110.0,
        stop_loss=95.0,
        position_size_pct=5.0,
    )


def _history_snapshot(points: int = 80) -> MarketSnapshot:
    closes = [100.0 + index * 0.4 + (index % 7 - 3) * 0.15 for index in range(points)]
    return MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=closes[-1],
        ohlcv=[{"date": f"2026-01-{index + 1:02d}", "close": value} for index, value in enumerate(closes)],
        raw={
            "data_contract": {
                "schema_version": "open_stock_ai.market_data_contract.v2",
                "decision_ready": True,
                "execution_eligible": True,
                "blockers": [],
            }
        },
    )


def test_backtest_is_past_only_and_never_execution_evidence():
    request = StockRequest(symbol="2330.TW", market="TW")
    snapshot = _history_snapshot()
    research = BacktestResearch()

    buy_result = research.evaluate(request, _signal("buy"), snapshot)
    sell_result = research.evaluate(request, _signal("sell"), snapshot)

    assert buy_result["lookahead_safe"] is True
    assert buy_result["signal_shift_periods"] == 1
    assert buy_result["transaction_costs_included"] is False
    assert buy_result["cost_model"]["mode"] == "unavailable_without_point_in_time_execution_context"
    assert "transaction_cost_schedule_missing" in buy_result["approval_blockers"]
    assert buy_result["strategy_replay_exact"] is False
    assert buy_result["empirical_valid"] is False
    assert buy_result["execution_evidence_eligible"] is False
    assert buy_result["passed"] is False
    assert "point_in_time_dataset_missing" in buy_result["approval_blockers"]
    assert buy_result["strategy_return_pct"] == sell_result["strategy_return_pct"]
    assert buy_result["benchmark_return_pct"] == sell_result["benchmark_return_pct"]
    assert buy_result["performance_metrics"]["schema_version"] == "open_stock_ai.performance_metrics.v1"
    assert buy_result["strategy_return_pct"] == buy_result["performance_metrics"]["compounded_return_pct"]
    assert buy_result["performance_metrics"]["average_turnover_pct"] is not None
    assert buy_result["statistical_significance"]["schema_version"] == "open_stock_ai.statistical_significance.v1"
    assert buy_result["regime_robustness"]["schema_version"] == "open_stock_ai.regime_robustness.v1"
    assert buy_result["regime_robustness"]["passed"] is False
    assert buy_result["historical_universe"]["schema_version"] == "open_stock_ai.historical_universe.v1"
    assert buy_result["historical_universe"]["passed"] is False
    assert buy_result["universe_membership_rows"] == []


def test_ohlcv_baseline_never_applies_generic_bps_as_a_production_cost_schedule():
    request = StockRequest(symbol="2330.TW", market="TW")
    snapshot = _history_snapshot()
    default_result = BacktestResearch().evaluate(request, _signal("buy"), snapshot)
    sensitivity_result = BacktestResearch(transaction_cost_bps=20.0).evaluate(
        request, _signal("buy"), snapshot
    )

    assert sensitivity_result["transaction_costs_included"] is False
    assert sensitivity_result["cost_model"]["execution_evidence_eligible"] is False
    assert sensitivity_result["strategy_return_pct"] == default_result["strategy_return_pct"]


def test_factor_projection_is_advisory_not_trained_qlib():
    result = FactorResearch().evaluate(
        StockRequest(symbol="2330.TW", market="TW"),
        _signal("buy"),
        _history_snapshot(),
    )

    assert result["advisory_ready"] is True
    assert result["runtime_connected"] is False
    assert result["empirical_valid"] is False
    assert result["execution_evidence_eligible"] is False
    assert result["rank_ic_method"] == "single_symbol_forward_return_proxy"
    assert result["rank_ic_is_cross_sectional"] is False
    assert "not a cross-sectional Rank IC" in result["rank_ic_warning"]
    assert "qlib_runtime_not_connected" in result["approval_blockers"]


def test_source_envelope_tracks_time_and_blocks_auxiliary_price_source():
    envelope = source_envelope(
        source_key="yahoo",
        source_name="Yahoo Finance",
        symbol="AAPL",
        market="US",
        role="market_price_history_news",
        payload={"price": 200.0},
        available_at="2026-07-14T01:00:00+00:00",
        received_at="2026-07-14T01:00:05+00:00",
    )

    assert envelope["schema_version"] == "open_stock_ai.data_source_envelope.v1"
    assert envelope["extended_schema_version"] == "open_stock_ai.data_source_envelope.v2"
    assert envelope["source_tier"] == 3
    assert envelope["freshness_seconds"] == 5.0
    assert envelope["decision_eligible"] is False
    assert "non_primary_source" in envelope["decision_blockers"]


@dataclass
class _MarketData:
    def load(self, request: StockRequest) -> MarketSnapshot:
        return _history_snapshot()


@dataclass
class _Intelligence:
    def analyze(self, request: StockRequest, snapshot: MarketSnapshot) -> IntelligenceResult:
        return IntelligenceResult(
            symbol=request.symbol,
            market=request.market,
            summary="test intelligence",
            sentiment_score=0.5,
            fundamental_view="positive",
            technical_view="uptrend",
        )


@dataclass
class _Strategy:
    def generate_signal(
        self,
        request: StockRequest,
        market_snapshot: MarketSnapshot,
        intelligence: IntelligenceResult,
    ) -> TradingSignal:
        return _signal("buy")


@dataclass
class _Research:
    def validate(
        self,
        request: StockRequest,
        signal: TradingSignal,
        market_snapshot: MarketSnapshot,
        intelligence: IntelligenceResult,
    ) -> ResearchResult:
        return ResearchResult(passed=True, summary="test", sharpe=2.0, max_drawdown_pct=3.0)


@dataclass
class _Risk:
    def evaluate(self, request, signal, research, paper_exposure=None) -> RiskDecision:
        return RiskDecision(
            approved=True,
            reason="paper approved",
            max_position_size_pct=10.0,
            adjusted_position_size_pct=5.0,
        )


@dataclass
class _Execution:
    called: bool = False

    def execute_paper(self, request, signal, risk) -> ExecutionResult:
        self.called = True
        return ExecutionResult(executed=True, mode="paper", order_id="PAPER-TEST")


@dataclass
class _PendingExecution:
    def execute_paper(self, request, signal, risk) -> ExecutionResult:
        return ExecutionResult(executed=False, mode="paper_pending_oms", order_id="PAPER-TEST")


@dataclass
class _TradeStoreReceipt:
    receipt: dict

    def exposure(self):
        return {"total_position_size_pct": 0.0, "symbols": {}}

    def save_paper_order(self, payload):
        return self.receipt


def test_agent_analysis_mode_never_calls_execution_engine():
    execution = _Execution()
    pipeline = SignalPipeline(
        market_data=_MarketData(),  # type: ignore[arg-type]
        intelligence=_Intelligence(),  # type: ignore[arg-type]
        strategy=_Strategy(),  # type: ignore[arg-type]
        research=_Research(),  # type: ignore[arg-type]
        risk=_Risk(),  # type: ignore[arg-type]
        execution=execution,  # type: ignore[arg-type]
    )

    decision = pipeline.run(StockRequest(symbol="2330.TW", market="TW"), execute=False)

    assert execution.called is False
    assert decision.risk.approved is True
    assert decision.execution.executed is False
    assert decision.execution.ledger["analysis_only"] is True
    assert decision.execution.reason == "Agent analysis mode: no paper order was submitted."


def _paper_pipeline(receipt: dict) -> SignalPipeline:
    return SignalPipeline(
        market_data=_MarketData(),  # type: ignore[arg-type]
        intelligence=_Intelligence(),  # type: ignore[arg-type]
        strategy=_Strategy(),  # type: ignore[arg-type]
        research=_Research(),  # type: ignore[arg-type]
        risk=_Risk(),  # type: ignore[arg-type]
        execution=_PendingExecution(),  # type: ignore[arg-type]
        trade_store=_TradeStoreReceipt(receipt),  # type: ignore[arg-type]
    )


def test_pipeline_marks_execution_true_only_from_authoritative_oms_fill_receipt():
    decision = _paper_pipeline(
        {
            "saved": True,
            "oms": {
                "schema_version": "open_stock_ai.paper_oms_result.v1",
                "filled": True,
                "order": {"order_id": "PAPER-TEST", "status": "filled"},
                "fill": {"fill_id": "PF-1", "order_id": "PAPER-TEST", "quantity": 10},
            },
        }
    ).run(StockRequest(symbol="2330.TW", market="TW"), execute=True)

    assert decision.execution.executed is True
    assert decision.execution.mode == "paper"
    assert decision.execution.ledger["oms"]["fill"]["fill_id"] == "PF-1"


def test_pipeline_keeps_execution_false_for_oms_reject_or_missing_receipt():
    rejected = _paper_pipeline(
        {
            "saved": True,
            "oms": {
                "schema_version": "open_stock_ai.paper_oms_result.v1",
                "filled": False,
                "order": {"status": "rejected", "rejection_reason": "risk_recheck_failed"},
            },
        }
    ).run(StockRequest(symbol="2330.TW", market="TW"), execute=True)
    missing = _paper_pipeline({"saved": True}).run(
        StockRequest(symbol="2330.TW", market="TW"), execute=True
    )

    assert rejected.execution.executed is False
    assert rejected.execution.reason.endswith("risk_recheck_failed.")
    assert missing.execution.executed is False
    assert missing.execution.reason.endswith("paper_oms_rejected.")


def test_pipeline_rejects_forged_or_order_mismatched_oms_fill_receipts():
    forged = _paper_pipeline(
        {
            "saved": True,
            "oms": {
                "schema_version": "open_stock_ai.paper_oms_result.v1",
                "filled": True,
                "order": {"order_id": "OTHER", "status": "filled"},
                "fill": {"fill_id": "PF-X", "order_id": "OTHER", "quantity": 10},
            },
        }
    ).run(StockRequest(symbol="2330.TW", market="TW"), execute=True)

    assert forged.execution.executed is False


def test_pipeline_discards_preclaimed_execution_without_oms_receipt():
    pipeline = SignalPipeline(
        market_data=_MarketData(),  # type: ignore[arg-type]
        intelligence=_Intelligence(),  # type: ignore[arg-type]
        strategy=_Strategy(),  # type: ignore[arg-type]
        research=_Research(),  # type: ignore[arg-type]
        risk=_Risk(),  # type: ignore[arg-type]
        execution=_Execution(),  # type: ignore[arg-type]
    )

    decision = pipeline.run(StockRequest(symbol="2330.TW", market="TW"), execute=True)

    assert decision.execution.executed is False
    assert decision.execution.mode == "paper_pending_oms"


def test_external_registry_accepts_matching_snapshot_marker(tmp_path):
    spec = EXPECTED_REPOSITORY_LOCKS["qlib"]
    project = tmp_path / "external" / "qlib"
    project.mkdir(parents=True)
    (project / ".source-lock.json").write_text(
        json.dumps(
            {
                "schema_version": "open_stock_ai.external_snapshot_lock.v1",
                "source_key": "qlib",
                "origin": spec["origin"],
                "branch": spec["branch"],
                "head": spec["head"],
                "snapshot_mode": "vendored_source_snapshot",
            }
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text("# qlib\n", encoding="utf-8")
    (project / "LICENSE").write_text("MIT License\n", encoding="utf-8")

    profile = ExternalProjectRegistry(project_paths={"qlib": str(project)}, root=tmp_path).profile("qlib")

    assert profile.exists is True
    assert profile.origin_verified is True
    assert profile.branch_verified is True
    assert profile.head_verified is True
    assert profile.lock_verified is True
    assert profile.lock_evidence_kind == "vendored_source_snapshot"
    assert profile.lock_evidence_path == "external/qlib/.source-lock.json"
    assert profile.clone_command is not None
    assert "--no-checkout" in profile.clone_command
    assert spec["head"] in profile.clone_command
