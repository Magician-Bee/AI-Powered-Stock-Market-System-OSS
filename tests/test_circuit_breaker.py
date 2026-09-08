from datetime import datetime, timezone

import pytest

from open_stock_ai.agent_runtime.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitBreakerRegistry,
    verify_circuit_breaker_receipt,
)


_T0 = datetime(2026, 8, 26, 4, 25, tzinfo=timezone.utc)


def test_circuit_breaker_opens_with_exponential_backoff_and_recovers_half_open():
    breaker = CircuitBreaker(
        "provider:ollama",
        CircuitBreakerPolicy(failure_threshold=2, base_backoff_seconds=5, maximum_backoff_seconds=20),
    )

    assert breaker.before_call(now=_T0).allowed is True
    assert breaker.record_failure(now=_T0).state == "closed"
    opened = breaker.record_failure(now=_T0)
    assert opened.state == "open"
    assert opened.allowed is False
    blocked = breaker.before_call(now=_T0.replace(second=4))
    assert blocked.reason == "open_backoff_active"
    probe = breaker.before_call(now=_T0.replace(second=5))
    assert probe.state == "half_open"
    assert probe.allowed is True
    assert breaker.before_call(now=_T0.replace(second=5)).reason == "half_open_probe_in_flight"
    closed = breaker.record_success(now=_T0.replace(second=6))
    assert closed.state == "closed"
    assert closed.consecutive_failures == 0
    assert verify_circuit_breaker_receipt(closed.as_dict()) is True


def test_registry_keeps_provider_source_and_broker_scopes_independent():
    registry = CircuitBreakerRegistry()
    provider = registry.breaker("provider:ollama", policy=CircuitBreakerPolicy(failure_threshold=1))
    source = registry.breaker("source:twse", policy=CircuitBreakerPolicy(failure_threshold=1))

    provider.record_failure(now=_T0)
    assert registry.before_call("provider:ollama", now=_T0).allowed is False
    assert registry.before_call("source:twse", now=_T0).allowed is True
    with pytest.raises(ValueError, match="policy_cannot_change"):
        registry.breaker("provider:ollama", policy=CircuitBreakerPolicy(failure_threshold=2))


def test_tampered_circuit_receipt_is_rejected():
    breaker = CircuitBreaker("broker:paper")
    receipt = breaker.before_call(now=_T0).as_dict()
    receipt["reason"] = "fake"
    assert verify_circuit_breaker_receipt(receipt) is False
