#!/usr/bin/env python3
"""Export critical retention data off host and restore it on a fresh runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.governance.content_retention import (  # noqa: E402
    ContentAddressedRetentionLedger,
)
from open_stock_ai.governance.durable_store import SQLiteRetentionStore  # noqa: E402
from open_stock_ai.governance.hosted_retention_gate import (  # noqa: E402
    build_hosted_retention_gate_receipt,
    verify_hosted_retention_gate_receipt,
)
from open_stock_ai.governance.retention_archive import (  # noqa: E402
    build_critical_retention_archive,
    restore_critical_retention_archive,
    verify_critical_retention_archive,
)


def _encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = _encoded(payload)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return hashlib.sha256(encoded).hexdigest()


def _create(source_database: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if source_database.exists() or output.exists():
        raise SystemExit("retention export requires fresh source and output paths")
    output.mkdir(parents=True)
    store = SQLiteRetentionStore(source_database)
    ledger = ContentAddressedRetentionLedger(max_signal_entries=2, store=store)
    for index in range(4):
        ledger.append(
            f"bounded-signal-{index}",
            {"symbol": "2330.TW", "score": index},
            critical=False,
            occurred_at=f"2026-09-07T00:0{index}:00+00:00",
        )
    ledger.append(
        "paper-oms-execution-PAPER-1",
        {
            "schema_version": "stock_ai.paper_oms_execution_snapshot.v1",
            "order_id": "PAPER-1",
            "source_of_truth": "paper_oms",
            "order": {"symbol": "2330.TW", "quantity": "1", "mode": "paper"},
            "oms": {"status": "filled", "fill_id": "FILL-1"},
        },
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:10:00+00:00",
    )
    ledger.append(
        "paper-broker-execution-PAPER-1-filled",
        {
            "schema_version": "stock_ai.paper_broker_execution_snapshot.v1",
            "order_id": "PAPER-1",
            "source_of_truth": "paper_broker_simulator",
            "result": {"order": {"status": "filled"}, "fill": {"fill_id": "FILL-1"}},
        },
        critical=True,
        kind="execution_broker_state",
        occurred_at="2026-09-07T00:11:00+00:00",
    )
    ledger.append(
        "oms-state-live-disabled-example",
        {
            "schema_version": "stock_ai.broker_oms_execution_state.v1",
            "intent": {"intent_id": "INTENT-1", "mode": "sandbox"},
            "state": "rejected",
            "receipt": None,
            "broker_order_id": None,
            "filled_quantity": "0",
            "remaining_quantity": "1",
            "parent_intent_id": None,
            "replacement_intent_id": None,
            "report_ids": [],
            "action_receipts": [],
            "human_approval_receipt": None,
            "updated_at": "2026-09-07T00:12:00+00:00",
        },
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:12:00+00:00",
    )
    ledger.prune_signals()
    archive = build_critical_retention_archive(
        store,
        source_authority=f"github-actions:{commit_sha}:{run_id}",
        created_at="2026-09-07T00:20:00+00:00",
    )
    archive_path = output / "critical-retention-archive.json"
    archive_file_sha256 = _write_json(archive_path, archive)
    evidence = {
        "commit_sha": commit_sha,
        "run_id": run_id,
        "export_job": "export-critical-retention",
        "restore_job": "clean-runner-restore",
        "off_host_artifact": f"stock-ai-critical-retention-{commit_sha}",
        "artifact_retention_days": 90,
        "production_years_satisfied": False,
        "scope": "hosted_drill_only",
        "archive_file_sha256": archive_file_sha256,
        "archive": archive,
        "source_signal_count_after_prune": ledger.retained_counts()["signals"],
    }
    _write_json(output / "critical-retention-export-evidence.json", evidence)
    return evidence


def _verify(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("clean retention restore output must not exist")
    output.mkdir(parents=True)
    archive_path = input_dir / "critical-retention-archive.json"
    evidence = json.loads(
        (input_dir / "critical-retention-export-evidence.json").read_text(encoding="utf-8")
    )
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    archive_file_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if evidence.get("commit_sha") != commit_sha or evidence.get("run_id") != run_id:
        raise SystemExit("critical retention artifact identity mismatch")
    if archive != evidence.get("archive") or not verify_critical_retention_archive(archive):
        raise SystemExit("critical retention archive verification failed")
    if archive_file_sha256 != evidence.get("archive_file_sha256"):
        raise SystemExit("critical retention archive file hash mismatch")
    restore = restore_critical_retention_archive(
        archive,
        output / "restored-critical-retention.sqlite",
        restored_at="2026-09-07T00:30:00+00:00",
    )
    gate = build_hosted_retention_gate_receipt(
        {**evidence, "restore": restore}, commit_sha=commit_sha, run_id=run_id
    )
    if not gate["passed"] or not verify_hosted_retention_gate_receipt(gate):
        raise SystemExit(f"hosted critical retention gate failed: {gate['blockers']}")
    _write_json(output / "critical-retention-restore-receipt.json", restore)
    _write_json(output / "critical-retention-gate-receipt.json", gate)
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("create", "verify"))
    parser.add_argument("--source-database")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--allow-retention-archive-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted retention archive gate must run on a GitHub Actions Linux runner")
    if not args.allow_retention_archive_drill:
        raise SystemExit("refusing retention archive drill without explicit opt-in")
    output = Path(args.output_dir)
    if args.mode == "create":
        if not args.source_database:
            raise SystemExit("create mode requires --source-database")
        _create(Path(args.source_database), output, args.commit_sha, args.run_id)
        return 0
    if not args.input_dir:
        raise SystemExit("verify mode requires --input-dir")
    print(json.dumps(_verify(Path(args.input_dir), output, args.commit_sha, args.run_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
