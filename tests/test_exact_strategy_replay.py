from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest
from hypothesis import given, settings, strategies as st

from open_stock_ai.research.cost_model import (
    PointInTimeMarketImpactModel,
    TaiwanMarketImpactRulebook,
    TaiwanSecondaryMarketImpactSchedule,
    TaiwanSecondaryMarketCostSchedule,
    UnsupportedCostSchedule,
)
from open_stock_ai.research.strategy_replay import ExactStrategyReplay
from open_stock_ai.strategy.strategy_engine import StrategyEngine
from open_stock_ai.types import StockRequest, TradingSignal


_PROPERTY_SETTINGS = settings(derandomize=True, max_examples=50, deadline=None)


def _signal() -> TradingSignal:
    return TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="hold",
        confidence=0.5,
        horizon="swing",
        reason="current",
    )


class _ExplicitPolicyFixture:
    def __init__(self, instructions):
        self.instructions = instructions

    def generate_signal(self, request, market_snapshot, intelligence):
        instruction = self.instructions.get(market_snapshot.raw["as_of"], {"action": "hold"})
        return TradingSignal(symbol=request.symbol, market=request.market, horizon=request.horizon,
                             confidence=1.0, reason="explicit policy fixture", **instruction)


def _flat_policy_replay(instructions, changes=None):
    rows = _rows(9)
    for row in rows:
        row.update(open=100.0, high=100.0, low=100.0, close=100.0, volume=100_000,
                   spread_bps=0.0, product_type="bond_etf", bond_etf_tax_exemption_eligible=True,
                   broker_commission_bps=0.0, broker_minimum_commission_twd=0.0)
    for index, change in (changes or {}).items():
        rows[index].update(change)
    strategy = _ExplicitPolicyFixture({rows[i]["timestamp"]: v for i,v in instructions.items()})
    engine = ExactStrategyReplay(strategy=strategy, initial_cash=10_000, feature_lookback=3,
                                 impact_coefficient_bps=0.0, slippage_bps=0.0)
    normalized, blockers = engine._normalize_rows(rows)
    assert not blockers
    return engine._replay_slice(StockRequest(symbol="2330.TW"), normalized, 3, 9)


def test_replay_freezes_decision_quantity_and_hold_never_spends_remaining_cash():
    replay = _flat_policy_replay({3:{"action":"buy","position_size_pct":10,"stop_loss":20}},
                                {4:{"open":50,"low":40}})
    assert [(f["side"],f["quantity"]) for f in replay["fill_ledger"]] == [("buy",10)]
    assert replay["position_ledger"][-1]["quantity"] == 10
    assert replay["cash_ledger"][-1]["balance"] == 9500
    assert replay["accounting_verified"]


def test_replay_reduce_sells_explicit_size_and_preserves_remainder():
    replay = _flat_policy_replay({3:{"action":"buy","position_size_pct":10,"stop_loss":90},
                                 4:{"action":"reduce","position_size_pct":5}})
    assert [(f["side"],f["quantity"]) for f in replay["fill_ledger"]] == [("buy",10),("sell",5)]
    assert replay["position_ledger"][-1]["quantity"] == 5


@pytest.mark.parametrize("bar,expected_price,reason", [
    ({"open":85,"high":95,"low":80,"close":85},85,"stop_loss"),
    ({"open":100,"high":125,"low":85,"close":100},90,"stop_loss"),
    ({"open":130,"high":135,"low":125,"close":130},120,"target_price"),
])
def test_replay_protective_exit_uses_declared_levels_with_cost_ledger(bar,expected_price,reason):
    replay = _flat_policy_replay({3:{"action":"buy","position_size_pct":10,
                                     "stop_loss":90,"target_price":120}}, {5:bar})
    fills = replay["fill_ledger"]
    assert len(fills)==2
    assert fills[1]["reference_price"]==expected_price
    assert fills[1]["protective_exit"]["reason"]==reason
    assert "venue_cost" in fills[1]["cost_model"]
    assert replay["position_ledger"][-1]["quantity"]==0
    assert replay["accounting_verified"] and replay["lifecycle_verified"]


def test_add_cannot_loosen_the_original_entry_lot_stop():
    replay = _flat_policy_replay({3:{"action":"buy","position_size_pct":10,"stop_loss":90},
                                 4:{"action":"add","position_size_pct":10,"stop_loss":80}},
                                {6:{"open":95,"high":100,"low":85,"close":95}})
    assert [(f["side"],f["quantity"]) for f in replay["fill_ledger"]] == [("buy",10),("buy",10),("sell",10)]
    assert replay["position_ledger"][-1]["quantity"] == 10
    assert replay["accounting_verified"]


def test_strategy_hash_covers_dependencies_configuration_and_execution_contract():
    engine=ExactStrategyReplay()
    manifest=engine.strategy_version_manifest()
    assert "strategy/structured_decision.py" in manifest["sources"]
    assert "strategy/target_stop_calibration.py" in manifest["sources"]
    assert "strategy/execution_policy.py" in manifest["sources"]
    assert "cost_model.py" in manifest["replay_sources"]
    assert engine.strategy_version_hash()!=ExactStrategyReplay(latency_bars=2).strategy_version_hash()
    assert engine.strategy_version_hash()!=ExactStrategyReplay(slippage_bps=20).strategy_version_hash()


def test_partial_stop_exit_keeps_residual_intent_after_price_recovers():
    replay=_flat_policy_replay({3:{"action":"buy","position_size_pct":10,"stop_loss":90}},
                              {5:{"open":85,"high":95,"low":80,"close":85,"volume":4}})
    fills=replay["fill_ledger"]
    assert [(f["side"],f["quantity"]) for f in fills] == [("buy",10),("sell",4),("sell",6)]
    assert fills[2]["reference_price"]==100  # exit stays pending despite rebound
    assert fills[2]["protective_exit"]["residual_market_exit"] is True
    assert replay["position_ledger"][-1]["quantity"]==0
    assert replay["accounting_verified"]


def _rows(count: int = 80):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for index in range(count):
        phase_up = (index // 10) % 2 == 0
        price *= 1.01 if phase_up else 0.99
        timestamp = start + timedelta(days=index)
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "event_time": timestamp.isoformat(),
                "published_at": timestamp.isoformat(),
                "available_at": timestamp.isoformat(),
                "effective_at": timestamp.isoformat(),
                "ingested_at": timestamp.isoformat(),
                "open": price * 0.999,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000 + index,
                "spread_bps": 8.0,
                "venue": "TWSE",
                "product_type": "stock",
                "lot_type": "board_lot",
                "broker_commission_bps": 8.55,
                "broker_minimum_commission_twd": 20.0,
                "broker_fee_schedule_id": "test-broker-fee-schedule-v1",
                "exchange_fee_bps": 0.0,
                "exchange_fee_schedule_id": "test-no-customer-pass-through-v1",
                "benchmark_observation": {
                    "timestamp": timestamp.isoformat(),
                    "available_at": timestamp.isoformat(),
                    "return": 0.0012 + (index % 5) * 0.00015,
                    "benchmark_id": "TWII",
                    "source_id": "twse_official_index",
                    "dataset_id": "twse_index_daily",
                    "revision_id": hashlib.sha256(b"twse-index-daily-revision-v1").hexdigest(),
                },
                "risk_free_observation": {
                    "timestamp": timestamp.isoformat(),
                    "available_at": timestamp.isoformat(),
                    "return": 0.00004 + (index % 3) * 0.00001,
                    "cash_rate_id": "twd_overnight_cash_rate",
                    "source_id": "central_bank_taiwan",
                    "dataset_id": "twd_cash_rate_daily",
                    "revision_id": hashlib.sha256(b"twd-cash-rate-revision-v1").hexdigest(),
                },
                "universe_membership": {
                    "schema_version": "open_stock_ai.historical_universe.v1",
                    "as_of": timestamp.isoformat(),
                    "required_entity_id": "EQ-2330",
                    "required_entity_in_universe": True,
                    "universe_scope_id": "taiwan-listed-and-otc-securities",
                    "universe_coverage_complete": True,
                    "point_in_time_verified": True,
                    "passed": True,
                    "manifest_hash": hashlib.sha256(
                        f"universe-{index}-{timestamp.isoformat()}".encode("utf-8")
                    ).hexdigest(),
                    "blockers": [],
                },
                "pit_intelligence": {
                    "summary": "PIT",
                    "sentiment_score": 0.0,
                    "fundamental_view": "positive" if phase_up else "negative",
                    "technical_view": "uptrend" if phase_up else "downtrend",
                    "evidence": [{"available_at": timestamp.isoformat()}],
                },
                "feature_records": [{
                    "feature_id": "intelligence.v1",
                    "event_time": timestamp.isoformat(),
                    "published_at": timestamp.isoformat(),
                    "available_at": timestamp.isoformat(),
                    "effective_at": timestamp.isoformat(),
                    "ingested_at": timestamp.isoformat(),
                    "production_contract_covered": True,
                    "availability_contract_sha256": hashlib.sha256(
                        b"test-feature-availability-contract-v1"
                    ).hexdigest(),
                }],
            }
        )
    return rows


def test_exact_replay_uses_production_strategy_with_walk_forward_and_version_hashes():
    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _signal(),
        _rows(),
    )

    # The ledger and lifecycle are exact, but the committed impact policy is a
    # transparent research baseline rather than an empirical fill calibration.
    # It must not silently become execution evidence.
    assert result["strategy_replay_exact"] is False
    assert result["empirical_valid"] is False
    assert result["passed"] is False
    assert result["execution_evidence_eligible"] is False
    assert result["lookahead_safe"] is True
    assert result["accounting_identity"]
    assert result["accounting_verified"] is True
    assert result["lifecycle_verified"] is True
    assert all(abs(item["residual"]) <= 1e-6 for item in result["accounting_identity"])
    assert result["transaction_costs_included"] is True
    assert result["cost_schedule_verified"] is False
    assert result["slippage_included"] is True
    assert result["walk_forward"]["folds"]
    assert len(result["purged_cross_validation"]["folds"]) == 2
    assert len(result["strategy_version_hash"]) == 64
    assert len(result["data_version_hash"]) == 64
    assert result["decision_audit"][0]["execution_time"] > result["decision_audit"][0]["signal_time"]
    assert len(result["decision_audit"]) <= len(result["order_ledger"])
    assert len(result["order_ledger"]) == len(result["broker_receipt_ledger"])
    orders = {item["order_id"]: item for item in result["order_ledger"]}
    receipts = {item["order_id"]: item for item in result["broker_receipt_ledger"]}
    for decision in result["decision_audit"]:
        order = orders[decision["order_id"]]
        receipt = receipts[decision["order_id"]]
        assert order["decision_id"] == decision["decision_id"]
        assert receipt["broker_receipt_id"] == decision["broker_receipt_id"]
        assert order["fill_ids"] == decision["fill_ids"]
    assert result["walk_forward"]["folds"][0]["fitted_policy"]["training_only"] is True
    assert result["purged_cross_validation"]["folds"][0]["embargo_verified"] is True
    assert result["historical_universe"]["passed"] is True
    assert result["historical_universe"]["universe_coverage_complete"] is True
    assert result["benchmark_metrics"]["alignment_verified"] is True
    assert result["benchmark_metrics"]["benchmark"]["benchmark_id"] == "TWII"
    assert result["monte_carlo_tail_risk"]["scenario_count"] == 2000
    assert "venue_product_date_or_broker_cost_schedule_not_verified" in result["approval_blockers"]
    assert result["cost_model"]["market_impact_schedule"]["snapshot_id"] == "taiwan-secondary-market-impact-2026-08-13"
    metrics = result["performance_metrics"]
    assert metrics["schema_version"] == "open_stock_ai.performance_metrics.v1"
    assert result["sharpe"] == float(metrics["sharpe"] or 0.0)
    assert result["trade_count"] == 0  # production baseline explicitly declares zero sizing
    assert result["fill_ledger"] == []
    assert result["max_drawdown_pct"] == metrics["max_drawdown_pct"]
    assert result["strategy_return_pct"] == metrics["compounded_return_pct"]
    assert metrics["exposure_pct"] is not None
    assert metrics["tail_loss_pct"] is not None


def test_exact_replay_rejects_rows_without_known_historical_universe_membership():
    rows = _rows()
    rows[12].pop("universe_membership")

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _signal(),
        rows,
    )

    assert result["strategy_replay_exact"] is False
    assert result["empirical_valid"] is False
    assert any(item.startswith("historical_universe_membership_missing:12") for item in result["approval_blockers"])


def test_exact_replay_rejects_missing_independent_benchmark_or_cash_rate_contract():
    rows = _rows()
    rows[50].pop("benchmark_observation")
    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _signal(),
        rows,
    )

    assert result["benchmark_metrics"]["passed"] is False
    assert "benchmark_observation_invalid:10" in result["benchmark_metrics"]["blockers"]
    assert "benchmark_and_cash_rate_alignment_gate_failed" in result["approval_blockers"]


def test_exact_replay_rejects_universe_receipt_without_complete_scope_or_sha256():
    rows = _rows()
    rows[12]["universe_membership"]["universe_coverage_complete"] = False
    rows[13]["universe_membership"]["manifest_hash"] = "short"

    result = ExactStrategyReplay().evaluate(
        StockRequest(symbol="2330.TW", market="TW"),
        _signal(),
        rows,
    )

    assert result["strategy_replay_exact"] is False
    assert result["historical_universe"]["passed"] is False
    assert any(item.startswith("historical_universe_membership_unverified:12") for item in result["approval_blockers"])
    assert any(item.startswith("historical_universe_membership_unverified:13") for item in result["approval_blockers"])


def test_exact_replay_blocks_late_or_missing_point_in_time_intelligence():
    rows = _rows(45)
    rows[10]["available_at"] = (datetime.fromisoformat(rows[10]["timestamp"]) + timedelta(days=1)).isoformat()
    rows[11].pop("pit_intelligence")

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _signal(),
        rows,
    )

    assert result["strategy_replay_exact"] is False
    assert result["execution_evidence_eligible"] is False
    assert any(item.startswith("feature_available_after_signal_time") for item in result["approval_blockers"])
    assert any(item.startswith("point_in_time_intelligence_missing") for item in result["approval_blockers"])


def test_exact_replay_rejects_feature_ingested_after_its_decision_time():
    rows = _rows(45)
    late_ingestion = datetime.fromisoformat(rows[12]["timestamp"]) + timedelta(seconds=1)
    rows[12]["feature_records"][0]["ingested_at"] = late_ingestion.isoformat()

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert result["strategy_replay_exact"] is False
    assert result["research_certification"]["certified"] is False
    assert "feature_ingested_after_signal_time:12:0" in result["approval_blockers"]


def test_exact_replay_rejects_future_dated_intelligence_evidence_actually_consumed_by_strategy():
    rows = _rows(45)
    rows[12]["pit_intelligence"]["evidence"][0]["available_at"] = "2099-01-01T00:00:00+00:00"

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert result["strategy_replay_exact"] is False
    assert result["lifecycle_verified"] is False
    assert "point_in_time_intelligence_evidence_after_signal_time:12:0" in result["approval_blockers"]


@pytest.mark.parametrize(
    ("leak_kind", "expected_blocker"),
    [
        ("row_available_after_decision", "feature_available_after_signal_time:12"),
        ("row_ingested_after_decision", "feature_ingested_after_signal_time:12"),
        ("row_event_after_decision", "invalid_feature_temporal_order:12"),
        ("row_effective_after_decision", "invalid_feature_temporal_order:12"),
        ("row_publication_after_availability", "invalid_feature_publication_order:12"),
        ("nested_available_after_decision", "feature_available_after_signal_time:12:0"),
        ("nested_ingested_after_decision", "feature_ingested_after_signal_time:12:0"),
        ("nested_event_after_decision", "invalid_feature_temporal_order:12:0"),
        ("nested_effective_after_decision", "invalid_feature_temporal_order:12:0"),
        ("nested_publication_after_availability", "invalid_feature_publication_order:12:0"),
        (
            "intelligence_evidence_after_decision",
            "point_in_time_intelligence_evidence_after_signal_time:12:0",
        ),
        (
            "future_historical_universe_membership",
            "historical_universe_membership_timestamp_mismatch:12",
        ),
    ],
)
def test_adversarial_temporal_leakage_matrix_cannot_become_execution_evidence(
    leak_kind: str, expected_blocker: str
):
    """Every decision-time contract axis must fail closed under future leakage."""

    rows = _rows(45)
    row = rows[12]
    future_time = (datetime.fromisoformat(row["timestamp"]) + timedelta(seconds=1)).isoformat()
    feature = row["feature_records"][0]
    if leak_kind == "row_available_after_decision":
        row["available_at"] = future_time
    elif leak_kind == "row_ingested_after_decision":
        row["ingested_at"] = future_time
    elif leak_kind == "row_event_after_decision":
        row["event_time"] = future_time
    elif leak_kind == "row_effective_after_decision":
        row["effective_at"] = future_time
    elif leak_kind == "row_publication_after_availability":
        row["published_at"] = future_time
    elif leak_kind == "nested_available_after_decision":
        feature["available_at"] = future_time
    elif leak_kind == "nested_ingested_after_decision":
        feature["ingested_at"] = future_time
    elif leak_kind == "nested_event_after_decision":
        feature["event_time"] = future_time
    elif leak_kind == "nested_effective_after_decision":
        feature["effective_at"] = future_time
    elif leak_kind == "nested_publication_after_availability":
        feature["published_at"] = future_time
    elif leak_kind == "intelligence_evidence_after_decision":
        row["pit_intelligence"]["evidence"][0]["available_at"] = future_time
    elif leak_kind == "future_historical_universe_membership":
        row["universe_membership"]["as_of"] = future_time
    else:  # pragma: no cover - protects this matrix when adding a new case.
        raise AssertionError(leak_kind)

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert expected_blocker in result["approval_blockers"]
    assert result["research_certification"]["certified"] is False
    assert result["strategy_replay_exact"] is False
    assert result["execution_evidence_eligible"] is False
    assert result["live_execution_evidence_eligible"] is False


def test_validation_folds_prove_train_test_information_is_disjoint_and_policy_is_frozen():
    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), _rows()
    )

    for fold in result["walk_forward"]["folds"] + result["purged_cross_validation"]["folds"]:
        assert fold["partition_verified"] is True
        assert fold["information_overlap_indices"] == []
        assert set(fold["training_indices"]).isdisjoint(fold["test_indices"])
        assert fold["training_data_hash"] != fold["test_data_hash"]
        assert fold["fitted_policy"]["training_data_hash"] == fold["training_data_hash"]
        assert fold["fitted_policy"]["fold_strategy_isolated"] is True
        assert len(fold["fitted_policy"]["fitted_policy_hash"]) == 64

    for fold in result["purged_cross_validation"]["folds"]:
        assert set(fold["train_indices"]).isdisjoint(fold["embargoed_indices"])
        assert fold["leakage_detected"] is False
        assert fold["embargo_verified"] is True


class _DeliberatelyLeakyReplay(ExactStrategyReplay):
    """Regression fixture that simulates an unsafe purge implementation.

    It deliberately returns every candidate training row, including rows whose
    feature lookback and forward label touch the test fold.  The certification
    gate must detect that overlap and reject the replay rather than reporting a
    false-safe exact result.
    """

    def _purged_training_indices(  # type: ignore[override]
        self,
        rows,
        *,
        candidate_indices,
        test_indices,
        embargoed_indices,
    ):
        return list(candidate_indices), []


def test_adversarial_leaky_partition_cannot_be_certified_as_exact_or_execution_evidence():
    result = _DeliberatelyLeakyReplay(
        min_points=40,
        feature_lookback=5,
        label_horizon_bars=2,
        embargo_bars=2,
    ).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"),
        _signal(),
        _rows(),
    )

    folds = result["walk_forward"]["folds"] + result["purged_cross_validation"]["folds"]
    assert any(fold["information_overlap_count"] > 0 for fold in folds)
    assert any(fold["partition_verified"] is False for fold in folds)
    assert any(fold["leakage_detected"] is True for fold in result["purged_cross_validation"]["folds"])
    assert result["purged_cross_validation"]["passed"] is False
    assert result["research_certification"]["certified"] is False
    assert result["strategy_replay_exact"] is False
    assert result["execution_evidence_eligible"] is False
    assert "purged_cross_validation_or_embargo_failed" in result["approval_blockers"]


class _WindowRecordingStrategy(StrategyEngine):
    def __init__(self) -> None:
        self.window_lengths: list[int] = []

    def generate_signal(self, request, market_snapshot, intelligence):  # type: ignore[no-untyped-def]
        self.window_lengths.append(len(market_snapshot.ohlcv))
        return super().generate_signal(request, market_snapshot, intelligence)


def test_replay_and_fit_never_expose_more_than_declared_feature_lookback():
    strategy = _WindowRecordingStrategy()
    engine = ExactStrategyReplay(strategy=strategy, min_points=40, feature_lookback=7)
    normalized, blockers = engine._normalize_rows(_rows())
    assert blockers == []
    engine._fit_policy(
        StockRequest(symbol="2330.TW", horizon="swing"),
        normalized,
        list(range(6, 35)),
        training_data_hash="training-fixture",
        strategy=strategy,
    )
    engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"), normalized, start=40, stop=55,
        strategy=strategy,
    )

    assert strategy.window_lengths
    assert max(strategy.window_lengths) == 7


class _GoldenGapStrategy:
    """A deterministic strategy used only by the independently calculated ledger."""

    def __init__(self, buy_at: str, sell_at: str) -> None:
        self.buy_at = buy_at
        self.sell_at = sell_at

    def generate_signal(self, request, market_snapshot, intelligence):  # type: ignore[no-untyped-def]
        as_of = market_snapshot.raw["as_of"]
        action = "buy" if as_of == self.buy_at else "sell" if as_of == self.sell_at else "hold"
        return TradingSignal(
            symbol=request.symbol,
            market=request.market,
            action=action,
            confidence=1.0,
            horizon=request.horizon,
            reason="hand-calculated golden ledger",
            position_size_pct=100.0,
            stop_loss=0.01,
        )


class _GoldenPartialFillStrategy:
    """Fixed decisions for a hand-calculated capacity-constrained ledger."""

    def __init__(self, actions_by_as_of: dict[str, str]) -> None:
        self.actions_by_as_of = actions_by_as_of

    def generate_signal(self, request, market_snapshot, intelligence):  # type: ignore[no-untyped-def]
        action = self.actions_by_as_of.get(market_snapshot.raw["as_of"], "hold")
        return TradingSignal(
            symbol=request.symbol,
            market=request.market,
            action=action,
            confidence=1.0,
            horizon=request.horizon,
            reason="hand-calculated partial-fill golden ledger",
            position_size_pct=100.0,
            stop_loss=0.01,
        )


def test_hand_calculated_golden_gap_ledger_matches_every_fill_cash_position_and_equity_value():
    rows = _rows(7)
    opens = (100.0, 100.0, 100.0, 100.0, 110.0, 90.0, 90.0)
    closes = (100.0, 100.0, 100.0, 100.0, 100.0, 90.0, 90.0)
    for row, open_price, close_price in zip(rows, opens, closes):
        row.update({
            # Open gaps exercise fill timing.  Flat closes through the buy
            # decision keep realized-volatility cost at zero for this pure
            # hand-calculated ledger fixture.
            "open": open_price,
            "high": max(open_price, close_price),
            "low": min(open_price, close_price),
            "close": close_price,
            "volume": 100,
            "spread_bps": 0.0,
            "venue": "TWSE",
            "product_type": "bond_etf",
            "lot_type": "board_lot",
            "broker_commission_bps": 0.0,
            "broker_minimum_commission_twd": 0.0,
            "broker_fee_schedule_id": "golden-zero-broker-fee-v1",
            "exchange_fee_bps": 0.0,
            "exchange_fee_schedule_id": "golden-zero-exchange-fee-v1",
            "bond_etf_tax_exemption_eligible": True,
        })
    strategy = _GoldenGapStrategy(rows[3]["timestamp"], rows[4]["timestamp"])
    engine = ExactStrategyReplay(
        strategy=strategy,  # type: ignore[arg-type]
        initial_cash=1_000.0,
        feature_lookback=3,
        latency_bars=1,
        impact_coefficient_bps=0.0,
        slippage_bps=0.0,
    )
    normalized, blockers = engine._normalize_rows(rows)

    assert blockers == []
    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"),
        normalized,
        start=3,
        stop=7,
    )

    # Independent hand calculation:
    # initial TWD 1,000; the next-open gap to 110 buys floor(1000 / 110)=9
    # shares for TWD 990.  The same bar closes at 100, then the following 90
    # open sells the 9 shares for TWD 810.
    assert [
        (fill["side"], fill["quantity"], fill["reference_price"], fill["fill_price"], fill["notional"], fill["fees"])
        for fill in replay["fill_ledger"]
    ] == [
        ("buy", 9, 110.0, 110.0, 990.0, 0.0),
        ("sell", 9, 90.0, 90.0, 810.0, 0.0),
    ]
    assert [(item["amount"], item["balance"]) for item in replay["cash_ledger"]] == [
        (1_000.0, 1_000.0),
        (-990.0, 10.0),
        (810.0, 820.0),
    ]
    assert [(item["quantity"], item["market_value"]) for item in replay["position_ledger"]] == [
        (0, 0.0),
        (9, 900.0),
        (0, 0.0),
        (0, 0.0),
    ]
    assert [(item["cash"], item["holdings_value"], item["equity"]) for item in replay["equity_ledger"]] == [
        (1_000.0, 0.0, 1_000.0),
        (10.0, 900.0, 910.0),
        (820.0, 0.0, 820.0),
        (820.0, 0.0, 820.0),
    ]
    assert replay["returns"] == pytest.approx([0.0, -0.09, -90.0 / 910.0, 0.0])
    assert [item["equity_change"] for item in replay["accounting_identity"]] == [0.0, -90.0, -90.0, 0.0]
    assert all(item["residual"] == 0.0 for item in replay["accounting_identity"])

    decisions = {item["order_id"]: item for item in replay["decision_audit"]}
    receipts = {item["order_id"]: item for item in replay["broker_receipt_ledger"]}
    for fill in replay["fill_ledger"]:
        decision = decisions[fill["order_id"]]
        receipt = receipts[fill["order_id"]]
        assert decision["fill_ids"] == [fill["fill_id"]]
        assert fill["broker_receipt_id"] == receipt["broker_receipt_id"]
        assert decision["feature_available_time"] <= decision["decision_time"]
        assert decision["decision_time"] <= decision["order_time"]
        assert decision["order_time"] <= receipt["received_at"] <= fill["fill_time"]


def test_hand_calculated_golden_ledger_includes_commission_exchange_fee_and_seller_tax():
    rows = _rows(7)
    for index, row in enumerate(rows):
        mark = 110.0 if index >= 5 else 100.0
        row.update(
            {
                "open": mark,
                "high": mark,
                "low": mark,
                "close": mark,
                "volume": 10_000,
                "spread_bps": 0.0,
                "venue": "TWSE",
                "product_type": "stock",
                "lot_type": "board_lot",
                "broker_commission_bps": 10.0,
                "broker_minimum_commission_twd": 0.0,
                "broker_fee_schedule_id": "golden-broker-10bps-v1",
                "exchange_fee_bps": 1.0,
                "exchange_fee_schedule_id": "golden-exchange-pass-through-1bps-v1",
            }
        )
    strategy = _GoldenGapStrategy(rows[3]["timestamp"], rows[4]["timestamp"])
    engine = ExactStrategyReplay(
        strategy=strategy,  # type: ignore[arg-type]
        initial_cash=10_000.0,
        feature_lookback=3,
        latency_bars=1,
        impact_coefficient_bps=0.0,
        slippage_bps=0.0,
    )
    normalized, blockers = engine._normalize_rows(rows)
    assert blockers == []

    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"), normalized, start=3, stop=7
    )

    assert [
        (item["side"], item["quantity"], item["notional"], item["fees"])
        for item in replay["fill_ledger"]
    ] == [
        ("buy", 99, 9_900.0, 10.89),
        ("sell", 99, 10_890.0, 44.65),
    ]
    assert replay["fill_ledger"][0]["cost_model"]["venue_cost"]["amounts"] == {
        "commission": 9.9,
        "exchange_fee": 0.99,
        "sell_tax": 0.0,
        "fees": 10.89,
    }
    assert replay["fill_ledger"][1]["cost_model"]["venue_cost"]["amounts"] == {
        "commission": 10.89,
        "exchange_fee": 1.09,
        "sell_tax": 32.67,
        "fees": 44.65,
    }
    assert replay["cash_ledger"][-1]["balance"] == pytest.approx(10_934.46)
    assert replay["equity_ledger"][-1]["equity"] == pytest.approx(10_934.46)
    assert replay["accounting_verified"] is True
    assert replay["lifecycle_verified"] is True


def test_hand_calculated_partial_fill_ledger_matches_taiwan_market_cost_rules():
    rows = _rows(9)
    for row in rows:
        row.update(
            {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                # A 50% participation cap makes each execution capacity two.
                "volume": 4,
                "spread_bps": 0.0,
                "venue": "TWSE",
                "product_type": "stock",
                "lot_type": "board_lot",
                "broker_commission_bps": 10.0,
                "broker_minimum_commission_twd": 0.0,
                "broker_fee_schedule_id": "golden-partial-broker-10bps-v1",
                "exchange_fee_bps": 1.0,
                "exchange_fee_schedule_id": "golden-partial-exchange-1bps-v1",
            }
        )
    strategy = _GoldenPartialFillStrategy(
        {
            rows[3]["timestamp"]: "buy",
            rows[4]["timestamp"]: "add",  # explicit add; hold must not buy more
            rows[5]["timestamp"]: "sell",
            rows[6]["timestamp"]: "sell",
        }
    )
    engine = ExactStrategyReplay(
        strategy=strategy,  # type: ignore[arg-type]
        initial_cash=1_000.0,
        feature_lookback=3,
        latency_bars=1,
        max_participation_rate=0.5,
        impact_coefficient_bps=0.0,
        slippage_bps=0.0,
    )
    normalized, blockers = engine._normalize_rows(rows)
    assert blockers == []

    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"), normalized, start=3, stop=9
    )

    # Independent calculation.  At TWD 100, buy costs are 10 bps commission
    # plus 1 bps exchange fee (TWD 0.22 per two shares); sell costs add the
    # 30 bps stock seller tax (TWD 0.82 per two shares).  The capacity limit
    # yields buy requests of 9 then 7, followed by sell requests of 4 then 2.
    assert [
        (fill["side"], fill["requested_quantity"], fill["quantity"], fill["notional"], fill["fees"])
        for fill in replay["fill_ledger"]
    ] == [
        ("buy", 9, 2, 200.0, 0.22),
        ("buy", 7, 2, 200.0, 0.22),
        ("sell", 4, 2, 200.0, 0.82),
        ("sell", 2, 2, 200.0, 0.82),
    ]
    assert [fill["partial_fill"] for fill in replay["fill_ledger"]] == [True, True, True, False]
    assert [
        (round(item["amount"], 2), round(item["balance"], 2))
        for item in replay["cash_ledger"]
    ] == [
        (1_000.0, 1_000.0),
        (-200.22, 799.78),
        (-200.22, 599.56),
        (199.18, 798.74),
        (199.18, 997.92),
    ]
    assert [(item["quantity"], item["market_value"]) for item in replay["position_ledger"]] == [
        (0, 0.0),
        (2, 200.0),
        (4, 400.0),
        (2, 200.0),
        (0, 0.0),
        (0, 0.0),
    ]
    assert [
        (round(item["cash"], 2), round(item["holdings_value"], 2), round(item["equity"], 2))
        for item in replay["equity_ledger"]
    ] == [
        (1_000.0, 0.0, 1_000.0),
        (799.78, 200.0, 999.78),
        (599.56, 400.0, 999.56),
        (798.74, 200.0, 998.74),
        (997.92, 0.0, 997.92),
        (997.92, 0.0, 997.92),
    ]
    assert [round(item["equity_change"], 2) for item in replay["accounting_identity"]] == [
        0.0,
        -0.22,
        -0.22,
        -0.82,
        -0.82,
        0.0,
    ]
    assert all(item["residual"] == 0.0 for item in replay["accounting_identity"])
    assert replay["accounting_verified"] is True
    assert replay["lifecycle_verified"] is True


@_PROPERTY_SETTINGS
@given(
    market_path=st.lists(
        st.tuples(
            st.floats(
                min_value=10.0,
                max_value=1_000.0,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.integers(min_value=1, max_value=100_000),
        ),
        min_size=7,
        max_size=18,
    ),
    participation=st.floats(
        min_value=0.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_event_replay_property_preserves_cash_position_equity_and_fill_invariants(
    market_path: list[tuple[float, int]], participation: float
) -> None:
    rows = _rows(len(market_path))
    for row, (price, volume) in zip(rows, market_path):
        row.update(
            {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "spread_bps": 0.0,
                "venue": "TPEX",
                "product_type": "stock",
                "lot_type": "odd_lot",
                "broker_commission_bps": 8.55,
                "broker_minimum_commission_twd": 0.0,
                "broker_fee_schedule_id": "property-broker-fee-v1",
                "exchange_fee_bps": 0.0,
                "exchange_fee_schedule_id": "property-no-pass-through-v1",
            }
        )
    strategy = _GoldenGapStrategy(rows[3]["timestamp"], rows[-2]["timestamp"])
    engine = ExactStrategyReplay(
        strategy=strategy,  # type: ignore[arg-type]
        initial_cash=1_000_000.0,
        feature_lookback=3,
        latency_bars=1,
        max_participation_rate=participation,
        impact_coefficient_bps=0.0,
        slippage_bps=0.0,
    )
    normalized, blockers = engine._normalize_rows(rows)
    assert blockers == []

    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"),
        normalized,
        start=3,
        stop=len(rows),
    )

    assert replay["accounting_verified"] is True
    assert replay["lifecycle_verified"] is True
    assert all(abs(item["residual"]) <= 1e-6 for item in replay["accounting_identity"])
    assert all(item["cash"] >= -1e-6 for item in replay["equity_ledger"])
    assert all(item["quantity"] >= 0 for item in replay["position_ledger"])
    for fill in replay["fill_ledger"]:
        assert fill["notional"] == pytest.approx(fill["quantity"] * fill["fill_price"])
        expected_cash_delta = (
            -(fill["notional"] + fill["fees"])
            if fill["side"] == "buy"
            else fill["notional"] - fill["fees"]
        )
        assert fill["cash_delta"] == pytest.approx(expected_cash_delta)
        assert fill["position_after"] == fill["position_before"] + fill["signed_quantity"]


def test_exact_replay_accounting_invariant_holds_across_generated_price_paths():
    for seed in range(12):
        rows = _rows(64)
        random = Random(seed)
        price = 100.0
        for index, row in enumerate(rows):
            direction = 1 if random.random() >= 0.45 else -1
            price *= 1.0 + direction * (0.003 + random.random() * 0.017)
            row.update({
                "open": price * (0.997 if direction > 0 else 1.003),
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 200 + random.randint(0, 2_000),
                "pit_intelligence": {
                    "summary": "generated PIT path",
                    "sentiment_score": 0.0,
                    "fundamental_view": "positive" if index % 4 < 2 else "negative",
                    "technical_view": "uptrend" if index % 4 < 2 else "downtrend",
                    "evidence": [{"available_at": row["timestamp"]}],
                },
            })
        result = ExactStrategyReplay(min_points=40, max_participation_rate=0.2).evaluate(
            StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
        )
        assert result["accounting_identity"]
        assert all(abs(item["residual"]) <= 1e-6 for item in result["accounting_identity"])


def test_exact_replay_ledger_handles_gap_and_partial_fill_with_reconcilable_costs():
    rows = _rows(52)
    rows[40]["open"] = rows[39]["close"] * 1.08
    rows[40]["volume"] = 4
    result = ExactStrategyReplay(
        strategy=_GoldenPartialFillStrategy({r["timestamp"]: "buy" for r in rows}),
        initial_cash=10_000.0,
        min_points=40,
        max_participation_rate=0.25,
        impact_coefficient_bps=40.0,
    ).evaluate(StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows)

    assert result["accounting_identity"]
    assert all(abs(item["residual"]) <= 1e-6 for item in result["accounting_identity"])
    assert result["cost_model"]["components"][-1] == "adv_and_bar_participation_impact"
    assert all(
        fill["feature_available_time"] <= fill["decision_time"] <= fill["order_time"]
        <= fill["broker_receive_time"] <= fill["fill_time"]
        for fill in result["fill_ledger"]
    )
    assert any(fill["partial_fill"] for fill in result["fill_ledger"])
    assert all(fill["cost_model"]["adv_volume_shares"] > 0 for fill in result["fill_ledger"])
    assert all("realized_volatility_bps" in fill["cost_model"] for fill in result["fill_ledger"])
    assert all(fill["cost_model"]["execution_evidence_eligible"] is False for fill in result["fill_ledger"])
    assert all(fill["cost_model"]["impact_rule_id"] for fill in result["fill_ledger"])
    assert all(
        fill["cost_model"]["calibration_status"] == "sensitivity_override"
        for fill in result["fill_ledger"]
    )
    # A row-level rate and a schedule label are sensitivity inputs, not an
    # account-bound, immutable broker receipt.
    assert all(fill["cost_model"]["venue_cost"]["execution_evidence_eligible"] is False for fill in result["fill_ledger"])


def test_lifecycle_verifier_rejects_cross_linked_fill_even_when_all_timestamps_are_ordered():
    engine = ExactStrategyReplay(feature_lookback=3, latency_bars=1)
    normalized, blockers = engine._normalize_rows(_rows(8))
    assert blockers == []
    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"), normalized, start=3, stop=8
    )
    assert replay["lifecycle_verified"] is True

    replay["decision_audit"][0]["fill_ids"] = ["fill-from-another-order"]
    assert engine._verify_lifecycle(
        decisions=replay["decision_audit"],
        orders=replay["order_ledger"],
        broker_receipts=replay["broker_receipt_ledger"],
        fills=replay["fill_ledger"],
        pending_orders=[],
    ) is False


def test_zero_participation_never_fabricates_a_one_share_fill():
    engine = ExactStrategyReplay(
        strategy=_GoldenGapStrategy(_rows(7)[3]["timestamp"], _rows(7)[4]["timestamp"]),  # type: ignore[arg-type]
        initial_cash=1_000.0,
        feature_lookback=3,
        latency_bars=1,
        max_participation_rate=0.0,
    )
    normalized, blockers = engine._normalize_rows(_rows(7))
    assert blockers == []

    replay = engine._replay_slice(
        StockRequest(symbol="2330.TW", horizon="swing"), normalized, start=3, stop=7
    )

    assert replay["fill_ledger"] == []
    assert replay["trade_count"] == 0


def test_market_impact_quote_is_monotonic_in_order_size_and_inverse_liquidity():
    model = PointInTimeMarketImpactModel(impact_coefficient_bps=25.0)
    common = {
        "side": "buy",
        "reference_price": 100.0,
        "bid_ask_spread_bps": 10.0,
        "realized_volatility_bps": 120.0,
        "bar_volume_shares": 10_000,
    }
    small = model.quote(quantity=100, adv_volume_shares=50_000, **common)
    large = model.quote(quantity=1_000, adv_volume_shares=50_000, **common)
    illiquid = model.quote(quantity=100, adv_volume_shares=5_000, **common)
    sell = model.quote(side="sell", quantity=100, adv_volume_shares=50_000, **{
        key: value for key, value in common.items() if key != "side"
    })

    assert large.total_slippage_bps > small.total_slippage_bps
    assert illiquid.total_slippage_bps > small.total_slippage_bps
    assert small.fill_price() > small.reference_price
    assert sell.fill_price() < sell.reference_price
    # A generic model has no venue/product/side/date calibration receipt and
    # therefore remains useful for sensitivity analysis only.
    assert small.receipt()["execution_evidence_eligible"] is False
    assert small.receipt()["simulation_mode"] == "spread_adv_proxy"
    assert small.receipt()["depth_data_status"] == "unavailable"
    assert small.receipt()["depth_simulation_eligible"] is False


def test_versioned_taiwan_impact_schedule_selects_effective_venue_product_side_rule():
    schedule = TaiwanSecondaryMarketImpactSchedule()
    buy = schedule.quote(
        venue="TWSE", product_type="stock", side="buy", trade_at="2026-03-02T09:00:00+08:00",
        reference_price=100.0, quantity=100, bid_ask_spread_bps=8.0,
        realized_volatility_bps=100.0, adv_volume_shares=100_000, bar_volume_shares=5_000,
    )
    sell = schedule.quote(
        venue="TPEX", product_type="stock", side="sell", trade_at="2026-03-02T09:00:00+08:00",
        reference_price=100.0, quantity=100, bid_ask_spread_bps=8.0,
        realized_volatility_bps=100.0, adv_volume_shares=100_000, bar_volume_shares=5_000,
    )

    assert buy.impact_rule_id == "twse-stock-buy-research-baseline-2026"
    assert sell.impact_rule_id == "tpex-stock-sell-research-baseline-2026"
    assert sell.impact_coefficient_bps > buy.impact_coefficient_bps
    assert buy.impact_schedule_version == "taiwan-secondary-market-impact-2026-08-13"
    assert len(str(buy.impact_schedule_sha256)) == 64
    assert buy.calibration_status == "research_baseline"
    assert buy.execution_evidence_eligible is False


def test_impact_schedule_fails_closed_outside_effective_dates_and_on_artifact_tampering(tmp_path):
    schedule = TaiwanSecondaryMarketImpactSchedule()
    with pytest.raises(UnsupportedCostSchedule, match="impact_rule_unverified"):
        schedule.rule(venue="TWSE", product_type="stock", side="buy", trade_at="2025-12-31")

    source = Path(__file__).resolve().parents[1] / "config" / "taiwan_market_impact_rules.yaml"
    tampered = tmp_path / "impact-rules.yaml"
    tampered.write_text(
        source.read_text(encoding="utf-8").replace(
            "04aa66deec4ff5ab04160d8ea6ec1487047b1267ae1f97f71d17683083d72b34",
            "0" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        TaiwanMarketImpactRulebook(tampered)


def test_exact_replay_blocks_missing_bid_ask_spread_instead_of_assuming_zero_cost():
    rows = _rows(45)
    rows[12].pop("spread_bps")

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert result["execution_evidence_eligible"] is False
    assert "market_impact_input_missing:12:bid_ask_spread_bps" in result["approval_blockers"]


def test_taiwan_cost_schedule_is_date_product_side_and_lot_aware():
    schedule = TaiwanSecondaryMarketCostSchedule()
    stock_sale = schedule.quote(
        venue="TWSE",
        product_type="stock",
        lot_type="board_lot",
        side="sell",
        trade_at=date(2026, 8, 11),
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="test-broker-fee-schedule-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="test-no-customer-pass-through-v1",
    )
    stock_amounts = stock_sale.amounts(100_000.0)
    assert stock_sale.sell_tax_bps == 30.0
    assert stock_amounts == {"commission": 85.5, "exchange_fee": 0.0, "sell_tax": 300.0, "fees": 385.5}
    assert stock_sale.execution_evidence_eligible is False

    day_trade_sale = schedule.quote(
        venue="TPEX",
        product_type="stock",
        lot_type="board_lot",
        side="sell",
        trade_at="2027-12-31T09:30:00+08:00",
        is_day_trade_offset=True,
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="test-broker-fee-schedule-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="test-no-customer-pass-through-v1",
    )
    assert day_trade_sale.sell_tax_bps == 15.0
    assert day_trade_sale.lot_type == "board_lot"

    odd_lot_day_trade_sale = schedule.quote(
        venue="TPEX",
        product_type="stock",
        lot_type="odd_lot",
        side="sell",
        # This UTC timestamp is already 2028-01-01 in Taiwan.  The rate must
        # use the exchange-local trading date and never grant the board-lot
        # day-trade concession to odd lots.
        trade_at="2027-12-31T16:30:00+00:00",
        is_day_trade_offset=True,
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="test-broker-fee-schedule-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="test-no-customer-pass-through-v1",
    )
    assert odd_lot_day_trade_sale.trade_date == date(2028, 1, 1)
    assert odd_lot_day_trade_sale.sell_tax_bps == 30.0

    etf_sale = schedule.quote(
        venue="TPEX",
        product_type="etf",
        lot_type="odd_lot",
        side="sell",
        trade_at=date(2026, 8, 11),
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="test-broker-fee-schedule-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="test-no-customer-pass-through-v1",
    )
    assert etf_sale.sell_tax_bps == 10.0
    assert etf_sale.amounts(10_000.0)["sell_tax"] == 10.0

    bond_etf_sale = schedule.quote(
        venue="TWSE",
        product_type="bond_etf",
        lot_type="board_lot",
        side="sell",
        trade_at=date(2026, 12, 31),
        broker_commission_bps=8.55,
        broker_minimum_commission_twd=20.0,
        broker_fee_schedule_id="test-broker-fee-schedule-v1",
        exchange_fee_bps=0.0,
        exchange_fee_schedule_id="test-no-customer-pass-through-v1",
        bond_etf_tax_exemption_eligible=True,
    )
    assert bond_etf_sale.sell_tax_bps == 0.0
    assert len(bond_etf_sale.rule_snapshot_sha256) == 64
    assert bond_etf_sale.tax_rule_id == "tw-bond-etf-exempt-seller-tax-2017-2026"
    with pytest.raises(UnsupportedCostSchedule, match="tax_rule_unverified"):
        schedule.quote(
            venue="TWSE",
            product_type="bond_etf",
            lot_type="board_lot",
            side="sell",
            trade_at=date(2027, 1, 1),
            broker_commission_bps=8.55,
            broker_minimum_commission_twd=20.0,
            broker_fee_schedule_id="test-broker-fee-schedule-v1",
            exchange_fee_bps=0.0,
            exchange_fee_schedule_id="test-no-customer-pass-through-v1",
            bond_etf_tax_exemption_eligible=True,
        )


def test_exact_replay_blocks_execution_evidence_without_a_broker_fee_schedule():
    rows = _rows()
    for row in rows:
        row.pop("broker_commission_bps")
        row.pop("broker_minimum_commission_twd")

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert result["strategy_replay_exact"] is False
    assert result["cost_schedule_verified"] is False
    assert result["empirical_valid"] is False
    assert result["execution_evidence_eligible"] is False
    assert "venue_product_date_or_broker_cost_schedule_not_verified" in result["approval_blockers"]


def test_exact_replay_rejects_unknown_venue_product_or_lot_cost_contract():
    rows = _rows(45)
    rows[12]["lot_type"] = "unsupported_lot"

    result = ExactStrategyReplay(min_points=40).evaluate(
        StockRequest(symbol="2330.TW", horizon="swing"), _signal(), rows
    )

    assert result["strategy_replay_exact"] is False
    assert result["execution_evidence_eligible"] is False
    assert any(item.startswith("cost_schedule_unavailable:12:unsupported_lot_type") for item in result["approval_blockers"])
