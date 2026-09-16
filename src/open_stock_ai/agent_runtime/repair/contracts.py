from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


def canonical_hash(value: Any) -> str:
    """Return a stable digest suitable for retry comparison, never Python's hash()."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ErrorReceipt:
    category: str
    component: str
    location: str
    expected: str
    actual: str
    retryable: bool
    same_error_count: int = 1
    completed_work_preserved: bool = True
    branch_id: str | None = None
    error_id: str = field(default_factory=lambda: f"ERR-{uuid4().hex}")
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.same_error_count < 1:
            raise ValueError("same_error_count must be positive")
        if not self.component or not self.location:
            raise ValueError("component and location are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.error_receipt.v1",
            **asdict(self),
        }


@dataclass(frozen=True, slots=True)
class FailureFingerprint:
    provider: str
    model: str
    tool: str
    schema_version: str
    error_category: str
    error_path: str
    expected: str
    actual: str

    @property
    def digest(self) -> str:
        return canonical_hash(asdict(self))

    @classmethod
    def from_receipt(
        cls,
        receipt: ErrorReceipt,
        *,
        provider: str,
        model: str,
        tool: str,
        schema_version: str,
    ) -> "FailureFingerprint":
        return cls(
            provider=provider,
            model=model,
            tool=tool,
            schema_version=schema_version,
            error_category=receipt.category,
            error_path=receipt.location,
            expected=receipt.expected,
            actual=receipt.actual,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.failure_fingerprint.v1",
            **asdict(self),
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class PatchOperation:
    op: Literal["add", "replace"]
    path: str
    value: Any


@dataclass(frozen=True, slots=True)
class ModelPatch:
    repair_for: str
    patch: tuple[PatchOperation, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelPatch":
        raw_patch = value.get("patch")
        if not isinstance(raw_patch, list) or not raw_patch:
            raise ValueError("Model patch must contain at least one operation")
        operations: list[PatchOperation] = []
        for item in raw_patch:
            if not isinstance(item, dict):
                raise ValueError("Patch operations must be objects")
            op = str(item.get("op") or "")
            if op not in {"add", "replace"}:
                raise ValueError(f"Unsupported patch operation: {op}")
            operations.append(
                PatchOperation(op=op, path=str(item.get("path") or ""), value=item.get("value"))
            )
        return cls(repair_for=str(value.get("repair_for") or ""), patch=tuple(operations))
