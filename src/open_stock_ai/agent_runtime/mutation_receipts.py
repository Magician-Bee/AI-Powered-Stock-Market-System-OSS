"""Content-addressed Host receipts for mutation validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA_VERSION = "open_stock_ai.mutation_receipt.v1"
DOMAIN_SCHEMA_VERSION = "open_stock_ai.domain_mutation_receipt.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_mutation_receipt(
    *,
    name: str,
    arguments: dict[str, Any],
    result: Any,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    accepted: bool,
) -> dict[str, Any]:
    """Bind the exact mutation request and observed state to one Host hash."""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "authority": "host_validator",
        "tool": str(name),
        "arguments_sha256": _sha256(arguments),
        "result_sha256": _sha256(result),
        "before_sha256": _sha256(before) if before is not None else None,
        "after_sha256": _sha256(after) if after is not None else None,
        "accepted": bool(accepted),
    }
    return {**payload, "receipt_sha256": _sha256(payload)}


def verify_mutation_receipt(receipt: dict[str, Any]) -> bool:
    required = {
        "schema_version",
        "authority",
        "tool",
        "arguments_sha256",
        "result_sha256",
        "before_sha256",
        "after_sha256",
        "accepted",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        return False
    if receipt.get("schema_version") != SCHEMA_VERSION:
        return False
    if receipt.get("authority") != "host_validator":
        return False
    if not isinstance(receipt.get("tool"), str) or not receipt["tool"].strip():
        return False
    for key in ("arguments_sha256", "result_sha256", "receipt_sha256"):
        if not _is_sha256(receipt.get(key)):
            return False
    for key in ("before_sha256", "after_sha256"):
        if receipt[key] is not None and not _is_sha256(receipt[key]):
            return False
    if type(receipt.get("accepted")) is not bool:
        return False
    supplied = str(receipt.get("receipt_sha256") or "")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return supplied == _sha256(payload)


def build_domain_mutation_receipt(
    *,
    provider: str,
    tool: dict[str, Any],
    mutation_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Issue a provider-scoped Host receipt after validating one mutation.

    Providers cannot self-attest success. The Host first binds the exact
    request, result and observed state in ``mutation_receipt``; this second
    receipt records the declared capability domain that produced that change.
    """

    provider_id = str(provider or "").strip()
    tool_name = str(tool.get("name") or "").strip()
    category = str(tool.get("category") or "").strip() or "uncategorized"
    execution_backend = str(tool.get("execution_backend") or "").strip() or "in_process"
    if not provider_id or not tool_name:
        raise ValueError("Domain mutation receipt requires provider and tool name")
    if not verify_mutation_receipt(mutation_receipt):
        raise ValueError("Domain mutation receipt requires a valid Host mutation receipt")
    payload = {
        "schema_version": DOMAIN_SCHEMA_VERSION,
        "authority": "host_domain_validator",
        "provider": provider_id,
        "tool": tool_name,
        "category": category,
        "execution_backend": execution_backend,
        "mutation_receipt_sha256": mutation_receipt["receipt_sha256"],
        "accepted": mutation_receipt["accepted"],
    }
    return {**payload, "receipt_sha256": _sha256(payload)}


def verify_domain_mutation_receipt(
    receipt: dict[str, Any],
    *,
    mutation_receipt: dict[str, Any],
) -> bool:
    """Fail closed unless a domain receipt is bound to a valid Host receipt."""

    required = {
        "schema_version",
        "authority",
        "provider",
        "tool",
        "category",
        "execution_backend",
        "mutation_receipt_sha256",
        "accepted",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        return False
    if receipt.get("schema_version") != DOMAIN_SCHEMA_VERSION:
        return False
    if receipt.get("authority") != "host_domain_validator":
        return False
    for key in ("provider", "tool", "category", "execution_backend"):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            return False
    if type(receipt.get("accepted")) is not bool:
        return False
    if not verify_mutation_receipt(mutation_receipt):
        return False
    if receipt.get("mutation_receipt_sha256") != mutation_receipt.get("receipt_sha256"):
        return False
    if receipt.get("accepted") is not mutation_receipt.get("accepted"):
        return False
    supplied = str(receipt.get("receipt_sha256") or "")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _is_sha256(supplied) and supplied == _sha256(payload)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)
