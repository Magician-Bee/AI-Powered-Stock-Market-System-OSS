#!/usr/bin/env python3
"""Read-only audit for the per-security autonomous research coverage ledger."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import zlib


COVERAGE_SCHEMA = "open_stock_ai.security_research_coverage.v1"
SUMMARY_SCHEMA = "open_stock_ai.security_research_coverage_summary.v1"
DOMAINS = (
    "identity", "daily_price", "intraday_price", "order_book", "price_history",
    "financials", "revenue", "ownership_flows", "news_events", "industry", "cross_market",
)
AVAILABILITY = {"ready", "partial", "unavailable", "conflict"}
FRESHNESS = {"current", "stale", "unknown"}
DEEP = {"never_researched", "current", "stale", "failed", "not_attributed"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decode(value: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(bytes(value)).decode("utf-8"))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'")}


def audit(database: str | Path, *, account_id: str) -> dict[str, Any]:
    path = Path(database).expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=on")
    conn.execute("begin")
    required = {
        "autonomous_security_research_coverage",
        "autonomous_security_research_coverage_snapshots",
        "autonomous_research_cycles",
        "autonomous_evidence",
    }
    tables = _table_names(conn)
    missing_tables = sorted(required - tables)
    result: dict[str, Any] = {
        "schema_version": "open_stock_ai.security_research_coverage_audit.v1",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "database_path_sha256": hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
        "account_id": account_id,
        "access": {"sqlite_mode": "ro", "query_only": True, "transaction": "BEGIN", "immutable": False},
        "missing_tables": missing_tables,
        "passed": False,
        "problems": [],
    }
    if missing_tables:
        conn.close()
        result["problems"] = ["required_table_missing:" + table for table in missing_tables]
        return result

    snapshot_row = conn.execute(
        """select snapshot_id,observed_at,source_cycle_id,entity_count,summary_json,snapshot_sha256
           from autonomous_security_research_coverage_snapshots
           where account_id=? order by observed_at desc,rowid desc limit 1""",
        (account_id,),
    ).fetchone()
    if snapshot_row is None:
        conn.close()
        result["problems"] = ["coverage_snapshot_missing"]
        return result
    try:
        saved_summary = json.loads(snapshot_row["summary_json"])
    except (TypeError, json.JSONDecodeError):
        saved_summary = {}
        result["problems"].append("coverage_summary_json_invalid")

    rows = conn.execute(
        """select coverage_key,entity_id,symbol,venue,name,lifecycle_status,product_type,
                  new_entry_eligible,exclusion_reasons_json,in_latest_universe,universe_observed_at,
                  universe_expires_at,deep_research_status,last_deep_research_at,
                  last_deep_research_cycle_id,last_deep_research_evidence_id,last_deep_research_error,
                  data_domains_blob,needs_update_mask,snapshot_sha256,updated_at
           from autonomous_security_research_coverage
           where account_id=? and in_latest_universe=1 order by symbol,coverage_key""",
        (account_id,),
    ).fetchall()
    cycle_ids = {str(row[0]) for row in conn.execute(
        "select cycle_id from autonomous_research_cycles where account_id=?", (account_id,))}
    evidence_ids = {str(row[0]) for row in conn.execute(
        "select evidence_id from autonomous_evidence where account_id=? and kind='price_history'", (account_id,))}

    deep_counts: Counter[str] = Counter()
    lifecycle_counts: Counter[str] = Counter()
    domain_counts = {
        domain: Counter({"ready_current": 0, "partial": 0, "stale": 0,
                         "unavailable": 0, "conflict": 0, "needs_update": 0})
        for domain in DOMAINS
    }
    entity_ids: Counter[str] = Counter()
    eligible = 0
    all_domains_current = bool(rows)
    problem_counts: Counter[str] = Counter()
    problem_samples: list[dict[str, Any]] = []

    def problem(code: str, row: sqlite3.Row | None = None) -> None:
        problem_counts[code] += 1
        if len(problem_samples) < 50:
            problem_samples.append({"code": code, **({"coverage_key": row["coverage_key"],
                                                       "symbol": row["symbol"]} if row is not None else {})})

    if snapshot_row["source_cycle_id"] and snapshot_row["source_cycle_id"] not in cycle_ids:
        problem("snapshot_source_cycle_reference_missing")

    for row in rows:
        if row["entity_id"]:
            entity_ids[str(row["entity_id"])] += 1
        deep_status = str(row["deep_research_status"])
        if deep_status not in DEEP:
            problem("deep_research_status_invalid", row)
        deep_counts[deep_status] += 1
        lifecycle_counts[str(row["lifecycle_status"])] += 1
        eligible += int(bool(row["new_entry_eligible"]))
        if row["last_deep_research_cycle_id"] and row["last_deep_research_cycle_id"] not in cycle_ids:
            problem("deep_research_cycle_reference_missing", row)
        if row["last_deep_research_evidence_id"] and row["last_deep_research_evidence_id"] not in evidence_ids:
            problem("deep_research_evidence_reference_missing", row)
        try:
            exclusions = json.loads(row["exclusion_reasons_json"])
            domains = _decode(row["data_domains_blob"])
        except (TypeError, ValueError, json.JSONDecodeError, zlib.error):
            problem("coverage_payload_decode_failed", row)
            continue
        if not isinstance(exclusions, list):
            problem("coverage_exclusion_reasons_invalid", row)
            exclusions = []
        if set(domains) != set(DOMAINS):
            problem("coverage_domain_set_incomplete", row)
            continue
        expected_mask = 0
        for index, domain in enumerate(DOMAINS):
            state = domains[domain]
            if not isinstance(state, dict) or state.get("availability") not in AVAILABILITY or state.get("freshness") not in FRESHNESS:
                problem("coverage_domain_state_invalid:" + domain, row)
                continue
            calculated_need = state.get("availability") != "ready" or state.get("freshness") != "current"
            if state.get("needs_update") is not calculated_need:
                problem("coverage_domain_need_mismatch:" + domain, row)
            expected_mask |= (1 << index) if calculated_need else 0
            domain_counts[domain]["ready_current"] += int(
                state.get("availability") == "ready" and state.get("freshness") == "current")
            domain_counts[domain]["partial"] += int(state.get("availability") == "partial")
            domain_counts[domain]["stale"] += int(state.get("freshness") == "stale")
            domain_counts[domain]["unavailable"] += int(state.get("availability") == "unavailable")
            domain_counts[domain]["conflict"] += int(state.get("availability") == "conflict")
            domain_counts[domain]["needs_update"] += int(calculated_need)
            all_domains_current = all_domains_current and not calculated_need
        if expected_mask != int(row["needs_update_mask"]):
            problem("coverage_needs_update_mask_mismatch", row)
        snapshot = {
            "schema_version": COVERAGE_SCHEMA,
            "account_id": account_id,
            "coverage_key": row["coverage_key"], "entity_id": row["entity_id"], "symbol": row["symbol"],
            "venue": row["venue"], "name": row["name"], "lifecycle_status": row["lifecycle_status"],
            "product_type": row["product_type"], "new_entry_eligible": bool(row["new_entry_eligible"]),
            "exclusion_reasons": exclusions, "in_latest_universe": True,
            "universe_observed_at": row["universe_observed_at"],
            "universe_expires_at": row["universe_expires_at"],
            "deep_research_status": row["deep_research_status"],
            "last_deep_research_at": row["last_deep_research_at"],
            "last_deep_research_cycle_id": row["last_deep_research_cycle_id"],
            "last_deep_research_evidence_id": row["last_deep_research_evidence_id"],
            "last_deep_research_error": row["last_deep_research_error"],
            "data_domains": domains,
        }
        if _sha(snapshot) != row["snapshot_sha256"]:
            problem("coverage_snapshot_hash_mismatch", row)

    duplicate_entities = sorted(entity_id for entity_id, count in entity_ids.items() if count > 1)
    if duplicate_entities:
        problem_counts["current_entity_identity_duplicated"] = len(duplicate_entities)
        problem_samples.extend({"code": "current_entity_identity_duplicated", "entity_id": value}
                               for value in duplicate_entities[:max(0, 50 - len(problem_samples))])
    recalculated = {
        "schema_version": SUMMARY_SCHEMA,
        "account_id": account_id,
        "observed_at": snapshot_row["observed_at"],
        "universe_expires_at": rows[0]["universe_expires_at"] if rows else None,
        "source_cycle_id": snapshot_row["source_cycle_id"],
        "scope": "latest_official_research_universe_expanded_by_entity_identity",
        "security_count": len(rows),
        "new_entry_eligible_count": eligible,
        "deep_research_status_counts": dict(sorted(deep_counts.items())),
        "lifecycle_status_counts": dict(sorted(lifecycle_counts.items())),
        "data_domain_counts": {domain: dict(counts) for domain, counts in domain_counts.items()},
        "complete_deep_coverage": bool(rows) and deep_counts.get("current", 0) == len(rows),
        "all_domains_current": bool(rows) and all_domains_current,
    }
    summary_hash = _sha(saved_summary) if saved_summary else None
    if saved_summary != recalculated:
        problem("coverage_summary_does_not_match_current_rows")
    if int(snapshot_row["entity_count"]) != len(rows):
        problem("coverage_snapshot_entity_count_mismatch")
    if snapshot_row["snapshot_sha256"] != summary_hash:
        problem("coverage_summary_hash_mismatch")
    if snapshot_row["snapshot_id"] != "ACV-" + str(summary_hash):
        problem("coverage_snapshot_id_mismatch")

    conn.close()
    result.update(
        snapshot_id=snapshot_row["snapshot_id"],
        observed_at=snapshot_row["observed_at"],
        source_cycle_id=snapshot_row["source_cycle_id"],
        current_row_count=len(rows),
        summary=recalculated,
        saved_summary_matches=recalculated == saved_summary,
        row_snapshot_hash_mismatch_count=problem_counts.get("coverage_snapshot_hash_mismatch", 0),
        duplicate_current_entity_count=len(duplicate_entities),
        missing_cycle_reference_count=problem_counts.get("deep_research_cycle_reference_missing", 0),
        missing_evidence_reference_count=problem_counts.get("deep_research_evidence_reference_missing", 0),
        problem_counts=dict(sorted(problem_counts.items())),
        problem_samples=problem_samples[:50],
    )
    result["problems"] = sorted(problem_counts)
    result["passed"] = not problem_counts
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--account-id", default="autonomous-paper-v1")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = audit(args.database, account_id=args.account_id)
    result["receipt_sha256"] = _sha(result)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
