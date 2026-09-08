from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId


class BrokerRateLimitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: BrokerId
    endpoint_group: str
    requests_per_window: int | None = Field(default=None, ge=1)
    window_seconds: int | None = Field(default=None, ge=1)
    maximum_concurrency: int = Field(default=1, ge=1)
    policy_verified: bool = False


class BrokerRateLimitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_rate_limit_decision.v1"] = (
        "stock_ai.broker_rate_limit_decision.v1"
    )
    broker_id: BrokerId
    endpoint_group: str
    allowed: bool
    reason: str
    retry_after_seconds: float = Field(ge=0)
    decided_at: datetime


class BrokerRateLimitGovernor:
    """Fail-conservative per-broker gate with explicit 429 backoff."""

    def __init__(
        self,
        policies: list[BrokerRateLimitPolicy] | None = None,
        *,
        unknown_policy_min_interval_seconds: float = 1.0,
        maximum_backoff_seconds: float = 300.0,
    ) -> None:
        self._policies = {
            (item.broker_id, item.endpoint_group): item for item in policies or []
        }
        self._requests: dict[tuple[str, str], deque[datetime]] = defaultdict(deque)
        self._in_flight: dict[tuple[str, str], int] = defaultdict(int)
        self._blocked_until: dict[tuple[str, str], datetime] = {}
        self._consecutive_429: dict[tuple[str, str], int] = defaultdict(int)
        self.unknown_policy_min_interval_seconds = (
            unknown_policy_min_interval_seconds
        )
        self.maximum_backoff_seconds = maximum_backoff_seconds

    def before_request(
        self,
        broker_id: BrokerId,
        endpoint_group: str,
        *,
        now: datetime | None = None,
    ) -> BrokerRateLimitDecision:
        observed_at = now or datetime.now(timezone.utc)
        key = (broker_id, endpoint_group)
        blocked_until = self._blocked_until.get(key)
        if blocked_until is not None and observed_at < blocked_until:
            return self._decision(
                broker_id,
                endpoint_group,
                False,
                "broker_backoff_active",
                (blocked_until - observed_at).total_seconds(),
                observed_at,
            )
        policy = self._policies.get(key)
        maximum_concurrency = policy.maximum_concurrency if policy else 1
        if self._in_flight[key] >= maximum_concurrency:
            return self._decision(
                broker_id,
                endpoint_group,
                False,
                "maximum_concurrency_reached",
                0,
                observed_at,
            )

        requests = self._requests[key]
        if (
            policy is not None
            and policy.policy_verified
            and policy.requests_per_window is not None
            and policy.window_seconds is not None
        ):
            window_start = observed_at - timedelta(seconds=policy.window_seconds)
            while requests and requests[0] <= window_start:
                requests.popleft()
            if len(requests) >= policy.requests_per_window:
                retry = (
                    requests[0]
                    + timedelta(seconds=policy.window_seconds)
                    - observed_at
                ).total_seconds()
                return self._decision(
                    broker_id,
                    endpoint_group,
                    False,
                    "verified_window_limit_reached",
                    max(0, retry),
                    observed_at,
                )
        elif requests:
            elapsed = (observed_at - requests[-1]).total_seconds()
            if elapsed < self.unknown_policy_min_interval_seconds:
                return self._decision(
                    broker_id,
                    endpoint_group,
                    False,
                    "unverified_policy_conservative_interval",
                    self.unknown_policy_min_interval_seconds - elapsed,
                    observed_at,
                )

        requests.append(observed_at)
        self._in_flight[key] += 1
        return self._decision(
            broker_id,
            endpoint_group,
            True,
            "request_permitted",
            0,
            observed_at,
        )

    def complete_request(
        self,
        broker_id: BrokerId,
        endpoint_group: str,
        *,
        status_code: int | None,
        retry_after_seconds: float | None = None,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(timezone.utc)
        key = (broker_id, endpoint_group)
        self._in_flight[key] = max(0, self._in_flight[key] - 1)
        if status_code == 429:
            self._consecutive_429[key] += 1
            exponential = min(
                self.maximum_backoff_seconds,
                2 ** (self._consecutive_429[key] - 1),
            )
            delay = max(float(retry_after_seconds or 0), float(exponential))
            self._blocked_until[key] = observed_at + timedelta(seconds=delay)
        elif status_code is not None and 200 <= status_code < 400:
            self._consecutive_429[key] = 0
            self._blocked_until.pop(key, None)

    @staticmethod
    def _decision(
        broker_id: BrokerId,
        endpoint_group: str,
        allowed: bool,
        reason: str,
        retry_after_seconds: float,
        decided_at: datetime,
    ) -> BrokerRateLimitDecision:
        return BrokerRateLimitDecision(
            broker_id=broker_id,
            endpoint_group=endpoint_group,
            allowed=allowed,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
            decided_at=decided_at,
        )
