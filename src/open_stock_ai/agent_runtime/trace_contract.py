"""Small W3C-compatible tracing contract for request-to-order correlation.

The runtime deliberately keeps this contract dependency-free.  A production
OpenTelemetry exporter can consume the immutable span payload without making
the decision or order path depend on an optional telemetry service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


TRACE_SCHEMA_VERSION = "open_stock_ai.trace_span.v1"
TRACE_EXPORT_SCHEMA_VERSION = "open_stock_ai.trace_export.v1"
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def stable_trace_id(value: str) -> str:
    """Derive a W3C-sized trace ID from an existing runtime identity."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def stable_span_id(value: str) -> str:
    """Derive a W3C-sized span ID from an event or operation identity."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    sampled: bool = True

    def __post_init__(self) -> None:
        if not _TRACE_ID.fullmatch(self.trace_id) or self.trace_id == "0" * 32:
            raise ValueError("trace_id must be a non-zero 32-hex W3C trace ID")
        if not _SPAN_ID.fullmatch(self.span_id) or self.span_id == "0" * 16:
            raise ValueError("span_id must be a non-zero 16-hex W3C span ID")
        if self.parent_span_id is not None and not _SPAN_ID.fullmatch(self.parent_span_id):
            raise ValueError("parent_span_id must be a 16-hex W3C span ID")

    @classmethod
    def create(cls, *, trace_id: str | None = None, parent: "TraceContext | None" = None) -> "TraceContext":
        resolved_trace = trace_id or uuid4().hex + uuid4().hex
        parent_span = parent.span_id if parent else None
        return cls(resolved_trace, uuid4().hex[:16], parent_span, parent.sampled if parent else True)

    def child(self) -> "TraceContext":
        return TraceContext(self.trace_id, uuid4().hex[:16], self.span_id, self.sampled)

    def headers(self) -> dict[str, str]:
        flags = "01" if self.sampled else "00"
        return {"traceparent": f"00-{self.trace_id}-{self.span_id}-{flags}"}

    @classmethod
    def from_headers(cls, headers: Mapping[str, Any]) -> "TraceContext":
        raw = str(headers.get("traceparent") or "")
        parts = raw.split("-")
        if len(parts) != 4 or parts[0] != "00" or not _TRACE_ID.fullmatch(parts[1]) or not _SPAN_ID.fullmatch(parts[2]):
            raise ValueError("invalid W3C traceparent")
        if parts[3] not in {"00", "01"}:
            raise ValueError("invalid W3C trace flags")
        return cls(parts[1], parts[2], sampled=parts[3] == "01")


@dataclass(slots=True)
class TraceSpan:
    name: str
    context: TraceContext
    started_at: str
    attributes: dict[str, Any] = field(default_factory=dict)
    ended_at: str | None = None
    status: str = "unset"
    error: str | None = None

    def finish(self, *, status: str = "ok", error: str | None = None, ended_at: str | None = None) -> dict[str, Any]:
        if status not in {"unset", "ok", "error"}:
            raise ValueError("trace span status must be unset, ok or error")
        if self.ended_at is not None:
            raise ValueError("trace span is already finished")
        self.ended_at = ended_at or datetime.now(timezone.utc).isoformat()
        self.status = status
        self.error = error
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "name": self.name,
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.context.parent_span_id,
            "traceparent": self.context.headers()["traceparent"],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
        }


class TraceRecorder:
    """Collect a bounded in-process span graph for durable/exportable output."""

    def __init__(self, *, root: TraceContext | None = None, max_spans: int = 256, clock: Any = None) -> None:
        self.root = root or TraceContext.create()
        self.max_spans = max(1, int(max_spans))
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.spans: list[TraceSpan] = []

    def start(self, name: str, *, parent: TraceContext | None = None, attributes: Mapping[str, Any] | None = None) -> TraceSpan:
        if len(self.spans) >= self.max_spans:
            raise RuntimeError("trace span budget exceeded")
        span = TraceSpan(
            name=str(name),
            context=(parent or self.root).child(),
            started_at=str(self.clock()),
            attributes={str(key): value for key, value in dict(attributes or {}).items()},
        )
        self.spans.append(span)
        return span

    def export(self) -> dict[str, Any]:
        payload = {
            "schema_version": TRACE_EXPORT_SCHEMA_VERSION,
            "trace_id": self.root.trace_id,
            "root_span_id": self.root.span_id,
            "spans": [span.to_dict() for span in self.spans],
            "span_count": len(self.spans),
        }
        return {**payload, "export_sha256": _hash_payload(payload)}


def verify_trace_export(export: Mapping[str, Any]) -> bool:
    """Verify a bounded trace export and its parent-chain consistency."""

    required = {"schema_version", "trace_id", "root_span_id", "spans", "span_count", "export_sha256"}
    if set(export) != required or export.get("schema_version") != TRACE_EXPORT_SCHEMA_VERSION:
        return False
    body = dict(export)
    actual = str(body.pop("export_sha256") or "")
    if len(actual) != 64 or not hmac.compare_digest(actual, _hash_payload(body)):
        return False
    trace_id = str(body.get("trace_id") or "")
    if not _TRACE_ID.fullmatch(trace_id) or trace_id == "0" * 32:
        return False
    root_span_id = str(body.get("root_span_id") or "")
    if not _SPAN_ID.fullmatch(root_span_id) or root_span_id == "0" * 16:
        return False
    spans = body.get("spans")
    if not isinstance(spans, list) or body.get("span_count") != len(spans):
        return False
    known_span_ids: set[str] = set()
    for span in spans:
        if not isinstance(span, Mapping):
            return False
        if span.get("schema_version") != TRACE_SCHEMA_VERSION:
            return False
        if span.get("trace_id") != trace_id or not str(span.get("name") or "").strip():
            return False
        span_id = str(span.get("span_id") or "")
        if not _SPAN_ID.fullmatch(span_id) or span_id in known_span_ids or span_id == "0" * 16:
            return False
        parent = span.get("parent_span_id")
        if parent is not None and (
            not _SPAN_ID.fullmatch(str(parent))
            or (str(parent) != root_span_id and str(parent) not in known_span_ids)
        ):
            return False
        traceparent = str(span.get("traceparent") or "")
        if not traceparent.startswith(f"00-{trace_id}-{span_id}-") or traceparent not in {
            f"00-{trace_id}-{span_id}-00", f"00-{trace_id}-{span_id}-01"
        }:
            return False
        known_span_ids.add(span_id)
    return True


def _hash_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
