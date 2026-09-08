from __future__ import annotations

import asyncio
import copy
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from open_stock_ai.agent_workspace import build_agent_watchlist
from open_stock_ai.analysis_contracts import (
    AnalysisProvenance,
    DecisionEnvelope,
    ModelInvocationReceipt,
)

from .codex_runtime import CodexRuntime


RADAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "symbol": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["buy_now", "sell_now", "wait_to_buy", "wait_to_sell", "hold"],
                    },
                    "timing": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "action", "timing", "confidence", "reason"],
            },
        },
    },
    "required": ["summary", "items"],
}

RADAR_CACHE_TTL_SECONDS = 300.0
_radar_cache: dict[tuple[int, bool], tuple[float, dict[str, Any]]] = {}
_radar_locks: dict[tuple[int, bool], asyncio.Lock] = {}


async def build_market_radar(
    runtime: CodexRuntime,
    *,
    limit: int = 6,
    explain: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    normalized_limit = max(2, min(limit, 8))
    key = (normalized_limit, explain)
    cached = _cached_radar(key)
    if cached is not None and not force:
        return cached

    lock = _radar_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cached_radar(key)
        if cached is not None and not force:
            return cached
        payload = await _build_market_radar(runtime, limit=normalized_limit, explain=explain)
        _radar_cache[key] = (time.monotonic(), copy.deepcopy(payload))
        payload["cached"] = False
        return payload


async def _build_market_radar(
    runtime: CodexRuntime,
    *,
    limit: int,
    explain: bool,
) -> dict[str, Any]:
    agent_watchlist = await asyncio.to_thread(
        build_agent_watchlist,
        horizon="swing",
        limit=limit,
    )
    workspace_items = [item for item in agent_watchlist.get("items", []) if isinstance(item, dict)]
    compact = [_compact_workspace(item) for item in workspace_items]
    universe = agent_watchlist.get("universe") if isinstance(agent_watchlist.get("universe"), dict) else {}
    universe_source = str(universe.get("source") or "none")
    symbols = [str(item.get("symbol")) for item in compact if str(item.get("symbol") or "").strip()]
    rule_items = [
        {**_fallback_item(item, explain=explain), "origin": "rule_strategy"}
        for item in compact
    ]
    model_items: list[dict[str, Any]] = []
    model_error: dict[str, str] | None = None
    call_id = f"market-radar-{uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc).isoformat()
    receipt = ModelInvocationReceipt(
        call_id=call_id,
        provider="codex",
        model_id="codex-app-server/default",
        status="not_run",
        started_at=started_at,
        completed_at=started_at,
    )
    if compact:
        try:
            ranked = await runtime.run_structured(
                "你是住在股市 AI 專案內的市場分析 Agent。請比較 supplied Agent workspaces，"
                "綜合 candidate_bucket、supplied_signal、data_ready、research_execution_eligible、"
                "risk_approved、paper exposure、技術面、新聞與 blocker 排序。"
                "buy_candidate/sell_candidate 只是研究候選，不是交易許可。"
                "只有 execution_permission=paper_approved、data_ready=true、"
                "research_execution_eligible=true 且 risk_approved=true 才能輸出 buy_now 或 sell_now。"
                "其餘正向訊號使用 wait_to_buy，負向訊號使用 wait_to_sell；資料阻擋時使用 hold。"
                "confidence 是模型自述的 0–1 排序強度，未經校準，不能視為成功機率。"
                "timing 請用精簡繁體中文；reason 最多 48 字並指出主要證據或 blocker。\n"
                f"Agent workspaces：{compact}",
                RADAR_SCHEMA,
            )
            model_items = [
                {
                    **item,
                    "origin": "model",
                    "model_call_id": call_id,
                    "model_call_succeeded": True,
                    "confidence_type": "model_self_reported",
                    "confidence_calibrated": False,
                }
                for item in _merge_and_enforce(compact, ranked.get("items", []), explain=explain)
            ]
            receipt = receipt.model_copy(
                update={
                    "status": "succeeded",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "raw_output_preserved": True,
                }
            )
        except Exception as exc:
            model_error = {
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            receipt = receipt.model_copy(
                update={
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )

    # A failed model call must not promote deterministic candidates into the
    # primary result. Rule evidence remains available in its dedicated envelope
    # section for diagnostics, while the displayed/model result stays empty.
    displayed_items = (
        model_items
        if receipt.status == "succeeded"
        else rule_items
        if receipt.status == "not_run"
        else []
    )
    source = (
        "Codex model analysis + quantitative rule workspace"
        if receipt.status == "succeeded"
        else "Quantitative rule workspace (non-model)"
    )
    summary = (
        _summary(displayed_items)
        if displayed_items
        else "模型分析失敗；未以量化規則結果代替。"
        if receipt.status == "failed"
        else "尚未選擇分析 Universe，本輪未執行模型或規則掃描。"
    )

    groups = {key: [] for key in ("buy_now", "sell_now", "wait_to_buy", "wait_to_sell", "hold")}
    for item in displayed_items:
        groups[item["action"]].append(item)
    data_sources = sorted(
        {
            str(item.get("price_source"))
            for item in compact
            if str(item.get("price_source") or "").strip()
        }
    )
    origin = (
        "hybrid"
        if receipt.status == "succeeded" and rule_items
        else "model"
        if receipt.status == "succeeded"
        else "rule_strategy"
        if rule_items
        else "none"
    )
    provenance = AnalysisProvenance(
        origin=origin,
        provider=receipt.provider if receipt.status != "not_run" else None,
        model_id=receipt.model_id if receipt.status != "not_run" else None,
        model_call_id=receipt.call_id if receipt.status != "not_run" else None,
        model_call_succeeded=receipt.status == "succeeded",
        rule_set_id="market_radar_rule_projection.v1" if rule_items else None,
        universe_source=universe_source,
        symbols_considered=symbols,
        data_sources=data_sources,
        data_ready=bool(compact) and all(item.get("data_ready") is True for item in compact),
        fallback_used=False,
        fallback_reason=None,
    )
    envelope = DecisionEnvelope(
        observation={"items": compact, "universe": universe},
        rule_analysis={
            "method": "market_radar_rule_projection.v1",
            "items": rule_items,
            "is_model_output": False,
        }
        if rule_items
        else None,
        model_analysis={
            "items": model_items,
            "status": "succeeded",
        }
        if receipt.status == "succeeded"
        else None,
        risk_evaluation={
            "method": "host_risk_gate",
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "execution_allowed": _can_execute(item, {"buy", "add", "sell", "reduce"}),
                    "reason": item.get("risk_reason"),
                }
                for item in compact
            ],
        },
        execution_status={
            "mode": "paper",
            "submitted": False,
            "explicit_execution_required": True,
        },
        provenance=provenance,
        model_invocation=receipt,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "badge": (
            "HYBRID"
            if receipt.status == "succeeded" and rule_items
            else "MODEL"
            if receipt.status == "succeeded"
            else "MODEL ERROR"
            if receipt.status == "failed"
            else "RULE"
            if rule_items
            else "NO UNIVERSE"
        ),
        "model_status": receipt.status,
        "model_error": model_error,
        "model_invocation": receipt.model_dump(),
        "provenance": provenance.model_dump(),
        "decision_envelope": envelope.model_dump(),
        "summary": summary,
        "count": len(displayed_items),
        "items": displayed_items,
        "rule_items": rule_items,
        "model_items": model_items,
        "groups": groups,
        "action_plan": _action_plan(displayed_items),
        "portfolio": agent_watchlist.get("portfolio") or {},
        "agent_tool_manifest": agent_watchlist.get("tool_manifest") or {},
        "agent_bucket_counts": agent_watchlist.get("bucket_counts") or {},
        "execution_boundary": "candidate_ranking_only_explicit_execution_required",
        "computer_use_ready": True,
        "explain": explain,
    }


def _cached_radar(key: tuple[int, bool]) -> dict[str, Any] | None:
    entry = _radar_cache.get(key)
    if entry is None or time.monotonic() - entry[0] > RADAR_CACHE_TTL_SECONDS:
        return None
    payload = copy.deepcopy(entry[1])
    payload["cached"] = True
    return payload


def _compact_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    decision = workspace.get("decision") if isinstance(workspace.get("decision"), dict) else {}
    snapshot = decision.get("market_snapshot") if isinstance(decision.get("market_snapshot"), dict) else {}
    raw = snapshot.get("raw") if isinstance(snapshot.get("raw"), dict) else {}
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    entity = summary.get("entity") if isinstance(summary.get("entity"), dict) else {}
    signal = workspace.get("signal_summary") if isinstance(workspace.get("signal_summary"), dict) else {}
    risk = workspace.get("risk_summary") if isinstance(workspace.get("risk_summary"), dict) else {}
    data_status = workspace.get("data_status") if isinstance(workspace.get("data_status"), dict) else {}
    research_status = workspace.get("research_status") if isinstance(workspace.get("research_status"), dict) else {}
    portfolio_status = workspace.get("portfolio_status") if isinstance(workspace.get("portfolio_status"), dict) else {}
    schema = decision.get("signal", {}).get("decision_schema", {}) if isinstance(decision.get("signal"), dict) else {}
    ratings = schema.get("source_ratings") if isinstance(schema, dict) and isinstance(schema.get("source_ratings"), dict) else {}
    blockers = [str(item) for item in workspace.get("blockers", []) if item]
    symbol = str(workspace.get("symbol") or signal.get("symbol") or "")
    return {
        "symbol": symbol,
        "name": entity.get("name") or symbol,
        "candidate_bucket": workspace.get("recommendation_bucket") or "watch",
        "execution_permission": workspace.get("execution_permission") or "blocked",
        "supplied_signal": signal.get("action") or "hold",
        "rating": schema.get("rating") or "Hold",
        "risk_approved": risk.get("approved") is True,
        "data_ready": data_status.get("decision_ready") is True,
        "data_realtime": data_status.get("is_realtime") is True,
        "data_fallback": data_status.get("is_fallback") is True,
        "price_source": data_status.get("price_source"),
        "exchange_timestamp": data_status.get("exchange_timestamp"),
        "research_advisory_ready": research_status.get("advisory_ready") is True,
        "research_execution_eligible": research_status.get("execution_evidence_eligible") is True,
        "confidence": round(float(signal.get("confidence") or 0.0), 3),
        "confidence_type": signal.get("confidence_type") or "none",
        "confidence_calibrated": signal.get("confidence_calibrated") is True,
        "rule_score": signal.get("rule_score"),
        "rule_set_id": signal.get("rule_set_id"),
        "decision_status": signal.get("decision_status"),
        "price": signal.get("entry_price"),
        "target_price": signal.get("target_price"),
        "stop_loss": signal.get("stop_loss"),
        "price_method_id": signal.get("price_method_id"),
        "unified_score": round(float(ratings.get("unified_score") or 0.0), 4),
        "technical": ratings.get("technical"),
        "sentiment": (ratings.get("sentiment") or {}).get("label")
        if isinstance(ratings.get("sentiment"), dict)
        else ratings.get("sentiment"),
        "horizon": workspace.get("horizon") or "swing",
        "risk_reason": risk.get("reason"),
        "paper_total_exposure_pct": portfolio_status.get("total_position_size_pct") or 0.0,
        "paper_symbol_exposure": portfolio_status.get("symbol_exposure") or {},
        "blockers": blockers,
        "agent_next_actions": workspace.get("agent_next_actions") or [],
    }


def _can_execute(item: dict[str, Any], allowed_signals: set[str]) -> bool:
    signal = str(item.get("supplied_signal") or "hold").lower()
    return bool(
        signal in allowed_signals
        and item.get("execution_permission") == "paper_approved"
        and item.get("risk_approved") is True
        and item.get("data_ready") is True
        and item.get("research_execution_eligible") is True
    )


def _fallback_item(item: dict[str, Any], *, explain: bool) -> dict[str, Any]:
    signal = str(item.get("supplied_signal") or "hold").lower()
    score = float(item.get("unified_score") or 0.0)
    wait_days = max(1, min(10, math.ceil((0.55 - min(abs(score), 0.55)) * 12)))
    if _can_execute(item, {"buy", "add"}):
        action, timing = "buy_now", "紙上風控已通過，可分批"
    elif _can_execute(item, {"sell", "reduce"}):
        action, timing = "sell_now", "紙上風控已通過，可分批減碼"
    elif item.get("data_ready") is not True:
        action, timing = "hold", "先更新主要行情資料"
    elif signal in {"buy", "add"} or item.get("candidate_bucket") == "buy_candidate":
        action, timing = "wait_to_buy", f"約 {wait_days} 個交易日內重估"
    elif signal in {"sell", "reduce"} or item.get("candidate_bucket") == "sell_candidate":
        action, timing = "wait_to_sell", f"約 {wait_days} 個交易日內重估"
    elif score > 0.12:
        action, timing = "wait_to_buy", f"約 {wait_days} 個交易日內重估"
    elif score < -0.12:
        action, timing = "wait_to_sell", f"約 {wait_days} 個交易日內重估"
    else:
        action, timing = "hold", "下一交易日收盤後重估"
    reason = _short_reason(item) if explain else ""
    return {
        **item,
        "action": action,
        "timing": timing,
        "reason": reason,
        **_action_details(item, action),
    }


def _merge_and_enforce(
    compact: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    *,
    explain: bool,
) -> list[dict[str, Any]]:
    by_symbol = {str(item.get("symbol")): item for item in ranked if isinstance(item, dict)}
    merged: list[dict[str, Any]] = []
    for source in compact:
        fallback = _fallback_item(source, explain=explain)
        candidate = by_symbol.get(str(source.get("symbol")), {})
        requested_action = str(candidate.get("action") or fallback["action"])
        action = requested_action
        gate_overrode_agent = False
        if action == "buy_now" and not _can_execute(source, {"buy", "add"}):
            action = fallback["action"]
            gate_overrode_agent = True
        if action == "sell_now" and not _can_execute(source, {"sell", "reduce"}):
            action = fallback["action"]
            gate_overrode_agent = True
        if source.get("data_ready") is not True:
            if action != "hold":
                gate_overrode_agent = True
            action = "hold"
        timing = str(candidate.get("timing") or fallback["timing"])
        if action not in {"buy_now", "sell_now"}:
            timing = fallback["timing"]
        if explain:
            reason = _short_reason(source) if gate_overrode_agent else str(candidate.get("reason") or _short_reason(source))[:96]
        else:
            reason = ""
        merged.append(
            {
                **source,
                "action": action,
                "timing": timing,
                "confidence": round(float(candidate.get("confidence") or source.get("confidence") or 0.0), 3),
                "reason": reason,
                "agent_requested_action": requested_action,
                "gate_overrode_agent": gate_overrode_agent,
                **_action_details(source, action),
            }
        )
    order = {"buy_now": 0, "sell_now": 1, "wait_to_buy": 2, "wait_to_sell": 3, "hold": 4}
    return sorted(merged, key=lambda item: (order.get(item["action"], 9), -item["confidence"]))


def _short_reason(item: dict[str, Any]) -> str:
    blockers = [str(value) for value in item.get("blockers", []) if value]
    if item.get("data_ready") is not True:
        return f"資料未就緒：{', '.join(blockers[:2]) or '主要行情不可用'}"[:96]
    if item.get("research_execution_eligible") is not True:
        return f"研究候選，尚不可執行：{', '.join(blockers[:2]) or '待實證驗證'}"[:96]
    if item.get("risk_approved") is not True:
        return str(item.get("risk_reason") or "尚未通過風控")[:96]
    return (
        f"技術面 {item.get('technical') or '中性'}，"
        f"未校準規則分數 {float(item.get('rule_score') or item.get('confidence') or 0):.3f}"
    )


def _action_details(item: dict[str, Any], action: str) -> dict[str, str]:
    price = _display_price(item.get("price"))
    target = _display_price(item.get("target_price"))
    stop = _display_price(item.get("stop_loss"))
    if action == "buy_now":
        next_action = "由使用者明確啟動紙上委託"
        trigger = f"資料、研究與風控已通過；參考價 {price}" if price else "資料、研究與風控已通過"
    elif action == "sell_now":
        next_action = "由使用者明確啟動紙上減碼"
        trigger = f"跌破 {stop} 優先檢查" if stop else "賣出訊號及所有 Gate 已通過"
    elif action == "wait_to_buy":
        next_action = "列入買進候選，設定提醒"
        trigger = f"站穩 {price} 且研究、風控通過" if price else "訊號維持且研究、風控通過"
    elif action == "wait_to_sell":
        next_action = "列入減碼候選，檢查既有曝險"
        trigger = f"跌破 {stop} 且研究、風控通過" if stop else "賣出條件與所有 Gate 通過"
    else:
        next_action = "保持觀察，不送出委託"
        if item.get("data_ready") is not True:
            trigger = "主要行情恢復後重新分析"
        elif target and stop:
            trigger = f"上看 {target}；跌破 {stop} 重估"
        elif stop:
            trigger = f"跌破 {stop} 立即重估"
        else:
            trigger = "下一交易日收盤後重新判斷"
    return {"next_action": next_action, "trigger": trigger}


def _display_price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number) or number <= 0:
        return ""
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _summary(items: list[dict[str, Any]]) -> str:
    counts = {
        key: sum(1 for item in items if item["action"] == key)
        for key in RADAR_SCHEMA["properties"]["items"]["items"]["properties"]["action"]["enum"]
    }
    if counts["buy_now"] or counts["sell_now"]:
        return (
            f"紙上執行條件完整：買進 {counts['buy_now']} 檔、賣出 {counts['sell_now']} 檔；"
            f"另有 {counts['wait_to_buy']} 檔買進候選、{counts['wait_to_sell']} 檔減碼候選。"
        )
    return (
        f"本輪沒有可直接執行標的；{counts['wait_to_buy']} 檔買進候選、"
        f"{counts['wait_to_sell']} 檔減碼候選、{counts['hold']} 檔觀察或資料阻擋。"
    )


def _action_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    buy_now = sum(1 for item in items if item["action"] == "buy_now")
    sell_now = sum(1 for item in items if item["action"] == "sell_now")
    buy_candidates = sum(1 for item in items if item["action"] == "wait_to_buy")
    sell_candidates = sum(1 for item in items if item["action"] == "wait_to_sell")
    monitored = sum(1 for item in items if item["action"] == "hold")
    if buy_now or sell_now:
        headline = f"可由使用者確認紙上動作：買進 {buy_now} 檔、賣出 {sell_now} 檔"
    elif buy_candidates or sell_candidates:
        headline = f"目前只產生候選：買進 {buy_candidates} 檔、減碼 {sell_candidates} 檔"
    else:
        headline = "目前維持觀察或等待資料更新"
    return {
        "headline": headline,
        "next_review": "5 分鐘後可更新；下一交易日收盤後完整重估",
        "buy_now_count": buy_now,
        "sell_now_count": sell_now,
        "buy_candidate_count": buy_candidates,
        "sell_candidate_count": sell_candidates,
        "monitored_count": monitored,
        "explicit_execution_required": True,
    }
