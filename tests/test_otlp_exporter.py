from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.hosted_otel_gate import (
    verify_collector_trace,
    verify_hosted_otel_gate_receipt,
)
from open_stock_ai.agent_runtime.otlp_exporter import (
    build_otlp_http_json,
    deliver_otlp_http_json,
    verify_otlp_delivery_receipt,
)
from scripts.run_hosted_otel_gate import COLLECTOR_IMAGE, _send, _source_trace, _verify


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "4" * 40


class _CaptureHandler(BaseHTTPRequestHandler):
    payload = b""

    def do_POST(self) -> None:  # noqa: N802
        type(self).payload = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        return


def test_otlp_json_preserves_cross_service_parent_chain_and_receipt() -> None:
    trace = _source_trace(COMMIT, "91")
    payload = build_otlp_http_json(trace)
    resources = payload["resourceSpans"]
    spans = [scope["spans"][0] for resource in resources for scope in resource["scopeSpans"]]

    assert len(resources) == 3
    assert [span["name"] for span in spans] == ["request", "decision", "order"]
    assert "parentSpanId" not in spans[0]
    assert spans[1]["parentSpanId"] == spans[0]["spanId"]
    assert spans[2]["parentSpanId"] == spans[1]["spanId"]

    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        receipt = deliver_otlp_http_json(trace, endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces")
    finally:
        server.shutdown()
        thread.join()
    assert verify_otlp_delivery_receipt(receipt) is True
    assert json.loads(_CaptureHandler.payload) == payload
    receipt["span_count"] = 4
    assert verify_otlp_delivery_receipt(receipt) is False


@pytest.mark.parametrize("endpoint", [
    "http://collector.example/v1/traces",
    "https://user:secret@collector.example/v1/traces",
    "https://collector.example/v1/traces?token=secret",
    "https://collector.example/other",
])
def test_otlp_exporter_rejects_unsafe_or_ambiguous_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError):
        deliver_otlp_http_json(_source_trace(COMMIT, "91"), endpoint=endpoint)


def test_hosted_gate_reverifies_real_http_payload_as_collector_output(tmp_path: Path) -> None:
    sender = tmp_path / "sender"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _send(sender, f"http://127.0.0.1:{server.server_port}/v1/traces", COMMIT, "91")
    finally:
        server.shutdown()
        thread.join()
    (sender / "collector-traces.json").write_bytes(_CaptureHandler.payload + b"\n")
    config = b"receivers: {otlp: {}}\n"
    (sender / "collector-config.yaml").write_bytes(config)
    (sender / "collector-runtime.json").write_text(json.dumps({
        "collector_image": COLLECTOR_IMAGE,
        "collector_image_id": "sha256:" + "5" * 64,
        "collector_config_sha256": hashlib.sha256(config).hexdigest(),
    }))

    gate = _verify(sender, tmp_path / "verification", COMMIT, "91")
    assert gate["passed"] is True
    assert gate["collector_verification"]["span_count"] == 3
    assert verify_hosted_otel_gate_receipt(gate) is True

    collector = verify_collector_trace(sender / "collector-traces.json", trace_export=json.loads((sender / "source-trace.json").read_text()))
    assert collector["service_names"] == ["stock-ai-api", "stock-ai-decision", "stock-ai-order"]


def test_hosted_otel_workflow_has_real_collector_two_runners_and_long_retention() -> None:
    workflow = (ROOT / ".github" / "workflows" / "otel-tracing.yml").read_text()
    config = (ROOT / ".github" / "otel" / "collector-config.yaml").read_text()
    script = (ROOT / "scripts" / "run_hosted_otel_gate.py").read_text()

    assert "collector-delivery:" in workflow
    assert "clean-runner-verify:" in workflow
    assert "needs: collector-delivery" in workflow
    assert 'cron: "23 5 * * 1"' in workflow
    assert workflow.count("retention-days: 90") == 2
    assert workflow.count(COLLECTOR_IMAGE) == 2
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "verify_hosted_otel_gate_receipt" in workflow
    assert "--mode send" in workflow and "--mode verify" in workflow
    assert "exporters: [file]" in config
    assert "GITHUB_ACTIONS" in script
    assert "--allow-hosted-otel-drill" in script
