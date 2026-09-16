from __future__ import annotations

import asyncio
import re
from typing import Any

from open_stock_ai.agent_runtime.routing import UnifiedMultiIntentRouter
from open_stock_ai.types import UniverseRequest

from .agent_drivers import load_agent_driver_settings
from .agent_service import get_agent_run_runtime
from .realtime_data import normalize_symbol
from .universe import UniverseResolutionError, resolve_universe


def guess_symbol(question: str) -> str | None:
    """Resolve only a symbol the user actually named; never choose a default."""

    mapping = {
        "台積電": "2330.TW",
        "鴻海": "2317.TW",
        "廣達": "2382.TW",
        "元大台灣50": "0050.TW",
        "台灣50": "0050.TW",
        "nvidia": "NVDA",
        "nvda": "NVDA",
        "蘋果": "AAPL",
        "apple": "AAPL",
        "微軟": "MSFT",
        "microsoft": "MSFT",
    }
    lowered = question.casefold()
    for key, symbol in mapping.items():
        if key.casefold() in lowered:
            return symbol
    identifier_context = any(
        term in lowered
        for term in (
            "錯誤碼",
            "錯誤代碼",
            "error code",
            "status code",
            "api",
            "http",
            "port",
            "版本",
            "build",
        )
    )
    numeric = re.search(r"\b\d{4,6}\b", question)
    if numeric and not identifier_context:
        return normalize_symbol(numeric.group(0))
    for ticker in re.finditer(r"\b[A-Za-z]{1,5}(?:\.[A-Za-z]{1,4})?\b", question):
        raw_token = ticker.group(0)
        token = raw_token.upper()
        reserved = {
            "AGENT",
            "AI",
            "API",
            "BUILD",
            "CODEX",
            "ERROR",
            "EPS",
            "ETF",
            "HTTP",
            "MODEL",
            "PORT",
            "ROE",
            "RUNTIME",
            "TWD",
            "USD",
        }
        if (raw_token == token or "." in raw_token) and token not in reserved:
            return normalize_symbol(token)
    return None


async def answer_question(
    question: str,
    *,
    driver_id: str | None = None,
    max_steps: int = 6,
) -> tuple[str, str, dict[str, Any], list[str]]:
    """Run the legacy Q endpoint through the same durable, validated Agent Runtime."""

    prompt = str(question or "").strip()
    if not prompt:
        raise ValueError("question is required")
    explicit_symbol = guess_symbol(prompt)
    hint = _task_hint(prompt, explicit_symbol)
    routing = UnifiedMultiIntentRouter().route(
        prompt,
        task_kind_hint=hint,
        supplied_symbols=(),
    )
    symbols = [explicit_symbol] if explicit_symbol else []
    universe_payload: dict[str, Any] = {
        "source": "explicit_symbols" if symbols else "none",
        "symbols": symbols,
        "count": len(symbols),
    }
    if _is_screening_intent(prompt) and not symbols:
        try:
            universe = await asyncio.to_thread(
                resolve_universe,
                UniverseRequest(
                    source="top_by_volume",
                    filters={"market": "all", "query_intent": prompt},
                    limit=20,
                ),
            )
        except UniverseResolutionError as exc:
            raise ValueError(f"Screening Universe could not be resolved: {exc}") from exc
        symbols = list(universe.symbols)
        universe_payload = {
            "source": universe.source,
            "symbols": symbols,
            "count": universe.count,
            "filters": universe.filters,
            "created_at": universe.created_at,
        }
    selected_driver = driver_id or load_agent_driver_settings().default_driver
    runtime = get_agent_run_runtime()
    run = await runtime.create_run(
        objective=prompt,
        symbols=symbols,
        driver_id=selected_driver,
        autonomy="advisory",
        max_steps=max_steps,
        run_metadata={
            "run_type": "unified_query",
            "routing_hint": routing.model_dump(mode="json"),
            "universe": universe_payload,
        },
    )
    result = await runtime.wait(run["run_id"])
    completion = (
        result.get("completion_validation")
        if isinstance(result.get("completion_validation"), dict)
        else {}
    )
    receipts = (
        result.get("model_invocations")
        if isinstance(result.get("model_invocations"), list)
        else []
    )
    receipt = next(
        (
            item
            for item in reversed(receipts)
            if isinstance(item, dict) and item.get("status") == "succeeded"
        ),
        None,
    )
    succeeded = (
        result.get("status") == "completed"
        and completion.get("passed") is True
        and receipt is not None
    )
    route = str(result.get("task_kind") or routing.primary_task_kind)
    sources = list(
        dict.fromkeys(
            str(item.get("tool"))
            for item in result.get("tool_trace") or []
            if isinstance(item, dict) and item.get("ok") is True and item.get("tool")
        )
    )
    if receipt:
        sources.append(
            f"model:{receipt.get('provider') or selected_driver}/{receipt.get('model_id') or 'unknown'}"
        )
    answer = str(result.get("summary") or "")
    if not succeeded:
        answer = (
            "Unified Agent Runtime 未完成可驗證答案；不會退回舊關鍵字分類器或空 Universe "
            f"Screener。內層狀態：{result.get('status') or 'unknown'}。"
        )
    return (
        route,
        answer,
        {
            "run_id": run["run_id"],
            "status": result.get("status"),
            "validated": succeeded,
            "decision": result.get("decision"),
            "completion_validation": completion,
            "model_invocation": receipt,
            "universe": universe_payload,
            "symbol": explicit_symbol,
            "symbol_source": "user_explicit" if explicit_symbol else "none",
        },
        sources,
    )


def _task_hint(question: str, symbol: str | None) -> str:
    lowered = question.casefold()
    if any(term in lowered for term in ("不要分析股票", "不分析股票", "not about stocks")):
        return "general_answer"
    if any(
        term in lowered
        for term in (
            "專案",
            "程式碼",
            "agent runtime",
            "api 錯誤",
            "error code",
            "repository",
            "codebase",
        )
    ):
        return "project_task"
    market_terms = (
        "股票",
        "股價",
        "台股",
        "行情",
        "財報",
        "營收",
        "法人",
        "篩選",
        "連動",
        "market",
        "stock",
    )
    return "market_information" if symbol or any(term in lowered for term in market_terms) else "general_answer"


def _is_screening_intent(question: str) -> bool:
    lowered = question.casefold()
    return any(
        term in lowered
        for term in (
            "找出",
            "篩選",
            "排名",
            "量價",
            "法人買超",
            "screen",
            "rank",
        )
    )
