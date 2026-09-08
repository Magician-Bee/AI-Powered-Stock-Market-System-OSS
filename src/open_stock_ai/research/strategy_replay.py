from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from math import floor
from statistics import mean, pstdev
from typing import Any

from open_stock_ai.research.cost_model import (
    TaiwanSecondaryMarketImpactSchedule,
    TaiwanSecondaryMarketCostSchedule,
    UnsupportedCostSchedule,
)
from open_stock_ai.research.benchmark_metrics import evaluate_benchmark_metrics
from open_stock_ai.research.monte_carlo import simulate_tail_risk
from open_stock_ai.research.performance_metrics import calculate_performance_metrics
from open_stock_ai.research.regime_robustness import evaluate_regime_robustness
from open_stock_ai.research.statistical_significance import evaluate_statistical_significance
from open_stock_ai.strategy.strategy_engine import StrategyEngine
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest, TradingSignal


@dataclass
class ExactStrategyReplay:
    """Replay the production StrategyEngine over strict point-in-time rows.

    Every signal uses only rows available at that timestamp and is filled at the
    following bar after explicit latency, slippage and transaction costs.  The
    engine refuses to call the result exact when any intelligence row lacks an
    availability timestamp or required point-in-time fields.
    """

    strategy: StrategyEngine = field(default_factory=StrategyEngine)
    min_points: int = 40
    cost_schedule: TaiwanSecondaryMarketCostSchedule = field(default_factory=TaiwanSecondaryMarketCostSchedule)
    # A generic override is retained for local sensitivity work only.  A
    # broker-specific row-level schedule is required for execution evidence.
    transaction_cost_bps: float | None = None
    # Kept only as an explicit sensitivity add-on.  Execution evidence always
    # also requires the PIT spread, volatility, ADV and participation inputs.
    slippage_bps: float | None = None
    # Likewise, this is only a local sensitivity default when a row does not
    # contain an explicit client pass-through fee.  The statutory seller tax
    # always comes from ``TaiwanSecondaryMarketCostSchedule``.
    exchange_fee_bps: float | None = None
    impact_coefficient_bps: float | None = None
    impact_schedule: TaiwanSecondaryMarketImpactSchedule = field(
        default_factory=TaiwanSecondaryMarketImpactSchedule
    )
    max_participation_rate: float = 1.0
    initial_cash: float = 1_000_000.0
    latency_bars: int = 1
    feature_lookback: int = 20
    label_horizon_bars: int = 1
    embargo_bars: int = 1

    def evaluate(
        self,
        request: StockRequest,
        current_signal: TradingSignal,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized, blockers = self._normalize_rows(rows)
        if len(normalized) < self.min_points:
            blockers.append("insufficient_point_in_time_rows")
        if blockers:
            return self._blocked(request, normalized, blockers)

        start = max(20, len(normalized) // 2)
        replay = self._replay_slice(request, normalized, start, len(normalized))
        returns = replay["returns"]
        trades = replay["trade_count"]
        decisions = replay["decision_audit"]
        universe_membership_rows = replay["universe_membership_rows"]
        fold_results = self._walk_forward(request, normalized)
        purged_results = self._purged_cross_validation(request, normalized)
        performance_metrics = calculate_performance_metrics(
            returns,
            position_weights=self._position_weights(replay["position_ledger"], replay["equity_ledger"]),
        )
        position_weights = self._position_weights(replay["position_ledger"], replay["equity_ledger"])
        regime_robustness = evaluate_regime_robustness(
            returns,
            replay["regime_observations"],
            position_weights=position_weights,
        )
        statistical_significance = evaluate_statistical_significance(
            returns,
            candidate_count=max(1, int(current_signal.decision_schema.get("candidate_count") or 1)),
        )
        benchmark_metrics = evaluate_benchmark_metrics(
            returns,
            replay["benchmark_observations"],
            replay["risk_free_observations"],
            evaluation_timestamps=replay["return_timestamps"],
        )
        monte_carlo_tail_risk = simulate_tail_risk(returns)
        sharpe = float(performance_metrics["sharpe"] or 0.0)
        max_drawdown = float(performance_metrics["max_drawdown_pct"])
        strategy_return = float(performance_metrics["compounded_return_pct"])
        active = [value for value, decision in zip(returns, decisions) if decision.get("position_quantity", 0) > 0]
        win_rate = round(sum(1 for value in active if value > 0) / len(active), 3) if active else 0.0
        walk_forward_passed = bool(fold_results) and all(
            item["sample_size"] > 0
            and item["fitted_policy"]["training_only"] is True
            and item["fitted_policy"]["fold_strategy_isolated"] is True
            and item["accounting_verified"] is True
            and item["partition_verified"] is True
            and item["fitted_policy"]["training_data_hash"] == item["training_data_hash"]
            for item in fold_results
        )
        oos_positive = bool(fold_results) and mean(item["strategy_return_pct"] for item in fold_results) > 0
        purged_cv_passed = len(purged_results) >= 2 and all(
            item["leakage_detected"] is False
            and item["embargo_verified"] is True
            and item["fitted_policy"]["training_only"] is True
            and item["fitted_policy"]["fold_strategy_isolated"] is True
            and item["partition_verified"] is True
            and item["fitted_policy"]["training_data_hash"] == item["training_data_hash"]
            for item in purged_results
        )
        metric_thresholds = sharpe >= 0.5 and max_drawdown <= 20.0 and trades >= 3
        historical_universe_verified = bool(universe_membership_rows) and all(
            item.get("passed") is True
            and item.get("point_in_time_verified") is True
            and item.get("required_entity_in_universe") is True
            and item.get("universe_coverage_complete") is True
            and bool(item.get("universe_scope_id"))
            and len(str(item.get("manifest_hash") or "")) == 64
            for item in universe_membership_rows
        )
        passed = (
            walk_forward_passed
            and oos_positive
            and purged_cv_passed
            and metric_thresholds
            and regime_robustness["passed"] is True
            and historical_universe_verified
            and benchmark_metrics["passed"] is True
            and monte_carlo_tail_risk["passed"] is True
        )
        strategy_hash = self.strategy_version_hash()
        data_hash = self.data_version_hash(normalized)
        accounting_verified = replay["accounting_verified"] is True
        lifecycle_verified = replay["lifecycle_verified"] is True
        cost_schedule_verified = replay["cost_schedule_verified"] is True
        certification = self._certification_receipt(
            accounting_verified=accounting_verified,
            lifecycle_verified=lifecycle_verified,
            cost_schedule_verified=cost_schedule_verified,
            walk_forward_passed=walk_forward_passed,
            purged_cv_passed=purged_cv_passed,
            historical_universe_verified=historical_universe_verified,
            benchmark_metrics_verified=benchmark_metrics["passed"] is True,
            monte_carlo_tail_risk_verified=monte_carlo_tail_risk["passed"] is True,
        )
        strategy_replay_exact = certification["certified"] is True
        empirical_valid = strategy_replay_exact and passed
        return {
            "schema_version": "open_stock_ai.exact_strategy_replay.v2",
            "backtest_id": f"exact-{strategy_hash[:10]}-{data_hash[:10]}",
            "validation_kind": "event_driven_production_strategy_engine_point_in_time_replay",
            "strategy_replay_exact": strategy_replay_exact,
            "empirical_valid": empirical_valid,
            "research_certification": certification,
            "execution_evidence_eligible": empirical_valid and replay["execution_evidence_eligible"] is True,
            "live_execution_evidence_eligible": False,
            "passed": passed,
            "lookahead_safe": lifecycle_verified and purged_cv_passed,
            "point_in_time_verified": lifecycle_verified,
            "accounting_verified": accounting_verified,
            "lifecycle_verified": lifecycle_verified,
            "signal_shift_periods": self.latency_bars,
            "execution_latency_bars": self.latency_bars,
            "transaction_costs_included": True,
            "cost_model": self._cost_model_receipt(),
            "transaction_cost_bps": self.cost_schedule.standard_commission_bps,
            "cost_schedule_verified": cost_schedule_verified,
            "slippage_included": True,
            "slippage_bps": self.slippage_bps,
            "walk_forward": {"passed": walk_forward_passed, "folds": fold_results},
            "out_of_sample": {"passed": oos_positive, "fold_count": len(fold_results)},
            "purged_cross_validation": {
                "passed": purged_cv_passed,
                "feature_lookback": self.feature_lookback,
                "label_horizon_bars": self.label_horizon_bars,
                "embargo_bars": self.embargo_bars,
                "folds": purged_results,
            },
            "strategy_version_hash": strategy_hash,
            "data_version_hash": data_hash,
            "performance_metrics": performance_metrics,
            "period_returns": returns,
            "return_timestamps": replay["return_timestamps"],
            "benchmark_observations": replay["benchmark_observations"],
            "risk_free_observations": replay["risk_free_observations"],
            "benchmark_metrics": benchmark_metrics,
            "monte_carlo_tail_risk": monte_carlo_tail_risk,
            "regime_observations": replay["regime_observations"],
            "regime_position_weights": position_weights,
            "statistical_significance": statistical_significance,
            "regime_robustness": regime_robustness,
            "historical_universe": {
                "schema_version": "open_stock_ai.historical_universe.v1",
                "passed": historical_universe_verified,
                "point_in_time_verified": historical_universe_verified,
                "universe_coverage_complete": historical_universe_verified,
                "universe_scope_ids": sorted(
                    {str(item.get("universe_scope_id")) for item in universe_membership_rows}
                ),
                "row_count": len(universe_membership_rows),
                "manifest_hashes": [item.get("manifest_hash") for item in universe_membership_rows],
                "blockers": [] if historical_universe_verified else ["historical_universe_membership_not_verified"],
            },
            "universe_membership_rows": universe_membership_rows,
            "sharpe": sharpe,
            "max_drawdown_pct": max_drawdown,
            "win_rate": win_rate,
            "strategy_return_pct": strategy_return,
            "benchmark_return_pct": benchmark_metrics["benchmark_return_pct"],
            "excess_return_pct": benchmark_metrics["excess_return_pct"],
            "trade_count": trades,
            "active_period_count": len(active),
            "sample_size": len(returns),
            "current_signal_action": current_signal.action,
            "approval_blockers": self._approval_blockers(
                accounting_verified=accounting_verified,
                lifecycle_verified=lifecycle_verified,
                cost_schedule_verified=cost_schedule_verified,
                walk_forward_passed=walk_forward_passed,
                purged_cv_passed=purged_cv_passed,
                metric_thresholds=metric_thresholds,
                regime_robustness_passed=regime_robustness["passed"] is True,
                historical_universe_verified=historical_universe_verified,
                benchmark_metrics_verified=benchmark_metrics["passed"] is True,
                monte_carlo_tail_risk_verified=monte_carlo_tail_risk["passed"] is True,
                passed=passed,
            ),
            "decision_audit": decisions,
            "order_ledger": replay["order_ledger"],
            "broker_receipt_ledger": replay["broker_receipt_ledger"],
            "event_ledger": replay["event_ledger"],
            "position_ledger": replay["position_ledger"],
            "cash_ledger": replay["cash_ledger"],
            "equity_ledger": replay["equity_ledger"],
            "fill_ledger": replay["fill_ledger"],
            "accounting_identity": replay["accounting_identity"],
            "note": "Production StrategyEngine replayed through an event-driven fill, position, cash and equity ledger. Live execution remains separately prohibited.",
        }

    def strategy_version_hash(self) -> str:
        source = inspect.getsource(type(self.strategy))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def data_version_hash(self, rows: list[dict[str, Any]]) -> str:
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _normalize_rows(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        normalized: list[dict[str, Any]] = []
        blockers: list[str] = []
        previous_time: datetime | None = None
        for index, source in enumerate(rows):
            try:
                observed = _timestamp(source.get("timestamp") or source.get("date"))
                available = _timestamp(source.get("available_at"))
                event_time = _timestamp(source.get("event_time"))
                published_at = (
                    _timestamp(source.get("published_at"))
                    if source.get("published_at")
                    else None
                )
                effective_at = _timestamp(source.get("effective_at"))
                ingested_at = _timestamp(source.get("ingested_at"))
                close = float(source["close"])
                open_price = float(source.get("open") or close)
            except (KeyError, TypeError, ValueError):
                blockers.append(f"incomplete_feature_temporal_contract:{index}")
                continue
            intelligence = source.get("pit_intelligence")
            if not isinstance(intelligence, dict):
                blockers.append(f"point_in_time_intelligence_missing:{index}")
                continue
            intelligence_evidence = intelligence.get("evidence")
            if not isinstance(intelligence_evidence, list) or not intelligence_evidence:
                blockers.append(f"point_in_time_intelligence_lineage_missing:{index}")
            else:
                for evidence_index, evidence in enumerate(intelligence_evidence):
                    if not isinstance(evidence, dict):
                        blockers.append(f"point_in_time_intelligence_evidence_invalid:{index}:{evidence_index}")
                        continue
                    try:
                        evidence_available = _timestamp(evidence.get("available_at"))
                    except (TypeError, ValueError):
                        blockers.append(f"point_in_time_intelligence_evidence_time_missing:{index}:{evidence_index}")
                        continue
                    if evidence_available > observed:
                        blockers.append(f"point_in_time_intelligence_evidence_after_signal_time:{index}:{evidence_index}")
            universe_membership = source.get("universe_membership")
            if not isinstance(universe_membership, dict):
                blockers.append(f"historical_universe_membership_missing:{index}")
                continue
            try:
                membership_as_of = _timestamp(universe_membership.get("as_of"))
            except (TypeError, ValueError):
                blockers.append(f"historical_universe_membership_timestamp_invalid:{index}")
                continue
            if membership_as_of != observed:
                blockers.append(f"historical_universe_membership_timestamp_mismatch:{index}")
            if (
                universe_membership.get("passed") is not True
                or universe_membership.get("point_in_time_verified") is not True
                or universe_membership.get("required_entity_in_universe") is not True
                or universe_membership.get("universe_coverage_complete") is not True
                or not str(universe_membership.get("universe_scope_id") or "").strip()
                or len(str(universe_membership.get("manifest_hash") or "")) != 64
            ):
                blockers.append(f"historical_universe_membership_unverified:{index}")
            if available > observed:
                blockers.append(f"feature_available_after_signal_time:{index}")
            if ingested_at > observed:
                blockers.append(f"feature_ingested_after_signal_time:{index}")
            if event_time > observed or effective_at > observed:
                blockers.append(f"invalid_feature_temporal_order:{index}")
            if published_at is not None and published_at > available:
                blockers.append(f"invalid_feature_publication_order:{index}")
            if ingested_at < available:
                blockers.append(f"invalid_feature_ingestion_order:{index}")
            venue = str(source.get("venue") or "").upper().strip()
            product_type = str(source.get("product_type") or "").lower().strip()
            lot_type = str(source.get("lot_type") or "").lower().strip()
            try:
                bond_etf_tax_exemption_eligible = _optional_bool(
                    source.get("bond_etf_tax_exemption_eligible")
                )
                is_day_trade_offset = _optional_bool(source.get("is_day_trade_offset")) or False
            except ValueError as exc:
                blockers.append(f"cost_schedule_unavailable:{index}:{exc}")
                bond_etf_tax_exemption_eligible = None
                is_day_trade_offset = False
            try:
                self.cost_schedule.quote(
                    venue=venue,
                    product_type=product_type,
                    lot_type=lot_type,
                    side="buy",
                    trade_at=observed,
                    broker_commission_bps=_number(source.get("broker_commission_bps")),
                    broker_minimum_commission_twd=_number(source.get("broker_minimum_commission_twd")),
                    broker_fee_schedule_id=str(source.get("broker_fee_schedule_id") or "") or None,
                    exchange_fee_bps=_row_fee_or_default(source.get("exchange_fee_bps"), self.exchange_fee_bps),
                    exchange_fee_schedule_id=str(source.get("exchange_fee_schedule_id") or "") or None,
                    bond_etf_tax_exemption_eligible=bond_etf_tax_exemption_eligible,
                )
            except (TypeError, ValueError, UnsupportedCostSchedule) as exc:
                blockers.append(f"cost_schedule_unavailable:{index}:{exc}")
            try:
                self.impact_schedule.rule(
                    venue=venue,
                    product_type=product_type,
                    side="buy",
                    trade_at=observed,
                )
                self.impact_schedule.rule(
                    venue=venue,
                    product_type=product_type,
                    side="sell",
                    trade_at=observed,
                )
            except (TypeError, ValueError, UnsupportedCostSchedule) as exc:
                blockers.append(f"market_impact_schedule_unavailable:{index}:{exc}")
            spread_bps = _number(source.get("spread_bps"))
            if spread_bps is None or spread_bps < 0:
                blockers.append(f"market_impact_input_missing:{index}:bid_ask_spread_bps")
            volume = int(source.get("volume") or 0)
            if volume <= 0:
                blockers.append(f"market_impact_input_missing:{index}:bar_volume_shares")
            feature_records = source.get("feature_records")
            if not isinstance(feature_records, list) or not feature_records:
                blockers.append(f"feature_lineage_missing:{index}")
                feature_records = []
            for feature_index, feature in enumerate(feature_records):
                if not isinstance(feature, dict):
                    blockers.append(f"invalid_feature_record:{index}:{feature_index}")
                    continue
                try:
                    feature_event = _timestamp(feature.get("event_time"))
                    feature_published = (
                        _timestamp(feature.get("published_at"))
                        if feature.get("published_at")
                        else None
                    )
                    feature_available = _timestamp(feature.get("available_at"))
                    feature_effective = _timestamp(feature.get("effective_at"))
                    feature_ingested = _timestamp(feature.get("ingested_at"))
                except (TypeError, ValueError):
                    blockers.append(f"incomplete_feature_temporal_contract:{index}:{feature_index}")
                    continue
                if feature_available > observed:
                    blockers.append(f"feature_available_after_signal_time:{index}:{feature_index}")
                if feature_ingested > observed:
                    blockers.append(f"feature_ingested_after_signal_time:{index}:{feature_index}")
                if feature_event > observed or feature_effective > observed:
                    blockers.append(f"invalid_feature_temporal_order:{index}:{feature_index}")
                if feature_published is not None and feature_published > feature_available:
                    blockers.append(f"invalid_feature_publication_order:{index}:{feature_index}")
                if feature.get("production_contract_covered") is not True:
                    blockers.append(f"feature_production_contract_missing:{index}:{feature_index}")
                if len(str(feature.get("availability_contract_sha256") or "")) != 64:
                    blockers.append(f"feature_availability_contract_hash_missing:{index}:{feature_index}")
            if previous_time is not None and observed <= previous_time:
                blockers.append("point_in_time_rows_not_strictly_ordered")
            previous_time = observed
            normalized.append(
                {
                    **source,
                    "timestamp": observed.isoformat(),
                    "event_time": event_time.isoformat(),
                    "published_at": published_at.isoformat() if published_at else None,
                    "available_at": available.isoformat(),
                    "effective_at": effective_at.isoformat(),
                    "ingested_at": ingested_at.isoformat(),
                    "open": open_price,
                    "high": float(source.get("high") or close),
                    "low": float(source.get("low") or close),
                    "close": close,
                    "volume": volume,
                    "spread_bps": spread_bps,
                    "venue": venue,
                    "product_type": product_type,
                    "lot_type": lot_type,
                    "broker_commission_bps": _number(source.get("broker_commission_bps")),
                    "broker_minimum_commission_twd": _number(source.get("broker_minimum_commission_twd")),
                    "broker_fee_schedule_id": str(source.get("broker_fee_schedule_id") or "") or None,
                    "exchange_fee_bps": _row_fee_or_default(source.get("exchange_fee_bps"), self.exchange_fee_bps),
                    "exchange_fee_schedule_id": str(source.get("exchange_fee_schedule_id") or "") or None,
                    "bond_etf_tax_exemption_eligible": bond_etf_tax_exemption_eligible,
                    "is_day_trade_offset": is_day_trade_offset,
                    "feature_records": [dict(item) for item in feature_records if isinstance(item, dict)],
                    "pit_intelligence": dict(intelligence),
                    "universe_membership": dict(universe_membership),
                }
            )
        return normalized, list(dict.fromkeys(blockers))

    def _replay_slice(
        self,
        request: StockRequest,
        rows: list[dict[str, Any]],
        start: int,
        stop: int,
        *,
        fitted_policy: dict[str, Any] | None = None,
        strategy: StrategyEngine | None = None,
    ) -> dict[str, Any]:
        """Run decision → order → broker receipt → fill through one ledger.

        No bar return is manufactured from a price pair.  The only strategy
        return is the period-over-period change in marked equity after every
        cash movement, fee and actual fill is recorded.
        """
        cash = float(self.initial_cash)
        position_quantity = 0
        previous_mark: float | None = None
        previous_equity = cash
        returns: list[float] = []
        market_returns: list[float] = []
        return_timestamps: list[str] = []
        benchmark_observations: list[Any] = []
        risk_free_observations: list[Any] = []
        trade_count = 0
        audit: list[dict[str, Any]] = []
        regime_observations: list[dict[str, Any]] = []
        universe_membership_rows: list[dict[str, Any]] = []
        pending_orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        broker_receipts: list[dict[str, Any]] = []
        event_ledger: list[dict[str, Any]] = []
        cash_ledger: list[dict[str, Any]] = [{"time": rows[start]["timestamp"], "kind": "initial_cash", "amount": cash, "balance": cash}]
        position_ledger: list[dict[str, Any]] = []
        equity_ledger: list[dict[str, Any]] = []
        identity_rows: list[dict[str, Any]] = []
        min_confidence = float((fitted_policy or {}).get("min_confidence") or 0.0)
        active_strategy = strategy or self.strategy
        replay_stop = min(stop, len(rows))
        for index in range(start, replay_stop):
            row = rows[index]
            position_before_bar = position_quantity
            fills_this_bar: list[dict[str, Any]] = []
            # Every earlier decision becomes a broker receipt only at its
            # scheduled arrival/fill bar.  Partial fills remain explicit and
            # leave the unfilled target to a later decision instead of being
            # silently assumed filled.
            due, pending_orders = (
                [item for item in pending_orders if item["fill_index"] == index],
                [item for item in pending_orders if item["fill_index"] != index],
            )
            for order in due:
                receipt = {
                    "broker_receipt_id": f"replay-receipt-{order['order_id']}",
                    "order_id": order["order_id"],
                    "received_at": row["timestamp"],
                    "status": "received",
                }
                broker_receipts.append(receipt)
                order["public_record"]["broker_receipt_id"] = receipt["broker_receipt_id"]
                order["public_record"]["broker_receive_time"] = row["timestamp"]
                order["audit_entry"]["broker_receipt_id"] = receipt["broker_receipt_id"]
                order["audit_entry"]["broker_receive_time"] = row["timestamp"]
                event_ledger.append(
                    {
                        "event_id": receipt["broker_receipt_id"],
                        "event_type": "broker_received",
                        "occurred_at": row["timestamp"],
                        "order_id": order["order_id"],
                    }
                )
                fill = self._fill_pending_order(
                    order=order,
                    row=row,
                    cash=cash,
                    position_quantity=position_quantity,
                )
                if fill is None:
                    receipt["status"] = "no_fill"
                    order["public_record"]["status"] = "no_fill"
                    order["audit_entry"]["status"] = "no_fill"
                    continue
                fill["broker_receipt_id"] = receipt["broker_receipt_id"]
                cash = float(fill["cash_after"])
                position_quantity = int(fill["position_after"])
                fills.append(fill)
                fills_this_bar.append(fill)
                cash_ledger.append(
                    {
                        "time": row["timestamp"],
                        "kind": f"{fill['side']}_fill",
                        "amount": float(fill["cash_delta"]),
                        "fees": float(fill["fees"]),
                        "balance": cash,
                        "fill_id": fill["fill_id"],
                    }
                )
                receipt["status"] = "partially_filled" if fill["partial_fill"] else "filled"
                receipt["fill_ids"] = [fill["fill_id"]]
                order["public_record"]["status"] = receipt["status"]
                order["public_record"]["fill_ids"] = [fill["fill_id"]]
                order["audit_entry"]["status"] = receipt["status"]
                order["audit_entry"]["fill_time"] = fill["fill_time"]
                order["audit_entry"]["fill_ids"] = [fill["fill_id"]]
                event_ledger.append(
                    {
                        "event_id": fill["fill_id"],
                        "event_type": "fill",
                        "occurred_at": fill["fill_time"],
                        "order_id": order["order_id"],
                        "broker_receipt_id": receipt["broker_receipt_id"],
                    }
                )
                trade_count += 1

            # The production strategy receives only the declared feature
            # lookback window.  Passing the full replay dataset would let a
            # future model adapter silently discover observations outside the
            # fold's explicit feature contract.
            target_position = 1 if position_quantity > 0 else 0
            if index + self.latency_bars < replay_stop:
                history_start = max(0, index - self.feature_lookback + 1)
                history = rows[history_start : index + 1]
                intelligence = self._intelligence(request, row["pit_intelligence"])
                snapshot = MarketSnapshot(
                    symbol=request.symbol,
                    market=request.market,
                    price=row["close"],
                    ohlcv=[{key: item[key] for key in ("timestamp", "open", "high", "low", "close", "volume")} for item in history],
                    raw={
                        "point_in_time": True,
                        "as_of": row["timestamp"],
                        "feature_window_start": history[0]["timestamp"],
                    },
                )
                signal = active_strategy.generate_signal(request, snapshot, intelligence)
                target_position = (
                    1
                    if signal.action in {"buy", "add"} and float(signal.confidence or 0.0) >= min_confidence
                    else 0
                    if signal.action in {"sell", "reduce"}
                    else (1 if position_quantity > 0 else 0)
                )
                execution_row = rows[index + self.latency_bars]
                impact_inputs = self._point_in_time_impact_inputs(rows, as_of_index=index)
                order_id = f"replay-order-{index}"
                decision_id = f"replay-decision-{index}"
                order_record = {
                    "order_id": order_id,
                    "decision_id": decision_id,
                    "submitted_at": row["timestamp"],
                    "scheduled_broker_receive_time": execution_row["timestamp"],
                    "target_position": target_position,
                    "signal_action": signal.action,
                    "status": "pending_broker_receipt",
                    "fill_ids": [],
                }
                audit_entry = {
                    "decision_id": decision_id,
                    "order_id": order_id,
                    "feature_available_time": row["available_at"],
                    "decision_time": row["timestamp"],
                    "order_time": row["timestamp"],
                    "broker_receive_time": None,
                    "fill_time": None,
                    "signal_time": row["timestamp"],
                    "execution_time": execution_row["timestamp"],
                    "action": signal.action,
                    "confidence": signal.confidence,
                    "target_position": target_position,
                    "position_quantity_at_decision": position_quantity,
                    "broker_receipt_id": None,
                    "fill_ids": [],
                    "status": "pending_broker_receipt",
                }
                audit.append(audit_entry)
                orders.append(order_record)
                event_ledger.extend(
                    [
                        {
                            "event_id": f"feature-{decision_id}",
                            "event_type": "feature_available",
                            "occurred_at": row["available_at"],
                            "decision_id": decision_id,
                        },
                        {
                            "event_id": decision_id,
                            "event_type": "decision",
                            "occurred_at": row["timestamp"],
                            "decision_id": decision_id,
                        },
                        {
                            "event_id": order_id,
                            "event_type": "order_submitted",
                            "occurred_at": row["timestamp"],
                            "decision_id": decision_id,
                            "order_id": order_id,
                        },
                    ]
                )
                pending_orders.append(
                    {
                        "order_id": order_id,
                        "decision_id": decision_id,
                        "decision_index": index,
                        "fill_index": index + self.latency_bars,
                        "feature_available_time": row["available_at"],
                        "decision_time": row["timestamp"],
                        "order_time": row["timestamp"],
                        "target_position": target_position,
                        "signal_action": signal.action,
                        "venue": row["venue"],
                        "product_type": row["product_type"],
                        "lot_type": row["lot_type"],
                        "broker_commission_bps": row["broker_commission_bps"],
                        "broker_minimum_commission_twd": row["broker_minimum_commission_twd"],
                        "broker_fee_schedule_id": row["broker_fee_schedule_id"],
                        "exchange_fee_bps": row["exchange_fee_bps"],
                        "exchange_fee_schedule_id": row["exchange_fee_schedule_id"],
                        "bond_etf_tax_exemption_eligible": row[
                            "bond_etf_tax_exemption_eligible"
                        ],
                        "is_day_trade_offset": row["is_day_trade_offset"],
                        "audit_entry": audit_entry,
                        "public_record": order_record,
                        **impact_inputs,
                    }
                )
            mark = float(row["close"])
            equity = cash + position_quantity * mark
            equity_change = equity - previous_equity
            mark_to_market = 0.0 if previous_mark is None else position_before_bar * (mark - previous_mark)
            # The explicit identity is calculated directly from cash and
            # position state.  The decomposition below is intentionally
            # retained for audit; a mismatch blocks exact certification.
            cash_delta = sum(float(fill["cash_delta"]) for fill in fills_this_bar)
            quantity_delta = sum(int(fill["signed_quantity"]) for fill in fills_this_bar)
            execution_price_pnl = sum(
                int(fill["signed_quantity"]) * (mark - float(fill["fill_price"]))
                for fill in fills_this_bar
            )
            fees = sum(float(fill["fees"]) for fill in fills_this_bar)
            reconstructed = mark_to_market + execution_price_pnl - fees
            residual = round(equity_change - reconstructed, 10)
            identity_rows.append(
                {
                    "time": row["timestamp"],
                    "previous_equity": previous_equity,
                    "equity": equity,
                    "equity_change": equity_change,
                    "cash_delta": cash_delta,
                    "holdings_mark_to_market": mark_to_market,
                    "execution_price_pnl": execution_price_pnl,
                    "fees": fees,
                    "quantity_delta": quantity_delta,
                    "reconstructed_equity_change": reconstructed,
                    "residual": residual,
                }
            )
            if previous_equity > 0:
                returns.append(equity / previous_equity - 1.0)
                return_timestamps.append(row["timestamp"])
                benchmark_observations.append(row.get("benchmark_observation"))
                risk_free_observations.append(row.get("risk_free_observation"))
                regime_observations.append(
                    {
                        "timestamp": row["timestamp"],
                        "close": row["close"],
                        "volume": row["volume"],
                        "earnings_event": row.get("earnings_event"),
                    }
                )
                universe_membership_rows.append(dict(row["universe_membership"]))
            if previous_mark and previous_mark > 0:
                market_returns.append(mark / previous_mark - 1.0)
            position_ledger.append(
                {
                    "time": row["timestamp"],
                    "quantity": position_quantity,
                    "mark_price": mark,
                    "market_value": position_quantity * mark,
                    "target_position": target_position,
                }
            )
            equity_ledger.append(
                {"time": row["timestamp"], "cash": cash, "holdings_value": position_quantity * mark, "equity": equity}
            )
            previous_equity = equity
            previous_mark = mark
        accounting_verified = all(abs(float(item["residual"])) <= 1e-6 for item in identity_rows)
        lifecycle_verified = self._verify_lifecycle(
            decisions=audit,
            orders=orders,
            broker_receipts=broker_receipts,
            fills=fills,
            pending_orders=pending_orders,
        )
        cost_schedule_verified = bool(fills) and all(
            item["cost_model"]["execution_evidence_eligible"] is True
            for item in fills
        )
        return {
            "returns": returns,
            "return_timestamps": return_timestamps,
            "benchmark_observations": benchmark_observations,
            "risk_free_observations": risk_free_observations,
            "regime_observations": regime_observations,
            "universe_membership_rows": universe_membership_rows,
            "market_returns": market_returns,
            "trade_count": trade_count,
            "decision_audit": audit,
            "order_ledger": orders,
            "broker_receipt_ledger": broker_receipts,
            "event_ledger": event_ledger,
            "fill_ledger": fills,
            "cash_ledger": cash_ledger,
            "position_ledger": position_ledger,
            "equity_ledger": equity_ledger,
            "accounting_identity": identity_rows,
            "accounting_verified": accounting_verified,
            "lifecycle_verified": lifecycle_verified,
            "cost_schedule_verified": cost_schedule_verified,
            "execution_evidence_eligible": cost_schedule_verified,
        }

    def _verify_lifecycle(
        self,
        *,
        decisions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        broker_receipts: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        pending_orders: list[dict[str, Any]],
    ) -> bool:
        """Verify linked events, not merely a collection of ordered timestamps."""

        if pending_orders or not decisions or len(decisions) != len(orders) or len(orders) != len(broker_receipts):
            return False
        orders_by_id = {item.get("order_id"): item for item in orders}
        receipts_by_order = {item.get("order_id"): item for item in broker_receipts}
        fills_by_order: dict[str, list[dict[str, Any]]] = {}
        for fill in fills:
            fills_by_order.setdefault(str(fill.get("order_id")), []).append(fill)
        if len(orders_by_id) != len(orders) or len(receipts_by_order) != len(broker_receipts):
            return False
        for decision in decisions:
            order_id = decision.get("order_id")
            order = orders_by_id.get(order_id)
            receipt = receipts_by_order.get(order_id)
            if order is None or receipt is None or order.get("decision_id") != decision.get("decision_id"):
                return False
            if (
                order.get("broker_receipt_id") != receipt.get("broker_receipt_id")
                or decision.get("broker_receipt_id") != receipt.get("broker_receipt_id")
            ):
                return False
            try:
                feature_time = _timestamp(decision.get("feature_available_time"))
                decision_time = _timestamp(decision.get("decision_time"))
                order_time = _timestamp(order.get("submitted_at"))
                receipt_time = _timestamp(receipt.get("received_at"))
            except (TypeError, ValueError):
                return False
            if not feature_time <= decision_time <= order_time <= receipt_time:
                return False
            linked_fills = fills_by_order.get(str(order_id), [])
            linked_fill_ids = [item.get("fill_id") for item in linked_fills]
            if decision.get("fill_ids") != linked_fill_ids or order.get("fill_ids") != linked_fill_ids:
                return False
            expected_status = (
                "partially_filled"
                if linked_fills and any(item.get("partial_fill") is True for item in linked_fills)
                else "filled"
                if linked_fills
                else "no_fill"
            )
            if any(item.get("status") != expected_status for item in (decision, order, receipt)):
                return False
            for fill in linked_fills:
                if fill.get("broker_receipt_id") != receipt.get("broker_receipt_id"):
                    return False
                try:
                    if _timestamp(fill.get("fill_time")) < receipt_time:
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    def _walk_forward(self, request: StockRequest, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Leave enough observations for an entirely disjoint training
        # information interval.  A test sample consumes its feature lookback
        # plus forward label horizon, not just its decision timestamp.
        initial = max(2 * self.feature_lookback + self.label_horizon_bars + 2, len(rows) // 2)
        fold_size = max(5, len(rows) // 10)
        folds = []
        test_start = initial
        while test_start + self.latency_bars < len(rows):
            test_end = min(len(rows), test_start + fold_size)
            test_indices = list(range(test_start, test_end))
            candidate_indices = list(range(self.feature_lookback - 1, test_start))
            train_indices, purged_indices = self._purged_training_indices(
                rows,
                candidate_indices=candidate_indices,
                test_indices=test_indices,
                embargoed_indices=(),
            )
            partition = self._partition_receipt(rows, train_indices, test_indices)
            training_strategy = deepcopy(self.strategy)
            policy = self._fit_policy(
                request,
                rows,
                train_indices,
                training_data_hash=partition["training_data_hash"],
                strategy=training_strategy,
                fold_strategy_isolated=training_strategy is not self.strategy,
            )
            frozen_strategy = deepcopy(training_strategy)
            replay = self._replay_slice(
                request,
                rows,
                test_start,
                test_end,
                fitted_policy=policy,
                strategy=frozen_strategy,
            )
            values = replay["returns"]
            if values:
                folds.append(
                    {
                        "train_end_index": test_start - 1,
                        "test_start_index": test_start,
                        "test_end_index": test_end - 1,
                        "sample_size": len(values),
                        "trade_count": replay["trade_count"],
                        "purged_indices": purged_indices,
                        "strategy_return_pct": self._compound_return(values),
                        "market_return_pct": self._compound_return(replay["market_returns"]),
                        "sharpe": self._sharpe(values),
                        "accounting_verified": replay["accounting_verified"],
                        "fitted_policy": policy,
                        **partition,
                    }
                )
            test_start = test_end
        return folds

    def _purged_cross_validation(self, request: StockRequest, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fold_size = max(5, len(rows) // 4)
        folds = []
        for fold in range(2):
            test_start = max(self.feature_lookback + 2, len(rows) - (2 - fold) * fold_size)
            test_end = min(len(rows), test_start + fold_size)
            test_indices = list(range(test_start, test_end))
            candidate_indices = list(range(self.feature_lookback - 1, len(rows) - self.label_horizon_bars))
            embargoed_indices = list(range(test_end, min(len(rows), test_end + self.embargo_bars)))
            train_indices, purged_indices = self._purged_training_indices(
                rows,
                candidate_indices=candidate_indices,
                test_indices=test_indices,
                embargoed_indices=embargoed_indices,
            )
            partition = self._partition_receipt(rows, train_indices, test_indices)
            training_strategy = deepcopy(self.strategy)
            policy = self._fit_policy(
                request,
                rows,
                train_indices,
                training_data_hash=partition["training_data_hash"],
                strategy=training_strategy,
                fold_strategy_isolated=training_strategy is not self.strategy,
            )
            frozen_strategy = deepcopy(training_strategy)
            replay = self._replay_slice(
                request,
                rows,
                test_start,
                test_end,
                fitted_policy=policy,
                strategy=frozen_strategy,
            )
            folds.append(
                {
                    "train_indices": train_indices,
                    "purged_indices": purged_indices,
                    "embargoed_indices": embargoed_indices,
                    "test_start_index": test_start,
                    "test_end_index": test_end - 1,
                    "sample_size": len(replay["returns"]),
                    "leakage_detected": partition["information_overlap_count"] > 0,
                    "overlap_count": partition["information_overlap_count"],
                    "embargo_verified": not any(index in set(embargoed_indices) for index in train_indices),
                    "strategy_return_pct": self._compound_return(replay["returns"]),
                    "accounting_verified": replay["accounting_verified"],
                    "fitted_policy": policy,
                    **partition,
                }
            )
        return folds

    def _purged_training_indices(
        self,
        rows: list[dict[str, Any]],
        *,
        candidate_indices: list[int],
        test_indices: list[int],
        embargoed_indices: tuple[int, ...] | list[int],
    ) -> tuple[list[int], list[int]]:
        """Exclude every training sample whose information set touches test.

        A sample's information interval starts at the first row in its feature
        lookback and ends at its forward-label horizon.  Purging this complete
        interval—not merely its decision row—prevents a fitted policy from
        learning either a test feature or a test outcome.
        """

        test_information = self._information_indices(rows, test_indices)
        embargoed = set(embargoed_indices)
        train_indices: list[int] = []
        purged_indices: list[int] = []
        for index in candidate_indices:
            if index in set(test_indices) or index in embargoed:
                continue
            if self._information_indices(rows, [index]) & test_information:
                purged_indices.append(index)
            else:
                train_indices.append(index)
        return train_indices, purged_indices

    def _information_indices(self, rows: list[dict[str, Any]], sample_indices: list[int]) -> set[int]:
        information: set[int] = set()
        for index in sample_indices:
            start = max(0, index - self.feature_lookback + 1)
            end = min(len(rows) - 1, index + self.label_horizon_bars)
            information.update(range(start, end + 1))
        return information

    def _partition_receipt(
        self,
        rows: list[dict[str, Any]],
        train_indices: list[int],
        test_indices: list[int],
    ) -> dict[str, Any]:
        train_information = self._information_indices(rows, train_indices)
        test_information = self._information_indices(rows, test_indices)
        overlap = sorted(train_information & test_information)
        train_rows = [rows[index] for index in sorted(train_information)]
        test_rows = [rows[index] for index in sorted(test_information)]
        return {
            "training_indices": list(train_indices),
            "test_indices": list(test_indices),
            "training_information_indices": sorted(train_information),
            "test_information_indices": sorted(test_information),
            "training_data_hash": self.data_version_hash(train_rows),
            "test_data_hash": self.data_version_hash(test_rows),
            "information_overlap_indices": overlap,
            "information_overlap_count": len(overlap),
            "partition_verified": not overlap and not (set(train_indices) & set(test_indices)),
        }

    def _fit_policy(
        self,
        request: StockRequest,
        rows: list[dict[str, Any]],
        train_indices: list[int],
        *,
        training_data_hash: str,
        strategy: StrategyEngine | None = None,
        fold_strategy_isolated: bool = False,
    ) -> dict[str, Any]:
        """Select a frozen confidence filter using training rows only.

        The production StrategyEngine is rule based today, so fitting means
        selecting the execution threshold from a versioned candidate set.  The
        selected state and its hash are persisted per fold, making a later
        model adapter replaceable without weakening the train/test boundary.
        """
        usable = [
            index
            for index in train_indices
            if index >= self.feature_lookback - 1 and index + self.label_horizon_bars < len(rows)
        ]
        candidates = (0.0, 0.25, 0.5, 0.75)
        candidate_scores: dict[str, float] = {}
        active_strategy = strategy or self.strategy
        for threshold in candidates:
            score = 0.0
            observations = 0
            for index in usable:
                row = rows[index]
                history_start = max(0, index - self.feature_lookback + 1)
                signal = active_strategy.generate_signal(
                    request,
                    MarketSnapshot(
                        symbol=request.symbol,
                        market=request.market,
                        price=row["close"],
                        ohlcv=[
                            {key: item[key] for key in ("timestamp", "open", "high", "low", "close", "volume")}
                            for item in rows[history_start : index + 1]
                        ],
                        raw={
                            "point_in_time": True,
                            "as_of": row["timestamp"],
                            "feature_window_start": rows[history_start]["timestamp"],
                        },
                    ),
                    self._intelligence(request, row["pit_intelligence"]),
                )
                if float(signal.confidence or 0.0) < threshold:
                    continue
                future = rows[index + self.label_horizon_bars]["close"]
                current = row["close"]
                direction = 1.0 if signal.action in {"buy", "add"} else -1.0 if signal.action in {"sell", "reduce"} else 0.0
                score += direction * ((future / current) - 1.0) if current else 0.0
                observations += 1
            candidate_scores[f"{threshold:.2f}"] = score / observations if observations else float("-inf")
        selected = max(candidates, key=lambda item: candidate_scores[f"{item:.2f}"])
        payload = {
            "schema_version": "open_stock_ai.replay_fitted_policy.v1",
            "selection_method": "train_only_confidence_threshold",
            "min_confidence": selected,
            "candidate_scores": candidate_scores,
            "train_sample_size": len(usable),
            "training_data_hash": training_data_hash,
            "training_indices": usable,
            "training_only": True,
            "fold_strategy_isolated": fold_strategy_isolated,
        }
        payload["fitted_policy_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    def _fill_pending_order(
        self,
        *,
        order: dict[str, Any],
        row: dict[str, Any],
        cash: float,
        position_quantity: int,
    ) -> dict[str, Any] | None:
        reference_price = float(row["open"])
        equity_before = cash + position_quantity * reference_price
        target_position = int(order["target_position"])
        buy_quote = self._cost_quote(order=order, row=row, side="buy")
        buy_rate = (buy_quote.commission_bps + buy_quote.exchange_fee_bps) / 10_000.0
        target_quantity = (
            int(floor(equity_before / (reference_price * (1.0 + buy_rate))))
            if target_position else 0
        )
        quantity_delta = target_quantity - position_quantity
        if quantity_delta == 0:
            return None
        side = "buy" if quantity_delta > 0 else "sell"
        requested_quantity = abs(quantity_delta)
        volume = max(0, int(row.get("volume") or 0))
        capacity = requested_quantity if volume <= 0 else int(floor(volume * self._participation_rate()))
        quantity = min(requested_quantity, capacity)
        if quantity <= 0:
            return None
        fill_price, cost_receipt = self._fill_price(
            reference_price=reference_price,
            side=side,
            quantity=quantity,
            volume=volume,
            row={**row, **_impact_fields(order)},
        )
        notional = quantity * fill_price
        fee_quote = self._cost_quote(order=order, row=row, side=side)
        fee_amounts = fee_quote.amounts(notional)
        fees = fee_amounts["fees"]
        if side == "buy":
            # Guard against a price/impact combination consuming more cash
            # than the decision-time target estimate allowed.
            affordable = int(floor(cash / (fill_price * (1.0 + buy_rate))))
            quantity = min(quantity, max(0, affordable))
            if quantity <= 0:
                return None
            fill_price, cost_receipt = self._fill_price(
                reference_price=reference_price,
                side=side,
                quantity=quantity,
                volume=volume,
                row={**row, **_impact_fields(order)},
            )
            notional = quantity * fill_price
            fee_amounts = fee_quote.amounts(notional)
            fees = fee_amounts["fees"]
            # Minimum commissions can make the rate-only affordability estimate
            # too optimistic; reduce deterministically until cash remains valid.
            while quantity > 0 and notional + fees > cash + 1e-9:
                quantity -= 1
                if quantity <= 0:
                    break
                fill_price, cost_receipt = self._fill_price(
                    reference_price=reference_price,
                    side=side,
                    quantity=quantity,
                    volume=volume,
                    row={**row, **_impact_fields(order)},
                )
                notional = quantity * fill_price
                fee_amounts = fee_quote.amounts(notional)
                fees = fee_amounts["fees"]
            if quantity <= 0:
                return None
            cash_delta = -(notional + fees)
            signed_quantity = quantity
        else:
            quantity = min(quantity, position_quantity)
            if quantity <= 0:
                return None
            notional = quantity * fill_price
            fee_amounts = fee_quote.amounts(notional)
            fees = fee_amounts["fees"]
            cash_delta = notional - fees
            signed_quantity = -quantity
        venue_cost_receipt = fee_quote.receipt(notional)
        return {
            "fill_id": f"replay-fill-{order['order_id']}",
            "order_id": order["order_id"],
            "side": side,
            "quantity": quantity,
            "signed_quantity": signed_quantity,
            "requested_quantity": requested_quantity,
            "partial_fill": quantity < requested_quantity,
            "reference_price": reference_price,
            "fill_price": fill_price,
            "notional": notional,
            "fees": fees,
            "commission": fee_amounts["commission"],
            "exchange_fee": fee_amounts["exchange_fee"],
            "sell_tax": fee_amounts["sell_tax"],
            "cash_delta": cash_delta,
            "cash_after": cash + cash_delta,
            "position_before": position_quantity,
            "position_after": position_quantity + signed_quantity,
            "feature_available_time": order["feature_available_time"],
            "decision_time": order["decision_time"],
            "order_time": order["order_time"],
            "broker_receive_time": row["timestamp"],
            "fill_time": row["timestamp"],
            "cost_model": {
                **cost_receipt,
                "venue_cost": venue_cost_receipt,
                "execution_evidence_eligible": (
                    cost_receipt["execution_evidence_eligible"] is True
                    and venue_cost_receipt["execution_evidence_eligible"] is True
                ),
            },
        }

    def _fill_price(
        self,
        *,
        reference_price: float,
        side: str,
        quantity: int,
        volume: int,
        row: dict[str, Any],
    ) -> tuple[float, dict[str, Any]]:
        quote = self.impact_schedule.quote(
            venue=str(row["venue"]),
            product_type=str(row["product_type"]),
            side=side,
            trade_at=str(row["timestamp"]),
            reference_price=reference_price,
            quantity=quantity,
            bid_ask_spread_bps=_number(row.get("spread_bps")),
            realized_volatility_bps=_number(row.get("realized_volatility_bps")),
            adv_volume_shares=_number(row.get("adv_volume_shares")),
            bar_volume_shares=volume,
        )
        if self.impact_coefficient_bps is not None or self.slippage_bps is not None:
            # Overrides are intentionally local sensitivity inputs.  They may
            # be useful to study a scenario but cannot replace an immutable,
            # empirically calibrated venue/product/side rule.
            quote = replace(
                quote,
                impact_coefficient_bps=(
                    max(0.0, float(self.impact_coefficient_bps))
                    if self.impact_coefficient_bps is not None
                    else quote.impact_coefficient_bps
                ),
                base_slippage_bps=(
                    max(0.0, float(self.slippage_bps))
                    if self.slippage_bps is not None
                    else quote.base_slippage_bps
                ),
                calibration_status="sensitivity_override",
                calibration_execution_evidence_eligible=False,
            )
        return quote.fill_price(), quote.receipt()

    def _point_in_time_impact_inputs(
        self,
        rows: list[dict[str, Any]],
        *,
        as_of_index: int,
    ) -> dict[str, Any]:
        """Derive ADV and realized volatility from only prior/known rows."""

        start = max(0, as_of_index - self.feature_lookback + 1)
        history = rows[start : as_of_index + 1]
        volumes = [float(item["volume"]) for item in history if float(item.get("volume") or 0) > 0]
        close_returns = [
            (float(history[index]["close"]) / float(history[index - 1]["close"]) - 1.0) * 10_000.0
            for index in range(1, len(history))
            if float(history[index - 1].get("close") or 0) > 0
        ]
        return {
            "adv_volume_shares": mean(volumes) if volumes else None,
            "realized_volatility_bps": pstdev(close_returns) if len(close_returns) >= 2 else None,
            "adv_window_bars": len(volumes),
            "volatility_window_returns": len(close_returns),
            "impact_inputs_as_of": rows[as_of_index]["timestamp"],
        }

    def _cost_quote(self, *, order: dict[str, Any], row: dict[str, Any], side: str):
        broker_commission = _number(order.get("broker_commission_bps"))
        quote = self.cost_schedule.quote(
            venue=order["venue"],
            product_type=order["product_type"],
            lot_type=order["lot_type"],
            side=side,
            trade_at=row["timestamp"],
            is_day_trade_offset=bool(order.get("is_day_trade_offset")),
            broker_commission_bps=broker_commission,
            broker_minimum_commission_twd=_number(order.get("broker_minimum_commission_twd")),
            broker_fee_schedule_id=str(order.get("broker_fee_schedule_id") or "") or None,
            exchange_fee_bps=_row_fee_or_default(order.get("exchange_fee_bps"), self.exchange_fee_bps),
            exchange_fee_schedule_id=str(order.get("exchange_fee_schedule_id") or "") or None,
            bond_etf_tax_exemption_eligible=_optional_bool(
                order.get("bond_etf_tax_exemption_eligible")
            ),
        )
        if broker_commission is None and self.transaction_cost_bps is not None:
            # A generic simulation assumption may model sensitivity, but must
            # never masquerade as an account-specific broker fee schedule.
            return replace(quote, commission_bps=max(0.0, float(self.transaction_cost_bps)))
        return quote

    def _participation_rate(self) -> float:
        return min(1.0, max(0.0, float(self.max_participation_rate)))

    def _cost_model_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.replay_cost_model.v3",
            "schedule_version": self.cost_schedule.rulebook.snapshot_id,
            "schedule_sha256": self.cost_schedule.rulebook.snapshot_sha256,
            "commission_bps": self.cost_schedule.standard_commission_bps,
            "generic_commission_sensitivity_bps": self.transaction_cost_bps,
            "exchange_fee_bps": (
                max(0.0, self.exchange_fee_bps)
                if self.exchange_fee_bps is not None
                else None
            ),
            "seller_tax_source": "venue_product_date_schedule",
            "market_impact_schedule": self.impact_schedule.rulebook.receipt(),
            "generic_base_slippage_sensitivity_bps": self.slippage_bps,
            "generic_impact_coefficient_sensitivity_bps": self.impact_coefficient_bps,
            "adv_window_bars": self.feature_lookback,
            "volatility_estimator": "point_in_time_close_return_population_stddev_bps",
            "max_participation_rate": self._participation_rate(),
            "components": ["commission", "exchange_fee", "sell_tax", "bid_ask_spread", "realized_volatility", "adv_and_bar_participation_impact"],
            "requires_venue_product_lot_date": True,
            "requires_broker_commission_schedule_for_execution_evidence": True,
            "requires_exchange_fee_schedule_for_execution_evidence": True,
            "requires_pit_market_impact_inputs_for_execution_evidence": True,
            "requires_empirically_calibrated_market_impact_rule_for_execution_evidence": True,
        }

    def _certification_receipt(
        self,
        *,
        accounting_verified: bool,
        lifecycle_verified: bool,
        cost_schedule_verified: bool,
        walk_forward_passed: bool,
        purged_cv_passed: bool,
        historical_universe_verified: bool,
        benchmark_metrics_verified: bool,
        monte_carlo_tail_risk_verified: bool,
    ) -> dict[str, Any]:
        """Return evidence-derived research certification, never a promise.

        ``strategy_replay_exact`` is intentionally a projection of this
        receipt rather than an independently set boolean.  Any missing ledger,
        time-contract, cost, fit/freeze or leakage proof revokes certification.
        """

        evidence = {
            "ledger_accounting_identity_verified": accounting_verified,
            "timestamp_lifecycle_verified": lifecycle_verified,
            "venue_and_broker_cost_schedule_verified": cost_schedule_verified,
            "walk_forward_fit_freeze_verified": walk_forward_passed,
            "purged_cv_and_embargo_verified": purged_cv_passed,
            "historical_universe_membership_verified": historical_universe_verified,
            "benchmark_and_cash_rate_alignment_verified": benchmark_metrics_verified,
            "monte_carlo_tail_risk_stability_verified": monte_carlo_tail_risk_verified,
        }
        certified = all(evidence.values())
        return {
            "schema_version": "open_stock_ai.research_certification.v1",
            "certification_kind": "event_driven_point_in_time_replay",
            "certified": certified,
            "status": "certified" if certified else "not_certified",
            "evidence": evidence,
            "missing_evidence": [name for name, verified in evidence.items() if not verified],
        }

    def _approval_blockers(
        self,
        *,
        accounting_verified: bool,
        lifecycle_verified: bool,
        cost_schedule_verified: bool,
        walk_forward_passed: bool,
        purged_cv_passed: bool,
        metric_thresholds: bool,
        regime_robustness_passed: bool,
        historical_universe_verified: bool,
        benchmark_metrics_verified: bool,
        monte_carlo_tail_risk_verified: bool,
        passed: bool,
    ) -> list[str]:
        blockers: list[str] = []
        if not accounting_verified:
            blockers.append("ledger_accounting_identity_failed")
        if not lifecycle_verified:
            blockers.append("timestamp_lifecycle_validation_failed")
        if not cost_schedule_verified:
            blockers.append("venue_product_date_or_broker_cost_schedule_not_verified")
        if not walk_forward_passed:
            blockers.append("walk_forward_fit_freeze_or_accounting_failed")
        if not purged_cv_passed:
            blockers.append("purged_cross_validation_or_embargo_failed")
        if not metric_thresholds:
            blockers.append("exact_replay_metrics_gate_failed")
        if not regime_robustness_passed:
            blockers.append("regime_robustness_gate_failed")
        if not historical_universe_verified:
            blockers.append("historical_universe_membership_gate_failed")
        if not benchmark_metrics_verified:
            blockers.append("benchmark_and_cash_rate_alignment_gate_failed")
        if not monte_carlo_tail_risk_verified:
            blockers.append("monte_carlo_tail_risk_stability_gate_failed")
        if not passed and not blockers:
            blockers.append("empirical_validation_not_certified")
        return blockers

    def _intelligence(self, request: StockRequest, payload: dict[str, Any]) -> IntelligenceResult:
        return IntelligenceResult(
            symbol=request.symbol,
            market=request.market,
            summary=str(payload.get("summary") or "Point-in-time intelligence"),
            sentiment_score=_number(payload.get("sentiment_score")),
            sentiment_label=payload.get("sentiment_label"),
            fundamental_view=payload.get("fundamental_view"),
            technical_view=payload.get("technical_view"),
            news_view=payload.get("news_view"),
            risks=[str(item) for item in payload.get("risks") or []],
            evidence=[dict(item) for item in payload.get("evidence") or [] if isinstance(item, dict)],
            raw=dict(payload.get("raw") or {}),
        )

    def _blocked(self, request: StockRequest, rows: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.exact_strategy_replay.v2",
            "backtest_id": None,
            "validation_kind": "production_strategy_engine_point_in_time_replay",
            "strategy_replay_exact": False,
            "empirical_valid": False,
            "research_certification": {
                "schema_version": "open_stock_ai.research_certification.v1",
                "certification_kind": "event_driven_point_in_time_replay",
                "certified": False,
                "status": "not_certified",
                "evidence": {},
                "missing_evidence": ["complete_point_in_time_dataset"],
            },
            "execution_evidence_eligible": False,
            "live_execution_evidence_eligible": False,
            "passed": False,
            "lookahead_safe": False,
            "point_in_time_verified": False,
            "accounting_verified": False,
            "lifecycle_verified": False,
            "transaction_costs_included": True,
            "transaction_cost_bps": self.cost_schedule.standard_commission_bps,
            "cost_schedule_verified": False,
            "slippage_included": True,
            "slippage_bps": self.slippage_bps,
            "cost_model": self._cost_model_receipt(),
            "regime_robustness": evaluate_regime_robustness([], []),
            "benchmark_metrics": evaluate_benchmark_metrics([], None, None, evaluation_timestamps=[]),
            "monte_carlo_tail_risk": simulate_tail_risk([]),
            "regime_observations": [],
            "return_timestamps": [],
            "benchmark_observations": [],
            "risk_free_observations": [],
            "regime_position_weights": [],
            "historical_universe": {
                "schema_version": "open_stock_ai.historical_universe.v1",
                "passed": False,
                "point_in_time_verified": False,
                "universe_coverage_complete": False,
                "universe_scope_ids": [],
                "row_count": 0,
                "manifest_hashes": [],
                "blockers": ["complete_historical_universe_membership"],
            },
            "universe_membership_rows": [],
            "strategy_version_hash": self.strategy_version_hash(),
            "data_version_hash": self.data_version_hash(rows),
            "sample_size": len(rows),
            "trade_count": 0,
            "decision_audit": [],
            "order_ledger": [],
            "broker_receipt_ledger": [],
            "event_ledger": [],
            "fill_ledger": [],
            "cash_ledger": [],
            "position_ledger": [],
            "equity_ledger": [],
            "accounting_identity": [],
            "approval_blockers": list(dict.fromkeys(blockers)),
            "requested_symbol": request.symbol,
            "note": "Exact replay requires a complete strictly ordered point-in-time intelligence dataset.",
        }

    def _sharpe(self, values: list[float]) -> float:
        return float(calculate_performance_metrics(values)["sharpe"] or 0.0)

    @staticmethod
    def _position_weights(
        positions: list[dict[str, Any]], equity: list[dict[str, Any]],
    ) -> list[float]:
        """Derive exposure only from the marked position/equity ledgers."""

        if len(positions) != len(equity):
            raise ValueError("position and equity ledgers must be aligned")
        return [
            float(position.get("market_value") or 0.0) / float(mark.get("equity") or 1.0)
            for position, mark in zip(positions, equity)
        ]

    def _compound_return(self, values: list[float]) -> float:
        equity = 1.0
        for value in values:
            equity *= 1.0 + value
        return round((equity - 1.0) * 100.0, 3)

    def _max_drawdown(self, values: list[float]) -> float:
        return float(calculate_performance_metrics(values)["max_drawdown_pct"])


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _impact_fields(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "adv_volume_shares": order.get("adv_volume_shares"),
        "realized_volatility_bps": order.get("realized_volatility_bps"),
        "adv_window_bars": order.get("adv_window_bars"),
        "volatility_window_returns": order.get("volatility_window_returns"),
        "impact_inputs_as_of": order.get("impact_inputs_as_of"),
    }


def _row_fee_or_default(value: Any, default: float | None) -> float | None:
    """Keep an explicit zero fee from being overwritten by a local default."""
    parsed = _number(value)
    return (float(default) if default is not None else None) if parsed is None else parsed


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes"}:
        return True
    if normalized in {"false", "no"}:
        return False
    raise ValueError("invalid_optional_boolean")
