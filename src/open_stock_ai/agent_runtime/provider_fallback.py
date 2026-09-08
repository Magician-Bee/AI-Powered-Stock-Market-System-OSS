"""Explicit, fail-closed provider fallback policy for Agent runs.

Provider failover changes the model that produced a turn.  It is therefore a
runtime decision with a visible receipt, never an implicit retry hidden inside
an HTTP client or driver.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal


ProviderFallbackMode = Literal["fail_closed", "explicit_alternate"]


@dataclass(frozen=True, slots=True)
class ProviderFallbackDecision:
    primary_driver: str
    alternate_driver: str | None
    selected_driver: str | None
    mode: ProviderFallbackMode
    task_kind: str
    autonomy: str
    status: Literal["allowed", "blocked"]
    reason: str
    quality_change: str
    user_notice: str
    receipt_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.provider_fallback_receipt.v1",
            "primary_driver": self.primary_driver,
            "alternate_driver": self.alternate_driver,
            "selected_driver": self.selected_driver,
            "mode": self.mode,
            "task_kind": self.task_kind,
            "autonomy": self.autonomy,
            "status": self.status,
            "reason": self.reason,
            "quality_change": self.quality_change,
            "user_notice": self.user_notice,
            "receipt_hash": self.receipt_hash,
        }


@dataclass(frozen=True, slots=True)
class ProviderFallbackPolicy:
    """Policy is deliberately conservative and immutable for one run."""

    primary_driver: str
    mode: ProviderFallbackMode = "fail_closed"
    allowed_alternates: tuple[str, ...] = ()
    allowed_autonomy: tuple[str, ...] = ("advisory",)
    allowed_task_kinds: tuple[str, ...] = (
        "general_answer",
        "current_information",
        "market_information",
        "market_radar",
    )
    max_switches: int = 1

    def decide(
        self,
        *,
        alternate_driver: str | None,
        task_kind: str,
        autonomy: str,
    ) -> ProviderFallbackDecision:
        alternate = str(alternate_driver or "").strip() or None
        status: Literal["allowed", "blocked"] = "blocked"
        selected: str | None = None
        reason = "provider_failure_fail_closed"
        quality_change = "none"
        notice = "主要 Provider 失敗；系統已安全停止，未靜默切換模型。"

        if self.mode == "explicit_alternate":
            if not alternate:
                reason = "explicit_alternate_required"
            elif alternate == self.primary_driver:
                reason = "alternate_must_differ_from_primary"
            elif alternate not in self.allowed_alternates:
                reason = "alternate_not_allowlisted"
            elif autonomy not in self.allowed_autonomy:
                reason = "fallback_forbidden_for_autonomy"
            elif task_kind not in self.allowed_task_kinds:
                reason = "fallback_forbidden_for_task_kind"
            elif self.max_switches < 1:
                reason = "fallback_switch_budget_exhausted"
            else:
                status = "allowed"
                selected = alternate
                reason = "primary_provider_failed_explicit_alternate_allowed"
                quality_change = "provider_changed_unverified_quality"
                notice = (
                    f"主要 Provider 失敗；已依明確設定切換至 {alternate}。"
                    "模型能力與品質可能不同，結果只可作為目前任務的可讀取輸出。"
                )

        payload = {
            "schema_version": "open_stock_ai.provider_fallback_receipt.v1",
            "primary_driver": self.primary_driver,
            "alternate_driver": alternate,
            "selected_driver": selected,
            "mode": self.mode,
            "task_kind": task_kind,
            "autonomy": autonomy,
            "status": status,
            "reason": reason,
            "quality_change": quality_change,
            "user_notice": notice,
        }
        receipt_hash = _receipt_hash(payload)
        return ProviderFallbackDecision(
            primary_driver=self.primary_driver,
            alternate_driver=alternate,
            selected_driver=selected,
            mode=self.mode,
            task_kind=task_kind,
            autonomy=autonomy,
            status=status,
            reason=reason,
            quality_change=quality_change,
            user_notice=notice,
            receipt_hash=receipt_hash,
        )


def verify_provider_fallback_receipt(receipt: dict[str, Any]) -> bool:
    """Verify a persisted fallback receipt without trusting its hash field."""

    if receipt.get("schema_version") != "open_stock_ai.provider_fallback_receipt.v1":
        return False
    expected = _receipt_hash({key: receipt.get(key) for key in _RECEIPT_FIELDS})
    actual = str(receipt.get("receipt_hash") or "")
    return bool(actual) and hmac.compare_digest(expected, actual)


_RECEIPT_FIELDS = (
    "schema_version",
    "primary_driver",
    "alternate_driver",
    "selected_driver",
    "mode",
    "task_kind",
    "autonomy",
    "status",
    "reason",
    "quality_change",
    "user_notice",
)


def _receipt_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
