#!/usr/bin/env python3
"""Run the R-009 load gate against a loopback Stock AI service."""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit
from urllib.request import urlopen

from open_stock_ai.governance import (
    LoadTestProfile,
    PerformanceBudget,
    run_hosted_load_gate,
    verify_hosted_load_gate_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--rps", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("hosted load gate only accepts an HTTP loopback target")
    if parsed.path != "/health" or parsed.query or parsed.fragment:
        raise SystemExit("hosted load gate target must be the Stock AI /health endpoint")
    with urlopen(args.url, timeout=5) as response:
        identity = json.load(response)
    profile = LoadTestProfile(
        "stock-ai-health-api",
        target_requests_per_second=args.rps,
        concurrency=args.concurrency,
        duration_seconds=args.duration,
        budget=PerformanceBudget(
            "stock-ai-health-api",
            max_p95_ms=250,
            max_error_rate=0.0,
            min_throughput=args.rps * 0.75,
            allowed_regression=0.50,
            max_p99_ms=500,
        ),
    )
    receipt = run_hosted_load_gate(
        profile,
        url=args.url,
        output_dir=args.output_dir,
        commit_sha=args.commit_sha,
        service_identity=identity,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["passed"] and verify_hosted_load_gate_receipt(receipt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
