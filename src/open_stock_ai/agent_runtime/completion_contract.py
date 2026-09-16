from __future__ import annotations

import re
from typing import Any, Iterable
from .autonomy_contract import autonomous_pipeline_requested, autonomous_campaign_execution_requested, campaign_completion


_BLOCKED_VALUES = {"data_blocked", "no_data", "unavailable", "not_found"}
_NEGATIVE_TRADE_PHRASES = (
    "不要下單",
    "不下單",
    "不要建立訂單",
    "不建立訂單",
    "不要交易",
    "不交易",
    "只做分析",
    "僅分析",
    "不建立紙上交易",
    "不要建立紙上訂單",
    "不建立紙上訂單",
    "不建立模擬交易",
    "不要建立模擬訂單",
    "不建立模擬訂單",
    "不要紙上或實盤交易",
    "不要紙上或真實交易",
    "不要任何紙上或實盤交易",
    "analysis only",
    "do not trade",
    "do not place",
    "no order",
)
_DECISION_PHRASES = (
    "該買",
    "要買",
    "買進",
    "該賣",
    "賣出",
    "加碼",
    "減碼",
    "進場",
    "出場",
    "下單",
    "持有嗎",
    "續抱",
    "值得投資",
    "投資建議",
    "買還是",
    "賣還是",
    "should i buy",
    "should i sell",
    "buy or sell",
    "trade decision",
    "position size",
)
_ORDER_PHRASES = (
    "下單",
    "送出委託",
    "提交委託",
    "執行委託",
    "place order",
    "submit order",
    "execute order",
)
_PAPER_ORDER_PHRASES = (
    "紙上",
    "模擬交易",
    "模擬下單",
    "paper trade",
    "paper order",
    "simulated trade",
    "simulated order",
)
_BEST_CANDIDATE_PHRASES = (
    "最好",
    "最佳",
    "最適合",
    "找一個",
    "選一個",
    "選出",
    "排名",
    "排行",
    "best candidate",
    "best stock",
    "rank candidates",
    "choose one",
)


def objective_completion_contract(objective: str, task_kind: str | None) -> dict[str, bool]:
    """Derive Host-owned outcome obligations from the user's requested result.

    The contract intentionally describes *what must be proven*, not which
    workflow or stock-specific steps the model has to use.  A provider routing
    hint may narrow tool disclosure, but it cannot erase an explicit request
    for a decision, candidate comparison, or order approval/execution.
    """

    text = _public_objective(objective).casefold()
    trade_negated = any(phrase in text for phrase in _NEGATIVE_TRADE_PHRASES) or bool(
        re.search(
            r"(?:不要|不|勿|禁止)\s*(?:建立|進行|做|執行)?"
            r"(?:[\w\s、，,／/]|artifact|產物){0,32}"
            r"(?:紙上|模擬)\s*(?:交易|下單|訂單)?",
            text,
        )
    )
    market_anchor = any(
        phrase in text
        for phrase in (
            "股票", "台股", "臺股", "股價", "行情", "市場", "stock", "market",
            "symbol", "買", "賣", "trade", "order", "投資",
        )
    )
    decision_requested = (
        (str(task_kind or "") == "market_decision" and market_anchor)
        or (not trade_negated and any(phrase in text for phrase in _DECISION_PHRASES))
    )
    paper_order_requested = not trade_negated and any(phrase in text for phrase in _PAPER_ORDER_PHRASES)
    pipeline_topic = autonomous_pipeline_requested(text)
    pipeline_requested = paper_order_requested and autonomous_campaign_execution_requested(text)
    if pipeline_topic and not pipeline_requested:
        # Merely discussing a paper campaign must not fall back to a generic
        # one-order obligation or restore a campaign execution permission.
        paper_order_requested = False
    order_requested = paper_order_requested or (
        not trade_negated and any(phrase in text for phrase in _ORDER_PHRASES)
    )
    best_candidate_requested = decision_requested and any(
        phrase in text for phrase in _BEST_CANDIDATE_PHRASES
    )
    return {
        "decision_requested": decision_requested and not pipeline_topic,
        "best_candidate_requested": best_candidate_requested and not pipeline_topic,
        "order_requested": order_requested and not pipeline_topic,
        "paper_order_requested": paper_order_requested and not pipeline_requested,
        **({"autonomous_pipeline_requested": True} if pipeline_requested else {}),
    }


def observation_is_substantive(observation: dict[str, Any]) -> bool:
    """Reject formally valid receipts that contain no usable objective evidence."""

    if observation.get("ok") is not True:
        return False
    if "result" not in observation:
        return True
    result = observation.get("result")
    if not isinstance(result, dict):
        return result is not None
    schema = str(result.get("schema_version") or "")
    status_code = result.get("status_code")
    if isinstance(status_code, int) and not 200 <= status_code < 400:
        return False
    if schema == "open_stock_ai.web_resource.v1":
        content = str(result.get("content") or result.get("text") or result.get("title") or "").strip()
        return bool(content) and not _looks_like_missing_page(result)
    if schema == "open_stock_ai.web_research.v1":
        sources = [item for item in result.get("sources") or [] if isinstance(item, dict)]
        return int(result.get("source_count") or len(sources)) > 0
    if schema == "open_stock_ai.agent_watchlist.v1":
        return int(result.get("count") or 0) > 0
    if schema == "stock_ai.market_universe_observation.v1":
        return bool(decision_ready_candidates_from_result(result))
    if schema in {"open_stock_ai.agent_workspace.v1", "open_stock_ai.agent_research_pack.v1"}:
        return bool(decision_ready_candidates_from_result(result)) or not _contains_blocked_workspace(result)
    for key in ("recommendation_bucket", "data_status", "analysis_status"):
        if str(result.get(key) or "").strip().casefold() in _BLOCKED_VALUES:
            return False
    return True


def evaluate_objective_completion(
    *,
    objective: str,
    task_kind: str | None,
    observations: Iterable[dict[str, Any]],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate whether validated Host receipts satisfy the requested outcome."""

    contract = objective_completion_contract(objective, task_kind)
    applies = any(contract.values())
    rows = [item for item in observations if isinstance(item, dict) and item.get("ok") is True]
    candidate_symbols: list[str] = []
    risk_checked_symbols: set[str] = set()
    order_receipts: list[str] = []
    paper_preview_receipts: list[str] = []
    for item in rows:
        tool = str(item.get("tool") or item.get("name") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        for symbol, risk_checked in decision_ready_candidates_from_result(result):
            if symbol not in candidate_symbols:
                candidate_symbols.append(symbol)
            if risk_checked:
                risk_checked_symbols.add(symbol)
        if tool == "paper.submit_order" or tool.startswith("broker.order."):
            order_receipts.append(str(item.get("call_id") or item.get("id") or tool))
        if tool == "paper.preview_order" and _verified_paper_preview(result):
            paper_preview_receipts.append(str(item.get("call_id") or item.get("id") or tool))

    decision_payload = decision if isinstance(decision, dict) else {}
    decision_symbol = _concrete_symbol(decision_payload.get("symbol"))
    decision_action = str(decision_payload.get("action") or "").casefold()
    valid_decision = bool(
        decision_symbol
        and decision_action in {"buy", "sell", "hold", "watch", "cancel_order", "none"}
        and decision_symbol in candidate_symbols
        and decision_symbol in risk_checked_symbols
    )
    missing: list[str] = []
    campaign = campaign_completion(rows) if contract.get("autonomous_pipeline_requested") else None
    if campaign is not None:
        missing.extend(campaign["missing_requirements"])
    # A local paper-training experiment is allowed to use a verified broker
    # quote even when production research/PIT gates are incomplete.  Keep the
    # stricter candidate/risk proof for an actual market decision request.
    if contract["decision_requested"] and not contract["paper_order_requested"] and not candidate_symbols:
        missing.append("decision_ready_market_candidate")
    if contract["decision_requested"] and not contract["paper_order_requested"] and not valid_decision:
        missing.append("host_grounded_market_decision")
    if contract["best_candidate_requested"] and not contract["paper_order_requested"] and len(candidate_symbols) < 2:
        missing.append("comparable_candidate_set")
    if contract["best_candidate_requested"] and not contract["paper_order_requested"] and not decision_symbol:
        missing.append("selected_best_candidate")
    if contract["paper_order_requested"] and not paper_preview_receipts:
        missing.append("verified_paper_order_preview")
    if contract["order_requested"] and not order_receipts:
        missing.append("approved_order_execution_receipt")
    return {
        "name": "objective_completion_contract",
        "passed": not applies or not missing,
        "applies": applies,
        "requirements": contract,
        "candidate_symbols": candidate_symbols,
        "risk_checked_symbols": sorted(risk_checked_symbols),
        "decision_symbol": decision_symbol,
        "decision_action": decision_action or None,
        "order_receipts": order_receipts,
        "paper_preview_receipts": paper_preview_receipts,
        "missing_requirements": list(dict.fromkeys(missing)),
        "campaign": campaign,
    }


def decision_ready_candidates_from_result(result: dict[str, Any]) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = []
    schema = str(result.get("schema_version") or "")
    if schema == "stock_ai.market_universe_observation.v1":
        for item in result.get("items") or []:
            if not isinstance(item, dict) or item.get("ok") is not True:
                continue
            analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
            candidate = _decision_ready_workspace(analysis)
            if candidate:
                candidates.append(candidate)
        return candidates
    if schema == "open_stock_ai.agent_research_pack.v1":
        workspace = result.get("pipeline_workspace")
        if isinstance(workspace, dict):
            candidate = _decision_ready_workspace({**workspace, "symbol": result.get("symbol") or workspace.get("symbol")})
            if candidate:
                candidates.append(candidate)
        return candidates
    candidate = _decision_ready_workspace(result)
    if candidate:
        candidates.append(candidate)
    return candidates


def _decision_ready_workspace(workspace: dict[str, Any]) -> tuple[str, bool] | None:
    symbol = _concrete_symbol(workspace.get("symbol"))
    if not symbol or _contains_blocked_workspace(workspace):
        return None
    data_status = workspace.get("data_status") if isinstance(workspace.get("data_status"), dict) else {}
    if data_status and data_status.get("decision_ready") is not True:
        return None
    risk = workspace.get("risk_summary") if isinstance(workspace.get("risk_summary"), dict) else {}
    return symbol, "approved" in risk


def _contains_blocked_workspace(workspace: dict[str, Any]) -> bool:
    if str(workspace.get("recommendation_bucket") or "").casefold() in _BLOCKED_VALUES:
        return True
    data_status = workspace.get("data_status")
    if isinstance(data_status, str) and data_status.casefold() in _BLOCKED_VALUES:
        return True
    if isinstance(data_status, dict) and data_status.get("decision_ready") is False:
        # A signed official close may support a clearly labelled advisory
        # analysis.  It remains excluded from every decision-ready candidate,
        # so this exception cannot unlock paper or live execution.
        return not (
            data_status.get("analysis_ready") is True
            and workspace.get("analysis_only") is True
            and workspace.get("execution_permission") == "blocked"
        )
    analysis_status = str(workspace.get("analysis_status") or "").casefold()
    return analysis_status in _BLOCKED_VALUES


def _verified_paper_preview(result: dict[str, Any]) -> bool:
    market = result.get("market") if isinstance(result.get("market"), dict) else {}
    price = market.get("price")
    envelope = market.get("source_envelope") if isinstance(market.get("source_envelope"), dict) else {}
    return bool(
        result.get("can_submit") is True
        and isinstance(price, (int, float))
        and price > 0
        and str(envelope.get("signature") or "").strip()
    )


def _concrete_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or re.fullmatch(r"(?:SYMBOL\d*|TARGET|UNKNOWN|N/?A|NONE)", symbol):
        return ""
    return symbol


def _looks_like_missing_page(result: dict[str, Any]) -> bool:
    text = " ".join(
        str(result.get(key) or "") for key in ("title", "content", "final_url", "requested_url")
    ).casefold()
    return any(marker in text for marker in ("page-not-found", "404 not found", "page not found"))


def _public_objective(value: str) -> str:
    text = re.sub(r"^\s*(?:\[MODEL_TASK_KIND:[^\]]+\]\s*)+", "", str(value or ""), flags=re.I)
    if text.startswith("[MARKET_SCOPE]") and "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    return text.strip()
