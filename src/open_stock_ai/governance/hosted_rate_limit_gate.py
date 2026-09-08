"""Hash-verified hosted evidence for rate-limit policies and real HTTP 429s."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from open_stock_ai.agent_runtime.rate_limit import verify_rate_limit_receipt


HOSTED_RATE_LIMIT_GATE_SCHEMA = "open_stock_ai.hosted_rate_limit_gate_receipt.v1"
RATE_POLICY_SCHEMA = "open_stock_ai.rate_limit_policy_receipt.v1"
HOSTED_RATE_SCOPES = (
    "endpoint:hosted-market-data",
    "tool:hosted-research",
    "provider:hosted-primary",
    "provider:hosted-alternate",
)


def build_rate_policy_receipt(scope: str, *, captured_at: str) -> dict[str, Any]:
    payload = {
        "schema_version": RATE_POLICY_SCHEMA,
        "scope": scope,
        "requests_per_window": 2,
        "window_seconds": 0.25,
        "maximum_concurrency": 1,
        "policy_verified": True,
        "captured_at": captured_at,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def build_hosted_rate_limit_gate_receipt(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    unknown_policy_observation: Mapping[str, Any],
    commit_sha: str,
    server_pid: int,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted rate-limit gate requires the exact candidate commit SHA")
    captured_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    scopes: list[dict[str, Any]] = []
    blockers: list[str] = []
    for scope in HOSTED_RATE_SCOPES:
        observation = dict(observations.get(scope) or {})
        entry = {
            "scope": scope,
            "policy_receipt": build_rate_policy_receipt(scope, captured_at=captured_at),
            **observation,
        }
        scopes.append(entry)
        if not _valid_scope(entry):
            blockers.append(f"rate_limit_invariant_failed:{scope}")
    unknown = dict(unknown_policy_observation)
    if not _valid_unknown_policy(unknown):
        blockers.append("unknown_policy_conservative_interval_failed")
    payload = {
        "schema_version": HOSTED_RATE_LIMIT_GATE_SCHEMA,
        "commit_sha": commit,
        "captured_at": captured_at,
        "server_pid": int(server_pid),
        "scopes": scopes,
        "unknown_policy": unknown,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_rate_limit_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version", "commit_sha", "captured_at", "server_pid", "scopes", "unknown_policy", "blockers", "passed", "receipt_sha256"
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_RATE_LIMIT_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    scopes = receipt.get("scopes")
    if not isinstance(scopes, list) or [item.get("scope") for item in scopes if isinstance(item, Mapping)] != list(HOSTED_RATE_SCOPES):
        return False
    valid = isinstance(receipt.get("server_pid"), int) and receipt["server_pid"] > 0
    valid = valid and all(_valid_scope(item) for item in scopes)
    valid = valid and _valid_unknown_policy(receipt.get("unknown_policy") or {})
    expected_passed = valid and not receipt.get("blockers")
    return receipt.get("passed") is expected_passed


def _valid_policy(raw: Mapping[str, Any], scope: str) -> bool:
    required = {
        "schema_version", "scope", "requests_per_window", "window_seconds", "maximum_concurrency", "policy_verified", "captured_at", "receipt_sha256"
    }
    if set(raw) != required or raw.get("schema_version") != RATE_POLICY_SCHEMA or raw.get("scope") != scope:
        return False
    payload = {key: raw[key] for key in required if key != "receipt_sha256"}
    return (
        raw.get("requests_per_window") == 2
        and raw.get("window_seconds") == 0.25
        and raw.get("maximum_concurrency") == 1
        and raw.get("policy_verified") is True
        and hmac.compare_digest(_digest(payload), str(raw.get("receipt_sha256") or ""))
    )


def _valid_scope(item: Mapping[str, Any]) -> bool:
    scope = str(item.get("scope") or "")
    decisions = [
        item.get("first_admission"), item.get("concurrency_block"), item.get("backoff_block"),
        item.get("recovery_first"), item.get("recovery_second"), item.get("window_block"),
    ]
    if not _valid_policy(item.get("policy_receipt") or {}, scope):
        return False
    if not all(isinstance(value, dict) and verify_rate_limit_receipt(value) for value in decisions):
        return False
    return (
        all(value.get("scope") == scope for value in decisions)
        and item["first_admission"].get("allowed") is True
        and item["concurrency_block"].get("reason") == "maximum_concurrency_reached"
        and item["backoff_block"].get("reason") == "backoff_active"
        and item["recovery_first"].get("allowed") is True
        and item["recovery_second"].get("allowed") is True
        and item["window_block"].get("reason") == "verified_window_limit_reached"
        and item.get("upstream_statuses") == [429, 200, 200]
        and item.get("upstream_request_count") == 3
    )


def _valid_unknown_policy(item: Mapping[str, Any]) -> bool:
    decisions = [item.get("first_admission"), item.get("interval_block"), item.get("recovered_admission")]
    return (
        all(isinstance(value, dict) and verify_rate_limit_receipt(value) for value in decisions)
        and item["first_admission"].get("allowed") is True
        and item["interval_block"].get("reason") == "unverified_policy_conservative_interval"
        and item["recovered_admission"].get("allowed") is True
        and item.get("upstream_statuses") == [200, 200]
        and item.get("upstream_request_count") == 2
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HOSTED_RATE_LIMIT_GATE_SCHEMA",
    "HOSTED_RATE_SCOPES",
    "RATE_POLICY_SCHEMA",
    "build_hosted_rate_limit_gate_receipt",
    "build_rate_policy_receipt",
    "verify_hosted_rate_limit_gate_receipt",
]
