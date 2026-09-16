from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Body, HTTPException

from stock_ai.agent_trading_api import router as agent_trading_router
from stock_ai.paper_training_api import router as paper_training_router

from open_stock_ai.agent_workspace import (
    build_agent_portfolio,
    build_agent_tool_manifest,
    build_agent_watchlist,
    build_agent_workspace,
)
from open_stock_ai.main import (
    broker_import_governance_to_dict,
    decision_logs_to_dict,
    decision_review_to_dict,
    external_source_lock_to_dict,
    external_sources_to_dict,
    integration_audit_to_dict,
    optional_external_sources_to_dict,
    paper_exposure_to_dict,
    paper_orders_to_dict,
    session_to_dict,
    signals_to_dict,
    storage_stats_to_dict,
)
from open_stock_ai.runtime import get_runtime_engine
from open_stock_ai.types import MissingSymbolError, StockRequest


router = APIRouter(prefix="/api/open-stock-ai", tags=["Open Stock AI"])
router.include_router(paper_training_router)
router.include_router(agent_trading_router)


def analyze_to_dict(
    symbol: str | None = None,
    market: str = "TW",
    horizon: str = "swing",
) -> dict:
    """Compatibility seam that reuses the configuration-aware runtime Engine."""
    if not str(symbol or "").strip():
        raise MissingSymbolError("analyze_to_dict requires an explicit symbol")
    request = StockRequest(symbol=symbol, market=market, horizon=horizon)  # type: ignore[arg-type]
    return asdict(get_runtime_engine().analyze_stock(request))


@router.get("/analyze")
def analyze(
    symbol: str,
    market: Literal["TW", "US", "CRYPTO"] = "TW",
    horizon: Literal["intraday", "swing", "weekly", "monthly"] = "swing",
) -> dict:
    try:
        return analyze_to_dict(symbol=symbol, market=market, horizon=horizon)
    except MissingSymbolError as exc:
        # A blank symbol is a client validation error, never an internal server
        # failure.  This also keeps API clients aligned with the UI boundary.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agent/tool-manifest")
def agent_tool_manifest() -> dict:
    return build_agent_tool_manifest()


@router.get("/agent/portfolio")
def agent_portfolio() -> dict:
    return build_agent_portfolio()


@router.get("/agent/workspace")
def agent_workspace(
    symbol: str,
    market: Literal["TW", "US", "CRYPTO"] = "TW",
    horizon: Literal["intraday", "swing", "weekly", "monthly"] = "swing",
) -> dict:
    return build_agent_workspace(symbol=symbol, market=market, horizon=horizon)


@router.get("/agent/watchlist")
def agent_watchlist(
    horizon: Literal["intraday", "swing", "weekly", "monthly"] = "swing",
    limit: int = 20,
) -> dict:
    return build_agent_watchlist(horizon=horizon, limit=max(1, min(limit, 100)))


@router.get("/paper-account")
def paper_account() -> dict:
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    if trade_store is None:
        return {
            "schema_version": "open_stock_ai.paper_account.v1",
            "mode": "paper",
            "is_simulated": True,
            "available": False,
            "positions": [],
            "execution_boundary": "local_paper_only_no_broker_submission",
        }
    return {**trade_store.account_summary(), "available": True}


@router.get("/paper-oms/orders")
def paper_oms_orders(limit: int = 20) -> dict:
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    items = trade_store.recent_oms_orders(limit=max(1, min(limit, 100))) if trade_store is not None else []
    return {
        "schema_version": "open_stock_ai.paper_oms_order_ledger.v1",
        "method": "paper_oms_order_state_ledger",
        "count": len(items),
        "items": items,
        "execution_boundary": "read_only_local_paper_ledger",
    }


@router.post("/paper-account/corporate-actions/sync")
def paper_corporate_actions_sync(payload: dict = Body(default_factory=dict)) -> dict:
    trade_store = get_runtime_engine().pipeline.trade_store
    if trade_store is None:
        raise HTTPException(status_code=503, detail="paper_account_unavailable")
    try:
        return trade_store.sync_corporate_actions(
            symbol=str(payload.get("symbol") or "").strip() or None,
            as_of=str(payload.get("as_of") or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/session/{session}")
def session(session: Literal["pre_market", "intraday", "after_market"]) -> dict:
    return session_to_dict(session=session)


@router.get("/external-sources")
def external_sources() -> dict:
    return external_sources_to_dict()


@router.get("/external-source-lock")
def external_source_lock() -> dict:
    return external_source_lock_to_dict()


@router.get("/broker-import-governance")
def broker_import_governance() -> dict:
    return broker_import_governance_to_dict()


@router.get("/optional-external-sources")
def optional_external_sources() -> dict:
    return optional_external_sources_to_dict()


@router.get("/storage")
def storage() -> dict:
    return storage_stats_to_dict()


@router.get("/signals")
def signals(limit: int = 20) -> dict:
    return signals_to_dict(limit=max(1, min(limit, 100)))


@router.get("/decision-log")
def decision_log(limit: int = 20) -> dict:
    return decision_logs_to_dict(limit=max(1, min(limit, 100)))


@router.get("/decision-review")
def decision_review(symbol: str | None = None, limit: int = 100) -> dict:
    normalized_symbol = symbol.strip() if symbol else None
    return decision_review_to_dict(symbol=normalized_symbol, limit=max(1, min(limit, 500)))


@router.get("/paper-orders")
def paper_orders(limit: int = 20) -> dict:
    return paper_orders_to_dict(limit=max(1, min(limit, 100)))


@router.get("/paper-exposure")
def paper_exposure() -> dict:
    return paper_exposure_to_dict()


@router.get("/integration-audit")
def integration_audit(symbol: str | None = None, limit: int = 100) -> dict:
    normalized_symbol = symbol.strip() if symbol else None
    return integration_audit_to_dict(symbol=normalized_symbol, limit=max(1, min(limit, 500)))
