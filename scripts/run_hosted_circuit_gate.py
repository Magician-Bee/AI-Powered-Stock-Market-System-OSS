#!/usr/bin/env python3
"""Drive real HTTP failure storms through provider, source and broker circuits."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.agent_runtime.circuit_breaker import (  # noqa: E402
    CircuitBreakerPolicy,
    CircuitBreakerRegistry,
)
from open_stock_ai.governance import (  # noqa: E402
    HOSTED_CIRCUIT_SCOPES,
    build_hosted_circuit_gate_receipt,
    verify_hosted_circuit_gate_receipt,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _label(scope: str) -> str:
    return scope.split(":", 1)[0]


def _run_server(control: Path, port_file: Path, request_log: Path) -> int:
    control.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            label = self.path.strip("/")
            with request_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"path": self.path, "pid": os.getpid()}, sort_keys=True) + "\n")
            healthy = (control / f"{label}.healthy").exists()
            body = b"healthy" if healthy else b"failure-storm"
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file.write_text(str(server.server_port), encoding="utf-8")
    server.serve_forever()
    return 0


def _wait_for(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.025)
    raise TimeoutError(f"timed out waiting for {path.name}")


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - fixed loopback URL
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _request_count(request_log: Path, label: str) -> int:
    if not request_log.exists():
        return 0
    return sum(
        1
        for line in request_log.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("path") == f"/{label}"
    )


def _exercise_scope(
    scope: str,
    *,
    peer_scope: str,
    registry: CircuitBreakerRegistry,
    base_url: str,
    control: Path,
    request_log: Path,
) -> dict[str, Any]:
    breaker = registry.breaker(scope)
    failures = []
    label = _label(scope)
    for _ in range(3):
        admission = breaker.before_call()
        if not admission.allowed or _http_status(f"{base_url}/{label}") != 503:
            raise RuntimeError(f"failure storm was not observed for {scope}")
        failures.append(breaker.record_failure().as_dict())
    before_block = _request_count(request_log, label)
    blocked = breaker.before_call()
    after_block = _request_count(request_log, label)
    isolation = registry.breaker(peer_scope).before_call()
    (control / f"{label}.healthy").write_text("healthy", encoding="utf-8")
    time.sleep(0.3)
    probe = breaker.before_call()
    if not probe.allowed or probe.state != "half_open":
        raise RuntimeError(f"half-open recovery probe was not admitted for {scope}")
    recovery_status = _http_status(f"{base_url}/{label}")
    recovered = breaker.record_success()
    observation = {
        "failure_decisions": failures,
        "blocked_decision": blocked.as_dict(),
        "isolation_decision": isolation.as_dict(),
        "probe_decision": probe.as_dict(),
        "recovered_decision": recovered.as_dict(),
        "requests_before_block": before_block,
        "requests_after_block": after_block,
        "requests_after_recovery": _request_count(request_log, label),
        "recovery_status": recovery_status,
    }
    return observation


def _run_campaign(output: Path, commit_sha: str) -> dict[str, Any]:
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted circuit gate must run on a GitHub Actions Linux runner")
    output.mkdir(parents=True, exist_ok=False)
    control = output / "server-control"
    port_file = output / "server-port"
    request_log = output / "server-requests.jsonl"
    server = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--server-control",
            str(control),
            "--server-port-file",
            str(port_file),
            "--server-request-log",
            str(request_log),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(port_file)
        base_url = f"http://127.0.0.1:{int(port_file.read_text(encoding='utf-8'))}"
        policy = CircuitBreakerPolicy(
            failure_threshold=3,
            base_backoff_seconds=0.25,
            maximum_backoff_seconds=0.25,
        )
        registry = CircuitBreakerRegistry()
        for scope in HOSTED_CIRCUIT_SCOPES:
            registry.breaker(scope, policy=policy)
        observations = {}
        for index, scope in enumerate(HOSTED_CIRCUIT_SCOPES):
            peer = HOSTED_CIRCUIT_SCOPES[(index + 1) % len(HOSTED_CIRCUIT_SCOPES)]
            observations[scope] = _exercise_scope(
                scope,
                peer_scope=peer,
                registry=registry,
                base_url=base_url,
                control=control,
                request_log=request_log,
            )
            _write_json(output / f"{_label(scope)}-failure-storm.json", observations[scope])
        receipt = build_hosted_circuit_gate_receipt(
            observations,
            commit_sha=commit_sha,
            server_pid=server.pid,
        )
        if not receipt["passed"] or not verify_hosted_circuit_gate_receipt(receipt):
            raise SystemExit(f"hosted circuit gate failed: {receipt['blockers']}")
        _write_json(output / "circuit-gate-receipt.json", receipt)
        return receipt
    finally:
        server.terminate()
        server.wait(timeout=5)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--commit-sha")
    parser.add_argument("--allow-failure-storm", action="store_true")
    parser.add_argument("--server-control")
    parser.add_argument("--server-port-file")
    parser.add_argument("--server-request-log")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.server_control:
        if not args.server_port_file or not args.server_request_log:
            raise SystemExit("server mode requires port and request-log paths")
        return _run_server(Path(args.server_control), Path(args.server_port_file), Path(args.server_request_log))
    if not args.allow_failure_storm:
        raise SystemExit("refusing failure storm without --allow-failure-storm")
    if not args.output_dir or not args.commit_sha:
        raise SystemExit("campaign mode requires --output-dir and --commit-sha")
    receipt = _run_campaign(Path(args.output_dir), args.commit_sha)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
