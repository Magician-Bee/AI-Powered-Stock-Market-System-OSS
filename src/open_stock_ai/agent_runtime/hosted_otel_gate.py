"""Hosted OpenTelemetry Collector delivery and cross-service trace gate."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .otlp_exporter import verify_otlp_delivery_receipt
from .trace_contract import verify_trace_export


HOSTED_OTEL_GATE_SCHEMA = "open_stock_ai.hosted_otel_gate.v1"
EXPECTED_SERVICES = ["stock-ai-api", "stock-ai-decision", "stock-ai-order"]
EXPECTED_SPANS = ["request", "decision", "order"]


def verify_collector_trace(
    path: Path,
    *,
    trace_export: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the exact trace as serialized by a real Collector file exporter."""

    if not verify_trace_export(trace_export):
        raise ValueError("source trace export failed verification")
    raw = path.read_bytes()
    documents = []
    for line in raw.splitlines():
        if line.strip():
            documents.append(json.loads(line))
    if not documents:
        raise ValueError("collector produced no trace documents")
    observed: list[dict[str, str]] = []
    for document in documents:
        for resource_span in document.get("resourceSpans", []):
            service = _service_name(resource_span)
            for scope in resource_span.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    if span.get("traceId") == trace_export["trace_id"]:
                        observed.append({
                            "service_name": service,
                            "name": str(span.get("name") or ""),
                            "span_id": str(span.get("spanId") or ""),
                            "parent_span_id": str(span.get("parentSpanId") or ""),
                        })
    source = {item["name"]: item for item in trace_export["spans"]}
    by_name = {item["name"]: item for item in observed}
    if len(observed) != 3 or set(by_name) != set(EXPECTED_SPANS):
        raise ValueError("collector trace must contain exactly the request, decision and order spans")
    expected_parent = {
        "request": "",
        "decision": source["request"]["span_id"],
        "order": source["decision"]["span_id"],
    }
    if any(by_name[name]["span_id"] != source[name]["span_id"] for name in EXPECTED_SPANS):
        raise ValueError("collector span IDs differ from the delivered trace")
    if any(by_name[name]["parent_span_id"] != expected_parent[name] for name in EXPECTED_SPANS):
        raise ValueError("collector parent chain differs from request to decision to order")
    services = sorted(item["service_name"] for item in observed)
    if services != EXPECTED_SERVICES:
        raise ValueError("collector resource services do not match the cross-service trace contract")
    return {
        "verified": True,
        "trace_id": trace_export["trace_id"],
        "span_count": len(observed),
        "service_names": services,
        "span_graph": [
            {"name": name, "span_id": by_name[name]["span_id"], "parent_span_id": by_name[name]["parent_span_id"]}
            for name in EXPECTED_SPANS
        ],
        "collector_output_sha256": hashlib.sha256(raw).hexdigest(),
    }


def build_hosted_otel_gate_receipt(
    evidence: Mapping[str, Any],
    *,
    commit_sha: str,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted OTEL gate requires the exact candidate commit SHA")
    blockers = [] if _valid_evidence(evidence) else ["hosted_otel_cross_service_trace_failed"]
    payload = {
        "schema_version": HOSTED_OTEL_GATE_SCHEMA,
        "commit_sha": commit,
        "run_id": str(run_id),
        "captured_at": (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "collector_job": str(evidence.get("collector_job") or ""),
        "verification_job": str(evidence.get("verification_job") or ""),
        "collector_image": str(evidence.get("collector_image") or ""),
        "collector_image_id": str(evidence.get("collector_image_id") or ""),
        "collector_config_sha256": str(evidence.get("collector_config_sha256") or ""),
        "off_host_artifact": str(evidence.get("off_host_artifact") or ""),
        "artifact_retention_days": int(evidence.get("artifact_retention_days") or 0),
        "delivery_receipt": dict(evidence.get("delivery_receipt") or {}),
        "trace_export_sha256": str(evidence.get("trace_export_sha256") or ""),
        "collector_verification": dict(evidence.get("collector_verification") or {}),
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_otel_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "run_id", "captured_at", "collector_job",
        "verification_job", "collector_image", "collector_image_id",
        "collector_config_sha256", "off_host_artifact", "artifact_retention_days",
        "delivery_receipt", "trace_export_sha256", "collector_verification",
        "blockers", "passed", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_OTEL_GATE_SCHEMA:
        return False
    body = dict(receipt)
    claimed = str(body.pop("receipt_sha256") or "")
    valid = len(claimed) == 64 and hmac.compare_digest(claimed, _digest(body)) and _valid_evidence(receipt)
    return valid and receipt.get("passed") is True and receipt.get("blockers") == []


def _valid_evidence(evidence: Mapping[str, Any]) -> bool:
    delivery = evidence.get("delivery_receipt") or {}
    verification = evidence.get("collector_verification") or {}
    image = str(evidence.get("collector_image") or "")
    return (
        verify_otlp_delivery_receipt(delivery)
        and delivery.get("trace_export_sha256") == evidence.get("trace_export_sha256")
        and verification.get("verified") is True
        and verification.get("trace_id") == delivery.get("trace_id")
        and verification.get("span_count") == delivery.get("span_count") == 3
        and verification.get("service_names") == delivery.get("service_names") == EXPECTED_SERVICES
        and [item.get("name") for item in verification.get("span_graph", [])] == EXPECTED_SPANS
        and len(str(verification.get("collector_output_sha256") or "")) == 64
        and image == "otel/opentelemetry-collector-contrib:0.160.0"
        and str(evidence.get("collector_image_id") or "").startswith("sha256:")
        and len(str(evidence.get("collector_config_sha256") or "")) == 64
        and evidence.get("artifact_retention_days", 0) >= 90
        and evidence.get("collector_job") == "collector-delivery"
        and evidence.get("verification_job") == "clean-runner-verify"
        and str(evidence.get("off_host_artifact") or "").startswith("stock-ai-otel-trace-")
    )


def _service_name(resource_span: Mapping[str, Any]) -> str:
    for item in resource_span.get("resource", {}).get("attributes", []):
        if item.get("key") == "service.name":
            return str(item.get("value", {}).get("stringValue") or "")
    return ""


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EXPECTED_SERVICES", "EXPECTED_SPANS", "HOSTED_OTEL_GATE_SCHEMA",
    "build_hosted_otel_gate_receipt", "verify_collector_trace",
    "verify_hosted_otel_gate_receipt",
]
