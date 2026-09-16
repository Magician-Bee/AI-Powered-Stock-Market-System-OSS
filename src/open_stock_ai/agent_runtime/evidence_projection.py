from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .completion_policy import _evidence_requirement_met, _observation_is_substantive
from .providers.transcript import result_summary as _result_summary
from .repair.recovery_policy import _alternative_tools_for_failure


def _evidence_feedback(
    task_kind: str,
    trace: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    *,
    objective: str = "",
    completion_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    successful = {
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if _observation_is_substantive(item)
    }
    failed = [str(item.get("tool") or "") for item in trace if item.get("ok") is False]
    unresolved_failed_nodes = [
        str(item.get("node_id") or item.get("call_id") or item.get("tool") or "")
        for item in trace
        if item.get("ok") is False
        and not any(
            str(link.get("failed_node_id") or "")
            == str(item.get("node_id") or "")
            for successful_item in trace
            if successful_item.get("ok") is True
            for link in (successful_item.get("recovery_for") or [])
            if isinstance(link, dict)
        )
    ]
    unsupported_numeric_claims = sorted(
        {
            str(value)
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_numeric_claims_are_evidence_grounded"
            and check.get("passed") is False
            for value in check.get("unsupported_claims") or []
            if str(value).strip()
        },
        key=lambda value: (len(value), value),
    )
    placeholder_measurements = sorted(
        {
            str(value)
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_has_no_placeholder_measurements"
            and check.get("passed") is False
            for value in check.get("placeholders") or []
            if str(value).strip()
        },
        key=lambda value: (len(value), value.casefold()),
    )
    scope_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_covers_requested_scope"
            and check.get("passed") is False
        ),
        None,
    )
    market_coverage_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "requested_market_evidence_coverage"
            and check.get("passed") is False
        ),
        None,
    )
    objective_contract_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "objective_completion_contract"
            and check.get("passed") is False
        ),
        None,
    )
    verified_summary = _reasoning_step_summary(
        node_title="本次任務",
        related_trace=trace,
        fallback="Host 已保留本次通過驗證的工具證據。",
    )
    comprehensive_market_request = (
        task_kind in {"market_information", "market_decision", "market_radar"}
        and (
            len(str(objective or "")) >= 48
            or sum(str(objective or "").count(token) for token in ("、", "，", ",", ";", "；")) >= 3
        )
    )
    grounded_rewrite_rule = (
        "請逐項引用下方 Host 事實；數值必須原樣複製，不得四捨五入。"
        "使用者要求的面向若未出現在 Host 事實中（例如法人流向），請明確寫『本次已驗證證據未取得』，"
        "不得用常識、歷史印象或假設補齊。不要猜測 blocker 內容，也不要新增交易價位或操作指令。"
        if comprehensive_market_request
        else ""
    )
    if unresolved_failed_nodes:
        alternatives = {
            failed_tool: _alternative_tools_for_failure(failed_tool, manifest)
            for failed_tool in sorted(set(failed))
            if failed_tool
        }
        message = (
            "先前有可恢復的工具失敗，目標尚未完成。不可回傳 state=complete，也不可只重述失敗。"
            f"未完成的失敗分支：{'、'.join(unresolved_failed_nodes)}。"
            f"請立刻使用不同的替代能力／來源取得可驗證證據：{json.dumps(alternatives, ensure_ascii=False)}；"
            "請依 error action_hints 選擇 choose_alternate_tool 或 alternative_source；"
            "成功的替代呼叫會由 Host 綁定到失敗分支，其他已完成分支會保留。"
        )
    elif objective_contract_check is not None:
        missing = "、".join(
            str(value)
            for value in objective_contract_check.get("missing_requirements") or []
            if str(value).strip()
        )
        message = (
            "目前的工具收據雖然格式有效，但還沒有滿足使用者要求的結果契約，不能停止。"
            f"尚缺：{missing or '目標結果的必要驗證'}。"
            "請先分析缺口並採取下一個可驗證行動；若某個來源失敗，請診斷原因、改用獨立替代來源，"
            "修正後重新執行。取得足夠候選、風險檢查、決策或核准收據前，不得回傳 state=complete。"
        )
    elif unsupported_numeric_claims:
        rendered = "、".join(unsupported_numeric_claims[:12])
        message = (
            "剛才的完成草稿含有未被 Host 證據支持的數值："
            f"{rendered}。請移除這些數值及其衍生結論，或只使用成功工具結果中可核對的數值；"
            "不要重新呼叫已完成的工具。修正後回傳 state=complete。"
            f"{grounded_rewrite_rule}可直接引用的 Host 事實：{verified_summary}"
        )
    elif placeholder_measurements:
        rendered = "、".join(placeholder_measurements[:8])
        message = (
            "剛才的完成草稿以佔位符冒充尚未取得的市場／財務數值："
            f"{rendered}。請移除佔位符與其衍生結論，明確說明資料不足，"
            "或只使用成功工具結果中可核對的值；不要重新呼叫已完成的工具。"
        )
    elif market_coverage_check is not None:
        missing = "、".join(
            str(value)
            for value in market_coverage_check.get("missing_dimensions") or []
        )
        required = "、".join(
            str(value)
            for value in market_coverage_check.get("required_tools") or []
        )
        message = (
            "使用者明確要求的市場證據面向尚未完整，不能只靠既有綜合分析直接完成。"
            f"目前缺少：{missing or '未辨識的必要面向'}。"
            f"請呼叫這些 Host 工具取得獨立證據：{required or '對應的市場證據工具'}；"
            "已完成且通過驗證的工具不要重複呼叫。取得證據後再逐項回答並回傳 state=complete。"
        )
    elif scope_check is not None:
        message = (
            "剛才的完成草稿只有標題或導言，尚未實際回答使用者列出的完整範圍。"
            f"目前回答有 {scope_check.get('answer_characters', 0)} 字、"
            f"{scope_check.get('content_units', 0)} 個內容單元；請根據使用者原始問題逐項形成具體結論、"
            "明確標示無法取得的資料與反方風險，不要用一句『以下是完整分析』代替內容，"
            "也不要重新呼叫已完成的工具。以下是 Host 從已驗證結果整理的可引用事實："
            f"{verified_summary}{grounded_rewrite_rule}"
        )
    elif task_kind != "general_answer" and _evidence_requirement_met(task_kind, trace):
        message = (
            "已有主系統驗證通過的工具證據；若使用者目標已完成，請停止重複呼叫工具，"
            "回傳 state=complete 並以成功的 evidence_ids 完成回答。"
        )
    elif task_kind == "general_answer":
        message = "No tool is required; return state=complete with a direct answer in summary."
    elif "web.search" in successful and not successful.intersection({"web.fetch", "web.research"}):
        message = (
            "搜尋結果只用來發現來源，尚不足以完成答案；請用 web.fetch 打開其中一個直接網址，"
            "或改用 web.research 自動搜尋並讀取多個來源。"
        )
    elif task_kind == "market_decision":
        message = "股票決策尚缺主系統行情證據；請先使用 market.research_pack 或 market.analyze_symbol。"
    elif task_kind == "project_task":
        message = "專案問題尚未讀取實際檔案；請使用 project.search_text、project.list_files 或 project.read_file。"
    elif task_kind == "artifact_task":
        message = "Artifact 尚未取得 Host 建立或讀取收據；請使用 artifact.create_text 或 artifact.list。"
    elif task_kind == "ui_task":
        message = "介面任務尚未有已確認的 UI Bridge 操作結果；請使用 ui.* 工具操作或讀取介面狀態。"
    else:
        message = (
            "即時或來源型問題尚缺已讀取的可靠來源。若使用者只提供公司名稱而沒有可執行代號，"
            "先使用 market.search_taiwan_securities 解析正式代號，再呼叫對應 market.* 工具；"
            "否則使用 web.research。若工具回傳 data_blocked、沒有資料或來源失敗，該結果不算完成證據，"
            "請改寫查詢並嘗試其他來源。"
        )
    return {
        "error": message,
        "failed_tools": failed,
        "verified_evidence_summary": verified_summary,
        "grounded_rewrite_rule": grounded_rewrite_rule,
        "available_tools": [item["name"] for item in manifest],
    }


def _evidence_claim_text(observation: dict[str, Any]) -> str:
    summary = _result_summary(observation)
    if not observation.get("ok"):
        message = str(summary.get("message") or summary.get("error") or "工具執行失敗")
        return " ".join(message.split())
    symbol = str(summary.get("symbol") or "").strip()
    if summary.get("schema_version") == "open_stock_ai.web_research.v1":
        query = " ".join(str(summary.get("query") or "").split())
        source_count = int(summary.get("source_count") or 0)
        source_label = f"{source_count} 個來源" if source_count else "外部來源"
        return f"外部研究已取得 {source_label}{f'：{query}' if query else '。'}"
    bucket = str(summary.get("recommendation_bucket") or "").strip()
    if bucket.casefold() == "data_blocked":
        return f"{symbol + '：' if symbol else ''}資料不足，尚無法形成可靠判斷。"
    for key in ("interpretation", "summary", "message", "status"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return f"{symbol + '：' if symbol else ''}{' '.join(value.split())}"
    compact = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    return compact[:240] if compact else "Host 已驗證工具結果。"


def _canonical_tool_evidence(
    *,
    observation: dict[str, Any],
    call: dict[str, Any],
    metadata: dict[str, Any],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build semantic Evidence records without confusing model, worker and data providers."""

    result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
    tool = str(call.get("name") or "unknown_tool")
    call_id = str(call.get("id") or f"EV-{uuid4().hex}")
    tool_provider = str(metadata.get("provider") or "host")
    observed_at = _now()
    result_provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}

    candidates: list[dict[str, Any]] = []
    if result.get("schema_version") == "open_stock_ai.web_research.v1":
        candidates = [dict(item) for item in result.get("sources") or [] if isinstance(item, dict)]
    elif isinstance(result.get("items"), list) and result.get("items"):
        candidates = [dict(item) for item in result["items"] if isinstance(item, dict)]
    if not candidates:
        candidates = [dict(result)]

    records: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for candidate in candidates:
        source_url = _first_text(
            candidate.get("source_url"),
            candidate.get("final_url"),
            candidate.get("url"),
            result_provenance.get("source_url"),
        )
        source = _first_text(
            candidate.get("source"),
            candidate.get("source_id"),
            candidate.get("provider_id"),
            result_provenance.get("source"),
            result_provenance.get("source_id"),
            source_url,
            f"{tool_provider}:{tool}",
        )
        # Prefer the provider's stable source identity for semantic de-duplication.
        # Official endpoints commonly vary request-only query parameters (for
        # example TWSE ``date=latest`` versus a resolved date) while returning
        # the same published record.  Keep the exact URL in provenance, but do
        # not let those transport details create duplicate Evidence nodes.
        source_key = source or source_url
        # A result may contain many rows from the same publication. Preserve
        # materially different observation dates, but do not flood the graph
        # with duplicate rows from one receipt.
        publication = _first_text(
            candidate.get("published_at"),
            candidate.get("available_at"),
            candidate.get("report_date"),
            candidate.get("trade_date"),
            candidate.get("data_timestamp"),
            result_provenance.get("published_at"),
        )
        claim = _canonical_source_claim(
            candidate=candidate,
            fallback=_evidence_claim_text(observation),
        )
        dedupe_key = f"{source_key}|{publication}|{claim}"
        if dedupe_key in seen_sources:
            continue
        seen_sources.add(dedupe_key)
        identity = json.dumps(
            {
                "run_id": str(run_id or "unscoped"),
                "tool": tool,
                "source": source_key,
                "published_at": publication,
                "claim": claim,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = f"EV-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"
        lowered_source = f"{source} {source_url or ''}".casefold()
        source_role = (
            "primary_official"
            if any(token in lowered_source for token in ("twse", "tpex", "taifex", "regulator", "official"))
            else "independent_web"
            if source_url
            else "host_validated_tool"
        )
        records.append(
            {
                "schema_version": "open_stock_ai.evidence.v1",
                "evidence_id": evidence_id,
                "claim": claim,
                "source_type": tool,
                "source": source,
                "source_url": source_url,
                "tool_provider": tool_provider,
                "worker_id": observation.get("worker_id"),
                "published_at": publication,
                "observed_at": _first_text(
                    candidate.get("observed_at"),
                    candidate.get("acquired_at"),
                    result_provenance.get("observed_at"),
                    observed_at,
                ),
                "freshness": _canonical_freshness(publication, observed_at),
                "confidence": 1.0,
                "source_role": source_role,
                "request_id": call_id,
                "supports": list(candidate.get("supports") or []),
                "contradicts": list(candidate.get("contradicts") or []),
            }
        )
    return records


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _canonical_source_claim(*, candidate: dict[str, Any], fallback: str) -> str:
    title = _first_text(candidate.get("title"), candidate.get("name"))
    snippet = _first_text(candidate.get("search_snippet"), candidate.get("summary"))
    if title and snippet:
        return f"{title}：{snippet}"[:500]
    if title:
        return title[:500]
    return fallback


def _canonical_freshness(published_at: str | None, observed_at: str) -> str:
    if not published_at:
        return "unknown"
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (observed - published).total_seconds() / 3600)
    if age_hours <= 24:
        return "current"
    if age_hours <= 168:
        return "recent"
    return "stale"


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # ``repr`` is Python's shortest round-trip representation.  A generic
        # ``:g`` format silently rounded Host evidence to six significant
        # digits, then the exact-number validator correctly rejected the model
        # for copying that rounded Host-authored value.
        return repr(value)
    return str(value)


def _workspace_evidence_details(workspace: dict[str, Any]) -> str:
    """Expose the exact validated workspace facts needed for grounded synthesis."""

    if not workspace:
        return ""
    portfolio = workspace.get("portfolio_status") or {}
    signal = workspace.get("signal_summary") or {}
    research = workspace.get("research_status") or {}
    risk = workspace.get("risk_summary") or {}
    blockers = [str(value) for value in workspace.get("blockers") or []]
    reason = " ".join(str(signal.get("reason") or "").split())
    details = (
        f"系統投資組合曝險 {_display_value(portfolio.get('total_position_size_pct'))}%，"
        f"標的曝險 {_display_value(portfolio.get('symbol_exposure') or {})}；"
        f"訊號 action={_display_value(signal.get('action'))}、"
        f"rule_score={_display_value(signal.get('rule_score'))}、"
        f"confidence_type={_display_value(signal.get('confidence_type'))}、"
        f"calibrated={_display_value(signal.get('confidence_calibrated'))}；"
        f"研究驗證 passed={_display_value(research.get('passed'))}、"
        f"advisory_ready={_display_value(research.get('advisory_ready'))}；"
        f"RiskEngine approved={_display_value(risk.get('approved'))}，"
        f"reason={_display_value(risk.get('reason'))}；"
        f"實際 blocker codes={','.join(blockers) if blockers else '無'}。"
    )
    if reason:
        details += f" 外部框架證據摘要原文：{reason[:900]}"
    return details


def _reasoning_step_summary(
    *,
    node_title: str,
    related_trace: list[dict[str, Any]],
    fallback: str,
) -> str:
    """Build one plan node's public result from its own Host-owned evidence."""
    completed = [item for item in related_trace if item.get("ok") is True]
    if not completed:
        return fallback
    statements: list[str] = []
    for observation in completed:
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        tool_name = str(
            observation.get("name")
            or observation.get("tool")
            or observation.get("call_id")
            or "Host 工具"
        )
        schema = str(result.get("schema_version") or "")
        symbol = _display_value(result.get("symbol"))
        if schema == "stock_ai.institutional_flow_evidence.v1":
            items = [item for item in result.get("items") or [] if isinstance(item, dict)]
            latest = items[0] if items else {}
            if items:
                statements.append(
                    f"{symbol} 法人證據已通過 Host 驗證：共 {len(items)} 筆，"
                    f"最新交易日 {_display_value(latest.get('trade_date'))}，"
                    f"外資淨額 {_display_value(latest.get('foreign_net'))}、"
                    f"投信淨額 {_display_value(latest.get('trust_net'))}、"
                    f"自營商淨額 {_display_value(latest.get('dealer_net'))}、"
                    f"三大法人合計 {_display_value(latest.get('total_institutional_net'))}，"
                    f"來源 {_display_value(latest.get('source'))}。"
                )
            else:
                statements.append(f"{symbol} 本次已驗證法人來源沒有可用資料。")
            continue
        if schema == "stock_ai.monthly_revenue_evidence.v1":
            items = [item for item in result.get("items") or [] if isinstance(item, dict)]
            latest = items[0] if items else {}
            if items:
                statements.append(
                    f"{symbol} 基本面月營收證據已通過 Host 驗證：共 {len(items)} 筆，"
                    f"最新期間 {_display_value(latest.get('period'))}，"
                    f"當月營收 {_display_value(latest.get('current_revenue'))} "
                    f"{_display_value(latest.get('unit'))}，"
                    f"月增率 {_display_value(latest.get('mom_change_percent'))}%、"
                    f"年增率 {_display_value(latest.get('yoy_change_percent'))}%、"
                    f"來源 {_display_value(latest.get('source'))}。"
                )
            else:
                statements.append(f"{symbol} 本次已驗證月營收來源沒有可用資料。")
            continue
        if schema == "open_stock_ai.agent_research_pack.v1":
            market = result.get("market_price") or {}
            technical = result.get("technical_features") or {}
            events = result.get("recent_events") or result.get("events") or []
            workspace = result.get("pipeline_workspace") or {}
            paper_position = result.get("paper_position")
            event_titles = [
                " ".join(str(item.get("title") or "").split())
                for item in events[:3]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            statements.append(
                f"{symbol} 行情與研究證據已通過 Host 驗證："
                f"價格 {_display_value(market.get('price'))}"
                f"（{_display_value(market.get('price_source'))}，"
                f"{_display_value(market.get('source_timestamp'))}），"
                f"SMA20 {_display_value(technical.get('sma_20'))}、"
                f"RSI14 {_display_value(technical.get('rsi_14'))}，"
                f"近期事件 {len(events)} 筆；"
                f"本系統 paper_position={'無持股' if paper_position is None else _display_value(paper_position)}。"
                f"{_workspace_evidence_details(workspace)}"
                + (f" 已讀取事件標題例：{'｜'.join(event_titles)}。" if event_titles else "")
            )
            continue
        if schema == "open_stock_ai.agent_workspace.v1":
            statements.append(
                f"{symbol} 策略與風險檢查已通過 Host 驗證："
                f"研究建議 {_display_value(result.get('recommendation_bucket'))}，"
                f"執行權限 {_display_value(result.get('execution_permission'))}，"
                f"{_workspace_evidence_details(result)}"
            )
            continue
        compact = _result_summary(observation)
        facts = "、".join(
            f"{key}={_display_value(value)}"
            for key, value in compact.items()
            if value not in (None, "", [], {})
        )
        statements.append(
            f"{tool_name} 已通過 Host 驗證"
            + (f"：{facts}。" if facts else "。")
        )
    if statements:
        return " ".join(statements)
    return f"{node_title}所需的 {len(completed)} 份 Host 證據已取得並通過驗證。"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
