from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from open_stock_ai.agent_runtime.rate_limit import RateLimitPolicy, ScopedRateLimitGovernor
from open_stock_ai.governance import (
    HOSTED_RATE_SCOPES,
    build_hosted_rate_limit_gate_receipt,
    verify_hosted_rate_limit_gate_receipt,
)


T0 = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _observations() -> tuple[dict, dict]:
    policies = {scope: RateLimitPolicy(2, 0.25, 1, True) for scope in HOSTED_RATE_SCOPES}
    governor = ScopedRateLimitGovernor(
        policies,
        unknown_policy_min_interval_seconds=0.2,
        maximum_backoff_seconds=0.2,
    )
    result = {}
    for scope in HOSTED_RATE_SCOPES:
        first = governor.before_request(scope, now=T0)
        concurrency = governor.before_request(scope, now=T0)
        governor.complete_request(scope, status_code=429, retry_after_seconds=0.1, now=T0)
        backoff = governor.before_request(scope, now=T0)
        recovered_at = T0 + timedelta(seconds=0.3)
        recovery_first = governor.before_request(scope, now=recovered_at)
        governor.complete_request(scope, status_code=200, now=recovered_at)
        recovery_second = governor.before_request(scope, now=recovered_at)
        governor.complete_request(scope, status_code=200, now=recovered_at)
        window = governor.before_request(scope, now=recovered_at)
        result[scope] = {
            "first_admission": first.as_dict(),
            "concurrency_block": concurrency.as_dict(),
            "backoff_block": backoff.as_dict(),
            "recovery_first": recovery_first.as_dict(),
            "recovery_second": recovery_second.as_dict(),
            "window_block": window.as_dict(),
            "upstream_statuses": [429, 200, 200],
            "upstream_request_count": 3,
        }
    unknown_scope = "tool:hosted-unverified-policy"
    first = governor.before_request(unknown_scope, now=T0)
    governor.complete_request(unknown_scope, status_code=200, now=T0)
    blocked = governor.before_request(unknown_scope, now=T0)
    recovered = governor.before_request(unknown_scope, now=T0 + timedelta(seconds=0.21))
    governor.complete_request(unknown_scope, status_code=200, now=T0 + timedelta(seconds=0.21))
    unknown = {
        "first_admission": first.as_dict(),
        "interval_block": blocked.as_dict(),
        "recovered_admission": recovered.as_dict(),
        "upstream_statuses": [200, 200],
        "upstream_request_count": 2,
    }
    return result, unknown


def test_hosted_rate_gate_verifies_policies_429s_and_all_admission_receipts() -> None:
    observations, unknown = _observations()
    receipt = build_hosted_rate_limit_gate_receipt(
        observations,
        unknown_policy_observation=unknown,
        commit_sha="a" * 40,
        server_pid=1234,
        clock=lambda: T0,
    )
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert verify_hosted_rate_limit_gate_receipt(receipt) is True

    tampered = json.loads(json.dumps(receipt))
    tampered["scopes"][0]["policy_receipt"]["requests_per_window"] = 200
    assert verify_hosted_rate_limit_gate_receipt(tampered) is False


def test_hosted_rate_gate_fails_closed_when_429_backoff_is_missing() -> None:
    observations, unknown = _observations()
    observations[HOSTED_RATE_SCOPES[0]]["upstream_statuses"] = [200, 200, 200]
    receipt = build_hosted_rate_limit_gate_receipt(
        observations,
        unknown_policy_observation=unknown,
        commit_sha="b" * 40,
        server_pid=1234,
    )
    assert receipt["passed"] is False
    assert receipt["blockers"] == [f"rate_limit_invariant_failed:{HOSTED_RATE_SCOPES[0]}"]
    assert verify_hosted_rate_limit_gate_receipt(receipt) is True


def test_hosted_rate_workflow_retains_real_429_and_policy_evidence() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/rate-limit-policy.yml").read_text(encoding="utf-8")
    assert "python scripts/run_hosted_rate_limit_gate.py" in workflow
    assert "--commit-sha \"${GITHUB_SHA}\"" in workflow
    assert "--allow-rate-limit-test" in workflow
    assert "verify_hosted_rate_limit_gate_receipt" in workflow
    assert "retention-days: 30" in workflow
    harness = (root / "scripts/run_hosted_rate_limit_gate.py").read_text(encoding="utf-8")
    for marker in ("Retry-After", "maximum_backoff_seconds", "upstream_statuses", "unknown-policy"):
        assert marker in harness
