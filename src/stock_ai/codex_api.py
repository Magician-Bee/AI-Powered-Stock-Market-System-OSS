from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import yaml

from open_stock_ai.agent_workspace import build_agent_tool_manifest, build_agent_workspace

from .agent_trading_api import account_snapshot
from .codex_market import build_market_radar
from .codex_runtime import CodexAuthenticationRequired, codex_runtime


router = APIRouter(prefix="/api/codex", tags=["Codex"])


class LoginRequest(BaseModel):
    flow: Literal["browser", "device_code"] = "browser"


class CodexRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    view: str | None = Field(default=None, max_length=80)
    symbol: str | None = Field(default=None, max_length=40)
    explain: bool = False
    computer_use: bool = False
    allow_project_changes: bool = False


class MarketRadarRequest(BaseModel):
    limit: int = Field(default=6, ge=2, le=8)
    explain: bool = False
    refresh: bool = False


@router.get("/account")
async def account(refresh: bool = False) -> dict:
    try:
        return await codex_runtime.account_status(refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Codex 執行核心無法啟動：{exc}") from exc


@router.post("/login")
async def login(payload: LoginRequest) -> dict:
    try:
        if payload.flow == "device_code":
            return await codex_runtime.start_device_login()
        return await codex_runtime.start_browser_login()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"無法啟動 ChatGPT 登入：{exc}") from exc


@router.get("/login/{login_id}")
def login_status(login_id: str) -> dict:
    return codex_runtime.login_status(login_id)


@router.post("/logout")
async def logout() -> dict:
    await codex_runtime.logout()
    return {"authenticated": False, "status": "logged_out"}


@router.post("/run")
async def run(payload: CodexRunRequest) -> dict:
    _require_native_diagnostics()
    try:
        agent_context = await _build_codex_agent_context(payload)
        enriched_prompt = _enrich_prompt(payload.prompt, agent_context)
        result = await codex_runtime.run(
            enriched_prompt,
            view=payload.view,
            symbol=payload.symbol,
            explain=payload.explain,
            computer_use=payload.computer_use,
            allow_project_changes=payload.allow_project_changes,
        )
        result["developer_debug_route"] = True
        paper_account = agent_context.get("paper_account") or {}
        result["agent_context"] = {
            "attached": agent_context.get("workspace_attached") is True,
            "symbol": agent_context.get("symbol"),
            "recommendation_bucket": agent_context.get("recommendation_bucket"),
            "execution_permission": agent_context.get("execution_permission"),
            "blockers": agent_context.get("blockers") or [],
            "paper_account_attached": bool(paper_account),
            "paper_account_revision": paper_account.get("account_revision"),
            "paper_total_equity": paper_account.get("total_equity"),
            "paper_cash_balance": paper_account.get("cash_balance"),
            "source": "open_stock_ai.agent_workspace+agent_trading_account",
        }
        return result
    except CodexAuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Codex 執行失敗：{exc}") from exc


@router.get("/capabilities")
def capabilities() -> dict:
    status = codex_runtime.capability_status()
    status["stock_ai_agent_workspace"] = build_agent_tool_manifest()
    status["agent_trading_workspace"] = {
        "session": "/api/open-stock-ai/agent/trading/session",
        "analyze": "/api/open-stock-ai/agent/trading/analyze",
        "control": "/api/open-stock-ai/agent/trading/control",
        "account_reset": "/api/open-stock-ai/agent/trading/account/reset",
        "supports_codex_paper_execution": True,
        "supports_computer_use": True,
    }
    status["interoperable_agent_runtime"] = {
        "runtime": "/api/agents",
        "tools": "/api/agents/tools",
        "run": "/api/agents/run",
        "codex_native_capabilities_preserved": True,
        "native_diagnostics_enabled": _native_diagnostics_enabled(),
        "role": (
            "Codex is the internal model provider for the Host-owned Stock AI Agent. "
            "The full-access native route remains available only when developer diagnostics are explicitly enabled."
        ),
    }
    return status


@router.post("/project/sync")
async def project_sync() -> dict:
    _require_native_diagnostics()
    try:
        return await codex_runtime.sync_project()
    except CodexAuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Codex 專案同步失敗：{exc}") from exc


@router.post("/market-radar")
async def market_radar(payload: MarketRadarRequest) -> dict:
    _require_native_diagnostics()
    try:
        await codex_runtime.require_account()
        return await build_market_radar(
            codex_runtime,
            limit=payload.limit,
            explain=payload.explain,
            force=payload.refresh,
        )
    except CodexAuthenticationRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"市場雷達執行失敗：{exc}") from exc


async def _build_codex_agent_context(payload: CodexRunRequest) -> dict[str, Any]:
    symbol = str(payload.symbol or "").strip().upper() or None
    if symbol is None:
        return {
            "workspace_attached": False,
            "symbol": None,
            "symbol_source": "none",
            "market": None,
            "paper_account_attached": False,
            "paper_account": {},
            "tool_manifest": build_agent_tool_manifest(),
        }
    market = _infer_market(symbol)
    workspace_result, account_result = await asyncio.gather(
        asyncio.to_thread(
            build_agent_workspace,
            symbol=symbol,
            market=market,
            horizon="swing",
        ),
        asyncio.to_thread(account_snapshot, refresh_prices=False),
        return_exceptions=True,
    )

    if isinstance(workspace_result, Exception):
        context: dict[str, Any] = {
            "workspace_attached": False,
            "symbol": symbol,
            "market": market,
            "error": {
                "type": type(workspace_result).__name__,
                "message": str(workspace_result),
            },
            "tool_manifest": build_agent_tool_manifest(),
        }
    else:
        workspace = workspace_result
        context = {
            "workspace_attached": True,
            "symbol": workspace.get("symbol"),
            "market": workspace.get("market"),
            "horizon": workspace.get("horizon"),
            "recommendation_bucket": workspace.get("recommendation_bucket"),
            "execution_permission": workspace.get("execution_permission"),
            "analysis_only": workspace.get("analysis_only") is True,
            "ranking": workspace.get("ranking") or {},
            "portfolio_status": workspace.get("portfolio_status") or {},
            "data_status": workspace.get("data_status") or {},
            "research_status": workspace.get("research_status") or {},
            "signal_summary": workspace.get("signal_summary") or {},
            "risk_summary": workspace.get("risk_summary") or {},
            "blockers": workspace.get("blockers") or [],
            "agent_next_actions": workspace.get("agent_next_actions") or [],
            "tool_manifest": workspace.get("tool_manifest") or build_agent_tool_manifest(),
        }

    if isinstance(account_result, Exception):
        context["paper_account_error"] = {
            "type": type(account_result).__name__,
            "message": str(account_result),
        }
        context["paper_account"] = {}
    else:
        learning = account_result.get("learning") or {}
        context["paper_account"] = {
            key: account_result.get(key)
            for key in (
                "account_id",
                "account_revision",
                "initial_cash",
                "cash_balance",
                "holdings_market_value",
                "total_equity",
                "realized_pnl",
                "unrealized_pnl",
                "total_return_pct",
                "positions",
                "open_orders",
                "recent_orders",
                "recent_fills",
                "source_of_truth",
            )
        } | {
            "learning": {
                "episode_count": learning.get("episode_count"),
                "total_reward": learning.get("total_reward"),
                "average_reward": learning.get("average_reward"),
                "episodes": (learning.get("episodes") or [])[:10],
                "reflections": (learning.get("reflections") or [])[:10],
            }
        }
    return context


def _native_diagnostics_enabled() -> bool:
    override = os.getenv("STOCK_AI_CODEX_NATIVE_DIAGNOSTICS")
    if override is not None:
        return override.strip().casefold() in {"1", "true", "yes", "on"}
    path = Path(__file__).resolve().parents[2] / "config" / "agent_runtime.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        return False
    policies = payload.get("policies") if isinstance(payload, dict) else {}
    return bool(policies.get("native_codex_diagnostics_enabled", False)) if isinstance(policies, dict) else False


def _require_native_diagnostics() -> None:
    if not _native_diagnostics_enabled():
        raise HTTPException(
            status_code=404,
            detail="Native Codex diagnostics are disabled; create a Stock AI Agent run through /api/agents/runs.",
        )


def _enrich_prompt(prompt: str, agent_context: dict[str, Any]) -> str:
    return (
        "以下是 OpenStockAIEngine 與 Agent Trading Workspace 為目前股票建立的完整結構化 Context。"
        "分析前必須先讀資料來源、時間、研究結果、RiskEngine 提示、虛擬現金、持倉、掛單、成交與歷史反思。"
        "需要看圖時再使用 Computer Use 檢查 UI、K 線、新聞、籌碼與基本面。"
        "正式交易 permission=blocked 時不得稱為可立即實盤；但使用者要求模擬學習時，可透過 Agent Trading API"
        "使用虛擬資金送出、撤銷和撮合 Paper Broker 委託。一般問答不得暗中建立委託；"
        "只有使用者明確要求執行模擬交易時才能送單，且不得只在文字中宣稱已交易。\n"
        f"AGENT_CONTEXT={json.dumps(agent_context, ensure_ascii=False, separators=(',', ':'))}\n"
        f"USER_REQUEST={prompt.strip()}"
    )


def _infer_market(symbol: str) -> Literal["TW", "US", "CRYPTO"]:
    normalized = symbol.upper()
    if normalized.endswith((".TW", ".TWO")) or normalized.replace(".", "").isdigit():
        return "TW"
    if normalized.endswith(("-USD", "/USD")):
        return "CRYPTO"
    return "US"
