from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from open_stock_ai.governance.production_requirements import production_requirement_status
from open_stock_ai.governance.authoritative_stores import authoritative_store_matrix
from open_stock_ai.governance.promotion_ladder import SQLitePromotionReceiptStore


_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _ROOT / "config" / "capability_status.yaml"


@dataclass(frozen=True)
class ExecutionStageResolution:
    mode: str
    live_trading_enabled: bool
    active_level: str
    requested_mode: str
    requested_live_trading_enabled: bool
    activation_blockers: tuple[str, ...]


@lru_cache(maxsize=1)
def _definition() -> dict[str, Any]:
    payload = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("capability status must be a mapping")
    levels = payload.get("levels")
    policy = payload.get("promotion_policy")
    if not isinstance(levels, dict) or not isinstance(policy, dict):
        raise ValueError("capability status requires levels and promotion_policy")
    ordered = policy.get("ordered_levels")
    if not isinstance(ordered, list) or not ordered:
        raise ValueError("capability status requires ordered_levels")
    if list(levels) != ordered:
        raise ValueError("capability status levels must match ordered_levels exactly")
    for ordinal, level in enumerate(ordered):
        entry = levels.get(level)
        if not isinstance(entry, dict) or entry.get("ordinal") != ordinal:
            raise ValueError("capability status level ordinals must be contiguous")
    if payload.get("current_level") not in levels:
        raise ValueError("capability status current_level must exist")
    requirement_ledger = str(payload.get("master_requirement_ledger") or "").strip()
    if not requirement_ledger or not (_ROOT / requirement_ledger).is_file():
        raise ValueError("capability status requires an existing master_requirement_ledger")
    return payload


def resolve_execution_stage(
    requested_mode: str | None,
    requested_live_trading_enabled: bool,
    *,
    promotion_store: SQLitePromotionReceiptStore | None = None,
) -> ExecutionStageResolution:
    """Resolve process settings without permitting configuration to skip stages."""
    definition = _definition()
    levels: Mapping[str, Mapping[str, Any]] = definition["levels"]
    active_level = _active_level(definition, promotion_store)
    active = levels[active_level]
    active_mode = str(active["execution_mode"])
    requested = str(requested_mode or active_mode).strip().casefold() or active_mode
    requested_live = bool(requested_live_trading_enabled)
    level_for_mode = {
        str(entry["execution_mode"]).casefold(): level for level, entry in levels.items()
    }
    blockers: list[str] = []
    requested_level = level_for_mode.get(requested)
    if requested_level is None:
        blockers.append("requested_execution_mode_is_not_a_governed_stage")
    elif int(levels[requested_level]["ordinal"]) > int(active["ordinal"]):
        blockers.append("requested_execution_mode_exceeds_current_approved_level")
    if requested_live:
        blockers.append("live_execution_requires_restricted_live_or_production_live_approval")
    if active.get("allows_broker_submission") is not False:
        blockers.append("current_capability_status_does_not_authorize_broker_submission")
    if blockers:
        return ExecutionStageResolution(
            mode=active_mode,
            live_trading_enabled=False,
            active_level=active_level,
            requested_mode=requested,
            requested_live_trading_enabled=requested_live,
            activation_blockers=tuple(blockers),
        )
    return ExecutionStageResolution(
        mode=requested,
        live_trading_enabled=False,
        active_level=active_level,
        requested_mode=requested,
        requested_live_trading_enabled=requested_live,
        activation_blockers=(),
    )


def capability_status(
    *,
    requested_mode: str | None = None,
    requested_live_trading_enabled: bool = False,
    promotion_store: SQLitePromotionReceiptStore | None = None,
) -> dict[str, Any]:
    definition = _definition()
    resolution = resolve_execution_stage(
        requested_mode,
        requested_live_trading_enabled,
        promotion_store=promotion_store,
    )
    levels = definition["levels"]
    ordered = definition["promotion_policy"]["ordered_levels"]
    production = production_requirement_status()
    promotion = _promotion_runtime_status(definition, promotion_store)
    return {
        "schema_version": definition["schema_version"],
        "source_of_truth": definition["source_of_truth"],
        "master_requirement_ledger": definition["master_requirement_ledger"],
        "current_level": resolution.active_level,
        "current_ordinal": levels[resolution.active_level]["ordinal"],
        "effective_execution_mode": resolution.mode,
        "live_order_submission_enabled": False,
        "model_direct_broker_submission_enabled": False,
        "promotion_policy": {
            **definition["promotion_policy"],
            "current_level_only": True,
        },
        "requested": {
            "mode": resolution.requested_mode,
            "live_trading_enabled": resolution.requested_live_trading_enabled,
        },
        "activation_blockers": list(resolution.activation_blockers),
        "promotion": promotion,
        "production_requirements": production,
        "authoritative_store_matrix": authoritative_store_matrix(),
        "research_execution_evidence_enabled": production["research_execution_evidence_enabled"],
        "levels": [
            {"name": name, **dict(levels[name]), "active": name == resolution.active_level}
            for name in ordered
        ],
    }


def _promotion_runtime_status(
    definition: dict[str, Any],
    store: SQLitePromotionReceiptStore | None,
) -> dict[str, Any]:
    configured_level = str(definition["current_level"])
    if store is None:
        return {
            "store_configured": False,
            "durable": False,
            "configured_level": configured_level,
            "persisted_level": None,
            "effective_level": configured_level,
            "active_level_source": "configured_baseline",
            "receipt_count": 0,
            "latest_receipt": None,
            "activation_blockers": ["durable_promotion_store_not_configured"],
        }
    receipts = store.receipts()
    latest = receipts[-1] if receipts else None
    persisted_level = latest.to_level.upper() if latest else None
    effective_level = persisted_level or configured_level
    return {
        "store_configured": True,
        "durable": True,
        "configured_level": configured_level,
        "persisted_level": persisted_level,
        "effective_level": effective_level,
        "active_level_source": "durable_promotion_receipt" if persisted_level else "configured_baseline",
        "receipt_count": len(receipts),
        "latest_receipt": latest.as_dict() if latest else None,
        "activation_blockers": [],
    }


def _active_level(
    definition: dict[str, Any],
    store: SQLitePromotionReceiptStore | None,
) -> str:
    """Resolve the actual capability stage from the immutable receipt ledger.

    The YAML value is the no-receipt baseline.  Once an owner-approved
    transition exists, a process must not ignore that durable authority and
    silently continue at the baseline stage after restart.
    """

    configured_level = str(definition["current_level"])
    if store is None:
        return configured_level
    receipts = store.receipts()
    if not receipts:
        return configured_level
    persisted_level = str(receipts[-1].to_level).upper()
    if persisted_level not in definition["levels"]:
        raise ValueError("durable_promotion_receipt_has_unknown_capability_level")
    return persisted_level


def render_readme_capability_status() -> str:
    status = capability_status()
    active = status["current_level"]
    mode = status["effective_execution_mode"]
    quant_gate = status["production_requirements"]["gates"]["quant_foundation"]
    quant_total = len(quant_gate["requirement_ids"])
    quant_complete = quant_total - len(quant_gate["incomplete_requirement_ids"])
    quant_blockers = "、".join(f"`{item}`" for item in quant_gate["incomplete_requirement_ids"])
    master_gate = status["production_requirements"]["gates"]["master_p0"]
    master_total = len(master_gate["requirement_ids"])
    master_incomplete = master_gate["incomplete_requirement_ids"]
    master_complete = master_total - len(master_incomplete)
    master_preview = "、".join(f"`{item}`" for item in master_incomplete[:8])
    if len(master_incomplete) > 8:
        master_preview = f"{master_preview} 等 {len(master_incomplete)} 項"
    release_complete = status["production_requirements"]["full_release_complete_count"]
    release_total = status["production_requirements"]["full_release_total"]
    return "\n".join(
        [
            "<!-- capability-status:start -->",
            "### 交易能力分級（自動產生）",
            "",
            f"唯一來源：`{status['source_of_truth']}`。目前核准最高等級為 **{active}**（`{mode}`）；",
            "券商實單、模型直接下單與跨級環境變數啟用均為關閉。",
            "升級必須依序通過 RESEARCH → PAPER → SHADOW → BROKER_SANDBOX → RESTRICTED_LIVE → PRODUCTION_LIVE，",
            "且需要簽章人工核准與該等級全部驗收證據。",
            f"P0–P105 真相登錄：`{status['master_requirement_ledger']}`；目前共有 **{release_total}** 項要求，",
            f"由 release gate 依阻擋項目逐項判定；P0 尚未完成 {master_preview}，任何未驗證項目都不得因為程式碼或 UI 存在而視為完成。",
            f"完整 P0–P105 release checklist：**{release_complete}/{release_total}**；P1/P2 未驗收項目同樣會阻擋 release。",
            f"Quant P0 基礎門檻為 **{quant_complete}/{quant_total}**；研究結果作為執行證據目前為 **關閉**，",
            f"待 {quant_blockers} 的阻擋條件全部驗收後才能開啟。",
            "<!-- capability-status:end -->",
        ]
    )
