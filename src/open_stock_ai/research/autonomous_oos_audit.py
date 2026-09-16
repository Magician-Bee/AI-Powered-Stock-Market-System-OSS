from __future__ import annotations

"""Read-only audit of retained autonomous candle OOS evaluations.

The audit verifies retained identities, chronological partitions, accounting
flags and cost inclusion.  It deliberately does not pool independent-symbol
ledgers into a fictional shared-cash portfolio or turn exploratory holdout
results into positive-expectancy qualification.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from open_stock_ai.strategy.provenance import content_hash


SCHEMA_VERSION = "open_stock_ai.autonomous_oos_cycle_audit.v1"


def _timestamp(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return result.astimezone(timezone.utc)


def _retained_id(account_id: str, kind: str, payload: dict[str, Any]) -> str:
    return "AE-" + content_hash({"account_id": account_id, "kind": kind, "payload": payload})


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _phase(qualification: dict[str, Any], name: str) -> dict[str, Any]:
    part = (qualification.get("partitions") or {}).get(name) or {}
    expectancy = part.get("net_expectancy") or {}
    performance = part.get("performance") or {}
    return {
        "bar_count": part.get("bar_count"),
        "closed_trade_count": expectancy.get("closed_trade_count"),
        "mean_net_trade_return_pct": expectancy.get("mean_net_return_pct"),
        "lower_confidence_bound_pct": expectancy.get("lower_confidence_bound_pct"),
        "portfolio_compounded_return_pct": performance.get("compounded_return_pct"),
        "max_drawdown_pct": performance.get("max_drawdown_pct"),
        "fill_count": part.get("fill_count"),
        "total_fees": part.get("total_fees"),
        "accounting_verified": part.get("accounting_verified") is True,
    }


def audit_research_cycle(
    database: str | Path,
    *,
    cycle_id: str | None = None,
    account_id: str = "autonomous-paper-v1",
) -> dict[str, Any]:
    """Audit one retained cycle without changing the runtime database."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError("autonomous_runtime_database_not_found")
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("pragma query_only=on")
    try:
        if cycle_id:
            row = connection.execute(
                "select cycle_id,payload_json from autonomous_research_cycles "
                "where account_id=? and cycle_id=?",
                (account_id, cycle_id),
            ).fetchone()
        else:
            row = connection.execute(
                "select cycle_id,payload_json from autonomous_research_cycles "
                "where account_id=? order by created_at desc limit 1",
                (account_id,),
            ).fetchone()
        if row is None:
            raise ValueError("autonomous_research_cycle_not_found")
        selected_cycle_id = str(row["cycle_id"])
        cycle = json.loads(row["payload_json"])
        violations: list[str] = []
        expected_cycle_id = "AC-" + content_hash({key: value for key, value in cycle.items() if key != "cycle_id"})
        if selected_cycle_id != expected_cycle_id or cycle.get("cycle_id") != selected_cycle_id:
            violations.append("cycle_content_identity_mismatch")
        if cycle.get("account_id") != account_id:
            violations.append("cycle_account_mismatch")
        try:
            cycle_time = _timestamp(cycle.get("created_at"))
        except (TypeError, ValueError):
            cycle_time = datetime.min.replace(tzinfo=timezone.utc)
            violations.append("cycle_timestamp_invalid")

        items: list[dict[str, Any]] = []
        reason_counts: Counter[str] = Counter()
        for result in cycle.get("results") or []:
            symbol = str(result.get("symbol") or "")
            history_id = str(result.get("history_id") or "")
            history_row = connection.execute(
                "select kind,payload_json from autonomous_evidence "
                "where account_id=? and evidence_id=?",
                (account_id, history_id),
            ).fetchone()
            history: dict[str, Any] = {}
            history_valid = False
            temporal_valid = False
            if history_row is None or history_row["kind"] != "price_history":
                violations.append(f"{symbol}:price_history_missing")
            else:
                history = json.loads(history_row["payload_json"])
                history_valid = _retained_id(account_id, "price_history", history) == history_id
                if not history_valid:
                    violations.append(f"{symbol}:price_history_identity_mismatch")
                bars = history.get("rows") or []
                try:
                    stamps = [_timestamp(bar.get("timestamp") or bar.get("date")) for bar in bars]
                    temporal_valid = bool(stamps) and all(
                        left < right for left, right in zip(stamps, stamps[1:])
                    ) and stamps[-1] <= cycle_time
                except (TypeError, ValueError):
                    temporal_valid = False
                if not temporal_valid:
                    violations.append(f"{symbol}:history_temporal_contract_failed")
                evidence = history.get("data_evidence") or {}
                if evidence.get("data_sha256") not in {None, content_hash(bars)}:
                    violations.append(f"{symbol}:history_data_hash_mismatch")

            for candidate in result.get("candidates") or []:
                qualification_id = str(candidate.get("qualification_id") or "")
                receipt_row = connection.execute(
                    "select kind,payload_json from autonomous_evidence "
                    "where account_id=? and evidence_id=?",
                    (account_id, qualification_id),
                ).fetchone()
                if receipt_row is None or receipt_row["kind"] != "qualification":
                    violations.append(f"{symbol}:{qualification_id}:qualification_missing")
                    continue
                qualification = json.loads(receipt_row["payload_json"])
                receipt_identity_valid = _retained_id(account_id, "qualification", qualification) == qualification_id
                receipt_hash_valid = content_hash({
                    key: value for key, value in qualification.items() if key != "receipt_sha256"
                }) == qualification.get("receipt_sha256")
                candidate_id = str(qualification.get("candidate_id") or "")
                if not receipt_identity_valid:
                    violations.append(f"{symbol}:{candidate_id}:qualification_identity_mismatch")
                if not receipt_hash_valid:
                    violations.append(f"{symbol}:{candidate_id}:qualification_receipt_hash_mismatch")
                if qualification.get("symbol") != symbol or candidate.get("strategy_id") != candidate_id:
                    violations.append(f"{symbol}:{candidate_id}:qualification_scope_mismatch")
                if history and qualification.get("data_version_hash") != content_hash(history.get("rows") or []):
                    violations.append(f"{symbol}:{candidate_id}:qualification_history_hash_mismatch")

                manifest = qualification.get("evaluation_manifest") or {}
                boundaries_valid = True
                ranges: list[tuple[int, int]] = []
                for phase_name in ("train", "validation", "holdout"):
                    bounds = manifest.get(phase_name)
                    if not (
                        isinstance(bounds, list)
                        and len(bounds) == 2
                        and all(isinstance(value, int) and not isinstance(value, bool) for value in bounds)
                        and 0 <= bounds[0] < bounds[1] <= int(qualification.get("valid_row_count") or 0)
                    ):
                        boundaries_valid = False
                        continue
                    ranges.append((bounds[0], bounds[1]))
                    part = (qualification.get("partitions") or {}).get(phase_name) or {}
                    if int(part.get("bar_count") or -1) != bounds[1] - bounds[0]:
                        boundaries_valid = False
                if len(ranges) != 3 or ranges[0][1] != ranges[1][0] or ranges[1][1] != ranges[2][0]:
                    boundaries_valid = False
                if manifest.get("split_basis") != "chronological_indices_before_evaluation":
                    boundaries_valid = False
                if not boundaries_valid:
                    violations.append(f"{symbol}:{candidate_id}:chronological_partition_contract_failed")

                costs = manifest.get("cost_assumptions") or {}
                cost_fields = (
                    "broker_commission_bps",
                    "broker_minimum_commission_twd",
                    "spread_bps",
                    "historical_impact_sensitivity_bps",
                    "adverse_execution_floor_bps",
                )
                costs_present = all(_number(costs.get(field)) is not None for field in cost_fields)
                accounting_verified = all(
                    ((qualification.get("partitions") or {}).get(name) or {}).get("accounting_verified") is True
                    for name in ("train", "validation", "holdout")
                )
                if not costs_present:
                    violations.append(f"{symbol}:{candidate_id}:cost_assumptions_missing")
                if not accounting_verified:
                    violations.append(f"{symbol}:{candidate_id}:partition_accounting_failed")
                reasons = [str(value) for value in qualification.get("reasons") or []]
                reason_counts.update(reasons)
                items.append({
                    "symbol": symbol,
                    "candidate_id": candidate_id,
                    "history_id": history_id,
                    "qualification_id": qualification_id,
                    "history_identity_valid": history_valid,
                    "history_temporal_contract_valid": temporal_valid,
                    "qualification_identity_valid": receipt_identity_valid,
                    "qualification_receipt_hash_valid": receipt_hash_valid,
                    "chronological_partitions_valid": boundaries_valid,
                    "cost_assumptions_present": costs_present,
                    "partition_accounting_verified": accounting_verified,
                    "evaluation_family_size": manifest.get("evaluation_family_size"),
                    "research_paper_candidate_eligible": qualification.get("research_paper_candidate_eligible") is True,
                    "positive_ev_qualified": qualification.get("positive_ev_qualified") is True,
                    "validation": _phase(qualification, "validation"),
                    "holdout": _phase(qualification, "holdout"),
                    "qualification_reasons": reasons,
                })
    finally:
        connection.close()

    phases: dict[str, Any] = {}
    for name in ("validation", "holdout"):
        phase_items = [item[name] for item in items]
        phases[name] = {
            "receipt_count": len(phase_items),
            "bar_counts": sorted({int(item["bar_count"]) for item in phase_items if isinstance(item.get("bar_count"), int)}),
            "closed_trade_count_sum_across_independent_ledgers": sum(int(item.get("closed_trade_count") or 0) for item in phase_items),
            "total_fees_sum_across_independent_ledgers": round(sum(float(item.get("total_fees") or 0.0) for item in phase_items), 6),
            "positive_compounded_return_receipts": sum((_number(item.get("portfolio_compounded_return_pct")) or 0.0) > 0 for item in phase_items),
            "positive_mean_closed_trade_receipts": sum((_number(item.get("mean_net_trade_return_pct")) or 0.0) > 0 for item in phase_items),
            "scope": "descriptive_sum_of_isolated_symbol_ledgers_not_a_shared_cash_portfolio_or_selection_rule",
        }
    audit = {
        "schema_version": SCHEMA_VERSION,
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "database": str(path),
        "account_id": account_id,
        "cycle_id": selected_cycle_id,
        "cycle_created_at": cycle.get("created_at"),
        "cycle_identity_valid": selected_cycle_id == expected_cycle_id,
        "deep_result_count": len(cycle.get("results") or []),
        "qualification_receipt_count": len(items),
        "integrity_passed": not violations,
        "integrity_violations": violations,
        "research_paper_candidate_eligible_count": sum(item["research_paper_candidate_eligible"] for item in items),
        "positive_ev_qualified_count": sum(item["positive_ev_qualified"] for item in items),
        "phases": phases,
        "qualification_reason_counts": dict(sorted(reason_counts.items())),
        "items": items,
        "conclusion": {
            "historical_oos_executed": bool(items) and not violations,
            "positive_expectancy_established": any(item["positive_ev_qualified"] for item in items),
            "selection_permitted_from_holdout": False,
            "forward_trial_still_required": True,
            "boundary": "exploratory_per_symbol_oos; holdout_results_must_not_choose_the_forward_policy",
        },
    }
    audit["receipt_sha256"] = content_hash(audit)
    return audit
