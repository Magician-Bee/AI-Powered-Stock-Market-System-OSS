#!/usr/bin/env python3
"""Inject all R-010 faults on an isolated Linux runner and retain receipts."""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from open_stock_ai.governance import ChaosRecoveryReceipt, DurableChaosRecoveryStore
from open_stock_ai.governance.hosted_chaos_gate import (
    build_hosted_chaos_gate_receipt,
    verify_hosted_chaos_gate_receipt,
)


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=20)


def _initialize_state(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=FULL")
        connection.execute("create table if not exists safety_state(key text primary key, value text not null)")
        connection.execute("insert or replace into safety_state values('new_orders_blocked', '1')")
        connection.commit()


def _state_invariants(path: Path, operator_receipt: Path) -> dict[str, bool]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("pragma quick_check").fetchone()[0]
        blocked = connection.execute(
            "select value from safety_state where key='new_orders_blocked'"
        ).fetchone()
    return {
        "durable_state_recovered": integrity == "ok",
        "new_orders_blocked_until_safe": blocked == ("1",),
        "operator_receipt_written": operator_receipt.is_file(),
    }


def _record_observation(root: Path, scenario: str, detail: dict[str, object]) -> Path:
    path = root / f"operator-{scenario}.json"
    path.write_text(json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _process_kill(root: Path, database: Path) -> dict[str, object]:
    ready = root / "process-kill-ready"
    code = (
        "import sqlite3,time,sys,pathlib;"
        "db,ready=sys.argv[1:];"
        "c=sqlite3.connect(db);"
        "c.execute(\"insert or replace into safety_state values('child_checkpoint','committed')\");"
        "c.commit();pathlib.Path(ready).write_text('ready');time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(database), str(ready)])
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.02)
    if not ready.exists():
        child.kill()
        raise RuntimeError("process-kill child never reached its durable checkpoint")
    os.kill(child.pid, signal.SIGKILL)
    return_code = child.wait(timeout=5)
    with sqlite3.connect(database) as connection:
        checkpoint = connection.execute(
            "select value from safety_state where key='child_checkpoint'"
        ).fetchone()
    return {"fault_observed": return_code == -signal.SIGKILL, "exit_code": return_code, "checkpoint": checkpoint[0] if checkpoint else None}


def _network_partition(root: Path) -> dict[str, object]:
    port = 18766
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rule = ["-p", "tcp", "-d", "127.0.0.1", "--dport", str(port), "-j", "REJECT"]
    inserted = False
    try:
        for _ in range(100):
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)
        _run(["sudo", "iptables", "-I", "OUTPUT", "1", *rule])
        inserted = True
        partition_observed = False
        try:
            with urlopen(f"http://127.0.0.1:{port}/", timeout=0.5):
                pass
        except OSError:
            partition_observed = True
    finally:
        if inserted:
            _run(["sudo", "iptables", "-D", "OUTPUT", *rule])
        server.terminate()
        server.wait(timeout=5)
    return {"fault_observed": partition_observed, "kernel_rule": "iptables OUTPUT REJECT", "recovered": True}


def _disk_full(root: Path) -> dict[str, object]:
    mountpoint = root / "disk-full-volume"
    mountpoint.mkdir()
    mounted = False
    observed = False
    bytes_written = 0
    try:
        _run(["sudo", "mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", str(mountpoint)])
        mounted = True
        _run(["sudo", "chmod", "0777", str(mountpoint)])
        try:
            with (mountpoint / "fill.bin").open("wb", buffering=0) as handle:
                block = b"0" * 65536
                while True:
                    bytes_written += handle.write(block)
        except OSError as exc:
            observed = exc.errno == errno.ENOSPC
    finally:
        if mounted:
            _run(["sudo", "umount", str(mountpoint)])
    return {"fault_observed": observed, "filesystem": "tmpfs", "limit_bytes": 1048576, "bytes_written": bytes_written, "recovered": True}


def _timeout() -> dict[str, object]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    observed = False
    try:
        child.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        observed = True
        child.kill()
        child.wait(timeout=5)
    return {"fault_observed": observed, "deadline_seconds": 0.1, "child_terminated": child.poll() is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--allow-privileged", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.getenv("GITHUB_ACTIONS") != "true" or not args.allow_privileged:
        raise SystemExit("real chaos injection is restricted to an explicitly enabled GitHub Linux runner")

    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / "safety-state.sqlite"
    _initialize_state(database)
    injectors = {
        "process_kill": lambda: _process_kill(root, database),
        "network_partition": lambda: _network_partition(root),
        "disk_full": lambda: _disk_full(root),
        "timeout": _timeout,
    }
    observations: dict[str, dict[str, object]] = {}
    for scenario, inject in injectors.items():
        detail = inject()
        operator_receipt = _record_observation(root, scenario, detail)
        observations[scenario] = {**detail, "invariants": _state_invariants(database, operator_receipt)}

    receipt = build_hosted_chaos_gate_receipt(observations, commit_sha=args.commit_sha)
    store = DurableChaosRecoveryStore(str(root / "chaos-receipts.sqlite"))
    for item in receipt["scenarios"]:
        store.record(ChaosRecoveryReceipt.from_dict(item))
    if len(DurableChaosRecoveryStore(str(root / "chaos-receipts.sqlite")).receipts()) != 4:
        raise SystemExit("durable chaos receipt restart verification failed")
    (root / "chaos-gate-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["passed"] and verify_hosted_chaos_gate_receipt(receipt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
