"""Fail-closed OTLP/HTTP JSON export for the durable trace contract."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .trace_contract import verify_trace_export


OTLP_PAYLOAD_SCHEMA = "open_stock_ai.otlp_http_json.v1"
OTLP_DELIVERY_SCHEMA = "open_stock_ai.otlp_delivery_receipt.v1"


def build_otlp_http_json(export: Mapping[str, Any]) -> dict[str, Any]:
    """Map one verified internal trace to OTLP JSON, grouped by service."""

    if not verify_trace_export(export):
        raise ValueError("trace export failed verification")
    grouped: dict[str, list[dict[str, Any]]] = {}
    root_span_id = str(export["root_span_id"])
    for source in export["spans"]:
        attributes = dict(source.get("attributes") or {})
        service = str(attributes.pop("service.name", "stock-ai-runtime")).strip()
        if not service:
            raise ValueError("service.name must not be empty")
        span = {
            "traceId": source["trace_id"],
            "spanId": source["span_id"],
            "name": source["name"],
            "kind": 2,
            "startTimeUnixNano": str(_unix_nanos(source["started_at"])),
            "endTimeUnixNano": str(_unix_nanos(source["ended_at"])),
            "attributes": [_attribute(key, value) for key, value in sorted(attributes.items())],
            "status": {"code": {"unset": 0, "ok": 1, "error": 2}[source["status"]]},
        }
        parent = source.get("parent_span_id")
        if parent and parent != root_span_id:
            span["parentSpanId"] = parent
        if source.get("error"):
            span["events"] = [{
                "timeUnixNano": span["endTimeUnixNano"],
                "name": "exception",
                "attributes": [_attribute("exception.message", source["error"])],
            }]
        grouped.setdefault(service, []).append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", service)]},
                "scopeSpans": [{
                    "scope": {"name": "open_stock_ai.trace_contract", "version": "1"},
                    "spans": spans,
                }],
            }
            for service, spans in sorted(grouped.items())
        ]
    }


def deliver_otlp_http_json(
    export: Mapping[str, Any],
    *,
    endpoint: str,
    timeout_seconds: float = 10,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Deliver verified OTLP JSON and return a tamper-evident receipt."""

    origin = _validated_origin(endpoint)
    payload = build_otlp_http_json(export)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "open-stock-ai-otlp/1"},
        method="POST",
    )
    with opener(request, timeout=timeout_seconds) as response:
        status_code = int(response.status)
        response.read()
    if not 200 <= status_code < 300:
        raise RuntimeError(f"OTLP collector rejected trace with HTTP {status_code}")
    services = sorted(
        _resource_service_name(item) for item in payload["resourceSpans"]
    )
    receipt = {
        "schema_version": OTLP_DELIVERY_SCHEMA,
        "trace_id": str(export["trace_id"]),
        "trace_export_sha256": str(export["export_sha256"]),
        "otlp_payload_sha256": hashlib.sha256(body).hexdigest(),
        "endpoint_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "transport": "otlp_http_json",
        "http_status": status_code,
        "span_count": int(export["span_count"]),
        "service_names": services,
        "delivered": True,
    }
    return {**receipt, "receipt_sha256": _digest(receipt)}


def verify_otlp_delivery_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "trace_id", "trace_export_sha256", "otlp_payload_sha256",
        "endpoint_origin_sha256", "transport", "http_status", "span_count",
        "service_names", "delivered", "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != OTLP_DELIVERY_SCHEMA:
        return False
    body = dict(receipt)
    claimed = str(body.pop("receipt_sha256") or "")
    return (
        len(claimed) == 64
        and hmac.compare_digest(claimed, _digest(body))
        and len(str(receipt.get("trace_id") or "")) == 32
        and all(len(str(receipt.get(key) or "")) == 64 for key in (
            "trace_export_sha256", "otlp_payload_sha256", "endpoint_origin_sha256"
        ))
        and receipt.get("transport") == "otlp_http_json"
        and 200 <= int(receipt.get("http_status") or 0) < 300
        and int(receipt.get("span_count") or 0) > 0
        and isinstance(receipt.get("service_names"), list)
        and receipt.get("service_names") == sorted(set(receipt["service_names"]))
        and receipt.get("delivered") is True
    )


def _validated_origin(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("OTLP endpoint must be an HTTP(S) URL without user info")
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1/traces":
        raise ValueError("OTLP endpoint must use the exact /v1/traces path without query or fragment")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise ValueError("plaintext OTLP is allowed only on loopback")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def _unix_nanos(value: Any) -> int:
    if not value:
        raise ValueError("finished trace spans require timestamps")
    resolved = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if resolved.tzinfo is None:
        raise ValueError("trace timestamps must include a timezone")
    return int(resolved.timestamp() * 1_000_000_000)


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    elif isinstance(value, str):
        encoded = {"stringValue": value}
    else:
        encoded = {"stringValue": json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}
    return {"key": str(key), "value": encoded}


def _resource_service_name(resource_span: Mapping[str, Any]) -> str:
    for item in resource_span.get("resource", {}).get("attributes", []):
        if item.get("key") == "service.name":
            return str(item.get("value", {}).get("stringValue") or "")
    return ""


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "OTLP_DELIVERY_SCHEMA", "OTLP_PAYLOAD_SCHEMA", "build_otlp_http_json",
    "deliver_otlp_http_json", "verify_otlp_delivery_receipt",
]
