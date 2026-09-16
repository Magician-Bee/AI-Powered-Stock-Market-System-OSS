#!/usr/bin/env python3
"""Audit one autonomous market update against durable source ingestion runs.

Integrity passing means the retained research receipt can be correlated to the
exact read-only source runs.  It does not mean every source refreshed, that an
update deadline was met, or that M2/task 08 is complete.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any


CYCLE_SCHEMA = "open_stock_ai.autonomous_research_cycle.v1"
REFRESH_SCHEMA = "stock_ai.autonomous_security_master_refresh.v1"
SLO_DECLARATION_SCHEMA = "stock_ai.autonomous_market_update_slo_declaration.v1"
SLO_POLICY_SCHEMA = "stock_ai.autonomous_market_update_slo_policy.v1"
ATTEMPTED_STATUSES = {"succeeded", "failed", "paused"}
CANONICAL_UPDATE_SLO_POLICY = {
    "schema_version": SLO_POLICY_SCHEMA,
    "scope": "taiwan_current_security_master_baseline_with_separate_historical_gap_ledger",
    "required_partition_keys": [
        "twse_isin_listed",
        "tpex_isin_otc",
        "tpex_isin_emerging",
        "twse_official_master",
        "tpex_official_master",
        "tpex_delisted_history",
    ],
    "accepted_partition_statuses": ["succeeded", "skipped_fresh"],
    "max_completion_seconds": 300.0,
    "max_attempt_failure_rate": 0.0,
    "measurement": "declaration_before_source_refresh_through_local_broad_scan_completion",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _connect(path: str | Path) -> tuple[Path, sqlite3.Connection]:
    resolved = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=on")
    conn.execute("begin")
    return resolved, conn


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _source_rows(partition: dict[str, Any], metadata: dict[str, Any]) -> int:
    classification = partition.get("classification") or {}
    sync = partition.get("sync") or {}
    candidates = (
        classification.get("row_count"),
        sync.get("source_observation_count"),
        metadata.get("source_row_count"),
        partition.get("record_count"),
    )
    return max(0, next((int(value) for value in candidates if isinstance(value, int)), 0))


def _assess_update_slo(
    refresh: dict[str, Any],
    partitions: list[dict[str, Any]],
    observed_counts: Counter,
    *,
    attempted: int,
    refresh_complete: bool,
    run_started: list[datetime],
    run_completed: list[datetime],
    fresh_cache_reports: list[dict[str, Any]],
    problems: list[str],
) -> dict[str, Any]:
    declaration = refresh.get("update_slo")
    if not isinstance(declaration, dict):
        return {
            "status": "not_configured",
            "met": None,
            "reason": "no_predeclared_full_market_update_deadline_in_retained_cycle",
        }
    if declaration.get("schema_version") != SLO_DECLARATION_SCHEMA:
        problems.append("update_slo_declaration_schema_invalid")
    policy = declaration.get("policy")
    if not isinstance(policy, dict):
        problems.append("update_slo_policy_missing")
        return {"status": "invalid", "met": False, "reasons": ["policy_missing"]}
    if policy.get("schema_version") != SLO_POLICY_SCHEMA:
        problems.append("update_slo_policy_schema_invalid")
    unsigned_policy = {key: value for key, value in policy.items() if key != "policy_id"}
    if policy.get("policy_id") != "AMUSLO-" + _sha(unsigned_policy):
        problems.append("update_slo_policy_hash_mismatch")
    if unsigned_policy != CANONICAL_UPDATE_SLO_POLICY:
        problems.append("update_slo_policy_not_canonical")

    required = policy.get("required_partition_keys")
    accepted = policy.get("accepted_partition_statuses")
    max_seconds = policy.get("max_completion_seconds")
    max_failure_rate = policy.get("max_attempt_failure_rate")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(value, str) or not value for value in required)
        or len(set(required)) != len(required)
    ):
        problems.append("update_slo_required_partitions_invalid")
        required = []
    if not isinstance(accepted, list) or not accepted or any(not isinstance(value, str) for value in accepted):
        problems.append("update_slo_accepted_statuses_invalid")
        accepted = []
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        problems.append("update_slo_max_completion_seconds_invalid")
        max_seconds = None
    if (
        isinstance(max_failure_rate, bool)
        or not isinstance(max_failure_rate, (int, float))
        or not math.isfinite(max_failure_rate)
        or not 0 <= max_failure_rate <= 1
    ):
        problems.append("update_slo_max_attempt_failure_rate_invalid")
        max_failure_rate = None

    try:
        declared_at = _utc(declaration["declared_at"])
        deadline_at = _utc(declaration["deadline_at"])
        completed_at = _utc(declaration["completed_at"])
    except (KeyError, TypeError, ValueError):
        problems.append("update_slo_timestamps_invalid")
        return {"status": "invalid", "met": False, "reasons": ["timestamps_invalid"]}
    if max_seconds is not None and abs((deadline_at - declared_at).total_seconds() - max_seconds) > 0.001:
        problems.append("update_slo_deadline_mismatch")
    if completed_at < declared_at:
        problems.append("update_slo_completion_precedes_declaration")
    if run_started and declared_at > min(run_started):
        problems.append("update_slo_not_predeclared")
    if run_completed and completed_at < max(run_completed):
        problems.append("update_slo_completion_precedes_source_runs")
    observed_duration = max(0.0, (completed_at - declared_at).total_seconds())
    retained_duration = declaration.get("observed_duration_seconds")
    if (
        isinstance(retained_duration, bool)
        or not isinstance(retained_duration, (int, float))
        or not math.isfinite(retained_duration)
        or abs(float(retained_duration) - observed_duration) > 0.001
    ):
        problems.append("update_slo_observed_duration_mismatch")

    partition_statuses: dict[str, list[str]] = {}
    for partition in partitions:
        if isinstance(partition, dict):
            partition_statuses.setdefault(str(partition.get("partition_key") or ""), []).append(
                str(partition.get("status") or "unknown")
            )
    scope_complete = bool(required) and all(len(partition_statuses.get(key, [])) == 1 for key in required)
    statuses_accepted = scope_complete and all(partition_statuses[key][0] in accepted for key in required)
    fresh_caches_valid = len(fresh_cache_reports) == observed_counts.get("skipped_fresh", 0) and all(
        report.get("checkpoint_status") == "succeeded"
        and report.get("cursor_matches") is True
        and report.get("fresh_at_declaration") is True
        for report in fresh_cache_reports
    )
    failure_rate = observed_counts.get("failed", 0) / attempted if attempted else 0.0
    deadline_met = max_seconds is not None and completed_at <= deadline_at and observed_duration <= max_seconds
    failure_rate_met = max_failure_rate is not None and failure_rate <= max_failure_rate
    checks = {
        "deadline_met": deadline_met,
        "partition_scope_complete": scope_complete,
        "required_partition_statuses_accepted": statuses_accepted,
        "skipped_fresh_checkpoints_valid": fresh_caches_valid,
        "attempt_failure_rate_met": failure_rate_met,
        "refresh_complete": refresh_complete,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    met = all(checks.values()) and not any(item.startswith("update_slo_") for item in problems)
    return {
        "status": "met" if met else "breach",
        "met": met,
        "policy_id": policy.get("policy_id"),
        "declared_at": declared_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "observed_duration_seconds": round(observed_duration, 6),
        "max_completion_seconds": max_seconds,
        "observed_attempt_failure_rate": round(failure_rate, 6),
        "max_attempt_failure_rate": max_failure_rate,
        "required_partition_keys": required,
        "skipped_fresh_checkpoints": fresh_cache_reports,
        "checks": checks,
        "reasons": reasons,
    }


def audit(
    trade_database: str | Path,
    market_database: str | Path,
    *,
    account_id: str,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    trade_path, trade = _connect(trade_database)
    market_path, market = _connect(market_database)
    result: dict[str, Any] = {
        "schema_version": "open_stock_ai.autonomous_market_update_audit.v1",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id,
        "access": {
            "sqlite_mode": "ro",
            "query_only": True,
            "transaction": "BEGIN per database",
            "immutable": False,
            "cross_database_atomic": False,
        },
        "database_path_sha256": {
            "trade": hashlib.sha256(str(trade_path).encode()).hexdigest(),
            "market": hashlib.sha256(str(market_path).encode()).hexdigest(),
        },
        "passed": False,
        "problems": [],
    }
    try:
        trade_tables = {str(row[0]) for row in trade.execute("select name from sqlite_master where type='table'")}
        market_tables = {str(row[0]) for row in market.execute("select name from sqlite_master where type='table'")}
        missing = []
        for table in ("autonomous_research_cycles", "autonomous_evidence"):
            if table not in trade_tables:
                missing.append("trade:" + table)
        for table in ("data_ingestion_runs", "data_ingestion_checkpoints"):
            if table not in market_tables:
                missing.append("market:" + table)
        if missing:
            result["problems"] = ["required_table_missing:" + table for table in missing]
            return result

        if cycle_id:
            cycle_row = trade.execute(
                "select cycle_id,created_at,payload_json from autonomous_research_cycles where account_id=? and cycle_id=?",
                (account_id, cycle_id),
            ).fetchone()
        else:
            cycle_row = trade.execute(
                "select cycle_id,created_at,payload_json from autonomous_research_cycles where account_id=? order by created_at desc,rowid desc limit 1",
                (account_id,),
            ).fetchone()
        if cycle_row is None:
            result["problems"] = ["research_cycle_missing"]
            return result
        cycle = json.loads(cycle_row["payload_json"])
        selected_cycle_id = str(cycle_row["cycle_id"])
        problems: list[str] = []
        if cycle.get("schema_version") != CYCLE_SCHEMA:
            problems.append("research_cycle_schema_invalid")
        unsigned_cycle = {key: value for key, value in cycle.items() if key != "cycle_id"}
        if selected_cycle_id != "AC-" + _sha(unsigned_cycle) or cycle.get("cycle_id") != selected_cycle_id:
            problems.append("research_cycle_hash_mismatch")

        evidence_id = str(cycle.get("bulk_evidence_id") or "")
        evidence_row = trade.execute(
            "select kind,payload_json from autonomous_evidence where account_id=? and evidence_id=?",
            (account_id, evidence_id),
        ).fetchone()
        if evidence_row is None:
            result.update(cycle_id=selected_cycle_id, problems=sorted(set(problems + ["bulk_evidence_missing"])))
            return result
        evidence = json.loads(evidence_row["payload_json"])
        if evidence_row["kind"] != "market_screen":
            problems.append("bulk_evidence_kind_invalid")
        if evidence_id != "AE-" + _sha({"account_id": account_id, "kind": evidence_row["kind"], "payload": evidence}):
            problems.append("bulk_evidence_hash_mismatch")
        refresh = evidence.get("security_master_refresh") or {}
        if refresh.get("schema_version") != REFRESH_SCHEMA:
            problems.append("security_master_refresh_schema_invalid")
        partitions = refresh.get("partitions") if isinstance(refresh.get("partitions"), list) else []
        if int(refresh.get("partition_count") or -1) != len(partitions):
            problems.append("security_master_partition_count_mismatch")
        observed_counts = Counter(str(item.get("status") or "unknown") for item in partitions if isinstance(item, dict))
        if dict(observed_counts) != (refresh.get("partition_status_counts") or {}):
            problems.append("security_master_partition_status_counts_mismatch")

        run_reports: list[dict[str, Any]] = []
        fresh_cache_reports: list[dict[str, Any]] = []
        started: list[datetime] = []
        completed: list[datetime] = []
        source_rows = 0
        total_run_seconds = 0.0
        current_identity_baseline_complete = True
        for index, partition in enumerate(partitions):
            if not isinstance(partition, dict):
                problems.append(f"partition_not_object:{index}")
                continue
            status = str(partition.get("status") or "unknown")
            if status == "skipped_fresh":
                source_id = str(partition.get("source_id") or "")
                dataset = str(partition.get("dataset") or "")
                partition_key = str(partition.get("partition_key") or "")
                checkpoint = market.execute(
                    "select cursor_value,status,last_success_at,metadata_json from data_ingestion_checkpoints "
                    "where source_id=? and dataset=? and partition_key=?",
                    (source_id, dataset, partition_key),
                ).fetchone()
                if checkpoint is None:
                    problems.append(f"skipped_fresh_checkpoint_missing:{partition_key or index}")
                    continue
                cursor_matches = str(checkpoint["cursor_value"] or "") == str(partition.get("cursor") or "")
                if checkpoint["status"] != "succeeded":
                    problems.append(f"skipped_fresh_checkpoint_status_invalid:{partition_key}")
                if not cursor_matches:
                    problems.append(f"skipped_fresh_checkpoint_cursor_mismatch:{partition_key}")
                try:
                    last_success = _utc(checkpoint["last_success_at"])
                    ttl_seconds = int(partition["ttl_seconds"])
                    if ttl_seconds <= 0:
                        raise ValueError("ttl")
                except (KeyError, TypeError, ValueError):
                    problems.append(f"skipped_fresh_checkpoint_time_invalid:{partition_key}")
                    continue
                fresh_cache_reports.append({
                    "source_id": source_id,
                    "dataset": dataset,
                    "partition_key": partition_key,
                    "checkpoint_status": checkpoint["status"],
                    "cursor_matches": cursor_matches,
                    "last_success_at": last_success.isoformat(),
                    "ttl_seconds": ttl_seconds,
                    "fresh_until": (last_success + timedelta(seconds=ttl_seconds)).isoformat(),
                })
                continue
            if status not in ATTEMPTED_STATUSES:
                continue
            run_id = str(partition.get("run_id") or "")
            if not run_id:
                problems.append(f"partition_run_reference_missing:{partition.get('partition_key') or index}")
                continue
            row = market.execute("select * from data_ingestion_runs where run_id=?", (run_id,)).fetchone()
            if row is None:
                problems.append(f"partition_run_reference_not_found:{run_id}")
                continue
            run = dict(row)
            for key in ("source_id", "dataset", "partition_key", "status"):
                if str(run.get(key) or "") != str(partition.get(key) or ""):
                    problems.append(f"partition_run_{key}_mismatch:{run_id}")
            try:
                begin = _utc(run["started_at"])
                end = _utc(run["completed_at"])
                if end < begin:
                    raise ValueError("negative")
            except (TypeError, ValueError):
                problems.append(f"partition_run_time_invalid:{run_id}")
                continue
            duration = (end - begin).total_seconds()
            metadata = json.loads(run.get("metadata_json") or "{}")
            identity = metadata.get("identity_resolution") if isinstance(metadata, dict) else None
            identity = identity if isinstance(identity, dict) else {}
            unresolved_identity_count = int(identity.get("unresolved_count") or 0)
            baseline_complete = identity.get("current_baseline_complete")
            if unresolved_identity_count or baseline_complete is False:
                current_identity_baseline_complete = False
            run_error = json.loads(run["error_json"]) if run.get("error_json") else None
            receipt_error = partition.get("error")
            if status == "failed":
                if not isinstance(run_error, dict) or not isinstance(receipt_error, dict):
                    problems.append(f"partition_run_error_missing:{run_id}")
                elif (
                    str(run_error.get("type") or "") != str(receipt_error.get("type") or "")
                    or str(run_error.get("message") or "")[:512] != str(receipt_error.get("message") or "")
                ):
                    problems.append(f"partition_run_error_mismatch:{run_id}")
            row_count = _source_rows(partition, metadata)
            source_rows += row_count
            total_run_seconds += duration
            started.append(begin)
            completed.append(end)
            run_reports.append({
                "run_id": run_id,
                "source_id": run["source_id"],
                "partition_key": run["partition_key"],
                "status": run["status"],
                "started_at": begin.isoformat(),
                "completed_at": end.isoformat(),
                "duration_seconds": round(duration, 6),
                "source_row_count": row_count,
                "committed_record_count": int(run.get("record_count") or 0),
                "error": run_error,
                "identity_resolution": {
                    "current_baseline_complete": baseline_complete,
                    "unresolved_count": unresolved_identity_count,
                    "historical_unresolved_count": int(identity.get("historical_unresolved_count") or 0),
                    "total_unresolved_count": int(identity.get("total_unresolved_count") or unresolved_identity_count),
                } if identity else None,
            })

        attempted = sum(observed_counts.get(status, 0) for status in ATTEMPTED_STATUSES)
        source_window = (max(completed) - min(started)).total_seconds() if started and completed else None
        try:
            cycle_observed_at = _utc(str(cycle_row["created_at"]))
            research_window = (cycle_observed_at - min(started)).total_seconds() if started else None
            if research_window is not None and research_window < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            research_window = None
            problems.append("research_observation_time_invalid")
        universe_count = int(cycle.get("universe_count") or 0)
        metrics = {
            "partition_count": len(partitions),
            "attempted_partition_count": attempted,
            "partition_status_counts": dict(sorted(observed_counts.items())),
            "attempt_failure_rate": round(observed_counts.get("failed", 0) / attempted, 6) if attempted else None,
            "source_attempt_window_seconds": round(source_window, 6) if source_window is not None else None,
            "summed_source_run_seconds": round(total_run_seconds, 6),
            "observed_source_row_count": source_rows,
            "source_rows_per_attempt_window_second": (
                round(source_rows / source_window, 6) if source_window and source_window > 0 else None
            ),
            "research_observation_window_seconds": round(research_window, 6) if research_window is not None else None,
            "universe_count": universe_count,
            "universe_rows_per_research_observation_second": (
                round(universe_count / research_window, 6) if research_window and research_window > 0 else None
            ),
            "ordinary_stock_count": int(cycle.get("ordinary_stock_count") or 0),
            "usable_bulk_count": int(cycle.get("usable_bulk_count") or 0),
            "ordinary_usable_bulk_count": (
                int(cycle["ordinary_usable_bulk_count"])
                if isinstance(cycle.get("ordinary_usable_bulk_count"), int)
                else None
            ),
            "deep_selected_count": int(cycle.get("deep_selected_count") or 0),
            "deep_success_count": int(cycle.get("deep_success_count") or 0),
            "deep_failure_rate": (
                round(1 - int(cycle.get("deep_success_count") or 0) / int(cycle.get("deep_selected_count") or 0), 6)
                if int(cycle.get("deep_selected_count") or 0) else None
            ),
        }
        refresh_complete = (
            refresh.get("status") in {"succeeded", "skipped_fresh"}
            and observed_counts.get("failed", 0) == 0
            and observed_counts.get("paused", 0) == 0
            and observed_counts.get("skipped_refresh_in_progress", 0) == 0
            and current_identity_baseline_complete
        )
        declaration = refresh.get("update_slo") if isinstance(refresh.get("update_slo"), dict) else {}
        try:
            declared_for_cache = _utc(declaration["declared_at"])
        except (KeyError, TypeError, ValueError):
            declared_for_cache = None
        for cache_report in fresh_cache_reports:
            cache_report["fresh_at_declaration"] = bool(
                declared_for_cache is not None
                and _utc(cache_report["last_success_at"]) <= declared_for_cache <= _utc(cache_report["fresh_until"])
            )
        deadline_assessment = _assess_update_slo(
            refresh,
            partitions,
            observed_counts,
            attempted=attempted,
            refresh_complete=refresh_complete,
            run_started=started,
            run_completed=completed,
            fresh_cache_reports=fresh_cache_reports,
            problems=problems,
        )
        result.update(
            cycle_id=selected_cycle_id,
            cycle_created_at=cycle_row["created_at"],
            bulk_evidence_id=evidence_id,
            security_master_refresh_status=refresh.get("status"),
            refresh_complete=refresh_complete,
            metrics=metrics,
            source_runs=run_reports,
            deadline_assessment=deadline_assessment,
            limitations=[
                "Source ingestion and trade ledgers are separate read transactions, not one atomic cross-database snapshot.",
                "The research observation window ends at the cycle observation time and excludes the later coverage-ledger projection.",
                "Integrity passing does not turn partial refresh, missing domains, or an undeclared deadline into task 08 acceptance.",
            ],
            problems=sorted(set(problems)),
        )
        result["passed"] = not result["problems"]
        result["security_master_update_acceptance_passed"] = bool(
            result["passed"] and refresh_complete and deadline_assessment.get("met") is True
        )
        return result
    except (json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        result["problems"] = sorted(set(result["problems"] + [f"audit_input_invalid:{type(exc).__name__}:{exc}"]))
        return result
    finally:
        trade.close()
        market.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-database", required=True)
    parser.add_argument("--market-database", required=True)
    parser.add_argument("--account-id", default="autonomous-paper-v1")
    parser.add_argument("--cycle-id")
    parser.add_argument("--output")
    parser.add_argument(
        "--require-security-master-acceptance",
        action="store_true",
        help="Exit nonzero when the canonical predeclared update SLO was not met.",
    )
    args = parser.parse_args()
    report = audit(
        args.trade_database,
        args.market_database,
        account_id=args.account_id,
        cycle_id=args.cycle_id,
    )
    report["receipt_sha256"] = _sha(report)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        if destination.exists():
            raise SystemExit("output_already_exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    accepted = report.get("security_master_update_acceptance_passed") is True
    return 0 if report["passed"] and (accepted or not args.require_security_master_acceptance) else 1


if __name__ == "__main__":
    raise SystemExit(main())
