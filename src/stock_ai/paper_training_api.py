from __future__ import annotations

import math
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from open_stock_ai.agent_workspace import build_agent_workspace
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.governance.artifact_rollback import ArtifactRollbackError
from open_stock_ai.runtime import get_runtime_engine
from open_stock_ai.data.execution_quote import execution_eligibility, quote_envelope
from open_stock_ai.learning.promotion_gate import PromotionGate

from .realtime_data import normalize_symbol
from .services import get_execution_price_summary, get_market_events, get_price_history


router = APIRouter(prefix="/agent", tags=["Agent Paper Training"])
logger = logging.getLogger(__name__)


class PaperResetRequest(BaseModel):
    initial_cash: float = Field(gt=0, le=1_000_000_000)


class PaperEpisodeRequest(BaseModel):
    objective: str = Field(default="Paper-trading strategy experiment", min_length=1, max_length=500)
    actor: Literal["user", "codex", "agent"] = "codex"


class BorrowLocateReceipt(BaseModel):
    """An explicit paper borrow receipt; public short activity is not a locate."""

    receipt_id: str = Field(min_length=1, max_length=200)
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=500)
    verified_at: str = Field(min_length=1, max_length=64)
    expires_at: str = Field(min_length=1, max_length=64)
    recall_at: str | None = Field(default=None, max_length=64)
    available_quantity: float = Field(gt=0, le=1_000_000_000)
    annual_fee_bps: float = Field(ge=0, le=1_600)


class PaperOrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell", "add", "reduce", "short_sell", "buy_to_cover"]
    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    time_in_force: Literal["rod", "ioc", "fok"] = "rod"
    lot_type: Literal["board_lot", "odd_lot"] = "board_lot"
    session: Literal["regular", "after_hours"] = "regular"
    quantity_lots: float | None = Field(default=None, gt=0, le=999_999)
    quantity_shares: float | None = Field(default=None, gt=0, le=1_000_000_000)
    cash_amount: float | None = Field(default=None, gt=0)
    position_size_pct: float | None = Field(default=None, gt=0, le=100)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    expires_at: str | None = Field(default=None, max_length=64)
    episode_id: str | None = None
    actor: Literal["user", "codex", "agent"] = "codex"
    training_fill_at_latest_mark: bool = False
    rationale: str = Field(default="", max_length=4000)
    borrow_receipt: BorrowLocateReceipt | None = None


class PaperCancelRequest(BaseModel):
    reason: str = Field(default="user_requested", min_length=1, max_length=300)


class PaperReplaceRequest(BaseModel):
    quantity_lots: float | None = Field(default=None, gt=0, le=999_999)
    quantity_shares: float | None = Field(default=None, gt=0, le=1_000_000_000)
    cash_amount: float | None = Field(default=None, gt=0)
    position_size_pct: float | None = Field(default=None, gt=0, le=100)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    expires_at: str | None = Field(default=None, max_length=64)
    rationale: str | None = Field(default=None, max_length=4000)
    actor: Literal["user", "codex", "agent"] = "user"


class PaperReflectionRequest(BaseModel):
    episode_id: str
    summary: str = Field(min_length=1, max_length=8000)
    lessons: list[str] = Field(default_factory=list, max_length=50)
    next_rules: list[str] = Field(default_factory=list, max_length=50)


class EpisodeEvaluationRequest(BaseModel):
    episode_id: str
    close: bool = False


class PolicyEvaluationRequest(BaseModel):
    evidence: dict[str, Any]
    risk_review: dict[str, Any]


class PolicyShadowResultRequest(BaseModel):
    sample_size: int = Field(ge=0)
    return_pct: float
    max_drawdown_pct: float = Field(ge=0)
    risk_violations: int = Field(ge=0)


class PolicyPromotionRequest(BaseModel):
    approved_by: str = Field(min_length=1, max_length=200)


class ArtifactRollbackRequest(BaseModel):
    artifact_scope: Literal["strategy", "model"]
    reason: str = Field(min_length=1, max_length=500)
    approved_by: str = Field(min_length=1, max_length=200)


def _lab() -> PaperTrainingLab:
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    if trade_store is None or trade_store.paper_oms is None:
        raise HTTPException(status_code=503, detail="Paper OMS is not available")
    return PaperTrainingLab(store=trade_store.store, oms=trade_store.paper_oms)


def _promotion_gate() -> PromotionGate:
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    if trade_store is None:
        raise HTTPException(status_code=503, detail="Paper training governance is not available")
    governance = engine.governance
    return PromotionGate(
        trade_store.store,
        artifact_rollback=governance.artifact_rollback if governance is not None else None,
    )


def _artifact_rollback_registry():
    governance = get_runtime_engine().governance
    if governance is None or not governance.durable:
        raise HTTPException(status_code=503, detail="Durable artifact governance is not available")
    return governance.artifact_rollback


def _artifact_governance_status() -> dict[str, Any]:
    registry = _artifact_rollback_registry()
    status = registry.status()
    status.update(
        {
            "durable": True,
            "approved_artifacts": [
                {
                    "artifact_id": record["artifact_id"],
                    "artifact_sha256": record["artifact_sha256"],
                    "approved_by": record["approved_by"],
                    "approved_at": record["approved_at"],
                    "metadata": dict(record.get("metadata") or {}),
                }
                for record in registry._approved.values()
            ],
            "activation_history": list(registry._activation_history),
            "rollback_receipts": [receipt.as_dict() for receipt in registry.receipts[-50:]],
        }
    )
    return status


def _broker() -> PaperBrokerSimulator:
    lab = _lab()
    governance = get_runtime_engine().governance
    return PaperBrokerSimulator(
        store=lab.store,
        oms=lab.oms,
        retention_ledger=governance.retention if governance is not None else None,
    )


def _clear_broker_order_book() -> None:
    lab = _lab()
    with lab.store._connect() as conn:
        conn.execute("delete from paper_broker_order_events where account_id = ?", (lab.oms.account_id,))
        conn.execute("delete from paper_broker_orders where account_id = ?", (lab.oms.account_id,))
        conn.commit()


def _verified_price(
    symbol: str,
    *,
    allow_official_close: bool = True,
    horizon: str = "swing",
    require_execution_quote: bool = True,
) -> dict[str, Any]:
    requested = str(symbol or "").strip()
    if not requested:
        raise HTTPException(status_code=422, detail="symbol is required")
    started_monotonic = time.monotonic()
    summary = get_execution_price_summary(requested)
    if summary is None:
        raise HTTPException(status_code=422, detail="No market price is available for this symbol")
    price = float(summary.latest_price.close or 0.0)
    if not math.isfinite(price) or price <= 0:
        raise HTTPException(status_code=422, detail="Market price is unavailable")
    source = str(summary.data_source or "").strip()
    envelope = quote_envelope(
        provider_id=str(summary.provider_id or "unknown"),
        connector_id=str(summary.connector_id or "unknown"),
        quote_kind=str(summary.quote_kind or "unknown"),
        exchange_timestamp=summary.latest_price.date,
        received_at=summary.data_timestamp,
        max_age_seconds=summary.max_age_seconds,
        authorized=summary.authorized,
        realtime=summary.realtime,
        delayed=summary.delayed,
        official_close=summary.official_close,
    )
    eligibility = execution_eligibility(envelope, horizon=horizon)
    if require_execution_quote and envelope["quote_kind"] not in {"last_trade", "official_close"}:
        raise HTTPException(status_code=422, detail="A last trade or exchange close is required")
    is_realtime = envelope["realtime"] is True and envelope["quote_kind"] == "last_trade"
    source_kind = {
        "last_trade": "realtime_last_trade" if is_realtime else "delayed_last_trade",
        "official_close": "official_exchange_close",
    }.get(envelope["quote_kind"], f"research_{envelope['quote_kind']}")
    if require_execution_quote and not is_realtime and not allow_official_close:
        raise HTTPException(status_code=422, detail="A realtime last trade is required")
    if require_execution_quote and eligibility["execution_eligible"] is not True:
        raise HTTPException(
            status_code=422,
            detail={"message": "The selected quote is not eligible for this execution horizon", **eligibility},
        )
    normalized = normalize_symbol(summary.entity.symbol or requested)
    result = {
        "symbol": normalized,
        "name": summary.entity.name,
        "market": "TW" if summary.entity.market == "taiwan" else str(summary.entity.market or "").upper(),
        "exchange": summary.entity.exchange,
        "industry": summary.entity.industry,
        "price": price,
        "price_source": source,
        "source_kind": source_kind,
        "source_timestamp": summary.data_timestamp or summary.latest_price.date,
        "is_realtime": is_realtime,
        "is_fallback": not is_realtime,
        "freshness_note": summary.freshness_note,
        "reliability_note": summary.reliability_note,
        "source_envelope": envelope,
        "execution_eligibility": eligibility,
        "research_eligible": True,
        "limit_up": getattr(summary, "limit_up", None),
        "limit_down": getattr(summary, "limit_down", None),
        "trading_state": getattr(summary, "trading_state", "unknown"),
        "buy_liquidity_confirmed": bool(getattr(summary, "buy_liquidity_confirmed", False)),
        "sell_liquidity_confirmed": bool(getattr(summary, "sell_liquidity_confirmed", False)),
        "exchange_rules_enforced": True,
        "settlement_rules_enforced": True,
    }
    try:
        data_at = _parse_observation_time(result["source_timestamp"])
        result["slo_observation"] = _record_runtime_slo(
            "data.freshness",
            latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
            success=True,
            data_at=data_at,
        )
    except ValueError as exc:
        # The price remains an explicit verified market payload, but absent or
        # malformed source time must be visible as a failed freshness sample.
        result["slo_observation"] = _record_runtime_slo(
            "data.freshness",
            latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
            success=False,
        )
        result["slo_observation"]["source_timestamp_error"] = str(exc)
    return result


def _parse_observation_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("source_timestamp_missing")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_runtime_slo(
    service: str,
    *,
    latency_ms: float,
    success: bool,
    data_at: datetime | None = None,
) -> dict[str, Any]:
    """Attach paper/data evidence to the Agent Dock's shared SLO ledger."""

    try:
        from .agent_service import get_agent_run_runtime

        return get_agent_run_runtime().record_slo_observation(
            service,
            latency_ms=latency_ms,
            success=success,
            data_at=data_at,
        )
    except Exception as exc:
        logger.exception("Unable to persist %s SLO observation", service)
        return {
            "service": service,
            "status": "recording_failed",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }


def _technical_features(points: list[Any]) -> dict[str, Any]:
    closes = [float(point.close) for point in points if getattr(point, "close", None) is not None]
    if not closes:
        return {"available": False, "reason": "no_price_history"}

    def sma(window: int) -> float | None:
        if len(closes) < window:
            return None
        return round(sum(closes[-window:]) / window, 6)

    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)) if closes[index - 1] != 0]
    recent_returns = returns[-20:]
    volatility_20d = None
    if len(recent_returns) >= 2:
        mean = sum(recent_returns) / len(recent_returns)
        variance = sum((value - mean) ** 2 for value in recent_returns) / (len(recent_returns) - 1)
        volatility_20d = math.sqrt(variance) * math.sqrt(252) * 100.0

    rsi_14 = None
    if len(closes) >= 15:
        changes = [closes[index] - closes[index - 1] for index in range(len(closes) - 14, len(closes))]
        gains = sum(max(change, 0.0) for change in changes) / 14
        losses = sum(max(-change, 0.0) for change in changes) / 14
        rsi_14 = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)

    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            max_drawdown = min(max_drawdown, close / peak - 1.0)

    latest = closes[-1]
    high_20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    low_20 = min(closes[-20:]) if len(closes) >= 20 else min(closes)
    return {
        "available": True,
        "history_points": len(closes),
        "latest_close": latest,
        "sma_5": sma(5),
        "sma_20": sma(20),
        "sma_60": sma(60),
        "return_1d_pct": round(returns[-1] * 100.0, 6) if returns else None,
        "return_5d_pct": round((latest / closes[-6] - 1.0) * 100.0, 6) if len(closes) >= 6 else None,
        "return_20d_pct": round((latest / closes[-21] - 1.0) * 100.0, 6) if len(closes) >= 21 else None,
        "annualized_volatility_20d_pct": round(volatility_20d, 6) if volatility_20d is not None else None,
        "rsi_14": round(rsi_14, 6) if rsi_14 is not None else None,
        "max_drawdown_available_history_pct": round(max_drawdown * 100.0, 6),
        "distance_from_20d_high_pct": round((latest / high_20 - 1.0) * 100.0, 6) if high_20 else None,
        "distance_from_20d_low_pct": round((latest / low_20 - 1.0) * 100.0, 6) if low_20 else None,
        "method": "deterministic_features_from_market_price_history",
    }


def _risk_advisory(account: dict[str, Any], *, symbol: str, side: str, quantity: float, price: float, industry: str | None) -> dict[str, Any]:
    engine = get_runtime_engine()
    positions = [
        {
            "symbol": item.get("symbol"),
            "industry": item.get("industry"),
            "market_value": item.get("market_value"),
            "quantity": item.get("quantity"),
            "position_size_pct": item.get("position_size_pct"),
        }
        for item in account.get("positions") or []
    ]
    preview = engine.pipeline.risk.evaluate_order_preview(
        symbol=symbol,
        side="buy" if side in {"buy", "add"} else "sell",
        quantity_shares=quantity,
        reference_price=price,
        industry=industry,
        account_summary={
            "total_equity": account.get("total_equity"),
            "cash_balance": account.get("cash_balance"),
            "today_pnl": 0.0,
            "positions": positions,
        },
    )
    return {
        **preview,
        "enforcement": "advisory_only",
        "blocked_order": False,
        "note": "Risk results are recorded as training evidence; the paper broker only enforces cash and owned-position accounting.",
    }


def _ticket(request: PaperOrderRequest, price: dict[str, Any], episode_id: str | None) -> dict[str, Any]:
    side = {
        "add": "buy",
        "reduce": "sell",
    }.get(request.side, request.side)
    return {
        "order_id": f"PB-{request.actor.upper()}-{uuid4().hex}",
        "symbol": price["symbol"],
        "market": price["market"],
        "side": side,
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
        "lot_type": request.lot_type,
        "session": request.session,
        "quantity_lots": request.quantity_lots,
        "quantity_shares": request.quantity_shares,
        "cash_amount": request.cash_amount,
        "position_size_pct": request.position_size_pct,
        "limit_price": request.limit_price,
        "stop_price": request.stop_price,
        "expires_at": request.expires_at,
        "episode_id": episode_id,
        "actor": request.actor,
        "training_fill_at_latest_mark": request.training_fill_at_latest_mark,
        "rationale": request.rationale,
        "borrow_receipt": request.borrow_receipt.model_dump(mode="json") if request.borrow_receipt else None,
    }


def _broker_market_for_training(price: dict[str, Any], request: PaperOrderRequest) -> dict[str, Any]:
    """Keep real market status visible while allowing an explicit local training fill."""

    if not request.training_fill_at_latest_mark:
        return price
    return {
        **price,
        "paper_training_fill_override": True,
        "paper_training_fill_basis": "latest_verified_market_mark",
    }


@router.get("/research-pack")
def research_pack(symbol: str, horizon: str = "swing") -> dict[str, Any]:
    # Research can safely use a positive, attributed indicative/delayed quote.
    # Paper and live execution endpoints keep the stricter last-trade/official-close gate.
    price = _verified_price(symbol, horizon=horizon, require_execution_quote=False)
    history = get_price_history(price["symbol"])
    events = get_market_events(price["symbol"], limit=40)
    workspace = build_agent_workspace(symbol=price["symbol"], market=price["market"], horizon=horizon)
    account = _lab().account_summary()
    position = next((item for item in account.get("positions") or [] if item.get("symbol") == price["symbol"]), None)
    return {
        "schema_version": "open_stock_ai.agent_research_pack.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": price["symbol"],
        "market_price": price,
        "technical_features": _technical_features(history),
        "history": [point.model_dump() for point in history[-250:]],
        "events": [event.model_dump() for event in events],
        "pipeline_workspace": workspace,
        "paper_position": position,
        "paper_account_summary": {
            key: account.get(key)
            for key in (
                "initial_cash",
                "cash_balance",
                "holdings_market_value",
                "total_equity",
                "realized_pnl",
                "unrealized_pnl",
                "total_return_pct",
            )
        },
        "learning_for_symbol": [
            event
            for event in (account.get("learning") or {}).get("events") or []
            if event.get("symbol") == price["symbol"]
        ][:50],
    }


@router.get("/paper-training/account")
def paper_training_account(refresh_prices: bool = False) -> dict[str, Any]:
    if refresh_prices:
        return paper_training_mark_to_market()["account"]
    lab = _lab()
    lab.oms.settle_due()
    lab.oms.accrue_borrow_fees()
    account = lab.account_summary()
    account["open_orders"] = _broker().recent_orders(status="open", limit=100)
    account["recent_orders"] = _broker().recent_orders(limit=50)
    account["recent_fills"] = _broker().recent_fills(limit=50)
    return account


@router.post("/paper-training/reset")
def paper_training_reset(request: PaperResetRequest) -> dict[str, Any]:
    amount = float(request.initial_cash)
    _clear_broker_order_book()
    result = _lab().reset_account(amount)
    verified = paper_training_account(refresh_prices=False)
    if (
        abs(float(verified.get("initial_cash") or 0.0) - amount) > 0.005
        or abs(float(verified.get("cash_balance") or 0.0) - amount) > 0.005
        or abs(float(verified.get("total_equity") or 0.0) - amount) > 0.005
    ):
        raise HTTPException(status_code=500, detail="Paper account reset did not persist atomically")
    result["account"] = verified
    result["reset_token"] = f"RESET-{uuid4().hex}"
    result["verified"] = True
    return result


@router.post("/paper-training/episode")
def paper_training_episode(request: PaperEpisodeRequest) -> dict[str, Any]:
    return _lab().start_episode(objective=request.objective, actor=request.actor)


@router.post("/paper-training/preview")
def paper_training_preview(request: PaperOrderRequest) -> dict[str, Any]:
    lab = _lab()
    price = _verified_price(request.symbol)
    ticket = _ticket(request, price, request.episode_id)
    broker_market = _broker_market_for_training(price, request)
    try:
        preview = _broker().preview(ticket, broker_market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    quantity = float((preview.get("ticket") or {}).get("requested_quantity") or 0.0)
    preview["risk_advisory"] = _risk_advisory(
        lab.account_summary(),
        symbol=price["symbol"],
        side=request.side,
        quantity=quantity,
        price=price["price"],
        industry=price.get("industry"),
    )
    preview["market"] = price
    preview["paper_training_fill"] = {
        "enabled": request.training_fill_at_latest_mark,
        "basis": "latest_verified_market_mark" if request.training_fill_at_latest_mark else None,
        "live_broker_submission": False,
    }
    return preview


@router.post("/paper-training/order")
def paper_training_order(request: PaperOrderRequest) -> dict[str, Any]:
    lab = _lab()
    price = _verified_price(request.symbol)
    episode_id = request.episode_id
    if not episode_id:
        episode_id = lab.start_episode(
            objective=request.rationale or f"{request.actor} paper experiment for {price['symbol']}",
            actor=request.actor,
            metadata={"auto_created": True},
        )["episode_id"]
    ticket = _ticket(request, price, episode_id)
    broker_market = _broker_market_for_training(price, request)
    try:
        preview = _broker().preview(ticket, broker_market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    quantity = float((preview.get("ticket") or {}).get("requested_quantity") or 0.0)
    advisory = _risk_advisory(
        lab.account_summary(),
        symbol=price["symbol"],
        side=request.side,
        quantity=quantity,
        price=price["price"],
        industry=price.get("industry"),
    )
    # This endpoint is the local PaperTrainingLab, never a live broker.  Risk
    # evidence remains durable training input, while the Paper Broker enforces
    # verifiable price, cash, owned-position and exchange-rule constraints.
    # Requiring production research/PIT validation here made routine Agent
    # paper experiments impossible even when the broker could safely simulate
    # the order, contradicting the published paper-training contract.
    risk_gate_enforced = False
    broker_started_monotonic = time.monotonic()
    try:
        broker_result = _broker().submit(ticket, broker_market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    order = broker_result.get("order") or {}
    order_slo = _record_runtime_slo(
        "order.lifecycle",
        latency_ms=(time.monotonic() - broker_started_monotonic) * 1000.0,
        success=str(order.get("status") or "") in {
            "submitted", "acknowledged", "open", "triggered", "partially_filled",
            "filled", "canceled", "rejected", "replaced", "expired",
        },
    )
    fill = order.get("fill") or (broker_result.get("oms") or {}).get("fill") or {}
    event_type = {
        "filled": "paper_order_filled",
        "open": "paper_order_open",
        "triggered": "paper_order_triggered",
        "canceled": "paper_order_canceled",
        "rejected": "paper_order_rejected",
    }.get(str(order.get("status")), "paper_order_submitted")
    lab.record_event(
        episode_id=episode_id,
        event_type=event_type,
        symbol=price["symbol"],
        action=request.side,
        price=float(fill.get("fill_price") or price["price"]),
        quantity=float(fill.get("quantity") or quantity),
        rationale=request.rationale,
        payload={
            "actor": request.actor,
            "order_id": order.get("order_id"),
            "order_type": request.order_type,
            "time_in_force": request.time_in_force,
            "lot_type": request.lot_type,
            "session": request.session,
            "market_price": price,
            "risk_advisory": advisory,
            "broker": broker_result,
        },
    )
    get_runtime_engine().pipeline.trade_store.store.save_trade(
        {
            "schema_version": "open_stock_ai.paper_broker_trade.v1",
            "symbol": price["symbol"],
            "action": request.side,
            "mode": "paper_training",
            "episode_id": episode_id,
            "risk_advisory": advisory,
            "broker": broker_result,
        }
    )
    lab.apply_market_mark(
        symbol=price["symbol"],
        market=price["market"],
        price=price["price"],
        price_source=price["price_source"],
        source_timestamp=price["source_timestamp"],
        is_realtime=price["is_realtime"],
        is_fallback=price["is_fallback"],
        metadata={"source_kind": price["source_kind"], "order_id": order.get("order_id")},
        episode_id=episode_id,
    )
    return {
        "schema_version": "open_stock_ai.paper_training_execution.v2",
        "episode_id": episode_id,
        "actor": request.actor,
        "market_price": price,
        "risk_advisory": advisory,
        "risk_gate_enforced": risk_gate_enforced,
        "research_gate_enforced": False,
        "paper_training_fill": {
            "enabled": request.training_fill_at_latest_mark,
            "basis": "latest_verified_market_mark" if request.training_fill_at_latest_mark else None,
            "live_broker_submission": False,
        },
        "broker": broker_result,
        "slo_observation": order_slo,
        "oms": broker_result.get("oms"),
        "account": paper_training_account(),
        "execution_boundary": "local_paper_broker_only_no_live_submission",
    }


@router.get("/paper-training/orders")
def paper_training_orders(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    items = _broker().recent_orders(status=status, limit=limit)
    return {"schema_version": "open_stock_ai.paper_broker_orders.v1", "count": len(items), "items": items}


@router.get("/paper-training/fills")
def paper_training_fills(limit: int = 100) -> dict[str, Any]:
    items = _broker().recent_fills(limit=limit)
    return {"schema_version": "open_stock_ai.paper_broker_fills.v1", "count": len(items), "items": items}


@router.post("/paper-training/orders/{order_id}/cancel")
def paper_training_cancel_order(order_id: str, request: PaperCancelRequest) -> dict[str, Any]:
    try:
        result = _broker().cancel(order_id, reason=request.reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    episode_id = str((result.get("order") or {}).get("episode_id") or "")
    if episode_id:
        _lab().record_event(
            episode_id=episode_id,
            event_type="paper_order_canceled",
            symbol=(result.get("order") or {}).get("symbol"),
            action=(result.get("order") or {}).get("side"),
            rationale=request.reason,
            payload={"broker": result},
        )
    return {**result, "account": paper_training_account()}


@router.post("/paper-training/orders/{order_id}/replace")
def paper_training_replace_order(order_id: str, request: PaperReplaceRequest) -> dict[str, Any]:
    broker = _broker()
    try:
        original = broker.get_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    original_order = original.get("order") or {}
    try:
        price = _verified_price(str(original_order.get("symbol") or ""))
        result = broker.replace(
            order_id,
            request.model_dump(exclude_none=True),
            price,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    episode_id = str((result.get("order") or {}).get("episode_id") or "")
    if episode_id:
        _lab().record_event(
            episode_id=episode_id,
            event_type="paper_order_replaced",
            symbol=(result.get("order") or {}).get("symbol"),
            action=(result.get("order") or {}).get("side"),
            rationale=str(request.rationale or "replace_order"),
            payload={"replaced_order_id": order_id, "broker": result},
        )
    return {**result, "account": paper_training_account()}


@router.post("/paper-training/mark-to-market")
def paper_training_mark_to_market() -> dict[str, Any]:
    lab = _lab()
    broker = _broker()
    account_before = lab.account_summary()
    position_symbols = {str(item.get("symbol") or "") for item in account_before.get("positions") or []}
    symbols = sorted((position_symbols | set(broker.open_symbols())) - {""})
    marks: list[dict[str, Any]] = []
    order_updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for symbol in symbols:
        try:
            price = _verified_price(symbol)
            if symbol in position_symbols:
                marks.append(
                    lab.apply_market_mark(
                        symbol=price["symbol"],
                        market=price["market"],
                        price=price["price"],
                        price_source=price["price_source"],
                        source_timestamp=price["source_timestamp"],
                        is_realtime=price["is_realtime"],
                        is_fallback=price["is_fallback"],
                        metadata={"source_kind": price["source_kind"], "reason": "portfolio_mark_to_market"},
                    )
                )
            tick_result = broker.process_market_tick(price["symbol"], price)
            order_updates.extend(tick_result.get("results") or [])
        except (HTTPException, ValueError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            errors.append({"symbol": symbol, "error": str(detail)})
    return {
        "schema_version": "open_stock_ai.paper_mark_to_market_batch.v2",
        "updated_count": len(marks),
        "processed_order_count": len(order_updates),
        "error_count": len(errors),
        "marks": [
            {
                key: item.get(key)
                for key in ("symbol", "market", "price", "price_source", "source_timestamp", "is_realtime", "is_fallback")
            }
            for item in marks
        ],
        "order_updates": order_updates,
        "errors": errors,
        "account": paper_training_account(refresh_prices=False),
    }


@router.get("/paper-training/learning")
def paper_training_learning(limit: int = 20) -> dict[str, Any]:
    return _lab().learning_summary(limit=limit)


@router.post("/paper-training/evaluate")
def paper_training_evaluate(request: EpisodeEvaluationRequest) -> dict[str, Any]:
    try:
        return _lab().evaluate_episode(request.episode_id, close=request.close)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/paper-training/reflection")
def paper_training_reflection(request: PaperReflectionRequest) -> dict[str, Any]:
    try:
        return _lab().save_reflection(
            episode_id=request.episode_id,
            summary=request.summary,
            lessons=request.lessons,
            next_rules=request.next_rules,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/paper-training/policy-proposals/{proposal_id}/evaluate")
def policy_proposal_evaluate(proposal_id: str, request: PolicyEvaluationRequest) -> dict[str, Any]:
    try:
        return _promotion_gate().evaluate(proposal_id, request.evidence, request.risk_review)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/paper-training/policy-proposals/{proposal_id}/shadow")
def policy_proposal_begin_shadow(proposal_id: str) -> dict[str, Any]:
    try:
        return _promotion_gate().begin_shadow(proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/paper-training/policy-proposals/{proposal_id}/shadow-result")
def policy_proposal_shadow_result(proposal_id: str, request: PolicyShadowResultRequest) -> dict[str, Any]:
    try:
        return _promotion_gate().record_shadow(proposal_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/paper-training/policy-proposals/{proposal_id}/promote")
def policy_proposal_promote(proposal_id: str, request: PolicyPromotionRequest) -> dict[str, Any]:
    try:
        return _promotion_gate().promote(proposal_id, approved_by=request.approved_by)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/paper-training/governance/artifacts")
def paper_training_artifact_governance() -> dict[str, Any]:
    """Show the durable artifact activation lanes without changing state."""

    return _artifact_governance_status()


@router.post("/paper-training/governance/artifacts/rollback")
def paper_training_artifact_rollback(request: ArtifactRollbackRequest) -> dict[str, Any]:
    """Restore the previous approved artifact inside the selected lane only."""

    try:
        receipt = _artifact_rollback_registry().rollback(
            artifact_scope=request.artifact_scope,
            reason=request.reason,
            approved_by=request.approved_by,
        )
    except ArtifactRollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"receipt": receipt.as_dict(), "governance": _artifact_governance_status()}
