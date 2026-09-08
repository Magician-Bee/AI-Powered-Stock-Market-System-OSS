#!/usr/bin/env python3
"""Exercise migration backup retention and verify it on a fresh hosted runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.storage.hosted_migration_gate import (  # noqa: E402
    build_hosted_migration_gate_receipt,
    verify_hosted_migration_gate_receipt,
)
from open_stock_ai.storage.migration_safety import (  # noqa: E402
    MigrationSafetyReceipt,
    cleanup_migration_backups,
    load_migration_safety_receipt,
    migration_manifest,
    verify_migration_cleanup_report,
)
from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION  # noqa: E402
from open_stock_ai.storage.sqlite_backup import verify_sqlite_backup  # noqa: E402
from open_stock_ai.storage.sqlite_store import SQLiteStore  # noqa: E402


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "create table signals (id integer primary key autoincrement, created_at text not null, "
            "symbol text, market text, horizon text, action text, confidence real, "
            "risk_approved integer not null default 0, executed integer not null default 0, "
            "payload_json text not null)"
        )
        connection.execute(
            "insert into signals(created_at, symbol, payload_json) values (?, ?, ?)",
            ("2026-09-07T00:00:00+00:00", "2330.TW", "{}"),
        )


def _rebind_receipt(payload: dict[str, Any], backup_name: str) -> dict[str, Any]:
    rebound = deepcopy(payload)
    rebound["backup_name"] = backup_name
    rebound["backup_receipt"]["backup_name"] = backup_name
    nested = {key: value for key, value in rebound["backup_receipt"].items() if key != "receipt_sha256"}
    rebound["backup_receipt"]["receipt_sha256"] = hashlib.sha256(_canonical(nested)).hexdigest()
    outer = {key: value for key, value in rebound.items() if key != "receipt_sha256"}
    rebound["receipt_sha256"] = hashlib.sha256(_canonical(outer)).hexdigest()
    return rebound


def _clone_historical_pair(
    root: Path,
    source_backup: Path,
    receipt: MigrationSafetyReceipt,
    *,
    index: int,
    prepared: bool = False,
) -> tuple[Path, Path]:
    label = "prepared" if prepared else f"old-{index}"
    backup = root / f".legacy-{label}.sqlite"
    receipt_path = root / f".legacy.sqlite.migration-v-{label}.receipt.json"
    shutil.copy2(source_backup, backup)
    payload = _rebind_receipt(receipt.as_dict(), backup.name)
    payload["prepared_at"] = f"2026-08-{10 + index:02d}T00:00:00+00:00"
    payload["completed_at"] = None if prepared else f"2026-08-{10 + index:02d}T00:01:00+00:00"
    payload["status"] = "prepared" if prepared else "completed"
    outer = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    payload["receipt_sha256"] = hashlib.sha256(_canonical(outer)).hexdigest()
    _write_json(receipt_path, payload)
    return backup, receipt_path


def _package_pair(output: Path, backup: Path, receipt_path: Path, index: int) -> dict[str, Any]:
    packaged_backup = output / f"retained-{index}.sqlite"
    packaged_receipt = output / f"retained-{index}.receipt.json"
    shutil.copy2(backup, packaged_backup)
    payload = _rebind_receipt(json.loads(receipt_path.read_text(encoding="utf-8")), packaged_backup.name)
    _write_json(packaged_receipt, payload)
    verified = verify_sqlite_backup(packaged_backup, payload["backup_receipt"])
    return {
        "backup": packaged_backup.name,
        "receipt": packaged_receipt.name,
        "status": payload["status"],
        "backup_sha256": verified["backup_sha256"],
        "receipt_sha256": payload["receipt_sha256"],
    }


def _create(work: Path, output: Path, commit_sha: str, run_id: str) -> None:
    if work.exists() or output.exists():
        raise SystemExit("migration drill requires fresh work and artifact directories")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    database = work / "legacy.sqlite"
    _legacy_database(database)
    SQLiteStore(db_path=database)
    receipt_path = next(work.glob(".legacy.sqlite.migration-v*.receipt.json"))
    receipt = load_migration_safety_receipt(receipt_path)
    backup = work / receipt.backup_name
    backup_verification = verify_sqlite_backup(backup, receipt.backup_receipt)
    with sqlite3.connect(backup) as connection:
        legacy_content_verified = connection.execute("select symbol from signals").fetchone() == ("2330.TW",)
    with sqlite3.connect(database) as connection:
        migrated_version = connection.execute("select max(version) from schema_migrations").fetchone()[0]
        migrated_content_verified = connection.execute("select symbol from signals").fetchone() == ("2330.TW",)
    if receipt.status != "completed" or migrated_version != LATEST_SCHEMA_VERSION or not migrated_content_verified:
        raise SystemExit("startup migration did not commit with preserved content")

    for index in range(3):
        _clone_historical_pair(work, backup, receipt, index=index)
    prepared_backup, prepared_receipt = _clone_historical_pair(
        work, backup, receipt, index=3, prepared=True
    )
    unknown = work / "not-a-migration-backup.sqlite"
    shutil.copy2(backup, unknown)

    dry_run = cleanup_migration_backups(work, retention_count=2, execute=False)
    execution = cleanup_migration_backups(work, retention_count=2, execute=True)
    if not verify_migration_cleanup_report(dry_run) or not verify_migration_cleanup_report(execution):
        raise SystemExit("migration cleanup report verification failed")
    if len(dry_run["planned"]) != 2 or execution["deleted"] != dry_run["planned"]:
        raise SystemExit("migration cleanup did not delete the exact dry-run plan")
    if not unknown.exists() or not prepared_backup.exists() or not prepared_receipt.exists():
        raise SystemExit("migration cleanup removed protected or unknown files")

    remaining: list[tuple[Path, Path, MigrationSafetyReceipt]] = []
    for candidate in sorted(work.glob(".legacy.sqlite.migration-v*.receipt.json")):
        loaded = load_migration_safety_receipt(candidate)
        candidate_backup = work / loaded.backup_name
        verify_sqlite_backup(candidate_backup, loaded.backup_receipt)
        remaining.append((candidate_backup, candidate, loaded))
    completed = [item for item in remaining if item[2].status == "completed"]
    prepared = [item for item in remaining if item[2].status == "prepared"]
    if len(completed) != 2 or len(prepared) != 1:
        raise SystemExit("migration retention result does not preserve two completed and one prepared pair")

    pairs = [
        _package_pair(output, pair[0], pair[1], index)
        for index, pair in enumerate([*completed, *prepared], start=1)
    ]
    manifest = migration_manifest()
    _write_json(output / "migration-manifest.json", manifest)
    evidence = {
        "commit_sha": commit_sha,
        "run_id": run_id,
        "migration_job": "migration-and-cleanup",
        "verification_job": "clean-runner-verify",
        "off_host_artifact": f"stock-ai-migration-retention-{commit_sha}",
        "artifact_retention_days": 90,
        "migration_manifest": manifest,
        "migration_receipt": receipt.as_dict(),
        "backup_verification": backup_verification,
        "dry_run_report": dry_run,
        "execution_report": execution,
        "retained_completed_count": len(completed),
        "prepared_pairs_preserved": len(prepared),
        "unknown_files_preserved": int(unknown.exists()),
        "retained_pairs": pairs,
        "retained_pairs_verified": False,
        "legacy_content_verified": legacy_content_verified,
    }
    _write_json(output / "migration-evidence.json", evidence)


def _verify(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("clean-runner verification output must not already exist")
    output.mkdir(parents=True)
    evidence = json.loads((input_dir / "migration-evidence.json").read_text(encoding="utf-8"))
    manifest = json.loads((input_dir / "migration-manifest.json").read_text(encoding="utf-8"))
    if evidence["commit_sha"] != commit_sha or evidence["run_id"] != run_id:
        raise SystemExit("migration artifact identity mismatch")
    if evidence["migration_manifest"] != manifest or manifest != migration_manifest():
        raise SystemExit("migration manifest differs from exact candidate")
    verified_statuses: list[str] = []
    for pair in evidence["retained_pairs"]:
        receipt_payload = json.loads((input_dir / pair["receipt"]).read_text(encoding="utf-8"))
        receipt = MigrationSafetyReceipt.from_dict(receipt_payload)
        verification = verify_sqlite_backup(input_dir / pair["backup"], receipt.backup_receipt)
        if verification["backup_sha256"] != pair["backup_sha256"]:
            raise SystemExit("retained migration backup hash mismatch")
        verified_statuses.append(receipt.status)
    if verified_statuses.count("completed") != 2 or verified_statuses.count("prepared") != 1:
        raise SystemExit("off-host migration pairs do not match retention policy")
    evidence["retained_pairs_verified"] = True
    gate = build_hosted_migration_gate_receipt(evidence, commit_sha=commit_sha, run_id=run_id)
    if not gate["passed"] or not verify_hosted_migration_gate_receipt(gate):
        raise SystemExit(f"hosted migration gate failed: {gate['blockers']}")
    _write_json(output / "migration-gate-receipt.json", gate)
    _write_json(
        output / "retained-pair-verification.json",
        {"commit_sha": commit_sha, "run_id": run_id, "statuses": verified_statuses, "verified": True},
    )
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("create", "verify"))
    parser.add_argument("--work-dir")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--allow-migration-retention-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted migration gate must run on a GitHub Actions Linux runner")
    if not args.allow_migration_retention_drill:
        raise SystemExit("refusing migration retention drill without explicit opt-in")
    output = Path(args.output_dir)
    if args.mode == "create":
        if not args.work_dir:
            raise SystemExit("create mode requires --work-dir")
        _create(Path(args.work_dir), output, args.commit_sha, args.run_id)
        return 0
    if not args.input_dir:
        raise SystemExit("verify mode requires --input-dir")
    gate = _verify(Path(args.input_dir), output, args.commit_sha, args.run_id)
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
