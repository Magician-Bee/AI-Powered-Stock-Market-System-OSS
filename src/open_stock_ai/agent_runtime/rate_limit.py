"""Shared endpoint/tool/provider rate-limit governor with verifiable receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


SCHEMA_VERSION = "open_stock_ai.rate_limit_decision.v1"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    requests_per_window: int | None = None
    window_seconds: float | None = None
    maximum_concurrency: int = 1
    policy_verified: bool = False

    def __post_init__(self) -> None:
        if self.requests_per_window is not None and self.requests_per_window < 1:
            raise ValueError("requests_per_window must be positive")
        if self.window_seconds is not None and self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.maximum_concurrency < 1:
            raise ValueError("maximum_concurrency must be positive")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    scope: str
    allowed: bool
    reason: str
    retry_after_seconds: float
    in_flight: int
    observed_at: str
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": self.scope,
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "in_flight": self.in_flight,
            "observed_at": self.observed_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}


class ScopedRateLimitGovernor:
    def __init__(self, policies: dict[str, RateLimitPolicy] | None = None, *, unknown_policy_min_interval_seconds: float = 1.0, maximum_backoff_seconds: float = 300.0) -> None:
        self._policies = dict(policies or {})
        self._requests: dict[str, deque[datetime]] = defaultdict(deque)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._blocked_until: dict[str, datetime] = {}
        self._consecutive_429: dict[str, int] = defaultdict(int)
        self.unknown_policy_min_interval_seconds = unknown_policy_min_interval_seconds
        self.maximum_backoff_seconds = maximum_backoff_seconds

    def before_request(self, scope: str, *, now: datetime | None = None) -> RateLimitDecision:
        observed_at = now or datetime.now(timezone.utc)
        scope = str(scope).strip()
        if not scope:
            raise ValueError("rate limit scope is required")
        blocked_until = self._blocked_until.get(scope)
        if blocked_until is not None and observed_at < blocked_until:
            return self._decision(
                scope, False, "backoff_active", (blocked_until - observed_at).total_seconds(), observed_at,
                self._in_flight[scope],
            )
        policy = self._policies.get(scope)
        if self._in_flight[scope] >= (policy.maximum_concurrency if policy else 1):
            return self._decision(scope, False, "maximum_concurrency_reached", 0, observed_at, self._in_flight[scope])
        requests = self._requests[scope]
        if policy and policy.policy_verified and policy.requests_per_window and policy.window_seconds:
            window_start = observed_at - timedelta(seconds=policy.window_seconds)
            while requests and requests[0] <= window_start:
                requests.popleft()
            if len(requests) >= policy.requests_per_window:
                retry = (requests[0] + timedelta(seconds=policy.window_seconds) - observed_at).total_seconds()
                return self._decision(scope, False, "verified_window_limit_reached", max(0, retry), observed_at, self._in_flight[scope])
        elif requests:
            elapsed = (observed_at - requests[-1]).total_seconds()
            if elapsed < self.unknown_policy_min_interval_seconds:
                return self._decision(
                    scope, False, "unverified_policy_conservative_interval",
                    self.unknown_policy_min_interval_seconds - elapsed, observed_at, self._in_flight[scope],
                )
        requests.append(observed_at)
        self._in_flight[scope] += 1
        return self._decision(scope, True, "request_permitted", 0, observed_at, self._in_flight[scope])

    def complete_request(self, scope: str, *, status_code: int | None, retry_after_seconds: float | None = None, now: datetime | None = None) -> None:
        observed_at = now or datetime.now(timezone.utc)
        scope = str(scope).strip()
        self._in_flight[scope] = max(0, self._in_flight[scope] - 1)
        if status_code == 429:
            self._consecutive_429[scope] += 1
            delay = min(self.maximum_backoff_seconds, 2 ** (self._consecutive_429[scope] - 1))
            self._blocked_until[scope] = observed_at + timedelta(seconds=max(float(retry_after_seconds or 0), delay))
        elif status_code is not None and 200 <= status_code < 400:
            self._consecutive_429[scope] = 0
            self._blocked_until.pop(scope, None)

    @staticmethod
    def _decision(
        scope: str,
        allowed: bool,
        reason: str,
        retry_after: float,
        observed_at: datetime,
        in_flight: int,
    ) -> RateLimitDecision:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "allowed": allowed,
            "reason": reason,
            "retry_after_seconds": max(0.0, float(retry_after)),
            "in_flight": max(0, int(in_flight)),
            "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        }
        return RateLimitDecision(scope=scope, allowed=allowed, reason=reason, retry_after_seconds=payload["retry_after_seconds"], in_flight=payload["in_flight"], observed_at=payload["observed_at"], receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest())


def verify_rate_limit_receipt(receipt: dict[str, Any]) -> bool:
    required = {"schema_version", "scope", "allowed", "reason", "retry_after_seconds", "in_flight", "observed_at", "receipt_sha256"}
    if set(receipt) != required or receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = hashlib.sha256(_canonical({key: receipt[key] for key in required if key != "receipt_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(receipt.get("receipt_sha256") or ""))
