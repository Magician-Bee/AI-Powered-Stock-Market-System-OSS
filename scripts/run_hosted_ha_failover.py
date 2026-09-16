#!/usr/bin/env python3
"""Exercise five service leader leases with real process death and standby takeover."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.governance import (  # noqa: E402
    HA_SERVICES,
    LeaseNotAcquired,
    SQLiteLeaderLease,
    build_hosted_ha_failover_receipt,
    verify_hosted_ha_failover_receipt,
)


def _receipt_dict(receipt: Any) -> dict[str, Any]:
    return {**receipt.payload(), "receipt_sha256": receipt.receipt_sha256}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _wait_for(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.025)
    raise TimeoutError(f"timed out waiting for {path.name}")


def _run_worker(args: argparse.Namespace) -> int:
    database = Path(args.database)
    store = SQLiteLeaderLease(database, ttl_seconds=args.ttl_seconds)
    receipt_path = Path(args.receipt_path)
    marker_path = Path(args.marker_path)
    if args.worker_role == "primary":
        receipt = store.claim(args.service, args.owner_id)
        _write_json(receipt_path, _receipt_dict(receipt))
        marker_path.write_text(str(os.getpid()), encoding="utf-8")
        while True:
            time.sleep(0.2)
            store.renew(args.service, args.owner_id, receipt.epoch)
    blocked_observed = False
    while True:
        try:
            receipt = store.claim(args.service, args.owner_id)
        except LeaseNotAcquired:
            if not blocked_observed:
                marker_path.write_text(str(os.getpid()), encoding="utf-8")
                blocked_observed = True
            time.sleep(0.05)
            continue
        _write_json(receipt_path, _receipt_dict(receipt))
        return 0


def _spawn_worker(
    *,
    role: str,
    database: Path,
    service: str,
    owner_id: str,
    receipt_path: Path,
    marker_path: Path,
    ttl_seconds: int,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker-role",
            role,
            "--database",
            str(database),
            "--service",
            service,
            "--owner-id",
            owner_id,
            "--receipt-path",
            str(receipt_path),
            "--marker-path",
            str(marker_path),
            "--ttl-seconds",
            str(ttl_seconds),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _exercise_service(output: Path, database: Path, service: str, *, ttl_seconds: int) -> dict[str, Any]:
    service_dir = output / service
    service_dir.mkdir()
    primary_receipt_path = service_dir / "primary-claim.json"
    standby_receipt_path = service_dir / "standby-claim.json"
    primary_ready = service_dir / "primary-ready"
    standby_blocked = service_dir / "standby-blocked"
    primary_owner = f"{service}-primary"
    standby_owner = f"{service}-standby"
    primary = _spawn_worker(
        role="primary",
        database=database,
        service=service,
        owner_id=primary_owner,
        receipt_path=primary_receipt_path,
        marker_path=primary_ready,
        ttl_seconds=ttl_seconds,
    )
    standby: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(primary_ready)
        _wait_for(primary_receipt_path)
        standby = _spawn_worker(
            role="standby",
            database=database,
            service=service,
            owner_id=standby_owner,
            receipt_path=standby_receipt_path,
            marker_path=standby_blocked,
            ttl_seconds=ttl_seconds,
        )
        _wait_for(standby_blocked)
        killed_at = time.monotonic()
        os.kill(primary.pid, signal.SIGKILL)
        primary_exit = primary.wait(timeout=5)
        standby_exit = standby.wait(timeout=10)
        failover_seconds = round(time.monotonic() - killed_at, 6)
        if primary_exit != -signal.SIGKILL or standby_exit != 0:
            raise RuntimeError(f"unexpected failover exits for {service}: {primary_exit}, {standby_exit}")
        primary_receipt = json.loads(primary_receipt_path.read_text(encoding="utf-8"))
        standby_receipt = json.loads(standby_receipt_path.read_text(encoding="utf-8"))
        store = SQLiteLeaderLease(database, ttl_seconds=ttl_seconds)
        stale_fenced = False
        try:
            store.renew(service, primary_owner, int(primary_receipt["epoch"]))
        except LeaseNotAcquired:
            stale_fenced = True
        current = store.current(service)
        observation = {
            "primary_pid": primary.pid,
            "standby_pid": standby.pid,
            "failover_seconds": failover_seconds,
            "primary_receipt": primary_receipt,
            "standby_receipt": standby_receipt,
            "separate_processes": primary.pid != standby.pid,
            "primary_killed": primary_exit == -signal.SIGKILL,
            "standby_acquired": current is not None and current["owner_id"] == standby_owner,
            "epoch_advanced": int(standby_receipt["epoch"]) == int(primary_receipt["epoch"]) + 1,
            "stale_owner_fenced": stale_fenced,
            "durable_state_recovered": current is not None and int(current["epoch"]) == int(standby_receipt["epoch"]),
        }
        _write_json(service_dir / "failover-observation.json", observation)
        return observation
    finally:
        if primary.poll() is None:
            primary.kill()
            primary.wait(timeout=5)
        if standby is not None and standby.poll() is None:
            standby.kill()
            standby.wait(timeout=5)


def _run_campaign(output: Path, commit_sha: str) -> dict[str, Any]:
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted HA failover must run on a GitHub Actions Linux runner")
    output.mkdir(parents=True, exist_ok=False)
    database = output / "ha-leases.sqlite"
    observations = {
        service: _exercise_service(output, database, service, ttl_seconds=1)
        for service in HA_SERVICES
    }
    with sqlite3.connect(database) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    receipt = build_hosted_ha_failover_receipt(
        observations,
        commit_sha=commit_sha,
        database_quick_check=quick_check,
    )
    if not receipt["passed"] or not verify_hosted_ha_failover_receipt(receipt):
        raise SystemExit(f"hosted HA failover failed: {receipt['blockers']}")
    _write_json(output / "ha-failover-receipt.json", receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--commit-sha")
    parser.add_argument("--allow-process-kill", action="store_true")
    parser.add_argument("--worker-role", choices=("primary", "standby"))
    parser.add_argument("--database")
    parser.add_argument("--service", choices=HA_SERVICES)
    parser.add_argument("--owner-id")
    parser.add_argument("--receipt-path")
    parser.add_argument("--marker-path")
    parser.add_argument("--ttl-seconds", type=int, default=1)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker_role:
        required = (args.database, args.service, args.owner_id, args.receipt_path, args.marker_path)
        if not all(required):
            raise SystemExit("worker mode requires database, service, owner, receipt and marker paths")
        return _run_worker(args)
    if not args.allow_process_kill:
        raise SystemExit("refusing HA failover without --allow-process-kill")
    if not args.output_dir or not args.commit_sha:
        raise SystemExit("campaign mode requires --output-dir and --commit-sha")
    receipt = _run_campaign(Path(args.output_dir), args.commit_sha)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
