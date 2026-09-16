from __future__ import annotations

import asyncio

import pytest

from open_stock_ai.agent_runtime.circuit_breaker import CircuitBreakerPolicy, CircuitBreakerRegistry
from open_stock_ai.agent_runtime.rate_limit import RateLimitPolicy, ScopedRateLimitGovernor
from stock_ai.brokers import (
    BrokerTransportGuard,
    UnifiedBrokerGateway,
)
from stock_ai.brokers.contracts import BrokerCapabilityUnavailable


def test_default_broker_gateways_share_the_process_transport_guard(monkeypatch) -> None:
    shared_guard = object()
    monkeypatch.setattr(
        "stock_ai.brokers.gateway.default_broker_transport_guard",
        lambda: shared_guard,
    )

    first = UnifiedBrokerGateway()
    second = UnifiedBrokerGateway()

    assert first.transport_guard is shared_guard
    assert second.transport_guard is shared_guard


def test_broker_transport_guard_admits_success_and_releases_rate_slot() -> None:
    circuits = CircuitBreakerRegistry()
    limits = ScopedRateLimitGovernor(
        {"broker:fubon:market_snapshot": RateLimitPolicy(10, 60, 1, True)}
    )
    guard = BrokerTransportGuard(circuits=circuits, rate_limits=limits)

    async def operation() -> dict[str, bool]:
        await asyncio.sleep(0)
        return {"ok": True}

    result = asyncio.run(guard.call("broker:fubon:market_snapshot", operation))

    assert result == {"ok": True}
    decision = circuits.breaker("broker:fubon:market_snapshot").before_call()
    assert decision.allowed is True
    assert limits.before_request("broker:fubon:market_snapshot").allowed is True


def test_broker_transport_guard_opens_after_transport_failures_and_does_not_retry() -> None:
    circuits = CircuitBreakerRegistry()
    circuits.breaker(
        "broker:test:health",
        policy=CircuitBreakerPolicy(failure_threshold=1, base_backoff_seconds=60, maximum_backoff_seconds=60),
    )
    limits = ScopedRateLimitGovernor(
        {"broker:test:health": RateLimitPolicy(10, 60, 1, True)}
    )
    guard = BrokerTransportGuard(circuits=circuits, rate_limits=limits)
    calls = 0

    async def failing_operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(guard.call("broker:test:health", failing_operation))
    with pytest.raises(BrokerCapabilityUnavailable, match="circuit blocked"):
        asyncio.run(guard.call("broker:test:health", failing_operation))

    assert calls == 1


def test_broker_transport_guard_rejects_rate_limited_call_without_running_adapter() -> None:
    circuits = CircuitBreakerRegistry()
    limits = ScopedRateLimitGovernor(
        {"broker:test:snapshot": RateLimitPolicy(1, 60, 1, True)}
    )
    guard = BrokerTransportGuard(circuits=circuits, rate_limits=limits)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    # Hold the only concurrency slot with the governor directly; the guard
    # must fail before invoking the adapter callback.
    assert limits.before_request("broker:test:snapshot").allowed is True
    with pytest.raises(BrokerCapabilityUnavailable, match="rate limit blocked"):
        asyncio.run(guard.call("broker:test:snapshot", operation))

    assert calls == 0
