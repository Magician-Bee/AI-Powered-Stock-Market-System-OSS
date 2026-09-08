from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from open_stock_ai.analysis_contracts import AnalysisProvenance, ModelInvocationReceipt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from open_stock_ai.agent_workspace import build_agent_workspace
from open_stock_ai.runtime import get_runtime_engine

from .codex_runtime import CodexAuthenticationRequired, codex_runtime
from .paper_training_api import (
    PaperOrderRequest,
    _broker,
    _clear_broker_order_book,
    _lab,
    paper_training_account,
    paper_training_order,
    research_pack,
)


router = APIRouter(prefix="/agent/trading", tags=["Agent Trading Workspace"])
_ACCOUNT_LOCK = RLock()


class AgentAccountResetRequest(BaseModel):
    initial_cash: float = Field(gt=0, le=1_000_000_000)


class AgentTradingAnalysisRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=5000)
    execute: bool = False
    horizon: Literal["intraday", "swing", "weekly", "monthly"] = "swing"


class AgentTradingControlRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=8000)
    explain: bool = True
    computer_use: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_identity() -> dict[str, Any]:
    lab = _lab()
    return {
        "account_id": lab.oms.account_id,
        "sqlite_path": str(lab.store.path),
        "base_currency": lab.oms.base_currency,
    }


def _revision(account: dict[str, Any]) -> str:
    basis = "|".join(
        str(value or "")
        for value in (
            account.get("account_id"),
            account.get("account_updated_at"),
            account.get("initial_cash"),
            account.get("cash_balance"),
            account.get("total_equity"),
            account.get("order_count"),
            account.get("fill_count"),
            account.get("position_count"),
        )
    )
    return f"PA-{abs(hash(basis)):016x}"


def account_snapshot(*, refresh_prices: bool = False) -> dict[str, Any]:
    account = paper_training_account(refresh_prices=refresh_prices)
    identity = _store_identity()
    account["account_revision"] = _revision(account)
    account["snapshot_generated_at"] = _now()
    account["source_of_truth"] = {
        **identity,
        "account_table": "paper_accounts",
        "positions_table": "paper_positions",
        "orders_table": "paper_broker_orders",
        "fills_table": "paper_fills",
    }
    return account


def _compact_research(symbol: str, horizon: str) -> dict[str, Any]:
    pack = research_pack(symbol=symbol, horizon=horizon)
    history = pack.get("history") or []
    events = pack.get("events") or []
    workspace = pack.get("pipeline_workspace") or {}
    return {
        "symbol": pack.get("symbol"),
        "market_price": pack.get("market_price") or {},
        "technical_features": pack.get("technical_features") or {},
        "recent_history": history[-40:],
        "recent_events": events[:25],
        "pipeline_workspace": {
            "recommendation_bucket": workspace.get("recommendation_bucket"),
            "execution_permission": workspace.get("execution_permission"),
            "signal_summary": workspace.get("signal_summary") or {},
            "research_status": workspace.get("research_status") or {},
            "risk_summary": workspace.get("risk_summary") or {},
            "blockers": workspace.get("blockers") or [],
            "agent_next_actions": workspace.get("agent_next_actions") or [],
        },
        "paper_position": pack.get("paper_position"),
        "learning_for_symbol": (pack.get("learning_for_symbol") or [])[:30],
    }


def _compact_account(account: dict[str, Any]) -> dict[str, Any]:
    learning = account.get("learning") or {}
    return {
        key: account.get(key)
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
            "position_count",
            "order_count",
            "fill_count",
            "positions",
            "open_orders",
            "recent_orders",
            "recent_fills",
        )
    } | {
        "learning": {
            "episode_count": learning.get("episode_count"),
            "evaluated_episode_count": learning.get("evaluated_episode_count"),
            "total_reward": learning.get("total_reward"),
            "average_reward": learning.get("average_reward"),
            "episodes": (learning.get("episodes") or [])[:10],
            "reflections": (learning.get("reflections") or [])[:10],
        }
    }


def _decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "action",
            "confidence",
            "confidence_type",
            "confidence_calibrated",
            "order_draft",
            "evidence",
            "risks",
            "memory_used",
            "next_checks",
        ],
        "properties": {
            "summary": {"type": "string"},
            "action": {
                "type": "string",
                "enum": ["buy", "sell", "hold", "watch", "cancel_order"],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "confidence_type": {
                "type": "string",
                "enum": ["model_self_reported"],
            },
            "confidence_calibrated": {"type": "boolean", "enum": [False]},
            "order_draft": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "side",
                            "order_type",
                            "time_in_force",
                            "lot_type",
                            "session",
                            "quantity_shares",
                            "quantity_lots",
                            "limit_price",
                            "stop_price",
                            "rationale",
                        ],
                        "properties": {
                            "side": {"type": "string", "enum": ["buy", "sell"]},
                            "order_type": {
                                "type": "string",
                                "enum": ["market", "limit", "stop", "stop_limit"],
                            },
                            "time_in_force": {
                                "type": "string",
                                "enum": ["rod", "ioc", "fok"],
                            },
                            "lot_type": {
                                "type": "string",
                                "enum": ["board_lot", "odd_lot"],
                            },
                            "session": {
                                "type": "string",
                                "enum": ["regular", "after_hours"],
                            },
                            "quantity_shares": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
                            "quantity_lots": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
                            "limit_price": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
                            "stop_price": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
                            "rationale": {"type": "string"},
                        },
                    },
                ]
            },
            "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "memory_used": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "next_checks": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
    }


def _decision_prompt(request: AgentTradingAnalysisRequest, context: dict[str, Any]) -> str:
    return (
        "你是住在股市AI系統內的交易 Agent。這是一個只使用虛擬資金、但行情與證券資料來自系統市場資料層的學習環境。"
        "先讀取帳戶現金、持倉、掛單、成交、歷史回合與反思，再分析目前股票。"
        "RiskEngine 在這個學習環境提供警告而不阻止實驗；但不可超出虛擬現金、不可賣出未持有部位。"
        "不得虛構市場價格或資料。order_draft 必須能直接交給 Paper Broker；沒有合理交易時設為 null。"
        "confidence 只能是模型自述的 0–100 分數，必須設定 confidence_type=model_self_reported、"
        "confidence_calibrated=false，不得描述為成功機率。"
        "請只輸出符合 schema 的 JSON。\n"
        f"USER_GOAL={request.prompt.strip()}\n"
        f"TRADING_CONTEXT={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
    )


def _order_request_from_decision(
    decision: dict[str, Any],
    *,
    symbol: str,
    episode_id: str | None,
) -> PaperOrderRequest:
    draft = decision.get("order_draft") or {}
    side = str(draft.get("side") or decision.get("action") or "").lower()
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=422, detail="Codex did not produce an executable buy/sell draft")
    quantity_shares = draft.get("quantity_shares")
    quantity_lots = draft.get("quantity_lots")
    if quantity_shares is None and quantity_lots is None:
        raise HTTPException(status_code=422, detail="Codex order draft is missing quantity")
    return PaperOrderRequest(
        symbol=symbol,
        side=side,
        order_type=draft.get("order_type") or "market",
        time_in_force=draft.get("time_in_force") or "rod",
        lot_type=draft.get("lot_type") or ("odd_lot" if quantity_shares is not None else "board_lot"),
        session=draft.get("session") or "regular",
        quantity_shares=quantity_shares,
        quantity_lots=quantity_lots,
        limit_price=draft.get("limit_price"),
        stop_price=draft.get("stop_price"),
        episode_id=episode_id,
        actor="codex",
        rationale=str(draft.get("rationale") or decision.get("summary") or "Codex paper decision"),
    )


@router.get("/session")
async def agent_trading_session(
    symbol: str | None = None,
    horizon: Literal["intraday", "swing", "weekly", "monthly"] = "swing",
    refresh_prices: bool = False,
) -> dict[str, Any]:
    selected_symbol = str(symbol or "").strip().upper()
    account = await asyncio.to_thread(account_snapshot, refresh_prices=refresh_prices)
    research: dict[str, Any] = {}
    research_error: str | None = None
    if selected_symbol:
        try:
            research = await asyncio.to_thread(_compact_research, selected_symbol, horizon)
        except Exception as exc:
            # Account state remains useful even when a quote source is temporarily
            # unavailable.  A research-data gap must not turn the whole workspace
            # into a 422 response or imply that an order was attempted.
            research_error = str(exc)
    try:
        codex = await codex_runtime.account_status(refresh=False)
    except Exception as exc:
        codex = {"authenticated": False, "error": str(exc)}
    return {
        "schema_version": "stock_ai.agent_trading_session.v1",
        "generated_at": _now(),
        "symbol": research.get("symbol") or selected_symbol or None,
        "account": account,
        "research": research,
        "research_error": research_error,
        "codex": codex,
        "agent_tools": {
            "analyze": "/api/open-stock-ai/agent/trading/analyze",
            "control": "/api/open-stock-ai/agent/trading/control",
            "reset_account": "/api/open-stock-ai/agent/trading/account/reset",
            "paper_preview": "/api/open-stock-ai/agent/paper-training/preview",
            "paper_order": "/api/open-stock-ai/agent/paper-training/order",
            "mark_to_market": "/api/open-stock-ai/agent/paper-training/mark-to-market",
        },
    }


@router.post("/account/reset")
def agent_trading_reset_account(request: AgentAccountResetRequest) -> dict[str, Any]:
    amount = float(request.initial_cash)
    with _ACCOUNT_LOCK:
        lab = _lab()
        _clear_broker_order_book()
        result = lab.reset_account(amount)
        persisted = account_snapshot(refresh_prices=False)
        values = (
            float(persisted.get("initial_cash") or 0.0),
            float(persisted.get("cash_balance") or 0.0),
            float(persisted.get("total_equity") or 0.0),
        )
        if any(abs(value - amount) > 0.005 for value in values):
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Agent account reset did not persist to the shared account snapshot",
                    "expected": amount,
                    "actual": {
                        "initial_cash": values[0],
                        "cash_balance": values[1],
                        "total_equity": values[2],
                    },
                    "source_of_truth": persisted.get("source_of_truth"),
                },
            )
        return {
            "schema_version": "stock_ai.agent_account_reset.v1",
            "reset_id": f"AR-{uuid4().hex}",
            "verified": True,
            "account": persisted,
            "episode": result.get("episode"),
        }


@router.post("/analyze")
async def agent_trading_analyze(request: AgentTradingAnalysisRequest) -> dict[str, Any]:
    await codex_runtime.require_account()
    account, research = await asyncio.gather(
        asyncio.to_thread(account_snapshot, refresh_prices=False),
        asyncio.to_thread(_compact_research, request.symbol, request.horizon),
    )
    context = {
        "generated_at": _now(),
        "account": _compact_account(account),
        "research": research,
    }
    model_call_id = f"MI-{uuid4().hex}"
    model_started_at = _now()
    try:
        decision = await codex_runtime.run_structured(
            _decision_prompt(request, context),
            _decision_schema(),
        )
    except CodexAuthenticationRequired:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Codex Agent analysis failed: {exc}") from exc
    model_completed_at = _now()
    model_receipt = ModelInvocationReceipt(
        call_id=model_call_id,
        provider="codex",
        model_id="codex-app-server/default",
        status="succeeded",
        started_at=model_started_at,
        completed_at=model_completed_at,
        raw_output_preserved=False,
    )
    decision["confidence_type"] = "model_self_reported"
    decision["confidence_calibrated"] = False

    execution = None
    if request.execute:
        draft = decision.get("order_draft")
        if draft is not None and decision.get("action") in {"buy", "sell"}:
            latest_episode = ((account.get("learning") or {}).get("episodes") or [None])[0] or {}
            order_request = _order_request_from_decision(
                decision,
                symbol=str(research.get("symbol") or request.symbol),
                episode_id=latest_episode.get("episode_id"),
            )
            execution = await asyncio.to_thread(paper_training_order, order_request)

    refreshed_account = await asyncio.to_thread(account_snapshot, refresh_prices=False)
    return {
        "schema_version": "stock_ai.agent_trading_decision.v1",
        "generated_at": _now(),
        "symbol": research.get("symbol") or request.symbol,
        "executed": execution is not None,
        "decision": decision,
        "model_invocation": model_receipt.model_dump(),
        "provenance": AnalysisProvenance(
            origin="model",
            provider=model_receipt.provider,
            model_id=model_receipt.model_id,
            model_call_id=model_receipt.call_id,
            model_call_succeeded=True,
            universe_source="explicit_symbols",
            symbols_considered=[str(research.get("symbol") or request.symbol)],
            data_sources=["agent_trading_context"],
            data_ready=True,
        ).model_dump(),
        "execution": execution,
        "account": refreshed_account,
    }


@router.post("/control")
async def agent_trading_control(request: AgentTradingControlRequest) -> dict[str, Any]:
    account, research = await asyncio.gather(
        asyncio.to_thread(account_snapshot, refresh_prices=False),
        asyncio.to_thread(_compact_research, request.symbol, "swing"),
    )
    context = {
        "account": _compact_account(account),
        "research": research,
        "instruction": (
            "你現在位於 Agent Trading Workspace。先使用這份帳戶、持倉、掛單、成交、研究與學習記憶；"
            "需要檢查圖表時再使用 Computer Use 操作 UI。任何模擬委託都必須走 Paper Broker API，"
            "不得只在文字中宣稱已買賣。"
        ),
    }
    enriched = (
        f"AGENT_TRADING_CONTEXT={json.dumps(context, ensure_ascii=False, separators=(',', ':'))}\n"
        f"USER_REQUEST={request.prompt.strip()}"
    )
    try:
        result = await codex_runtime.run(
            enriched,
            view="paper-trading",
            symbol=str(research.get("symbol") or request.symbol),
            explain=request.explain,
            computer_use=request.computer_use,
        )
    except CodexAuthenticationRequired:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Codex Agent control failed: {exc}") from exc
    result["account_revision"] = account.get("account_revision")
    result["agent_trading_context_attached"] = True
    return result
