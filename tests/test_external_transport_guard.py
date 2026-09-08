from datetime import datetime, timezone
import asyncio

import httpx
import pytest

from open_stock_ai.agent_runtime.circuit_breaker import (
    CircuitBreakerPolicy,
    CircuitBreakerRegistry,
)
from open_stock_ai.agent_runtime.rate_limit import (
    RateLimitPolicy,
    ScopedRateLimitGovernor,
)
from open_stock_ai.agent_runtime.transport_guard import (
    ExternalTransportAdmissionError,
    ExternalTransportGuard,
)


def _guard() -> ExternalTransportGuard:
    circuits = CircuitBreakerRegistry()
    circuits.breaker(
        "provider:test",
        policy=CircuitBreakerPolicy(
            failure_threshold=1, base_backoff_seconds=60, maximum_backoff_seconds=60
        ),
    )
    limits = ScopedRateLimitGovernor(
        {"provider:test": RateLimitPolicy(10, 60, 1, True)}
    )
    return ExternalTransportGuard(circuits=circuits, rate_limits=limits)


def test_external_guard_records_5xx_and_blocks_follow_up_call():
    guard = _guard()
    calls = 0

    async def operation() -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    response = asyncio.run(guard.call("provider:test", operation))

    assert response.status_code == 503
    with pytest.raises(ExternalTransportAdmissionError, match="circuit blocked"):
        asyncio.run(guard.call("provider:test", operation))
    assert calls == 1


def test_external_guard_releases_rate_slot_after_success():
    guard = ExternalTransportGuard(
        rate_limits=ScopedRateLimitGovernor(
            {"source:test": RateLimitPolicy(10, 60, 1, True)}
        )
    )
    seen: list[str] = []

    async def operation() -> httpx.Response:
        seen.append("called")
        return httpx.Response(200)

    result = asyncio.run(guard.call("source:test", operation))

    assert result.status_code == 200
    assert seen == ["called"]
    decision = guard.rate_limits.before_request(
        "source:test", now=datetime(2026, 8, 26, 4, tzinfo=timezone.utc)
    )
    assert decision.allowed is True


def test_external_guard_applies_the_same_policy_to_sync_source_clients():
    guard = ExternalTransportGuard(
        circuits=CircuitBreakerRegistry(),
        rate_limits=ScopedRateLimitGovernor(
            {"source:sync": RateLimitPolicy(10, 60, 1, True)}
        ),
    )
    calls: list[str] = []

    class Response:
        status = 200
        headers = {}

    result = guard.call_sync("source:sync", lambda: (calls.append("called"), Response())[1])

    assert isinstance(result, Response)
    assert calls == ["called"]
    assert guard.rate_limits.before_request("source:sync").allowed is True


def test_sync_external_guard_opens_a_circuit_after_repeated_server_failures():
    circuits = CircuitBreakerRegistry()
    circuits.breaker(
        "source:sync-failure",
        policy=CircuitBreakerPolicy(
            failure_threshold=1, base_backoff_seconds=60, maximum_backoff_seconds=60
        ),
    )
    guard = ExternalTransportGuard(circuits=circuits)

    class Response:
        status = 503
        headers = {}

    guard.call_sync("source:sync-failure", Response)
    with pytest.raises(ExternalTransportAdmissionError, match="circuit blocked"):
        guard.call_sync("source:sync-failure", Response)


def test_external_guard_marks_rate_admission_refusals_as_recoverable():
    guard = ExternalTransportGuard(
        rate_limits=ScopedRateLimitGovernor(
            {"source:limited": RateLimitPolicy(10, 60, 1, True)}
        )
    )
    scope = "source:limited"
    decision = guard.rate_limits.before_request(scope)
    assert decision.allowed is True

    with pytest.raises(ExternalTransportAdmissionError, match="maximum_concurrency_reached"):
        guard.call_sync(scope, lambda: None)

    guard.rate_limits.complete_request(scope, status_code=200)
