"""Small, dependency-free HTTP load runner for the load-test contract.

The runner is intentionally separate from the contract: it can exercise a
local or staging HTTP endpoint, while the contract remains usable by k6,
Locust, or another measurement tool.  Reports are fail-closed when no
requests were completed or when a baseline is not supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .load_test_contract import LoadTestProfile, LoadTestReport, evaluate_load_test
from .performance_regression import PerformanceBudget


@dataclass(frozen=True, slots=True)
class RequestSample:
    """One completed request measurement."""

    latency_ms: float
    succeeded: bool


def summarize_samples(samples: Iterable[RequestSample], elapsed_seconds: float) -> dict[str, float | int]:
    """Convert completed request samples into contract metric fields.

    Percentiles use the nearest-rank definition so the result is deterministic
    across Python versions and does not interpolate between observations.
    """

    values = [sample for sample in samples if math.isfinite(sample.latency_ms) and sample.latency_ms >= 0]
    if not values or not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        return {"samples": 0, "p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 1.0, "throughput": 0.0}
    ordered = sorted(sample.latency_ms for sample in values)
    p95_rank = max(1, math.ceil(len(ordered) * 0.95))
    p99_rank = max(1, math.ceil(len(ordered) * 0.99))
    successes = sum(1 for sample in values if sample.succeeded)
    return {
        "samples": len(values),
        "p95_ms": float(ordered[p95_rank - 1]),
        "p99_ms": float(ordered[p99_rank - 1]),
        "error_rate": float((len(values) - successes) / len(values)),
        "throughput": float(len(values) / elapsed_seconds),
    }


RequestFn = Callable[[str, float], RequestSample]


def collect_load_metrics(
    profile: LoadTestProfile,
    *,
    url: str,
    timeout_seconds: float = 10.0,
    request_fn: RequestFn | None = None,
) -> dict[str, float | int]:
    """Measure one bounded load interval without deciding whether it passes."""

    if not url.strip():
        raise ValueError("load-test URL is required")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("load-test timeout must be positive")
    requester = request_fn or _http_request
    samples: list[RequestSample] = []
    samples_lock = Lock()
    started = time.monotonic()
    deadline = started + profile.duration_seconds
    interval = 1.0 / profile.target_requests_per_second
    next_submission = started
    pending: set[Future[RequestSample]] = set()

    with ThreadPoolExecutor(max_workers=profile.concurrency, thread_name_prefix="stock-ai-load") as pool:
        while time.monotonic() < deadline or pending:
            now = time.monotonic()
            while now < deadline and len(pending) < profile.concurrency and now >= next_submission:
                pending.add(pool.submit(requester, url, timeout_seconds))
                next_submission += interval
                now = time.monotonic()
            done, pending = wait(pending, timeout=min(0.05, max(0.0, deadline - now)), return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    sample = future.result()
                except Exception:
                    continue
                with samples_lock:
                    samples.append(sample)
            if not done and now >= deadline and pending:
                done, pending = wait(pending)
                for future in done:
                    try:
                        sample = future.result()
                    except Exception:
                        continue
                    with samples_lock:
                        samples.append(sample)

    return summarize_samples(samples, max(time.monotonic() - started, 1e-9))


def run_load_test(
    profile: LoadTestProfile,
    *,
    url: str,
    baseline: dict[str, object] | None,
    timeout_seconds: float = 10.0,
    request_fn: RequestFn | None = None,
) -> LoadTestReport:
    """Run a bounded fixed-rate test and evaluate it against ``baseline``.

    Requests are scheduled at the profile's target rate and never exceed the
    configured concurrency.  The request function is injectable so tests can
    exercise scheduling and aggregation without a network dependency.
    """

    candidate = collect_load_metrics(
        profile,
        url=url,
        timeout_seconds=timeout_seconds,
        request_fn=request_fn,
    )
    return evaluate_load_test(profile, runner="open_stock_ai.load_test_runner", baseline=baseline, candidate=candidate)


def write_report(report: LoadTestReport, destination: str | Path) -> None:
    """Write a verified report and refuse to overwrite different evidence."""

    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise FileExistsError(f"refusing to overwrite load-test report: {path}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _http_request(url: str, timeout_seconds: float) -> RequestSample:
    started = time.perf_counter()
    try:
        request = Request(url, headers={"User-Agent": "open-stock-ai-load-runner/1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            response.read(1)
            succeeded = 200 <= int(response.status) < 400
    except (HTTPError, URLError, TimeoutError, OSError):
        succeeded = False
    return RequestSample(latency_ms=(time.perf_counter() - started) * 1000.0, succeeded=succeeded)


def _load_baseline(path: str | None) -> dict[str, object] | None:
    if not path:
        return None
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if isinstance(metrics, dict):
        return metrics
    if isinstance(payload, dict):
        return payload
    raise ValueError("baseline JSON must be an object")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTP endpoint to exercise")
    parser.add_argument("--baseline", help="JSON baseline artifact or metrics object")
    parser.add_argument("--output", required=True, help="path for the hash-verified report")
    parser.add_argument("--name", default="agent-api-snapshot")
    parser.add_argument("--rps", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--max-p95-ms", type=float, default=250.0)
    parser.add_argument("--max-p99-ms", type=float)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--min-throughput", type=float, default=18.0)
    args = parser.parse_args(argv)
    profile = LoadTestProfile(
        args.name,
        target_requests_per_second=args.rps,
        concurrency=args.concurrency,
        duration_seconds=args.duration,
        budget=PerformanceBudget(
            args.name,
            args.max_p95_ms,
            args.max_error_rate,
            args.min_throughput,
            max_p99_ms=args.max_p99_ms,
        ),
    )
    report = run_load_test(profile, url=args.url, baseline=_load_baseline(args.baseline))
    write_report(report, args.output)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.performance.passed and report.verify() else 2


if __name__ == "__main__":
    raise SystemExit(main())
