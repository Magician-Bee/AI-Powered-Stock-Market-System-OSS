from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sqlite3
import sys

from stock_ai import autonomous_trading_service as wiring


SPEC = spec_from_file_location(
    "audit_autonomous_market_update",
    Path(__file__).parents[1] / "scripts" / "audit_autonomous_market_update.py",
)
assert SPEC and SPEC.loader
AUDIT = module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_audit_and_runtime_share_the_exact_canonical_update_slo_policy():
    assert AUDIT.CANONICAL_UPDATE_SLO_POLICY == wiring._SECURITY_MASTER_UPDATE_SLO_POLICY


def _fixture(
    tmp_path: Path,
    *,
    missing_failed_run_id: bool = False,
    configured_slo: bool = False,
    complete_refresh: bool = False,
    tampered_slo_policy_id: bool = False,
    late_completion: bool = False,
    declared_after_source_start: bool = False,
) -> tuple[Path, Path, str]:
    trade_path = tmp_path / "trade.sqlite"
    market_path = tmp_path / "market.sqlite"
    account_id = "autonomous-paper-v1"
    cycle_payload = {
        "schema_version": AUDIT.CYCLE_SCHEMA,
        "account_id": account_id,
        "created_at": "2026-09-13T10:07:49+00:00",
        "universe_count": 1000,
        "ordinary_stock_count": 600,
        "usable_bulk_count": 500,
        "ordinary_usable_bulk_count": 400,
        "deep_selected_count": 2,
        "deep_success_count": 1,
        "results": [],
        "errors": [{"symbol": "2330.TW", "error": "fixture"}],
        "bulk_evidence_id": "pending",
        "model_calls": 0,
    }
    second_status = "succeeded" if complete_refresh else "failed"
    second_partition = {
        "status": second_status,
        **({} if missing_failed_run_id else {"run_id": "DIR-failed"}),
        "source_id": "tpex_openapi",
        "dataset": "security_master",
        "partition_key": "tpex_official_master",
        "record_count": 50 if complete_refresh else 0,
    }
    if not complete_refresh:
        second_partition["error"] = {"type": "IncompleteRead", "message": "fixture truncated"}
    refresh = {
        "schema_version": AUDIT.REFRESH_SCHEMA,
        "status": "succeeded" if complete_refresh else "partial",
        "force": False,
        "scope": "attempted_security_master_source_partitions_only",
        "partition_count": 6,
        "partitions": [
            {
                "status": "succeeded",
                "run_id": "DIR-success",
                "source_id": "twse_isin",
                "dataset": "security_master",
                "partition_key": "twse_isin_listed",
                "record_count": 100,
                "classification": {"row_count": 100},
            },
            {
                "status": "skipped_fresh", "source_id": "twse_isin", "dataset": "security_master",
                "partition_key": "tpex_isin_otc", "record_count": 0, "cursor": "cache-otc", "ttl_seconds": 3600,
            },
            {
                "status": "skipped_fresh", "source_id": "twse_isin", "dataset": "security_master",
                "partition_key": "tpex_isin_emerging", "record_count": 0,
                "cursor": "cache-emerging", "ttl_seconds": 3600,
            },
            {
                "status": "skipped_fresh", "source_id": "twse_openapi", "dataset": "security_master",
                "partition_key": "twse_official_master", "record_count": 0,
                "cursor": "cache-twse", "ttl_seconds": 3600,
            },
            second_partition,
            {
                "status": "skipped_fresh", "source_id": "tpex_official_web", "dataset": "security_master",
                "partition_key": "tpex_delisted_history", "record_count": 0,
                "cursor": "cache-delisted", "ttl_seconds": 3600,
            },
        ],
        "partition_status_counts": (
            {"succeeded": 2, "skipped_fresh": 4}
            if complete_refresh else {"succeeded": 1, "skipped_fresh": 4, "failed": 1}
        ),
    }
    if configured_slo:
        policy = dict(AUDIT.CANONICAL_UPDATE_SLO_POLICY)
        policy["policy_id"] = "tampered" if tampered_slo_policy_id else "AMUSLO-" + AUDIT._sha(policy)
        declared_at = "2026-09-13T10:07:05+00:00" if declared_after_source_start else "2026-09-13T10:06:59+00:00"
        deadline_at = "2026-09-13T10:12:05+00:00" if declared_after_source_start else "2026-09-13T10:11:59+00:00"
        completed_at = "2026-09-13T10:12:00+00:00" if late_completion else "2026-09-13T10:07:20+00:00"
        duration = 295.0 if late_completion and declared_after_source_start else 301.0 if late_completion else 15.0 if declared_after_source_start else 21.0
        refresh["update_slo"] = {
            "schema_version": AUDIT.SLO_DECLARATION_SCHEMA,
            "policy": policy,
            "declared_at": declared_at,
            "deadline_at": deadline_at,
            "completed_at": completed_at,
            "observed_duration_seconds": duration,
        }
    evidence = {"features": [], "security_master_refresh": refresh}
    evidence_id = "AE-" + AUDIT._sha({"account_id": account_id, "kind": "market_screen", "payload": evidence})
    cycle_payload["bulk_evidence_id"] = evidence_id
    cycle_id = "AC-" + AUDIT._sha(cycle_payload)
    cycle_payload["cycle_id"] = cycle_id

    trade = sqlite3.connect(trade_path)
    trade.executescript(
        """
        create table autonomous_research_cycles(cycle_id text primary key,account_id text,created_at text,payload_json text);
        create table autonomous_evidence(evidence_id text primary key,account_id text,kind text,payload_json text);
        """
    )
    trade.execute(
        "insert into autonomous_research_cycles values(?,?,?,?)",
        (cycle_id, account_id, cycle_payload["created_at"], json.dumps(cycle_payload)),
    )
    trade.execute(
        "insert into autonomous_evidence values(?,?,?,?)",
        (evidence_id, account_id, "market_screen", json.dumps(evidence)),
    )
    trade.commit()
    trade.close()

    market = sqlite3.connect(market_path)
    market.execute(
        """create table data_ingestion_runs(
            run_id text primary key,source_id text,dataset text,partition_key text,status text,
            starting_cursor text,committed_cursor text,batch_count integer,record_count integer,
            started_at text,updated_at text,completed_at text,error_json text,metadata_json text
        )"""
    )
    market.execute(
        """create table data_ingestion_checkpoints(
            source_id text,dataset text,partition_key text,cursor_value text,status text,
            last_attempt_at text,last_success_at text,error_json text,metadata_json text,
            primary key(source_id,dataset,partition_key)
        )"""
    )
    market.executemany(
        "insert into data_ingestion_runs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "DIR-success", "twse_isin", "security_master", "twse_isin_listed", "succeeded",
                None, "cursor", 1, 100, "2026-09-13T10:07:00+00:00", "2026-09-13T10:07:10+00:00",
                "2026-09-13T10:07:10+00:00", None, json.dumps({"source_row_count": 100}),
            ),
            (
                "DIR-failed", "tpex_openapi", "security_master", "tpex_official_master", second_status,
                None, None, 1 if complete_refresh else 0, 50 if complete_refresh else 0,
                "2026-09-13T10:07:10+00:00", "2026-09-13T10:07:20+00:00",
                "2026-09-13T10:07:20+00:00",
                None if complete_refresh else json.dumps({"type": "IncompleteRead", "message": "fixture truncated"}),
                json.dumps({"source_row_count": 0}),
            ),
        ],
    )
    market.executemany(
        "insert into data_ingestion_checkpoints values(?,?,?,?,?,?,?,?,?)",
        [
            ("twse_isin", "security_master", "tpex_isin_otc", "cache-otc", "succeeded",
             "2026-09-13T10:06:00+00:00", "2026-09-13T10:06:00+00:00", None, "{}"),
            ("twse_isin", "security_master", "tpex_isin_emerging", "cache-emerging", "succeeded",
             "2026-09-13T10:06:00+00:00", "2026-09-13T10:06:00+00:00", None, "{}"),
            ("twse_openapi", "security_master", "twse_official_master", "cache-twse", "succeeded",
             "2026-09-13T10:06:00+00:00", "2026-09-13T10:06:00+00:00", None, "{}"),
            ("tpex_official_web", "security_master", "tpex_delisted_history", "cache-delisted", "succeeded",
             "2026-09-13T10:06:00+00:00", "2026-09-13T10:06:00+00:00", None, "{}"),
        ],
    )
    market.commit()
    market.close()
    return trade_path, market_path, cycle_id


def test_audit_correlates_partial_refresh_and_reports_truthful_metrics(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is True and report["problems"] == []
    assert report["refresh_complete"] is False
    assert report["metrics"]["attempt_failure_rate"] == 0.5
    assert report["metrics"]["source_attempt_window_seconds"] == 20
    assert report["metrics"]["observed_source_row_count"] == 100
    assert report["metrics"]["ordinary_usable_bulk_count"] == 400
    assert report["metrics"]["deep_failure_rate"] == 0.5
    assert report["deadline_assessment"]["met"] is None
    assert [row["run_id"] for row in report["source_runs"]] == ["DIR-success", "DIR-failed"]


def test_audit_rejects_an_attempted_partition_without_exact_run_reference(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, missing_failed_run_id=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is False
    assert report["refresh_complete"] is False
    assert "partition_run_reference_missing:tpex_official_master" in report["problems"]


def test_audit_rejects_tampered_market_run_status(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path)
    connection = sqlite3.connect(market)
    connection.execute("update data_ingestion_runs set status='succeeded' where run_id='DIR-failed'")
    connection.commit()
    connection.close()
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is False
    assert "partition_run_status_mismatch:DIR-failed" in report["problems"]


def test_audit_accepts_a_predeclared_complete_update_within_deadline(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True, complete_refresh=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is True
    assert report["refresh_complete"] is True
    assert report["deadline_assessment"]["status"] == "met"
    assert report["deadline_assessment"]["observed_duration_seconds"] == 21
    assert report["deadline_assessment"]["checks"] == {
        "deadline_met": True,
        "partition_scope_complete": True,
        "required_partition_statuses_accepted": True,
        "skipped_fresh_checkpoints_valid": True,
        "attempt_failure_rate_met": True,
        "refresh_complete": True,
    }
    assert report["security_master_update_acceptance_passed"] is True


def test_audit_reports_a_configured_source_failure_as_slo_breach(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is True
    assert report["deadline_assessment"]["status"] == "breach"
    assert report["deadline_assessment"]["checks"]["deadline_met"] is True
    assert report["deadline_assessment"]["checks"]["attempt_failure_rate_met"] is False
    assert report["deadline_assessment"]["checks"]["refresh_complete"] is False
    assert report["security_master_update_acceptance_passed"] is False


def test_audit_rejects_a_tampered_predeclared_policy_hash(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True, complete_refresh=True,
                                      tampered_slo_policy_id=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is False
    assert "update_slo_policy_hash_mismatch" in report["problems"]
    assert report["security_master_update_acceptance_passed"] is False


def test_audit_reports_a_complete_update_after_the_deadline_as_breach(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True, complete_refresh=True,
                                      late_completion=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is True
    assert report["deadline_assessment"]["status"] == "breach"
    assert report["deadline_assessment"]["checks"]["deadline_met"] is False
    assert report["security_master_update_acceptance_passed"] is False


def test_audit_rejects_a_policy_declared_after_source_work_started(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True, complete_refresh=True,
                                      declared_after_source_start=True)
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is False
    assert "update_slo_not_predeclared" in report["problems"]
    assert report["security_master_update_acceptance_passed"] is False


def test_cli_acceptance_gate_exits_nonzero_for_an_integrity_valid_slo_breach(monkeypatch, capsys):
    monkeypatch.setattr(AUDIT, "audit", lambda *_args, **_kwargs: {
        "passed": True, "security_master_update_acceptance_passed": False,
    })
    monkeypatch.setattr(sys, "argv", [
        "audit_autonomous_market_update.py", "--trade-database", "trade.sqlite",
        "--market-database", "market.sqlite", "--require-security-master-acceptance",
    ])
    assert AUDIT.main() == 1
    assert '"security_master_update_acceptance_passed": false' in capsys.readouterr().out


def test_audit_does_not_accept_an_expired_checkpoint_labelled_skipped_fresh(tmp_path):
    trade, market, cycle_id = _fixture(tmp_path, configured_slo=True, complete_refresh=True)
    connection = sqlite3.connect(market)
    connection.execute(
        "update data_ingestion_checkpoints set last_success_at='2026-09-13T08:00:00+00:00' "
        "where partition_key='twse_official_master'"
    )
    connection.commit()
    connection.close()
    report = AUDIT.audit(trade, market, account_id="autonomous-paper-v1", cycle_id=cycle_id)
    assert report["passed"] is True
    assert report["deadline_assessment"]["checks"]["skipped_fresh_checkpoints_valid"] is False
    assert report["deadline_assessment"]["status"] == "breach"
    assert report["security_master_update_acceptance_passed"] is False
