from datetime import datetime, timezone

import pytest

from open_stock_ai.agent_runtime.rate_limit import (
    RateLimitPolicy,
    ScopedRateLimitGovernor,
    verify_rate_limit_receipt,
)


_T0 = datetime(2026, 8, 26, 4, 55, tzinfo=timezone.utc)


def test_scoped_governor_enforces_verified_window_concurrency_and_429_backoff():
    governor = ScopedRateLimitGovernor({"provider:ollama": RateLimitPolicy(2, 60, 1, True)})
    first = governor.before_request("provider:ollama", now=_T0)
    assert first.allowed is True
    assert first.in_flight == 1
    assert verify_rate_limit_receipt(first.as_dict()) is True
    blocked_by_concurrency = governor.before_request("provider:ollama", now=_T0)
    assert blocked_by_concurrency.reason == "maximum_concurrency_reached"
    assert blocked_by_concurrency.in_flight == 1
    governor.complete_request("provider:ollama", status_code=200, now=_T0)
    second = governor.before_request("provider:ollama", now=_T0)
    assert second.allowed is True
    governor.complete_request("provider:ollama", status_code=429, now=_T0)
    blocked = governor.before_request("provider:ollama", now=_T0)
    assert blocked.reason == "backoff_active"


def test_unknown_scope_uses_conservative_interval_and_scopes_are_independent():
    governor = ScopedRateLimitGovernor(unknown_policy_min_interval_seconds=5)
    assert governor.before_request("tool:market", now=_T0).allowed is True
    governor.complete_request("tool:market", status_code=200, now=_T0)
    assert governor.before_request("tool:market", now=_T0.replace(second=3)).reason == "unverified_policy_conservative_interval"
    assert governor.before_request("endpoint:twse", now=_T0).allowed is True
    tampered = governor.before_request("provider:other", now=_T0).as_dict()
    tampered["reason"] = "fake"
    assert verify_rate_limit_receipt(tampered) is False


def test_rate_policy_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        RateLimitPolicy(requests_per_window=0)
