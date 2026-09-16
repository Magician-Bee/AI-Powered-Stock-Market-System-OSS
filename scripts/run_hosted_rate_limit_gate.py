#!/usr/bin/env python3
"""Exercise verified rate policies and real HTTP 429 recovery on a hosted runner."""

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
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.agent_runtime.rate_limit import (  # noqa: E402
    RateLimitPolicy,
    ScopedRateLimitGovernor,
)
from open_stock_ai.governance import (  # noqa: E402
    HOSTED_RATE_SCOPES,
    build_hosted_rate_limit_gate_receipt,
    verify_hosted_rate_limit_gate_receipt,
)


UNKNOWN_SCOPE = "tool:hosted-unverified-policy"


def _label(scope: str) -> str:
    return scope.replace(":", "-")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _run_server(control: Path, port_file: Path, request_log: Path) -> int:
    control.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = defaultdict(int)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            label = self.path.strip("/")
            counts[label] += 1
            with request_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"path": self.path, "count": counts[label], "pid": os.getpid()}, sort_keys=True) + "\n")
            always_healthy = (control / f"{label}.always-healthy").exists()
            status = 200 if always_healthy or counts[label] > 1 else 429
            body = b"ok" if status == 200 else b"rate-limited"
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "0.1")
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


def _http(url: str) -> tuple[int, float | None]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - fixed loopback URL
            return int(response.status), None
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After")
        return int(exc.code), float(retry) if retry else None


def _count(request_log: Path, label: str) -> int:
    if not request_log.exists():
        return 0
    return sum(
        1
        for line in request_log.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("path") == f"/{label}"
    )


def _exercise_verified_scope(
    governor: ScopedRateLimitGovernor,
    scope: str,
    *,
    base_url: str,
    request_log: Path,
) -> dict[str, Any]:
    label = _label(scope)
    first = governor.before_request(scope)
    concurrency = governor.before_request(scope)
    status_429, retry_after = _http(f"{base_url}/{label}")
    governor.complete_request(scope, status_code=status_429, retry_after_seconds=retry_after)
    backoff = governor.before_request(scope)
    time.sleep(0.3)
    recovery_first = governor.before_request(scope)
    status_first, _ = _http(f"{base_url}/{label}")
    governor.complete_request(scope, status_code=status_first)
    recovery_second = governor.before_request(scope)
    status_second, _ = _http(f"{base_url}/{label}")
    governor.complete_request(scope, status_code=status_second)
    window = governor.before_request(scope)
    return {
        "first_admission": first.as_dict(),
        "concurrency_block": concurrency.as_dict(),
        "backoff_block": backoff.as_dict(),
        "recovery_first": recovery_first.as_dict(),
        "recovery_second": recovery_second.as_dict(),
        "window_block": window.as_dict(),
        "upstream_statuses": [status_429, status_first, status_second],
        "upstream_request_count": _count(request_log, label),
    }


def _exercise_unknown_scope(
    governor: ScopedRateLimitGovernor,
    *,
    base_url: str,
    control: Path,
    request_log: Path,
) -> dict[str, Any]:
    label = _label(UNKNOWN_SCOPE)
    (control / f"{label}.always-healthy").write_text("healthy", encoding="utf-8")
    first = governor.before_request(UNKNOWN_SCOPE)
    status_first, _ = _http(f"{base_url}/{label}")
    governor.complete_request(UNKNOWN_SCOPE, status_code=status_first)
    blocked = governor.before_request(UNKNOWN_SCOPE)
    time.sleep(0.21)
    recovered = governor.before_request(UNKNOWN_SCOPE)
    status_second, _ = _http(f"{base_url}/{label}")
    governor.complete_request(UNKNOWN_SCOPE, status_code=status_second)
    return {
        "first_admission": first.as_dict(),
        "interval_block": blocked.as_dict(),
        "recovered_admission": recovered.as_dict(),
        "upstream_statuses": [status_first, status_second],
        "upstream_request_count": _count(request_log, label),
    }


def _run_campaign(output: Path, commit_sha: str) -> dict[str, Any]:
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted rate-limit gate must run on a GitHub Actions Linux runner")
    output.mkdir(parents=True, exist_ok=False)
    control = output / "server-control"
    port_file = output / "server-port"
    request_log = output / "server-requests.jsonl"
    server = subprocess.Popen(
        [
            sys.executable, str(Path(__file__).resolve()),
            "--server-control", str(control),
            "--server-port-file", str(port_file),
            "--server-request-log", str(request_log),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(port_file)
        base_url = f"http://127.0.0.1:{int(port_file.read_text(encoding='utf-8'))}"
        policies = {
            scope: RateLimitPolicy(2, 0.25, 1, True)
            for scope in HOSTED_RATE_SCOPES
        }
        governor = ScopedRateLimitGovernor(
            policies,
            unknown_policy_min_interval_seconds=0.2,
            maximum_backoff_seconds=0.2,
        )
        observations = {
            scope: _exercise_verified_scope(governor, scope, base_url=base_url, request_log=request_log)
            for scope in HOSTED_RATE_SCOPES
        }
        for scope, observation in observations.items():
            _write_json(output / f"{_label(scope)}.json", observation)
        unknown = _exercise_unknown_scope(
            governor,
            base_url=base_url,
            control=control,
            request_log=request_log,
        )
        _write_json(output / "unknown-policy.json", unknown)
        receipt = build_hosted_rate_limit_gate_receipt(
            observations,
            unknown_policy_observation=unknown,
            commit_sha=commit_sha,
            server_pid=server.pid,
        )
        if not receipt["passed"] or not verify_hosted_rate_limit_gate_receipt(receipt):
            raise SystemExit(f"hosted rate-limit gate failed: {receipt['blockers']}")
        _write_json(output / "rate-limit-gate-receipt.json", receipt)
        return receipt
    finally:
        server.terminate()
        server.wait(timeout=5)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--commit-sha")
    parser.add_argument("--allow-rate-limit-test", action="store_true")
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
    if not args.allow_rate_limit_test:
        raise SystemExit("refusing hosted rate-limit test without --allow-rate-limit-test")
    if not args.output_dir or not args.commit_sha:
        raise SystemExit("campaign mode requires --output-dir and --commit-sha")
    receipt = _run_campaign(Path(args.output_dir), args.commit_sha)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
