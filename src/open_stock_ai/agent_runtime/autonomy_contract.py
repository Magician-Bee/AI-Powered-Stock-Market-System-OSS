"""Outcome contract for a continuing campaign, distinct from an immediate order."""
from __future__ import annotations

from typing import Any
import re

CAMPAIGN_MUTATIONS = frozenset({"autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"})


def autonomous_pipeline_requested(text: str) -> bool:
    lowered = text.casefold()
    return (any(word in lowered for word in ("自主", "自動", "autonomous", "autonomy."))
            and any(word in lowered for word in ("流水線", "流程", "全市場", "進出場", "持倉管理", "pipeline", "campaign", "autonomy.")))


def autonomous_campaign_execution_requested(text: str) -> bool:
    """A campaign topic, explanation, design or status check is not consent.

    Check actionable clauses separately so 'explain, then activate' remains an
    execution request, while 'explain how to activate' cannot grant authority.
    """
    if not autonomous_pipeline_requested(text):
        return False
    for clause in re.split(r"[。！？!?；;，,\n]", text.casefold()):
        clause = clause.strip()
        if re.search(r"如何|怎樣|怎麼|是否|什麼|\b(?:how|whether|what|why)\b", clause):
            continue
        if re.match(
            r"^(?:(?:請|先|請先|幫我|替我|麻煩|please|can you|could you)\s*)*"
            r"(?:說明|解釋|介紹|評估|設計|討論|研究|分析|查詢|確認|查看|檢查|了解|想知道|我想了解|如何|怎樣|怎麼|"
            r"explain\b|describe\b|design\b|evaluate\b|assess\b|how\b|what\b|check\b|tell me\b)", clause,
        ):
            continue
        if re.search(r"(?:不要|不必|無需|勿|禁止|暫不|尚未|還沒|不)\s*(?:啟動|啟用|開始|執行|建立|恢復|繼續)|\b(?:do not|don't|never)\s+(?:start|activate|enable|run|execute|resume)", clause):
            continue
        if re.match(r"^(?:昨天|昨日|先前|之前|過去|原本|已經|目前已|系統已|我已)", clause):
            continue
        if re.search(r"單元測試|測試程式|程式碼|架構圖|文件|\b(?:unit tests?|test suite|documentation|source code)\b", clause):
            continue
        campaign_object = (autonomous_pipeline_requested(clause)
                           or bool(re.search(r"(?:紙上|模擬|paper|simulated).*(?:交易|流程|trade|order|pipeline|campaign)", clause))
                           or bool(re.search(r"(?:該|此|上述|同一|這個)(?:自主|紙上|交易)*(?:流程|流水線)|"
                                             r"(?:啟動|啟用|恢復|繼續執行)\s*它|\b(?:activate|start|resume)\s+(?:it|that campaign)\b", clause)))
        if not campaign_object:
            continue
        if re.search(r"(?<!已)(?:啟動|啟用|開始|執行|建立|恢復|繼續|持續管理|正在執行)|"
                     r"\b(?:start|activate|enable|launch|execute|resume|continue|run)\b", clause):
            return True
    return False


def bind_campaign_authorization(context: Any, *, objective: str, task_kind: str | None) -> None:
    """Derive permission from this Host objective, including after restoration."""
    from .completion_contract import objective_completion_contract

    context.state["explicit_autonomous_campaign_authorized"] = bool(
        context.allow_paper_orders and context.autonomy in {"paper_execute", "full_execute"}
        and objective_completion_contract(objective, task_kind).get("autonomous_pipeline_requested") is True
    )


def campaign_execution_authorized(context: Any) -> bool:
    return bool(context.allow_paper_orders and context.autonomy in {"paper_execute", "full_execute"}
                and context.state.get("explicit_autonomous_campaign_authorized") is True)


def valid_campaign_mutation(name: str, arguments: dict, result: dict) -> bool:
    if result.get("schema_version") != "open_stock_ai.autonomy_tool_receipt.v1" or result.get("mode") != "paper":
        return False
    if not result.get("account_id") or result.get("action") != name:
        return False
    if not re.fullmatch(r"AE-[0-9a-f]{64}", str(result.get("campaign_receipt_id") or "")):
        return False
    if name in {"autonomy.propose_plan", "autonomy.close_plan"}:
        plan = result.get("plan")
        if not (isinstance(plan, dict) and plan.get("account_id") == result["account_id"]
                and plan.get("plan_id") and plan.get("symbol") and isinstance(plan.get("definition"), dict)
                and plan["definition"].get("strategy_id") and isinstance(plan.get("state"), dict)
                and plan.get("status") and plan["state"].get("status") == plan["status"]):
            return False
        if name == "autonomy.propose_plan":
            metadata = plan["definition"].get("metadata")
            return bool(arguments.get("cycle_id") and result.get("cycle_id") == arguments["cycle_id"]
                        and plan["symbol"] == str(arguments.get("symbol") or "").upper()
                        and isinstance(metadata, dict) and metadata.get("cycle_id") == arguments["cycle_id"])
        request = result.get("exit_request")
        if not (plan["plan_id"] == arguments.get("plan_id") and isinstance(request, dict)
                and request.get("plan_id") == plan["plan_id"]
                and request.get("request_id") and request.get("reason") == "agent_reassessment"
                and re.fullmatch(r"AE-[0-9a-f]{64}", str(request.get("evidence_id") or ""))):
            return False
    management = result.get("management")
    if not isinstance(management, dict) or management.get("account_id") != result["account_id"]:
        return False
    if not isinstance(management.get("results"), list) or management.get("errors") != []:
        return False
    if name == "autonomy.activate":
        return (result.get("cycle_id") == arguments.get("cycle_id") and isinstance(result.get("plans"), list)
                and isinstance(result.get("skipped"), list) and management.get("enabled") is True)
    return name in {"autonomy.manage", "autonomy.close_plan"}


def campaign_completion(observations: list[dict[str, Any]]) -> dict[str, Any]:
    research = [row["result"] for row in observations
                if row.get("tool", row.get("name")) == "autonomy.research" and isinstance(row.get("result"), dict)
                and row["result"].get("schema_version") == "open_stock_ai.autonomous_research_cycle.v1"
                and row["result"].get("deep_success_count", 0) > 0]
    activations = [row["result"] for row in observations
                   if row.get("tool", row.get("name")) == "autonomy.activate" and isinstance(row.get("result"), dict)
                   and valid_campaign_mutation("autonomy.activate", {"cycle_id": row["result"].get("cycle_id")}, row["result"])]
    completed = [a for a in activations if any(r["cycle_id"] == a["cycle_id"] for r in research)]
    return {"passed": bool(completed), "missing_requirements": [] if completed else ["retained_research_and_campaign_activation"],
            "cycle_ids": [a["cycle_id"] for a in completed],
            "outcome_scope": "autonomous_campaign_activated; awaiting_conditions_is_valid; fills_and_profitability_require_separate_receipts"}
