from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from open_stock_ai.agent_runtime.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerPolicy,
)
from open_stock_ai.governance import (
    HOSTED_CIRCUIT_SCOPES,
    build_hosted_circuit_gate_receipt,
    verify_hosted_circuit_gate_receipt,
)


def _observations() -> dict:
    started = datetime(2026, 9, 7, tzinfo=timezone.utc)
    result = {}
    for index, scope in enumerate(HOSTED_CIRCUIT_SCOPES):
        breaker = CircuitBreaker(
            scope,
            CircuitBreakerPolicy(failure_threshold=3, base_backoff_seconds=1, maximum_backoff_seconds=1),
        )
        failures = [breaker.record_failure(now=started).as_dict() for _ in range(3)]
        blocked = breaker.before_call(now=started + timedelta(milliseconds=100))
        isolation = CircuitBreaker(HOSTED_CIRCUIT_SCOPES[(index + 1) % 3]).before_call(now=started)
        probe = breaker.before_call(now=started + timedelta(seconds=1))
        recovered = breaker.record_success(now=started + timedelta(seconds=1, milliseconds=1))
        result[scope] = {
            "failure_decisions": failures,
            "blocked_decision": blocked.as_dict(),
            "isolation_decision": isolation.as_dict(),
            "probe_decision": probe.as_dict(),
            "recovered_decision": recovered.as_dict(),
            "requests_before_block": 3,
            "requests_after_block": 3,
            "requests_after_recovery": 4,
            "recovery_status": 200,
        }
    return result


def test_hosted_circuit_gate_verifies_all_scopes_and_nested_receipts() -> None:
    receipt = build_hosted_circuit_gate_receipt(
        _observations(),
        commit_sha="a" * 40,
        server_pid=1234,
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert [item["scope"] for item in receipt["scopes"]] == list(HOSTED_CIRCUIT_SCOPES)
    assert verify_hosted_circuit_gate_receipt(receipt) is True

    tampered = json.loads(json.dumps(receipt))
    tampered["scopes"][0]["requests_after_block"] = 4
    assert verify_hosted_circuit_gate_receipt(tampered) is False


def test_hosted_circuit_gate_fails_closed_when_a_scope_does_not_recover() -> None:
    observations = _observations()
    observations[HOSTED_CIRCUIT_SCOPES[1]]["recovery_status"] = 503
    receipt = build_hosted_circuit_gate_receipt(observations, commit_sha="b" * 40, server_pid=1234)
    assert receipt["passed"] is False
    assert receipt["blockers"] == [f"failure_storm_invariant_failed:{HOSTED_CIRCUIT_SCOPES[1]}"]
    assert verify_hosted_circuit_gate_receipt(receipt) is True


def test_hosted_circuit_workflow_retains_real_http_failure_storm_evidence() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/circuit-breaker-recovery.yml").read_text(encoding="utf-8")
    assert "python scripts/run_hosted_circuit_gate.py" in workflow
    assert "--commit-sha \"${GITHUB_SHA}\"" in workflow
    assert "--allow-failure-storm" in workflow
    assert "verify_hosted_circuit_gate_receipt" in workflow
    assert "retention-days: 30" in workflow
    harness = (root / "scripts/run_hosted_circuit_gate.py").read_text(encoding="utf-8")
    for marker in ("ThreadingHTTPServer", "failure-storm", "half_open", "requests_after_block"):
        assert marker in harness
