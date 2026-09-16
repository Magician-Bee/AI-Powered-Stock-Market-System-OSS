from __future__ import annotations


SUPPORTED_MISFIRE_POLICIES = frozenset({"run_once", "catch_up", "skip"})


def validate_misfire_policy(value: str | None) -> str:
    policy = str(value or "run_once")
    if policy not in SUPPORTED_MISFIRE_POLICIES:
        raise ValueError(f"Unsupported misfire_policy: {policy}")
    return policy
