#!/usr/bin/env python3
"""Create an off-host workflow artifact and restore it on a fresh runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.storage.backup_policy import (  # noqa: E402
    backup_policy_from_mapping,
    evaluate_backup_schedule,
)
from open_stock_ai.storage.hosted_backup_gate import (  # noqa: E402
    build_hosted_backup_gate_receipt,
    verify_hosted_backup_gate_receipt,
)
from open_stock_ai.storage.sqlite_backup import (  # noqa: E402
    SQLiteBackupError,
    SQLiteBackupReceipt,
    create_sqlite_backup,
    restore_sqlite_backup,
    verify_sqlite_backup,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _content(path: Path) -> tuple[list[list[Any]], str]:
    with sqlite3.connect(path) as connection:
        rows = [list(row) for row in connection.execute(
            "select decision_id, symbol, action, receipt_sha256 from decisions order by decision_id"
        ).fetchall()]
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return rows, digest


def _create(output: Path, commit_sha: str, run_id: str, repository: str) -> None:
    output.mkdir(parents=True, exist_ok=False)
    source = output / "authoritative.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("pragma journal_mode=wal")
        connection.execute(
            "create table decisions (decision_id text primary key, symbol text not null, action text not null, receipt_sha256 text not null)"
        )
        connection.executemany(
            "insert into decisions values (?, ?, ?, ?)",
            [
                ("decision-1", "2330.TW", "observe", "a" * 64),
                ("decision-2", "0050.TW", "hold", "b" * 64),
                ("decision-3", "2887.TW", "paper-only", "c" * 64),
            ],
        )
        connection.commit()
    rows, content_sha = _content(source)
    backup = output / "authoritative.backup.sqlite"
    receipt = create_sqlite_backup(source, backup)
    policy = backup_policy_from_mapping(
        {
            "interval_hours": 24,
            "retention_count": 14,
            "destination_uri": f"github-actions-artifact://{repository}/runs/{run_id}/stock-ai-sqlite-backup-{commit_sha}",
            "off_host_required": True,
            "owner": "platform-storage",
        }
    )
    decision = evaluate_backup_schedule(policy, last_success_at=None)
    _write_json(output / "backup-receipt.json", receipt.as_dict())
    _write_json(output / "backup-policy-decision.json", decision)
    _write_json(
        output / "source-manifest.json",
        {"commit_sha": commit_sha, "run_id": run_id, "rows": rows, "content_sha256": content_sha},
    )
    source.unlink()
    _remove_sqlite_sidecars(source)
    _remove_sqlite_sidecars(backup)


def _restore(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("clean-runner restore output must not already exist")
    output.mkdir(parents=True)
    backup = input_dir / "authoritative.backup.sqlite"
    receipt_payload = json.loads((input_dir / "backup-receipt.json").read_text(encoding="utf-8"))
    receipt = SQLiteBackupReceipt.from_dict(receipt_payload)
    verification = verify_sqlite_backup(backup, receipt)
    restored = output / "restored.sqlite"
    restoration = restore_sqlite_backup(backup, restored, receipt)
    manifest = json.loads((input_dir / "source-manifest.json").read_text(encoding="utf-8"))
    if manifest["commit_sha"] != commit_sha or manifest["run_id"] != run_id:
        raise SystemExit("downloaded backup manifest identity mismatch")
    rows, restored_content_sha = _content(restored)
    if rows != manifest["rows"]:
        raise SystemExit("restored SQLite content differs from source manifest")
    protected = output / "protected.sqlite"
    protected.write_bytes(b"caller-owned")
    refused = False
    try:
        restore_sqlite_backup(backup, protected, receipt)
    except SQLiteBackupError as exc:
        refused = "target_already_exists" in str(exc)
    evidence = {
        "backup_job": "create-backup",
        "restore_job": "clean-runner-restore",
        "off_host_artifact": f"stock-ai-sqlite-backup-{commit_sha}",
        "policy_decision": json.loads((input_dir / "backup-policy-decision.json").read_text(encoding="utf-8")),
        "backup_receipt": receipt_payload,
        "restoration": {**verification, **restoration},
        "source_content_sha256": manifest["content_sha256"],
        "restored_content_sha256": restored_content_sha,
        "existing_target_refused": refused,
    }
    gate = build_hosted_backup_gate_receipt(evidence, commit_sha=commit_sha, run_id=run_id)
    if not gate["passed"] or not verify_hosted_backup_gate_receipt(gate):
        raise SystemExit(f"hosted backup gate failed: {gate['blockers']}")
    _write_json(output / "backup-gate-receipt.json", gate)
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("backup", "restore"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--input-dir")
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", default="")
    parser.add_argument("--allow-off-host-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted backup gate must run on a GitHub Actions Linux runner")
    if not args.allow_off_host_drill:
        raise SystemExit("refusing off-host backup drill without --allow-off-host-drill")
    if args.mode == "backup":
        if not args.repository:
            raise SystemExit("backup mode requires --repository")
        _create(Path(args.output_dir), args.commit_sha, args.run_id, args.repository)
        return 0
    if not args.input_dir:
        raise SystemExit("restore mode requires --input-dir")
    gate = _restore(Path(args.input_dir), Path(args.output_dir), args.commit_sha, args.run_id)
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
