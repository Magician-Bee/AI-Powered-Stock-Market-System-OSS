from __future__ import annotations

"""Bounded, source-scoped evaluation of frozen candle research hypotheses.

The evaluator shares the event/fill ledger with ExactStrategyReplay but does
not invent fundamental PIT features or claim historical-universe certification
for a single supplied symbol. Qualification is separate from permission to
collect bounded paper observations, and never grants live trading permission.
"""

from copy import deepcopy
import hashlib
import json
import re
from datetime import datetime
from math import ceil, isfinite, sqrt
from random import Random
from statistics import mean
from typing import Any

from open_stock_ai.research.performance_metrics import calculate_performance_metrics
from open_stock_ai.research.strategy_replay import ExactStrategyReplay
from open_stock_ai.strategy.candle_candidates import CandleCandidate
from open_stock_ai.strategy.provenance import content_hash
from open_stock_ai.types import StockRequest


SCHEMA_VERSION = "open_stock_ai.candle_qualification.v1"
QUALIFICATION_REQUIREMENTS = {
    "validation_minimum_bars": 60, "validation_minimum_closed_trades": 10,
    "holdout_minimum_bars": 120, "holdout_minimum_closed_trades": 30,
    "fixed_candidate_count": 2, "family_alpha": 0.05,
    "source_evidence_required": ["corporate_actions_verified", "historical_vintage_verified",
                                 "execution_costs_verified", "source_provenance_verified"],
}


def _closed_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for fill in fills:
        quantity = int(fill["quantity"])
        if fill["side"] == "buy":
            entries.append({"remaining": quantity, "quantity": quantity,
                            "unit_cost": (fill["notional"] + fill["fees"]) / quantity,
                            "entry_notional": fill["notional"], "entry_time": fill["fill_time"],
                            "net_pnl": 0.0, "exit_count": 0})
            continue
        net_proceeds = (fill["notional"] - fill["fees"]) / quantity
        # Replay liquidates explicit entry lots for protective exits. Candidate
        # entries never pyramid, so FIFO and per-lot matching are identical.
        for entry in entries:
            matched = min(quantity, entry["remaining"])
            if not matched:
                continue
            entry["net_pnl"] += matched * (net_proceeds - entry["unit_cost"])
            entry["remaining"] -= matched
            quantity -= matched
            entry["exit_count"] += 1
            if entry["remaining"] == 0:
                closed.append({"entry_time": entry["entry_time"], "exit_time": fill["fill_time"],
                               "quantity": entry["quantity"], "net_pnl": entry["net_pnl"],
                               "net_return_pct": 100*entry["net_pnl"]/entry["entry_notional"]})
            if quantity == 0:
                break
    return closed


def _expectancy(trades: list[dict[str, Any]], family_size: int = 2) -> dict[str, Any]:
    values = [float(t["net_return_pct"]) for t in trades]
    if not values:
        return {"closed_trade_count": 0, "mean_net_return_pct": None,
                "mean_net_pnl": None, "lower_confidence_bound_pct": None,
                "block_length": None, "resamples": 0}
    if len(values) < 5:
        return {"closed_trade_count": len(values), "mean_net_return_pct": mean(values),
                "mean_net_pnl": mean(t["net_pnl"] for t in trades),
                "lower_confidence_bound_pct": None, "block_length": None, "resamples": 0,
                "evaluation_family_size": family_size, "tail_resample_count": 0,
                "method": "insufficient_closed_trades_for_uncertainty_estimation"}
    block = max(1, ceil(sqrt(len(values))))
    random = Random(20260911)
    means = []
    for _ in range(1000):
        sample: list[float] = []
        while len(sample) < len(values):
            start = random.randrange(len(values))
            sample.extend(values[(start+i) % len(values)] for i in range(block))
        means.append(mean(sample[:len(values)]))
    means.sort()
    alpha = QUALIFICATION_REQUIREMENTS["family_alpha"] / family_size
    return {"closed_trade_count": len(values), "mean_net_return_pct": mean(values),
            "mean_net_pnl": mean(t["net_pnl"] for t in trades),
            "lower_confidence_bound_pct": means[int(alpha * len(means))],
            "block_length": block, "resamples": len(means),
            "method": "circular_block_bootstrap_trade_sequence",
            "family_adjusted_one_sided_alpha": alpha,
            "evaluation_family_size": family_size,
            "tail_resample_count": alpha * len(means),
            "scope_limit": "declared_candidate_instrument_family_not_all_historical_strategy_search"}


def _qualification_reasons(receipt: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if receipt.get("research_paper_candidate_eligible") is not True:
        reasons.append("candidate_input_contract_not_eligible")
    evidence = receipt.get("data_evidence") or {}
    if evidence.get("source_kind") not in {"exchange_official", "licensed_vendor"}:
        reasons.append("real_market_source_not_verified")
    for field in QUALIFICATION_REQUIREMENTS["source_evidence_required"]:
        if evidence.get(field) is not True:
            reasons.append(field + "_missing")
    manifest = receipt.get("evaluation_manifest") or {}
    if manifest.get("selection_method") != "fixed_parameters_no_validation_or_holdout_selection":
        reasons.append("frozen_candidate_protocol_missing")
    if manifest.get("registered_frozen_configuration") is not True:
        reasons.append("configuration_requires_new_preregistered_candidate_family")
    family_size = manifest.get("evaluation_family_size")
    if not isinstance(family_size, int) or isinstance(family_size, bool) or family_size < 2:
        reasons.append("declared_candidate_instrument_family_size_missing")
    context = manifest.get("evaluation_context") or {}
    if context.get("purpose") == "exploratory_market_campaign":
        reasons.append("exploratory_campaign_requires_independent_locked_confirmation")
    for name in ("validation", "holdout"):
        part = (receipt.get("partitions") or {}).get(name) or {}
        expectation = part.get("net_expectancy") or {}
        if int(part.get("bar_count") or 0) < QUALIFICATION_REQUIREMENTS[name + "_minimum_bars"]:
            reasons.append(name + "_insufficient_bars")
        if int(expectation.get("closed_trade_count") or 0) < QUALIFICATION_REQUIREMENTS[name + "_minimum_closed_trades"]:
            reasons.append(name + "_insufficient_closed_trades")
        if expectation.get("evaluation_family_size") != family_size or float(expectation.get("tail_resample_count") or 0) < 5:
            reasons.append(name + "_multiple_test_tail_resolution_insufficient")
        lower = expectation.get("lower_confidence_bound_pct")
        if not isinstance(lower, (int, float)) or not isfinite(lower) or lower <= 0:
            reasons.append(name + "_positive_net_expectancy_not_established")
        if part.get("accounting_verified") is not True:
            reasons.append(name + "_accounting_not_verified")
        period_return = (part.get("performance") or {}).get("compounded_return_pct")
        if not isinstance(period_return, (int, float)) or not isfinite(period_return) or period_return <= 0:
            reasons.append(name + "_positive_marked_portfolio_return_not_established")
    return reasons


def verify_qualification_receipt(
    receipt: dict[str, Any], *, strategy_id: str | None = None,
    strategy_version_hash: str | None = None,
) -> bool:
    """Check content integrity and EV requirements, not external attestation.

    The host must retrieve the receipt from its trusted local evaluation store;
    a hash does not make model/client-supplied provenance trustworthy.
    """
    try:
        if receipt.get("schema_version") != SCHEMA_VERSION:
            return False
        payload = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        if content_hash(payload) != receipt.get("receipt_sha256"):
            return False
        if strategy_id is not None and strategy_id != receipt.get("candidate_id"):
            return False
        if strategy_version_hash is not None and strategy_version_hash != receipt.get("strategy_version_hash"):
            return False
        return receipt.get("passed") is True and receipt.get("positive_ev_qualified") is True and not _qualification_reasons(receipt)
    except (TypeError, ValueError, OverflowError, AttributeError, KeyError):
        return False


def evaluate_candidate(
    candidate: CandleCandidate, request: StockRequest, rows: list[dict[str, Any]], *,
    data_evidence: dict[str, Any], initial_cash: float = 1_000_000.0,
    cost_assumptions: dict[str, Any] | None = None,
    split_indices: tuple[int, int] | None = None,
    evaluation_family_size: int = 2,
    evaluation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a frozen candidate on disjoint train/validation/holdout ledgers.

    Splits depend only on row count (or caller-frozen indices), never prices or
    measured returns. Previous bars supply warmup only. No fitting occurs and
    no test-result-based ranking changes the parameters. All costs remain in
    the common ledger; assumption costs are not called broker-certified.
    """
    evidence = deepcopy(data_evidence)
    costs = {"venue": "TPEX" if request.symbol.endswith(".TWO") else "TWSE", "product_type": "stock", "lot_type": "odd_lot",
             "broker_commission_bps": 14.25, "broker_minimum_commission_twd": 20.0,
             "exchange_fee_bps": 0.0, "spread_bps": 5.0,
             "historical_impact_sensitivity_bps": 15.0, **(cost_assumptions or {})}
    normalized: list[dict[str, Any]] = []
    previous: datetime | None = None
    input_reasons: list[str] = []
    if isinstance(evaluation_family_size, bool) or not isinstance(evaluation_family_size, int) or not 2 <= evaluation_family_size <= 10000:
        input_reasons.append("invalid_evaluation_family_size")
    if request.market != "TW" or not re.fullmatch(r"\d{4,6}\.(TW|TWO)", request.symbol):
        input_reasons.append("canonical_taiwan_instrument_identity_required")
    if evidence.get("symbol") is not None and evidence["symbol"] != request.symbol:
        input_reasons.append("source_instrument_identity_mismatch")
    if evidence.get("coverage_complete") is False:
        input_reasons.append("declared_source_range_incomplete")
    if evidence.get("instrument_identity_verified") is False:
        input_reasons.append("instrument_identity_not_verified")
    for index, source in enumerate(rows):
        try:
            stamp = str(source.get("timestamp") or source.get("date") or "")
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if previous is not None and observed <= previous:
                raise ValueError("non_monotonic_or_duplicate_bar")
            if previous is not None and (observed - previous).days > 20:
                raise ValueError("unresolved_long_gap_in_candle_sequence")
            previous = observed
            bar = {k: float(source[k]) for k in ("open", "high", "low", "close", "volume")}
            if any(not isfinite(v) or v <= 0 for v in bar.values()):
                raise ValueError("nonpositive_or_nonfinite_ohlcv")
            if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
                raise ValueError("inconsistent_ohlc_range")
            normalized.append({
                **bar, **costs, "timestamp": observed.isoformat(), "available_at": observed.isoformat(),
                "pit_intelligence": {}, "universe_membership": {"passed": False,
                    "scope": "declared_single_symbol_history_no_full_market_pit_claim"},
                "broker_fee_schedule_id": None, "exchange_fee_schedule_id": None,
                "bond_etf_tax_exemption_eligible": costs.get("bond_etf_tax_exemption_eligible"),
                "is_day_trade_offset": False,
            })
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            input_reasons.append(f"invalid_bar:{index}:{exc}")
    count = len(normalized)
    warmup = candidate.slow_window + 1
    train_end, validation_end = split_indices or (floor_fraction(count, .70), floor_fraction(count, .85))
    if not warmup < train_end < validation_end < count:
        input_reasons.append("insufficient_or_invalid_chronological_split")
    if evidence.get("source_kind") not in {"exchange_official", "licensed_vendor"}:
        input_reasons.append("real_market_source_not_verified")
    if not str(evidence.get("source_id") or "").strip():
        input_reasons.append("source_identity_missing")
    try:
        data_hash = content_hash(rows)
    except (TypeError, ValueError):
        data_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
    if evidence.get("data_sha256") is not None and evidence["data_sha256"] != data_hash:
        input_reasons.append("source_data_hash_mismatch")
    manifest = {
        "schema_version": "open_stock_ai.frozen_candle_evaluation.v1",
        "candidate_id": candidate.candidate_id, "configuration": candidate.policy_metadata()["configuration"],
        "registered_frozen_configuration": candidate.policy_metadata()["registered_frozen_configuration"],
        "selection_method": "fixed_parameters_no_validation_or_holdout_selection",
        "candidate_count": 2, "warmup_bars": warmup,
        "evaluation_family_size": evaluation_family_size,
        "train": [warmup, train_end], "validation": [train_end, validation_end],
        "holdout": [validation_end, count], "split_basis": "chronological_indices_before_evaluation",
        "position_reset_at_partition_boundary": True, "ending_open_positions": "marked_not_counted_as_closed_trades",
        "daily_bar_timestamp_semantics": "bar_labels_not_broker_wall_clock_receipts",
        "cost_assumptions": costs,
    }
    if evaluation_context is not None:
        # This protocol is supplied before evaluating returns. An exploratory
        # rotating campaign cannot promote repeatedly observed holdout windows
        # into fresh confirmation merely because a confidence interval passes.
        manifest["evaluation_context"] = deepcopy(evaluation_context)
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "candidate_id": candidate.candidate_id,
        "strategy_version_hash": candidate.strategy_version_hash(),
        "configuration": candidate.policy_metadata()["configuration"],
        "evaluation_manifest": manifest, "evaluation_manifest_sha256": content_hash(manifest),
        "data_version_hash": data_hash,
        "data_evidence": evidence,
        "symbol": request.symbol, "input_row_count": len(rows), "valid_row_count": count,
        "research_paper_candidate_eligible": not input_reasons,
        "input_reasons": input_reasons, "partitions": {},
        "full_market_point_in_time_certified": False, "live_execution_eligible": False,
        "qualification_requirements": deepcopy(QUALIFICATION_REQUIREMENTS),
    }
    if not input_reasons:
        replay = ExactStrategyReplay(strategy=candidate, initial_cash=initial_cash,
                                     feature_lookback=warmup, max_participation_rate=.01,
                                     historical_impact_sensitivity_bps=costs["historical_impact_sensitivity_bps"],
                                     adverse_execution_floor_bps=costs.get("adverse_execution_floor_bps"))
        for name in ("train", "validation", "holdout"):
            start, stop = manifest[name]
            ledger = replay._replay_slice(request, normalized, start, stop, strategy=candidate)
            trades = _closed_trades(ledger["fill_ledger"])
            receipt["partitions"][name] = {
                "start": normalized[start]["timestamp"], "end": normalized[stop-1]["timestamp"],
                "bar_count": stop-start, "net_expectancy": _expectancy(trades, evaluation_family_size),
                "performance": calculate_performance_metrics(ledger["returns"]),
                "net_period_returns": ledger["returns"], "closed_trades": trades,
                "fill_count": len(ledger["fill_ledger"]),
                "total_fees": sum(f["fees"] for f in ledger["fill_ledger"]),
                "ending_open_quantity": ledger["position_ledger"][-1]["quantity"],
                "accounting_verified": ledger["accounting_verified"],
                "ledger_sha256": content_hash({k: ledger[k] for k in ("fill_ledger", "equity_ledger")}),
            }
        receipt["replay_version_hash"] = replay.strategy_version_hash()
    receipt["reasons"] = _qualification_reasons(receipt)
    receipt["net_expectancy"] = (receipt["partitions"].get("validation") or {}).get("net_expectancy")
    receipt["ranking_partition"] = "validation_only_holdout_reserved_for_qualification"
    receipt["positive_ev_qualified"] = receipt["passed"] = not receipt["reasons"]
    receipt["receipt_sha256"] = content_hash(receipt)
    return receipt


def floor_fraction(count: int, fraction: float) -> int:
    return int(count * fraction)
