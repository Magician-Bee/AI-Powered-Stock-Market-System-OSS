from __future__ import annotations

import json
from typing import Any

from .completion_contract import (
    evaluate_objective_completion,
    objective_completion_contract,
    observation_is_substantive,
)
from .contracts import AgentRunContext
from .plan_graph import PlanGraph
from .repair.recovery_policy import _unresolved_failure_nodes
from .validators import _has_completed_critic_receipt


def _complete_finalize_nodes(plan: PlanGraph) -> None:
    completed = {
        node.node_id for node in plan.nodes.values() if node.status in {"completed", "skipped"}
    }
    for node in plan.nodes.values():
        if (
            node.node_type == "finalize"
            and node.status in {"pending", "ready"}
            and all(dependency in completed for dependency in node.dependencies)
        ):
            node.status = "completed"


def _host_completion_evaluation(
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a completion receipt from validated host observations only."""

    evidence_ids = [
        str(item.get("call_id"))
        for item in trace
        if item.get("ok") is True and str(item.get("call_id") or "").strip()
    ]
    unresolved = _unresolved_failure_nodes(plan, trace)
    return {
        "criteria_met": not unresolved,
        "criterion_results": [
            {
                "criterion": criterion,
                "met": not unresolved,
                "evidence_ids": evidence_ids,
            }
            for criterion in plan.completion_criteria
        ],
        "evidence_ids": evidence_ids,
        "remaining_gaps": [f"unresolved_recovery:{node_id}" for node_id in unresolved],
    }


def _objective_contract_check(
    *,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
    decision: dict[str, Any] | None,
    completion_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the Host-owned outcome gap, even when a provider said complete.

    Completion validation is persisted in older runs and can therefore be
    absent or stale. Recompute this small contract from the current Host trace
    so the durable runtime can resume a false-completed objective safely.
    """

    check = evaluate_objective_completion(
        objective=objective,
        task_kind=task_kind,
        observations=(item for item in trace if isinstance(item, dict)),
        decision=decision,
    )
    persisted = next(
        (
            item
            for item in (completion_validation or {}).get("checks") or []
            if isinstance(item, dict) and item.get("name") == "objective_completion_contract"
        ),
        None,
    )
    if isinstance(persisted, dict):
        check = {
            **check,
            "persisted_passed": persisted.get("passed"),
            "persisted_missing_requirements": list(
                persisted.get("missing_requirements") or []
            ),
        }
    return check


def _should_host_finalize_from_validated_evidence(
    *,
    turn: dict[str, Any],
    plan: PlanGraph,
    trace: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    evidence_requirement_met: bool,
) -> bool:
    if (
        turn.get("state") != "continue"
        or turn.get("tool_calls")
        or not str(turn.get("summary") or "").strip()
        or not evidence_requirement_met
    ):
        return False
    # Alternative evidence cannot close a failed branch until the Host has
    # linked that evidence to the original failure.
    if _unresolved_failure_nodes(plan, trace):
        return False
    if any(
        node.status not in {"completed", "skipped"}
        and node.node_type not in {"reasoning", "finalize"}
        for node in plan.nodes.values()
    ):
        return False
    # Require two consecutive empty provider turns after validated evidence,
    # rather than turning the first formatting/planning pause into completion.
    empty_continue_turns = sum(
        1
        for item in transcript
        if item.get("role") == "agent"
        and item.get("type") == "decision"
        and (item.get("content") or {}).get("state") == "continue"
        and not (item.get("content") or {}).get("tool_calls")
    )
    return empty_continue_turns >= 2


def _should_host_finalize_verified_paper_order(
    *,
    turn: dict[str, Any],
    trace: list[dict[str, Any]],
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
) -> bool:
    """Finish an explicit local paper order from Host receipts, not model prose."""

    if (
        turn.get("state") not in {"continue", "complete"}
        or turn.get("tool_calls")
        or context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return False
    completion = evaluate_objective_completion(
        objective=objective,
        task_kind=task_kind,
        observations=trace,
        decision=None,
    )
    return completion["passed"] is True and completion["requirements"]["paper_order_requested"] is True


def _should_host_finalize_analysis_only_after_numeric_rejection(
    *,
    task_kind: str | None,
    turn: dict[str, Any],
    plan: PlanGraph,
    trace: list[dict[str, Any]],
    completion_validation: dict[str, Any],
) -> bool:
    """Bound a safe analysis-only answer when only model numerics were rejected.

    This is deliberately narrower than general completion repair.  It applies
    only after a provider has requested completion, every required Host check
    except numeric grounding already passed, and no mutation or unresolved
    recovery exists.  It never applies to trading, automation or project work.
    """

    if (
        task_kind != "market_information"
        or turn.get("state") != "complete"
        or turn.get("tool_calls")
        or not _evidence_requirement_met(task_kind, trace)
        or _unresolved_failure_nodes(plan, trace)
    ):
        return False
    failed_checks = {
        str(item.get("name") or "")
        for item in completion_validation.get("checks") or []
        if isinstance(item, dict) and item.get("passed") is False
    }
    return bool(failed_checks) and failed_checks <= {
        "final_answer_numeric_claims_are_evidence_grounded",
        "final_answer_period_measurements_are_evidence_bound",
    }


def _should_host_finalize_analysis_only_data_unavailable(
    *,
    task_kind: str | None,
    objective: str,
    context: AgentRunContext,
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> bool:
    """Stop a no-trade analysis once Host evidence proves data is unavailable.

    A data-blocked receipt remains unsuitable for a market decision or a
    research certification.  A normal information request is analysis-only by
    default: users say what they want analysed, rather than having to add
    procedural language such as "only analyse" or "do not trade".  This
    narrow terminal path therefore reports the verified limitation whenever
    the request does not itself ask for a decision or an order.
    """

    if (
        task_kind != "market_information"
        or context.allow_paper_orders is True
        or _unresolved_failure_nodes(plan, trace)
    ):
        return False
    contract = objective_completion_contract(objective, task_kind)
    if any(
        contract[name]
        for name in ("decision_requested", "order_requested", "paper_order_requested")
    ):
        return False
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "market.analyze_symbol":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        data_status = (
            result.get("data_status")
            if isinstance(result.get("data_status"), dict)
            else {}
        )
        if (
            str(result.get("recommendation_bucket") or "").casefold() == "data_blocked"
            or data_status.get("decision_ready") is False
        ):
            return True
    return False


def _should_host_finalize_ui_task(
    *,
    task_kind: str | None,
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> bool:
    """Finish a bounded UI request from validated UI bridge evidence.

    This is deliberately limited to ``ui_task`` runs. It never authorizes an
    arbitrary mutation or covers an unresolved failure; every executable Plan
    node must already be completed before the Host produces the terminal
    acknowledgement.
    """

    if task_kind != "ui_task" or _unresolved_failure_nodes(plan, trace):
        return False
    if any(isinstance(item, dict) and item.get("ok") is False for item in trace):
        return False
    completed = {"completed", "skipped"}
    if any(
        node.status not in completed
        and node.node_type not in {"reasoning", "finalize"}
        for node in plan.nodes.values()
    ):
        return False
    return any(
        isinstance(item, dict)
        and item.get("ok") is True
        and str(item.get("tool") or "").startswith("ui.")
        and _observation_is_substantive(item)
        for item in trace
    )


def _verified_ui_task_summary(trace: list[dict[str, Any]]) -> str:
    """Render a concise final acknowledgement from Host-validated UI tools."""

    tools = list(
        dict.fromkeys(
            str(item.get("tool") or "")
            for item in trace
            if isinstance(item, dict)
            and item.get("ok") is True
            and str(item.get("tool") or "").startswith("ui.")
        )
    )
    rendered = "、".join(tools) or "ui 操作"
    return (
        f"Host 已完成並驗證介面操作：{rendered}。"
        "此 Run 僅執行本機介面操作；未建立自動化、未建立或送出紙上訂單、"
        "未發送外部通知，也未執行真實交易。"
    )


def _objective_explicitly_analysis_only(objective: str) -> bool:
    lowered = str(objective or "").casefold()
    return any(
        phrase in lowered
        for phrase in (
            "只做市場分析",
            "只做分析",
            "僅分析",
            "不要交易",
            "不交易",
            "不要下單",
            "不下單",
            "analysis only",
            "do not trade",
            "do not place",
        )
    )


def _verified_data_unavailable_analysis_summary(trace: list[dict[str, Any]]) -> str:
    """Render a bounded no-trade result from a Host-validated data receipt."""

    symbol = "該標的"
    source = "未取得可安全用於判讀的市場來源時間"
    timestamp = "來源沒有提供可用時間"
    blockers: list[str] = []
    critic_requested = any(
        isinstance(item, dict)
        and str(item.get("tool") or "") == "agent.run_subtasks"
        and str(
            (
                (item.get("arguments") or {}).get("role")
                if isinstance(item.get("arguments"), dict)
                else ((item.get("result") or {}).get("role") if isinstance(item.get("result"), dict) else "")
            )
            or ""
        ).casefold()
        == "critic"
        for item in trace
    )
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "market.analyze_symbol":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        data_status = (
            result.get("data_status")
            if isinstance(result.get("data_status"), dict)
            else {}
        )
        if (
            str(result.get("recommendation_bucket") or "").casefold() != "data_blocked"
            and data_status.get("decision_ready") is not False
        ):
            continue
        symbol = str(result.get("symbol") or symbol).strip().upper() or symbol
        source = str(data_status.get("price_source") or source).strip() or source
        timestamp = (
            str(data_status.get("exchange_timestamp") or timestamp).strip() or timestamp
        )
        candidates = [
            *list(data_status.get("blockers") or []),
            *list((result.get("research_status") or {}).get("blockers") or []),
        ]
        blockers = [str(value).strip() for value in candidates if str(value).strip()]
        break
    blocker_text = (
        "、".join(dict.fromkeys(blockers[:6])) or "沒有可用於決策的完整市場資料"
    )
    critic_summary = ""
    if critic_requested:
        critic_summary = (
            "獨立 Critic：已完成並 Join 反方子分支；其結果只用於揭露資料限制與反方風險，"
            "不會把資料不足改寫為投資結論。"
            if _completed_critic_observation(trace)
            else "獨立 Critic：子分支沒有取得完成 receipt，因此本次不宣稱已完成反方驗證。"
        )
    return (
        f"已完成 {symbol} 的資料可用性檢查。本次沒有取得可安全用於價格趨勢或技術判讀的決策級市場資料，"
        "因此不以推測補齊趨勢、指標或投資建議。"
        f"資料來源：{source}。資料時間：{timestamp}。"
        f"已驗證的主要限制：{blocker_text}。"
        f"{critic_summary}"
        "結論：本次只回報資料不足，未建立自動化、未建立紙上訂單，也未向任何實盤券商送單。"
    )


def _verified_analysis_only_summary(trace: list[dict[str, Any]]) -> str:
    """Render a complete, non-numeric analysis summary from Host receipts.

    Numbers are intentionally omitted.  The numeric validator has already
    proved that the provider's display values cannot be bound exactly enough to
    publish.  A generic short fallback is not sufficient for a multi-part
    request: it fails the same scope validator that caused the fallback and
    sends a safe, completed Run back through another expensive model turn.
    Preserve every completed Host-owned dimension instead, without promoting
    ungrounded provider claims or numbers.
    """

    symbol = "該標的"
    has_research_pack = False
    has_analysis = False
    has_monthly_revenue = False
    has_institutional_flow = False
    has_portfolio_status = False
    has_external_research = False
    has_independent_critic = False
    risk_blocked = False
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        candidate = str(result.get("symbol") or "").strip().upper()
        if candidate:
            symbol = candidate
        tool = str(item.get("tool") or "")
        has_research_pack = has_research_pack or tool == "market.research_pack"
        has_analysis = has_analysis or tool == "market.analyze_symbol"
        has_monthly_revenue = has_monthly_revenue or tool == "market.monthly_revenue"
        has_institutional_flow = has_institutional_flow or tool == "market.institutional_flow"
        has_independent_critic = has_independent_critic or (
            tool == "agent.run_subtasks" and _has_completed_critic_receipt(result)
        )
        has_portfolio_status = has_portfolio_status or "portfolio_status" in result
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True).casefold()
        has_external_research = has_external_research or any(
            marker in result_text
            for marker in ("fingpt", "finrobot", "tradingagents")
        )
        risk = result.get("risk_summary") if isinstance(result.get("risk_summary"), dict) else {}
        risk_blocked = risk_blocked or risk.get("approved") is False

    sections = [
        f"已完成 {symbol} 的受限市場分析；以下只保留本次通過 Host 驗證的工具收據，不重述模型中無法逐項核對的數字或價格。",
        (
            "持股／投資組合：已讀取本次分析脈絡中的部位狀態；本次是假設性分析，沒有讀取真實帳戶，也不把未提供的持倉細節視為事實。"
            if has_portfolio_status
            else "持股／投資組合：本次沒有讀取真實帳戶；分析只以使用者指定的假設持有情境評估，不假設持倉數量或成本。"
        ),
        (
            "技術面：Host 已完成行情、趨勢與技術特徵檢查，並保留資料時間、來源與可追溯收據；不將該觀察擴張為保證報酬或買賣價格。"
            if has_analysis or has_research_pack
            else "技術面：本次沒有足夠的 Host 技術觀察可安全延伸，因此不以推測補齊。"
        ),
        (
            "基本面／月營收：已取得月營收資料收據，供基本面脈絡比對；本摘要不重述未經逐項綁定的模型數字。"
            if has_monthly_revenue
            else "基本面／月營收：本次沒有取得可驗證的月營收收據，這是保留的資料限制。"
        ),
        (
            "法人：已取得法人流向資料收據，可與技術與基本面觀察交叉檢視；不將單一資料點解讀成確定方向。"
            if has_institutional_flow
            else "法人：本次沒有取得可驗證的法人流向收據，不能以推測補齊。"
        ),
        (
            "外部研究：已納入可追溯的外部研究框架或研究證據，僅作交叉驗證，不把外部內容直接當成交易指令。"
            if has_external_research
            else "外部研究：本次沒有取得可安全發布的外部研究收據，這是結論的限制。"
        ),
        (
            "獨立 Critic：已建立並完成獨立反方子分支；其證據已加入本次判讀，用來挑戰而非覆寫主分支的已驗證結果。"
            if has_independent_critic
            else "獨立 Critic：本次沒有完成獨立反方子分支，因此不假稱已經過反方驗證。"
        ),
        (
            "風險：研究與風險閘門未核准任何執行，資料與研究限制已保留；因此不提出交易指令。"
            if risk_blocked
            else "風險：本次只保留已驗證的風險觀察，不延伸為任何交易授權。"
        ),
        "結論：本次僅完成分析；未建立自動化、未建立紙上訂單，也未向任何實盤券商送單。",
    ]
    return "\n".join(sections)


def _should_defer_reflection_until_paper_order_receipt(
    *,
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
) -> bool:
    """Keep an explicit local paper order on the Host execution path.

    A paper-order request already carries the user's bounded authorization.
    Before a verified ``paper.submit_order`` receipt exists, a provider's
    generic reflection is not a meaningful external acceptance condition; it
    is a premature detour that would otherwise leave the Run waiting forever.
    """

    if (
        context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return False
    contract = objective_completion_contract(objective, task_kind)
    if contract["paper_order_requested"] is not True:
        return False
    return not any(
        isinstance(item, dict)
        and item.get("ok") is True
        and str(item.get("tool") or "") == "paper.submit_order"
        for item in trace
    )


def _verified_paper_order_summary(trace: list[dict[str, Any]]) -> str:
    """Return a user-safe summary whose claims are limited to Host receipts."""

    analysis_summary = _paper_order_analysis_summary(trace)

    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "paper.submit_order":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        order = (result.get("broker") or {}).get("order") or result.get("order") or {}
        symbol = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or order.get("action") or "").strip().casefold()
        action = {"buy": "買進", "sell": "賣出"}.get(side, "交易")
        subject = f" {symbol}" if symbol else ""
        summary = (
            f"已完成{subject} 的紙上模擬{action}；Host 已驗證預覽、訂單持久化與執行狀態，"
            "未送往實盤券商。"
        )
        return f"{summary}{analysis_summary}"
    return f"已完成 Host 驗證的紙上模擬交易；未送往實盤券商。{analysis_summary}"


def _paper_order_analysis_summary(trace: list[dict[str, Any]]) -> str:
    """Preserve the completed analysis scope in a paper-run terminal receipt.

    A paper sandbox request commonly combines an analysis request with a local
    order.  Returning only the fill receipt makes the completed fundamental,
    technical, and risk work appear to have vanished, which in turn invites a
    user to repeat the same request.  The provider's prose is intentionally
    not reused here: this short summary only exposes dimensions that a Host
    tool actually produced and never turns a paper fill into a recommendation.
    """

    analysis: dict[str, Any] | None = None
    has_monthly_revenue = False
    for item in trace:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        tool = str(item.get("tool") or "")
        if tool == "market.monthly_revenue":
            has_monthly_revenue = True
        if tool != "market.analyze_symbol":
            continue
        result = item.get("result")
        if isinstance(result, dict):
            analysis = result

    if analysis is None:
        return ""

    symbol = str(analysis.get("symbol") or "該標的").strip().upper() or "該標的"
    data_status = analysis.get("data_status")
    data_status = data_status if isinstance(data_status, dict) else {}
    risk_summary = analysis.get("risk_summary")
    risk_summary = risk_summary if isinstance(risk_summary, dict) else {}
    data_blocked = (
        str(analysis.get("recommendation_bucket") or "").casefold() == "data_blocked"
        or data_status.get("decision_ready") is False
    )
    technical = (
        "技術面：Host 已完成行情與技術訊號檢查；結果僅供分析，不構成買賣建議。"
        if data_status.get("analysis_ready") is True
        else "技術面：本次沒有取得可安全發布的 Host 技術判讀證據。"
    )
    fundamental = (
        "基本面：已取得官方月營收證據，未以模型未驗證數字補寫。"
        if has_monthly_revenue
        else "基本面：本次沒有取得可驗證的基本面證據。"
    )
    risk = (
        "風險：Host 風險閘門未核准研究／策略交易訊號；紙上單不代表投資決策。"
        if risk_summary.get("approved") is False
        else "風險：僅保留 Host 已驗證的風險檢查，紙上單不代表投資決策。"
    )
    limitation = (
        "市場分析資料未達決策門檻；本筆僅為依使用者指示執行的紙上沙盒交易。"
        if data_blocked
        else "本筆是本機紙上沙盒交易，不會改變研究或實盤執行門檻。"
    )
    return f" 分析摘要（{symbol}）：{fundamental}{technical}{risk}{limitation}"


def _observation_is_substantive(observation: dict[str, Any]) -> bool:
    """Return whether a successful receipt contains usable evidence.

    Analysis-only tools may safely return a validated ``data_blocked``
    workspace.  That is a valid Host receipt, but it is not evidence that the
    user's factual objective has been answered.
    """

    return observation_is_substantive(observation)


def _evidence_requirement_met(task_kind: str, trace: list[dict[str, Any]]) -> bool:
    if task_kind == "general_answer":
        return True
    successful = {
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if _observation_is_substantive(item)
    }
    if task_kind in {"market_decision", "market_radar"}:
        return (
            any(name.startswith("market.") and name != "market.taifex_foreign_open_interest" for name in successful)
            or bool(successful & {"autonomy.research", "autonomy.evidence", "autonomy.status"})
        )
    if task_kind == "market_information":
        return any(name.startswith("market.") for name in successful) or bool(
            successful & {"web.fetch", "web.research"}
        )
    if task_kind == "current_information":
        return bool(successful & {"web.fetch", "web.research"}) or any(
            name.startswith("market.") for name in successful
        )
    if task_kind == "ui_task":
        return any(name.startswith("ui.") for name in successful)
    if task_kind == "artifact_task":
        return any(name.startswith("artifact.") for name in successful)
    if task_kind == "project_task":
        # Tool identity and execution receipts, rather than a hard-coded prefix
        # taxonomy, establish that a project action actually ran.  This keeps
        # custom and external framework tools usable without granting them any
        # additional execution permission.
        return bool(successful)
    return bool(successful)


def _critic_disclosed_tools(
    manifest: list[dict[str, Any]],
    *,
    successful_observations: int,
) -> list[dict[str, Any]]:
    """Apply the Host-owned local capability and evidence budget for a Critic."""

    scoped = [item for item in manifest if item.get("name") != "agent.run_subtasks"]
    if successful_observations >= 3:
        return []
    return scoped


def _completed_critic_observation(trace: list[dict[str, Any]]) -> bool:
    return any(
        item.get("ok") is True
        and item.get("tool") == "agent.run_subtasks"
        and isinstance(item.get("result"), dict)
        and _has_completed_critic_receipt(item["result"])
        for item in trace
    )


def _partition_redundant_critic_calls(
    calls: list[dict[str, Any]],
    *,
    trace: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _completed_critic_observation(trace):
        return calls, []
    redundant = [item for item in calls if item.get("name") == "agent.run_subtasks"]
    if not redundant:
        return calls, []
    return (
        [item for item in calls if item.get("name") != "agent.run_subtasks"],
        redundant,
    )
