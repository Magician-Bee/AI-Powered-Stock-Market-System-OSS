#!/usr/bin/env python3
"""Export one content-addressed Session and restore it on a clean runner."""

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

from open_stock_ai.agent_runtime.hosted_session_archive_gate import (  # noqa: E402
    build_hosted_session_archive_gate_receipt,
    verify_hosted_session_archive_gate_receipt,
)
from open_stock_ai.agent_runtime.session_archival import (  # noqa: E402
    build_session_archive,
    load_materialized_session_archive,
    materialize_session_archive,
    verify_session_archive,
)


def _encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write(path: Path, payload: dict[str, Any]) -> str:
    encoded = _encoded(payload)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return hashlib.sha256(encoded).hexdigest()


def _fixture_archive() -> dict[str, Any]:
    session_id = "AS-hosted-archive"
    run_id = "AR-hosted-completed"
    return build_session_archive(
        session={
            "session_id": session_id,
            "namespace": "stock-ai",
            "title": "Hosted recovery evidence",
            "status": "archived",
            "created_at": "2026-09-08T00:00:00+00:00",
            "updated_at": "2026-09-08T00:10:00+00:00",
            "last_run_id": run_id,
            "metadata": {"purpose": "hosted_restore", "api_key": "must-not-leak"},
        },
        title_history=[
            {
                "title_history_id": "ATH-hosted-1",
                "session_id": session_id,
                "revision": 1,
                "title": "Hosted recovery evidence",
                "reason": "created",
                "created_at": "2026-09-08T00:00:00+00:00",
                "metadata": {},
            }
        ],
        messages=[
            {
                "message_id": "AM-hosted-user",
                "session_id": session_id,
                "run_id": run_id,
                "role": "user",
                "kind": "text",
                "status": "completed",
                "created_at": "2026-09-08T00:01:00+00:00",
                "content": {"text": "Preserve this completed decision lineage"},
                "source": {"token": "must-not-leak"},
            },
            {
                "message_id": "AM-hosted-result",
                "session_id": session_id,
                "run_id": run_id,
                "role": "assistant",
                "kind": "result",
                "status": "completed",
                "created_at": "2026-09-08T00:09:00+00:00",
                "content": {"text": "Completed fixture only; no provider was called."},
                "source": {"type": "hosted_fixture"},
            },
        ],
        runs=[
            {
                "run": {
                    "run_id": run_id,
                    "session_id": session_id,
                    "status": "completed",
                    "objective": "verify Session archive restore without executing a model",
                    "driver": "fixture-only",
                    "autonomy": "advisory",
                    "created_at": "2026-09-08T00:01:00+00:00",
                    "updated_at": "2026-09-08T00:09:00+00:00",
                    "started_at": "2026-09-08T00:01:00+00:00",
                    "completed_at": "2026-09-08T00:09:00+00:00",
                    "request": {"objective": "hosted fixture", "max_steps": 1},
                    "result": {"status": "fixture_complete"},
                },
                "events": [
                    {
                        "event_id": "ARE-hosted-complete",
                        "run_id": run_id,
                        "sequence": 1,
                        "type": "decision.completed",
                        "payload": {"artifact_id": "AA-hosted-lineage"},
                    }
                ],
                "artifacts": [
                    {"artifact_id": "AA-hosted-lineage", "kind": "decision_receipt"}
                ],
                "evidence": [{"evidence_id": "AE-hosted-1", "source": "fixture"}],
                "versions": {"code": "candidate-bound-by-workflow"},
            }
        ],
    )


def _create(output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("Session export output must be fresh")
    output.mkdir(parents=True)
    archive = _fixture_archive()
    verify_session_archive(archive, expected_session_id="AS-hosted-archive")
    archive_file_sha256 = _write(output / "session-archive.json", archive)
    evidence = {
        "commit_sha": commit_sha,
        "run_id": run_id,
        "export_job": "export-session-archive",
        "restore_job": "clean-runner-session-restore",
        "off_host_artifact": f"stock-ai-session-archive-{commit_sha}",
        "artifact_retention_days": 90,
        "archive_file_sha256": archive_file_sha256,
        "archive": archive,
    }
    _write(output / "session-archive-export-evidence.json", evidence)
    return evidence


def _verify(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("Session restore output must be fresh")
    output.mkdir(parents=True)
    archive_path = input_dir / "session-archive.json"
    evidence = json.loads(
        (input_dir / "session-archive-export-evidence.json").read_text(encoding="utf-8")
    )
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    if evidence.get("commit_sha") != commit_sha or evidence.get("run_id") != run_id:
        raise SystemExit("Session archive artifact identity mismatch")
    if evidence.get("archive") != archive:
        raise SystemExit("Session archive evidence mismatch")
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != evidence.get("archive_file_sha256"):
        raise SystemExit("Session archive file hash mismatch")
    verify_session_archive(archive, expected_session_id="AS-hosted-archive")
    target = output / "restored-session.sqlite"
    restore = materialize_session_archive(
        archive, target, expected_session_id="AS-hosted-archive"
    )
    restored_archive = load_materialized_session_archive(
        target, session_id="AS-hosted-archive"
    )
    with sqlite3.connect(target) as connection:
        quick_check = str(connection.execute("pragma quick_check").fetchone()[0])
    overwrite_refused = False
    try:
        materialize_session_archive(
            archive, target, expected_session_id="AS-hosted-archive"
        )
    except ValueError as exc:
        overwrite_refused = "refuses to overwrite" in str(exc)
    completed = {
        **evidence,
        "restore": restore,
        "restored_database_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "quick_check": quick_check,
        "materialized_archive_verified": restored_archive == archive,
        "overwrite_refused": overwrite_refused,
    }
    gate = build_hosted_session_archive_gate_receipt(
        completed, commit_sha=commit_sha, run_id=run_id
    )
    if not gate["passed"] or not verify_hosted_session_archive_gate_receipt(gate):
        raise SystemExit(f"hosted Session archive gate failed: {gate['blockers']}")
    _write(output / "session-archive-restore-receipt.json", restore)
    _write(output / "session-archive-gate-receipt.json", gate)
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("create", "verify"))
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--allow-session-archive-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted Session archive gate must run on a GitHub Actions Linux runner")
    if not args.allow_session_archive_drill:
        raise SystemExit("refusing Session archive drill without explicit opt-in")
    output = Path(args.output_dir)
    if args.mode == "create":
        _create(output, args.commit_sha, args.run_id)
        return 0
    if not args.input_dir:
        raise SystemExit("verify mode requires --input-dir")
    print(json.dumps(_verify(Path(args.input_dir), output, args.commit_sha, args.run_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
