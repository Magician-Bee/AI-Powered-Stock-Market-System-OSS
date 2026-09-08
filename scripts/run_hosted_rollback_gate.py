#!/usr/bin/env python3
"""Run a real API and durable strategy/model rollback drill on GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.governance import (  # noqa: E402
    ApprovedArtifactRollbackRegistry,
    ArtifactRollbackError,
    SQLiteGovernanceStore,
    build_hosted_rollback_gate_receipt,
    verify_hosted_rollback_gate_receipt,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    session_token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    if session_token:
        headers["X-Stock-AI-Session"] = session_token
        if data:
            origin = url.split("/api/", 1)[0]
            headers["Origin"] = origin
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed loopback URL
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _runtime_session_token(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:  # noqa: S310 - fixed loopback URL
        index = response.read().decode("utf-8")
    match = re.search(r'<meta name="stock-ai-runtime-session" content="([^"]+)"', index)
    if match is None:
        raise RuntimeError("runtime session token was not injected into the index")
    return match.group(1)


def _wait_for_api(base_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"rollback API exited early: {stderr}")
        try:
            status, _ = _request(f"{base_url}/health")
            if status == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise TimeoutError("rollback API did not become ready")


def _seed_artifacts(output: Path, database: Path) -> tuple[bool, bool]:
    artifact_dir = output / "artifacts"
    artifact_dir.mkdir()
    registry = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(database))
    for scope in ("strategy", "model"):
        for version in (1, 2):
            artifact_id = f"{scope}-v{version}"
            artifact_path = artifact_dir / f"{artifact_id}.json"
            artifact_path.write_text(
                json.dumps({"scope": scope, "version": version}, sort_keys=True),
                encoding="utf-8",
            )
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            registry.approve(
                artifact_id,
                digest,
                approved_by="hosted-release-owner",
                approved_at=f"2026-09-07T10:0{version}:00+00:00",
                metadata={"artifact_scope": scope, "artifact_path": artifact_path.name},
            )
            registry.activate(artifact_id)
    immutable_conflict = False
    try:
        registry.approve(
            "strategy-v1",
            "f" * 64,
            approved_by="hosted-release-owner",
            approved_at="2026-09-07T10:01:00+00:00",
            metadata={"artifact_scope": "strategy"},
        )
    except ArtifactRollbackError:
        immutable_conflict = True
    unapproved_activation = False
    try:
        registry.activate("strategy-unapproved")
    except ArtifactRollbackError:
        unapproved_activation = True
    return immutable_conflict, unapproved_activation


def _run_campaign(output: Path, commit_sha: str) -> dict[str, Any]:
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted rollback gate must run on a GitHub Actions Linux runner")
    output.mkdir(parents=True, exist_ok=False)
    database = output / "governance.sqlite"
    immutable_conflict, unapproved_activation = _seed_artifacts(output, database)
    port = _port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "OPEN_STOCK_AI_SQLITE_PATH": str(database),
            "STOCK_AI_AGENT_BACKGROUND_PAUSED": "1",
            "STOCK_AI_GIT_COMMIT": commit_sha,
        }
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "stock_ai.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_api(base_url, server)
        session_token = _runtime_session_token(base_url)
        status, before = _request(
            f"{base_url}/api/open-stock-ai/agent/paper-training/governance/artifacts",
            session_token=session_token,
        )
        if status != 200:
            raise RuntimeError("artifact governance status unavailable")
        rejected_status, _ = _request(
            f"{base_url}/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback",
            payload={"artifact_scope": "strategy", "reason": "forged", "approved_by": "agent"},
            session_token=session_token,
        )
        strategy_status, strategy = _request(
            f"{base_url}/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback",
            payload={
                "artifact_scope": "strategy",
                "reason": "hosted_strategy_rollback_drill",
                "approved_by": "hosted-release-owner",
            },
            session_token=session_token,
        )
        model_status, model = _request(
            f"{base_url}/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback",
            payload={
                "artifact_scope": "model",
                "reason": "hosted_model_rollback_drill",
                "approved_by": "hosted-release-owner",
            },
            session_token=session_token,
        )
    finally:
        server.terminate()
        server.wait(timeout=10)
    restarted = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(database))
    with sqlite3.connect(database) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    observations = {
        "immutable_conflict_rejected": immutable_conflict,
        "unapproved_activation_rejected": unapproved_activation,
        "agent_rollback_rejected": rejected_status == 409,
        "database_quick_check": quick_check,
        "scope_results": {
            "strategy": {
                "before_artifact_id": before["scopes"]["strategy"]["current_artifact_id"],
                "after_artifact_id": strategy["governance"]["scopes"]["strategy"]["current_artifact_id"],
                "durable_after_restart": restarted.current_artifact_id_for("strategy"),
                "other_lane_unchanged": strategy["governance"]["scopes"]["model"]["current_artifact_id"] == "model-v2",
                "http_status": strategy_status,
                "rollback_receipt": strategy["receipt"],
            },
            "model": {
                "before_artifact_id": before["scopes"]["model"]["current_artifact_id"],
                "after_artifact_id": model["governance"]["scopes"]["model"]["current_artifact_id"],
                "durable_after_restart": restarted.current_artifact_id_for("model"),
                "other_lane_unchanged": model["governance"]["scopes"]["strategy"]["current_artifact_id"] == "strategy-v1",
                "http_status": model_status,
                "rollback_receipt": model["receipt"],
            },
        },
    }
    receipt = build_hosted_rollback_gate_receipt(
        observations,
        commit_sha=commit_sha,
        server_pid=server.pid,
    )
    if not receipt["passed"] or not verify_hosted_rollback_gate_receipt(receipt):
        raise SystemExit(f"hosted rollback gate failed: {receipt['blockers']}")
    _write_json(output / "rollback-gate-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--allow-rollback-drill", action="store_true")
    args = parser.parse_args()
    if not args.allow_rollback_drill:
        raise SystemExit("refusing rollback drill without --allow-rollback-drill")
    receipt = _run_campaign(Path(args.output_dir), args.commit_sha)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
