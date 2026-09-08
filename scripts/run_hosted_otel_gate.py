#!/usr/bin/env python3
"""Deliver and independently verify a hosted cross-service OTLP trace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.agent_runtime.hosted_otel_gate import (  # noqa: E402
    build_hosted_otel_gate_receipt,
    verify_collector_trace,
    verify_hosted_otel_gate_receipt,
)
from open_stock_ai.agent_runtime.otlp_exporter import (  # noqa: E402
    build_otlp_http_json,
    deliver_otlp_http_json,
    verify_otlp_delivery_receipt,
)
from open_stock_ai.agent_runtime.trace_contract import TraceContext, TraceRecorder, verify_trace_export  # noqa: E402


COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.160.0"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _source_trace(commit_sha: str, run_id: str) -> dict[str, Any]:
    trace_id = hashlib.sha256(f"{commit_sha}:{run_id}:otel".encode()).hexdigest()[:32]
    started = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    ticks = iter((started + timedelta(milliseconds=index)).isoformat() for index in range(6))
    recorder = TraceRecorder(root=TraceContext.create(trace_id=trace_id), clock=lambda: next(ticks))
    request = recorder.start("request", attributes={"service.name": "stock-ai-api", "http.route": "/api/v1/research"})
    decision = recorder.start("decision", parent=request.context, attributes={"service.name": "stock-ai-decision", "decision.mode": "rules-only"})
    order = recorder.start("order", parent=decision.context, attributes={"service.name": "stock-ai-order", "order.mode": "paper"})
    request.finish(ended_at=next(ticks))
    decision.finish(ended_at=next(ticks))
    order.finish(ended_at=next(ticks))
    return recorder.export()


def _send(output: Path, endpoint: str, commit_sha: str, run_id: str) -> None:
    if output.exists():
        raise SystemExit("OTEL sender output must not already exist")
    output.mkdir(parents=True)
    trace = _source_trace(commit_sha, run_id)
    if not verify_trace_export(trace):
        raise SystemExit("source trace failed verification")
    delivery = deliver_otlp_http_json(trace, endpoint=endpoint)
    if not verify_otlp_delivery_receipt(delivery):
        raise SystemExit("OTLP delivery receipt failed verification")
    _write(output / "source-trace.json", trace)
    _write(output / "otlp-payload.json", build_otlp_http_json(trace))
    _write(output / "delivery-receipt.json", delivery)
    _write(output / "candidate.json", {
        "commit_sha": commit_sha,
        "run_id": run_id,
        "collector_job": "collector-delivery",
        "verification_job": "clean-runner-verify",
        "collector_image": COLLECTOR_IMAGE,
        "off_host_artifact": f"stock-ai-otel-trace-{commit_sha}",
        "artifact_retention_days": 90,
    })


def _verify(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("OTEL clean-runner output must not already exist")
    output.mkdir(parents=True)
    candidate = json.loads((input_dir / "candidate.json").read_text())
    trace = json.loads((input_dir / "source-trace.json").read_text())
    delivery = json.loads((input_dir / "delivery-receipt.json").read_text())
    runtime = json.loads((input_dir / "collector-runtime.json").read_text())
    if candidate["commit_sha"] != commit_sha or candidate["run_id"] != run_id:
        raise SystemExit("OTEL candidate identity mismatch")
    if runtime["collector_image"] != candidate["collector_image"]:
        raise SystemExit("collector runtime image differs from candidate")
    if hashlib.sha256((input_dir / "collector-config.yaml").read_bytes()).hexdigest() != runtime["collector_config_sha256"]:
        raise SystemExit("collector configuration hash mismatch")
    payload = json.dumps(build_otlp_http_json(trace), sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(payload).hexdigest() != delivery["otlp_payload_sha256"]:
        raise SystemExit("delivered OTLP payload differs from source trace")
    collector = verify_collector_trace(input_dir / "collector-traces.json", trace_export=trace)
    evidence = {
        **candidate,
        "collector_image_id": runtime["collector_image_id"],
        "collector_config_sha256": runtime["collector_config_sha256"],
        "delivery_receipt": delivery,
        "trace_export_sha256": trace["export_sha256"],
        "collector_verification": collector,
    }
    gate = build_hosted_otel_gate_receipt(evidence, commit_sha=commit_sha, run_id=run_id)
    if not gate["passed"] or not verify_hosted_otel_gate_receipt(gate):
        raise SystemExit(f"hosted OTEL gate failed: {gate['blockers']}")
    _write(output / "otel-gate-receipt.json", gate)
    _write(output / "collector-verification.json", collector)
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("send", "verify"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--input-dir")
    parser.add_argument("--endpoint")
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--allow-hosted-otel-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted OTEL gate must run on a GitHub Actions Linux runner")
    if not args.allow_hosted_otel_drill:
        raise SystemExit("refusing hosted OTEL drill without explicit opt-in")
    output = Path(args.output_dir)
    if args.mode == "send":
        if not args.endpoint:
            raise SystemExit("send mode requires --endpoint")
        _send(output, args.endpoint, args.commit_sha, args.run_id)
        return 0
    if not args.input_dir:
        raise SystemExit("verify mode requires --input-dir")
    gate = _verify(Path(args.input_dir), output, args.commit_sha, args.run_id)
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
