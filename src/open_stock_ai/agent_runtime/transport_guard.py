"""Shared fail-closed transport admission for non-broker HTTP providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .circuit_breaker import CircuitBreakerRegistry
from .rate_limit import ScopedRateLimitGovernor


class ExternalTransportAdmissionError(RuntimeError):
    """A source call was deliberately not admitted by the shared guard.

    This is distinct from an error raised by the provider itself. Callers with
    a documented persisted-data fallback can degrade only a guard refusal,
    while unexpected runtime failures still surface for investigation.
    """


class ExternalTransportGuard:
    """Gate provider/source calls with independent circuit and rate scopes."""

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
            raise ValueError("external transport scope is required")
        circuit = self.circuits.breaker(normalized)
        circuit_decision = circuit.before_call()
        if not circuit_decision.allowed:
            raise ExternalTransportAdmissionError(
                f"external transport circuit blocked {normalized}: {circuit_decision.reason}"
            )
        rate_decision = self.rate_limits.before_request(normalized)
        if not rate_decision.allowed:
            circuit.record_success()
            raise ExternalTransportAdmissionError(
                f"external transport rate limit blocked {normalized}: {rate_decision.reason}"
            )
        try:
            result = await operation()
        except BaseException:
            circuit.record_failure()
            self.rate_limits.complete_request(normalized, status_code=None)
            raise

        status_code = getattr(result, "status_code", None)
        if status_code == 429 or (
            isinstance(status_code, int) and status_code >= 500
        ):
            circuit.record_failure()
        else:
            circuit.record_success()
        self.rate_limits.complete_request(
            normalized,
            status_code=status_code if isinstance(status_code, int) else 200,
            retry_after_seconds=_retry_after(result),
        )
        return result

    def call_sync(
        self,
        scope: str,
        operation: Callable[[], Any],
    ) -> Any:
        """Apply the same admission policy to synchronous source clients.

        A few official-data loaders intentionally remain synchronous because
        they are called from batch workers.  They must not bypass the shared
        circuit/rate governor merely because their HTTP client is blocking.
        The operation is returned unchanged so callers can still consume
        context-manager responses such as ``urllib``'s HTTPResponse.
        """
        normalized = str(scope).strip()
        if not normalized:
            raise ValueError("external transport scope is required")
        circuit = self.circuits.breaker(normalized)
        circuit_decision = circuit.before_call()
        if not circuit_decision.allowed:
            raise ExternalTransportAdmissionError(
                f"external transport circuit blocked {normalized}: {circuit_decision.reason}"
            )
        rate_decision = self.rate_limits.before_request(normalized)
        if not rate_decision.allowed:
            circuit.record_success()
            raise ExternalTransportAdmissionError(
                f"external transport rate limit blocked {normalized}: {rate_decision.reason}"
            )
        try:
            result = operation()
        except BaseException:
            circuit.record_failure()
            self.rate_limits.complete_request(normalized, status_code=None)
            raise

        status_code = _status_code(result)
        if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
            circuit.record_failure()
        else:
            circuit.record_success()
        self.rate_limits.complete_request(
            normalized,
            status_code=status_code if isinstance(status_code, int) else 200,
            retry_after_seconds=_retry_after(result),
        )
        return result


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _status_code(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    if value is None:
        value = getattr(response, "status", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_DEFAULT_EXTERNAL_TRANSPORT_GUARD: ExternalTransportGuard | None = None


def default_external_transport_guard() -> ExternalTransportGuard:
    """Return the process-wide guard used by synchronous source loaders."""

    global _DEFAULT_EXTERNAL_TRANSPORT_GUARD
    if _DEFAULT_EXTERNAL_TRANSPORT_GUARD is None:
        _DEFAULT_EXTERNAL_TRANSPORT_GUARD = ExternalTransportGuard()
    return _DEFAULT_EXTERNAL_TRANSPORT_GUARD


__all__ = [
    "ExternalTransportAdmissionError",
    "ExternalTransportGuard",
    "default_external_transport_guard",
]
