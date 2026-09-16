from __future__ import annotations

"""Shared fail-closed transport guard for broker adapter calls."""

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from open_stock_ai.agent_runtime.circuit_breaker import CircuitBreakerRegistry
from open_stock_ai.agent_runtime.rate_limit import (
    RateLimitDecision,
    ScopedRateLimitGovernor,
)

from .contracts import BrokerCapabilityUnavailable


class BrokerTransportGuard:
    """Apply one circuit and one rate-limit scope to every broker call.

    The guard does not retry or silently switch brokers.  A closed circuit and
    an admitted rate-limit decision are prerequisites for the adapter call;
    any adapter exception records a failure and is re-raised so the caller can
    preserve its existing fail-closed semantics.
    """

    def __init__(
        self,
        *,
        circuits: CircuitBreakerRegistry | None = None,
        rate_limits: ScopedRateLimitGovernor | None = None,
    ) -> None:
        self.circuits = circuits or CircuitBreakerRegistry()
        self.rate_limits = rate_limits or ScopedRateLimitGovernor()

    async def call(
        self,
        scope: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        normalized = str(scope).strip()
        if not normalized:
            raise ValueError("broker transport scope is required")
        circuit = self.circuits.breaker(normalized)
        circuit_decision = circuit.before_call()
        if not circuit_decision.allowed:
            raise BrokerCapabilityUnavailable(
                f"broker transport circuit blocked {normalized}: {circuit_decision.reason}"
            )
        rate_decision = self.rate_limits.before_request(normalized)
        if not rate_decision.allowed:
            # A rate-limit decision is a host-side admission result, not a
            # provider failure. Close a half-open probe without charging the
            # circuit and leave the governor's in-flight counter untouched.
            circuit.record_success()
            raise BrokerCapabilityUnavailable(
                f"broker transport rate limit blocked {normalized}: {rate_decision.reason}"
            )
        try:
            result = await operation()
        except BaseException:
            circuit.record_failure()
            self.rate_limits.complete_request(normalized, status_code=None)
            raise
        circuit.record_success()
        self.rate_limits.complete_request(normalized, status_code=200)
        return result


@lru_cache(maxsize=1)
def default_broker_transport_guard() -> BrokerTransportGuard:
    """Return the process-wide guard used by default broker gateways.

    Callers may still inject a guard when they need an isolated test or an
    explicitly separate broker process.  The default path must share circuit
    and rate-limit state across gateway instances so one runtime surface
    cannot bypass another surface's admission decision.
    """

    return BrokerTransportGuard()


__all__ = ["BrokerTransportGuard", "default_broker_transport_guard"]
