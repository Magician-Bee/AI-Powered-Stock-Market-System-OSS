from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any

from open_stock_ai.types import MarketSnapshot, StockRequest, TradingSignal
from .strategy_replay import ExactStrategyReplay
from .performance_metrics import calculate_performance_metrics
from .benchmark_metrics import evaluate_benchmark_metrics
from .monte_carlo import simulate_tail_risk
from .regime_robustness import evaluate_regime_robustness
from .statistical_significance import evaluate_statistical_significance


@dataclass
class BacktestResearch:
    """Past-only baseline validation.

    This module deliberately does not claim to replay the complete StrategyEngine.
    It produces a look-ahead-safe moving-average baseline with transaction costs so
    the UI and agents can inspect research metrics without allowing those metrics to
    approve a paper order. A future event-driven backtester must set
    ``strategy_replay_exact`` and ``empirical_valid`` to true before RiskEngine may
    treat the result as execution evidence.
    """

    min_points: int = 30
    fast_window: int = 5
    slow_window: int = 20
    # Kept as an optional, explicitly named sensitivity knob for backwards
    # compatibility.  The fallback baseline never treats it as a production
    # fee schedule; only ExactStrategyReplay can certify row-level costs.
    transaction_cost_bps: float | None = None
    min_trade_count: int = 3

    def evaluate(self, request: StockRequest, signal: TradingSignal, snapshot: MarketSnapshot) -> dict[str, Any]:
        point_in_time = snapshot.raw.get("point_in_time_dataset") if isinstance(snapshot.raw, dict) else None
        if isinstance(point_in_time, list):
            return ExactStrategyReplay(
                min_points=self.min_points,
                transaction_cost_bps=self.transaction_cost_bps,
            ).evaluate(request, signal, point_in_time)
        ohlcv_rows = [row for row in snapshot.ohlcv if row.get("close") is not None]
        closes = [float(row.get("close")) for row in ohlcv_rows]
        required_points = max(self.min_points, self.slow_window + 2)
        if len(closes) < required_points:
            return self._insufficient_result(request, len(closes), required_points)

        strategy_returns: list[float] = []
        positions: list[int] = []
        previous_position = 0
        trade_count = 0

        # Signal at t-1 is calculated only from closes available at t-1 and is then
        # applied to the t-1 -> t return. This one-period shift prevents look-ahead.
        for index in range(self.slow_window, len(closes)):
            history_end = index
            fast_slice = closes[history_end - self.fast_window : history_end]
            slow_slice = closes[history_end - self.slow_window : history_end]
            fast_average = mean(fast_slice)
            slow_average = mean(slow_slice)
            position = 1 if fast_average > slow_average else 0

            prior_close = closes[index - 1]
            current_close = closes[index]
            raw_return = (current_close - prior_close) / prior_close if prior_close else 0.0
            turnover = abs(position - previous_position)
            # This fallback has only OHLCV.  It does not know venue, product,
            # lot type, account commission/minimum, seller tax or exchange
            # schedule, so applying a convenient fixed bps value would make
            # the result look more realistic than the evidence supports.
            # ExactStrategyReplay is the only path allowed to certify costs.
            net_return = position * raw_return

            if turnover:
                trade_count += 1
            strategy_returns.append(net_return)
            positions.append(position)
            previous_position = position

        performance_metrics = calculate_performance_metrics(
            strategy_returns,
            position_weights=[float(item) for item in positions],
        )
        statistical_significance = evaluate_statistical_significance(strategy_returns)
        regime_observations = [
            {
                "timestamp": ohlcv_rows[index + self.slow_window].get("timestamp") or ohlcv_rows[index + self.slow_window].get("date"),
                "close": closes[index + self.slow_window],
                "volume": ohlcv_rows[index + self.slow_window].get("volume"),
                "earnings_event": ohlcv_rows[index + self.slow_window].get("earnings_event"),
            }
            for index in range(len(strategy_returns))
        ]
        regime_robustness = evaluate_regime_robustness(
            strategy_returns,
            regime_observations,
            position_weights=[float(item) for item in positions],
        )
        evaluation_timestamps = [
            str(ohlcv_rows[index + self.slow_window].get("timestamp") or ohlcv_rows[index + self.slow_window].get("date") or "")
            for index in range(len(strategy_returns))
        ]
        raw_snapshot = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        benchmark_rows = raw_snapshot.get("benchmark_observations")
        cash_rows = raw_snapshot.get("risk_free_observations")
        if isinstance(benchmark_rows, list) and len(benchmark_rows) == len(ohlcv_rows):
            benchmark_rows = benchmark_rows[self.slow_window :]
        if isinstance(cash_rows, list) and len(cash_rows) == len(ohlcv_rows):
            cash_rows = cash_rows[self.slow_window :]
        benchmark_metrics = evaluate_benchmark_metrics(
            strategy_returns,
            benchmark_rows if isinstance(benchmark_rows, list) else None,
            cash_rows if isinstance(cash_rows, list) else None,
            evaluation_timestamps=evaluation_timestamps,
        )
        monte_carlo_tail_risk = simulate_tail_risk(strategy_returns)
        sharpe = float(performance_metrics["sharpe"] or 0.0)
        max_drawdown = float(performance_metrics["max_drawdown_pct"])
        active_returns = [value for value, position in zip(strategy_returns, positions) if position != 0]
        wins = sum(1 for value in active_returns if value > 0)
        win_rate = round(wins / len(active_returns), 3) if active_returns else 0.0
        strategy_return_pct = float(performance_metrics["compounded_return_pct"])
        baseline_passed = (
            bool(active_returns)
            and trade_count >= self.min_trade_count
            and sharpe >= 0.5
            and max_drawdown <= 15.0
            and benchmark_metrics["passed"] is True
            and monte_carlo_tail_risk["passed"] is True
            and float(benchmark_metrics["excess_return_pct"] or 0.0) > 0
        )

        return {
            "backtest_id": f"past-only-baseline-{request.symbol}-{request.horizon}",
            "validation_kind": "past_only_ma_baseline_not_strategy_replay",
            "lookahead_safe": True,
            "signal_shift_periods": 1,
            "transaction_costs_included": False,
            "transaction_cost_bps": self.transaction_cost_bps,
            "cost_model": {
                "schema_version": "open_stock_ai.baseline_cost_model.v2",
                "mode": "unavailable_without_point_in_time_execution_context",
                "execution_evidence_eligible": False,
                "blockers": ["transaction_cost_schedule_missing"],
            },
            "strategy_replay_exact": False,
            "empirical_valid": False,
            "execution_evidence_eligible": False,
            "baseline_passed": baseline_passed,
            "passed": False,
            "approval_blockers": [
                "point_in_time_dataset_missing",
                "transaction_cost_schedule_missing",
            ],
            "performance_metrics": performance_metrics,
            "statistical_significance": statistical_significance,
            "benchmark_metrics": benchmark_metrics,
            "monte_carlo_tail_risk": monte_carlo_tail_risk,
            "regime_robustness": regime_robustness,
            "historical_universe": self._historical_universe_unavailable(),
            "universe_membership_rows": [],
            "regime_observations": regime_observations,
            "regime_position_weights": [float(item) for item in positions],
            "sharpe": sharpe,
            "max_drawdown_pct": max_drawdown,
            "win_rate": win_rate,
            "strategy_return_pct": strategy_return_pct,
            "benchmark_return_pct": benchmark_metrics["benchmark_return_pct"],
            "excess_return_pct": benchmark_metrics["excess_return_pct"],
            "trade_count": trade_count,
            "active_period_count": len(active_returns),
            "sample_size": len(strategy_returns),
            "current_signal_action": signal.action,
            "note": (
                "Past-only moving-average baseline with one-period execution delay and "
                "transaction costs. It is advisory research only and does not replay the "
                "complete StrategyEngine because point-in-time intelligence is absent, so it cannot approve execution."
            ),
        }

    def _insufficient_result(self, request: StockRequest, sample_size: int, required_points: int) -> dict[str, Any]:
        return {
            "backtest_id": None,
            "validation_kind": "past_only_ma_baseline_not_strategy_replay",
            "lookahead_safe": True,
            "signal_shift_periods": 1,
            "transaction_costs_included": False,
            "transaction_cost_bps": self.transaction_cost_bps,
            "cost_model": {
                "schema_version": "open_stock_ai.baseline_cost_model.v2",
                "mode": "unavailable_without_point_in_time_execution_context",
                "execution_evidence_eligible": False,
                "blockers": ["transaction_cost_schedule_missing"],
            },
            "strategy_replay_exact": False,
            "empirical_valid": False,
            "execution_evidence_eligible": False,
            "baseline_passed": False,
            "passed": False,
            "approval_blockers": [
                "insufficient_ohlcv",
                "point_in_time_dataset_missing",
                "transaction_cost_schedule_missing",
            ],
            "performance_metrics": calculate_performance_metrics([]),
            "statistical_significance": evaluate_statistical_significance([]),
            "regime_robustness": evaluate_regime_robustness([], []),
            "benchmark_metrics": evaluate_benchmark_metrics([], None, None, evaluation_timestamps=[]),
            "monte_carlo_tail_risk": simulate_tail_risk([]),
            "historical_universe": self._historical_universe_unavailable(),
            "universe_membership_rows": [],
            "sharpe": None,
            "max_drawdown_pct": None,
            "win_rate": None,
            "strategy_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "trade_count": 0,
            "active_period_count": 0,
            "sample_size": sample_size,
            "note": f"Need at least {required_points} OHLCV points for past-only baseline validation.",
            "requested_symbol": request.symbol,
        }

    def _compound_return(self, returns: list[float]) -> float:
        equity = 1.0
        for value in returns:
            equity *= 1 + value
        return round((equity - 1.0) * 100, 3)

    @staticmethod
    def _historical_universe_unavailable() -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.historical_universe.v1",
            "passed": False,
            "point_in_time_verified": False,
            "universe_coverage_complete": False,
            "universe_scope_ids": [],
            "row_count": 0,
            "blockers": ["point_in_time_dataset_missing"],
        }
