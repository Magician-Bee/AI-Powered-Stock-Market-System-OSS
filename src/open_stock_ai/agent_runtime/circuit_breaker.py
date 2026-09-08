"""Shared fail-closed circuit breakers for provider/source/broker calls."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal


CircuitState = Literal["closed", "open", "half_open"]
SCHEMA_VERSION = "open_stock_ai.circuit_breaker_decision.v1"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 3
    base_backoff_seconds: float = 1.0
    maximum_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if self.base_backoff_seconds <= 0 or self.maximum_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("circuit backoff bounds are invalid")


@dataclass(frozen=True, slots=True)
class CircuitBreakerDecision:
    scope: str
    allowed: bool
    state: CircuitState
    reason: str
    retry_after_seconds: float
    consecutive_failures: int
    observed_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": self.scope,
            "allowed": self.allowed,
            "state": self.state,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "consecutive_failures": self.consecutive_failures,
            "observed_at": self.observed_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.receipt_sha256)


class CircuitBreaker:
    """A deterministic closed/open/half-open state machine for one scope."""

    def __init__(self, scope: str, policy: CircuitBreakerPolicy | None = None) -> None:
        self.scope = str(scope).strip()
        if not self.scope:
            raise ValueError("circuit scope is required")
        self.policy = policy or CircuitBreakerPolicy()
        self.state: CircuitState = "closed"
        self.consecutive_failures = 0
        self._opened_until: datetime | None = None
        self._probe_in_flight = False

    def before_call(self, *, now: datetime | None = None) -> CircuitBreakerDecision:
        observed_at = now or datetime.now(timezone.utc)
        if self.state == "open":
            if self._opened_until is None or observed_at < self._opened_until:
                retry = max(0.0, (self._opened_until - observed_at).total_seconds()) if self._opened_until else 0.0
                return self._decision(False, "open_backoff_active", retry, observed_at)
            self.state = "half_open"
            self._probe_in_flight = False
        if self.state == "half_open":
            if self._probe_in_flight:
                return self._decision(False, "half_open_probe_in_flight", 0.0, observed_at)
            self._probe_in_flight = True
            return self._decision(True, "half_open_probe_allowed", 0.0, observed_at)
        return self._decision(True, "circuit_closed", 0.0, observed_at)

    def record_success(self, *, now: datetime | None = None) -> CircuitBreakerDecision:
        observed_at = now or datetime.now(timezone.utc)
        self.state = "closed"
        self.consecutive_failures = 0
        self._opened_until = None
        self._probe_in_flight = False
        return self._decision(True, "success_closed_circuit", 0.0, observed_at)

    def record_failure(self, *, now: datetime | None = None) -> CircuitBreakerDecision:
        observed_at = now or datetime.now(timezone.utc)
        self.consecutive_failures += 1
        self._probe_in_flight = False
        if self.consecutive_failures >= self.policy.failure_threshold:
            self.state = "open"
            exponent = self.consecutive_failures - self.policy.failure_threshold
            delay = min(self.policy.maximum_backoff_seconds, self.policy.base_backoff_seconds * (2**exponent))
            self._opened_until = observed_at + timedelta(seconds=delay)
            return self._decision(False, "failure_threshold_opened_circuit", delay, observed_at)
        self.state = "closed"
        return self._decision(True, "failure_below_threshold", 0.0, observed_at)

    def _decision(self, allowed: bool, reason: str, retry_after: float, observed_at: datetime) -> CircuitBreakerDecision:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scope": self.scope,
            "allowed": allowed,
            "state": self.state,
            "reason": reason,
            "retry_after_seconds": max(0.0, float(retry_after)),
            "consecutive_failures": self.consecutive_failures,
            "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        }
        return CircuitBreakerDecision(
            scope=self.scope,
            allowed=allowed,
            state=self.state,
            reason=reason,
            retry_after_seconds=max(0.0, float(retry_after)),
            consecutive_failures=self.consecutive_failures,
            observed_at=payload["observed_at"],
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )


class CircuitBreakerRegistry:
    """Keep independent breakers for provider, source and broker scopes."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, scope: str, *, policy: CircuitBreakerPolicy | None = None) -> CircuitBreaker:
        normalized = str(scope).strip()
        existing = self._breakers.get(normalized)
        if existing is not None:
            if policy is not None and policy != existing.policy:
                raise ValueError("circuit_policy_cannot_change_for_existing_scope")
            return existing
        created = CircuitBreaker(normalized, policy)
        self._breakers[normalized] = created
        return created

    def before_call(self, scope: str, *, now: datetime | None = None) -> CircuitBreakerDecision:
        return self.breaker(scope).before_call(now=now)


def verify_circuit_breaker_receipt(receipt: dict[str, Any]) -> bool:
    required = {"schema_version", "scope", "allowed", "state", "reason", "retry_after_seconds", "consecutive_failures", "observed_at", "receipt_sha256"}
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = hashlib.sha256(_canonical({key: receipt[key] for key in required if key != "receipt_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(receipt.get("receipt_sha256") or ""))
