from __future__ import annotations

import pytest

from open_stock_ai.agent_runtime import TraceContext, TraceRecorder, verify_trace_export
from open_stock_ai.agent_runtime.contracts import build_runtime_event


def test_trace_context_round_trips_w3c_traceparent_and_children() -> None:
    root = TraceContext.create(trace_id="1" * 32)
    child = root.child()

    assert TraceContext.from_headers(root.headers()) == root
    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert child.span_id != root.span_id


def test_trace_recorder_preserves_request_decision_order_parent_chain() -> None:
    recorder = TraceRecorder(root=TraceContext.create(trace_id="2" * 32), clock=lambda: "2026-08-26T00:00:00+00:00")
    request = recorder.start("request", attributes={"route": "/api/agent/run"})
    decision = recorder.start("decision", parent=request.context, attributes={"run_id": "AR-1"})
    order = recorder.start("order", parent=decision.context, attributes={"order_id": "ORD-1"})

    request.finish(ended_at="2026-08-26T00:00:01+00:00")
    decision.finish(ended_at="2026-08-26T00:00:02+00:00")
    order.finish(ended_at="2026-08-26T00:00:03+00:00")
    exported = recorder.export()

    assert exported["trace_id"] == "2" * 32
    assert [item["name"] for item in exported["spans"]] == ["request", "decision", "order"]
    assert exported["spans"][1]["parent_span_id"] == exported["spans"][0]["span_id"]
    assert exported["spans"][2]["parent_span_id"] == exported["spans"][1]["span_id"]
    assert all(item["status"] == "ok" for item in exported["spans"])
    assert exported["spans"][0]["traceparent"].startswith("00-" + "2" * 32 + "-")
    assert verify_trace_export(exported) is True
    assert verify_trace_export({**exported, "span_count": 99}) is False


def test_runtime_event_always_exposes_trace_and_causation_fields() -> None:
    event = build_runtime_event(
        "decision.created",
        sequence=2,
        run_id="AR-trace",
        session_id="AS-trace",
        previous_event_id="ARE-parent",
    )

    assert len(event["trace_id"]) == 64
    assert len(event["span_id"]) == 16
    assert event["parent_span_id"]
    assert event["trace_id"] == build_runtime_event(
        "order.created", sequence=3, run_id="AR-trace", session_id="AS-trace"
    )["trace_id"]


def test_trace_context_rejects_malformed_traceparent() -> None:
    with pytest.raises(ValueError):
        TraceContext.from_headers({"traceparent": "00-invalid-invalid-01"})
