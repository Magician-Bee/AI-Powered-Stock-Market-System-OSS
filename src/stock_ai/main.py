import asyncio
import logging
import os
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
import tomllib
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .codex_api import router as codex_router
from .broker_api import router as broker_router
from .data_platform.api import router as data_platform_router
from .data_platform.gateway import get_news_history_coverage_store
from .data_platform.ui_api import ui_data_route
from .market_intelligence.api import router as market_intelligence_router
from .ui_contract import router as ui_contract_router
from .autonomous_trading_api import router as autonomous_trading_router
from .autonomous_trading_service import autonomous_trading_loop
from .agent_api import router as agent_router
from .agent_event_bus import agent_event_bus
from .agent_service import get_agent_run_runtime
from .codex_runtime import codex_runtime
from .models import NotificationSendRequest, QueryRequest, QueryResponse, ScreenerRequest
from .screener_conditions import ScreenerConditionError
from .intraday_candles import (
    IntradayCandleError,
    InvalidIntradayTimeframe,
    get_intraday_candle_store,
    intraday_candle_query,
    intraday_candle_status,
)
from .daily_history import DailyHistoryQueryError
from .liquidity import LiquidityAssessmentError, assess_symbol_liquidity
from .monthly_revenue import (
    MonthlyRevenueHistoryError,
    MonthlyRevenueSourceError,
    backfill_monthly_revenue_history,
    query_monthly_revenue_history,
)
from .income_statement import (
    IncomeStatementHistoryError,
    IncomeStatementSourceError,
    backfill_income_statement_history,
    query_income_statement_history,
)
from .balance_sheet import (
    BalanceSheetHistoryError,
    BalanceSheetSourceError,
    backfill_balance_sheet_history,
    query_balance_sheet_history,
)
from .cash_flow_statement import (
    CashFlowHistoryError,
    CashFlowSourceError,
    backfill_cash_flow_history,
    query_cash_flow_history,
)
from .financial_ratios import (
    FinancialRatioHistoryError,
    query_financial_ratio_history,
)
from .financial_revisions import (
    FinancialRevisionHistoryError,
    query_financial_revision_history,
)
from .industry_metrics import IndustryMetricsError, query_industry_metrics
from .guidance_tracking import GuidanceTrackingError, query_financial_guidance
from .financial_anomalies import query_financial_anomalies
from .basic_valuation import BasicValuationError, query_basic_valuation
from .valuation_percentiles import (
    ValuationPercentileError,
    query_valuation_percentiles,
)
from .peer_comparison import PeerComparisonError, query_peer_comparison
from .dcf_valuation import DCFValuationError, build_dcf_valuation
from .valuation_model_policy import (
    ValuationModelPolicyError,
    build_valuation_model_policy,
)
from .growth_metrics import GrowthHistoryError, query_growth_history
from .trading_anomalies import (
    TradingAnomalyError,
    TradingAnomalyStore,
    scan_symbol_anomalies,
)
from .local_security import local_runtime_security
from .mvp_features import get_news_center, get_notification_channels, get_notification_previews, get_watchlist_overview, send_notification
from .official_derivatives import (
    import_taifex_derivatives,
    import_taifex_derivatives_csv,
    import_tdcc_holding_distribution,
    import_tdcc_holding_distribution_csv,
    official_derivatives_status,
    taifex_derivatives_summary_contract,
    tdcc_holding_distribution_contract,
)
from .official_events import (
    import_mops_company_events,
    import_mops_company_events_csv,
    official_events_status,
)
from .schedule_guard import schedule_guard_status
from .source_policy import evaluate_source_policy, source_policy_status
from .phase1_data import (
    generate_daily_report,
    list_institutional_flows,
    list_margin_trading,
    list_monthly_revenues,
    list_securities_master,
    securities_master_status,
)
from .chip_history import query_chip_history
from .short_daytrade_history import query_short_daytrade_history
from .tdcc_holding_history import query_tdcc_holding_history
from .query import answer_question
from .universe import UniverseResolutionError, resolve_universe
from .realtime_quotes import RealtimeNotConfigured, RealtimeProviderError, fetch_realtime_quote, sse_stream, status as realtime_status
from .services import (
    clear_market_event_caches,
    explain_linkage,
    get_ai_trading_workspace,
    get_asset_workspace,
    get_market_detail_summary,
    get_market_events,
    get_market_overview,
    get_market_summary,
    get_overview_indices,
    get_price_history,
    get_price_history_payload,
    get_risk_workspace,
    get_trading_workspace,
    list_supported_sources,
    load_catalog,
    run_screener,
    search_entities,
)
from .system_contract import stock_app_requirement_contract
from .update_runner import build_update_plan, dry_run_update
from .twse_openapi import fetch_twse_openapi_path, search_twse_openapi
from open_stock_ai.api import router as open_stock_ai_router
from open_stock_ai.config.settings import load_settings as load_open_stock_ai_settings
from open_stock_ai.governance import capability_status
from open_stock_ai.governance.promotion_ladder import SQLitePromotionReceiptStore
from open_stock_ai.runtime import get_runtime_engine
from open_stock_ai.types import UniverseRequest, UniverseSnapshot

settings = get_settings()
logger = logging.getLogger(__name__)


async def _retention_maintenance_loop(scheduler) -> None:
    """Keep bounded retention current without relying on an Agent request."""

    while True:
        try:
            scheduler.run_due()
        except Exception:  # keep the local UI alive; the next interval retries
            logger.exception("Durable retention maintenance pass failed")
        await asyncio.sleep(scheduler.interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .autonomous_deployment import initialize_source_snapshot
    initialize_source_snapshot(capture_stage="asgi_lifespan_start")
    runtime_governance = get_runtime_engine(load_open_stock_ai_settings()).governance
    retention_task = None
    if runtime_governance is not None:
        retention_task = asyncio.create_task(
            _retention_maintenance_loop(runtime_governance.retention_maintenance),
            name="stock-ai-retention-maintenance",
        )
    await get_agent_run_runtime().start_background()
    autonomy_task = asyncio.create_task(autonomous_trading_loop(), name="stock-ai-autonomous-trading")
    try:
        yield
    finally:
        autonomy_task.cancel()
        with suppress(asyncio.CancelledError):
            await autonomy_task
        if retention_task is not None:
            retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await retention_task
        await get_agent_run_runtime().close()
        await codex_runtime.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="股市 AI 完整生態系 MVP：API + Web GUI",
    lifespan=lifespan,
)
# The desktop shell is served from 127.0.0.1.  A few latency-sensitive local
# data views use the equivalent localhost origin so their request queue is not
# blocked by long-lived dashboard streams.  Keep that alias tightly scoped to
# the two loopback origins and require the existing runtime-session header.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Stock-AI-Session", "Content-Type"],
)

STATIC_DIR = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _is_critical_slo_api_request(method: str, path: str) -> bool:
    """Keep durable API SLO evidence on state-changing command paths only."""

    return str(method).upper() in {"POST", "PUT", "PATCH", "DELETE"} and str(path).startswith((
        "/api/agents/run",
        "/agent/paper-training",
        "/api/open-stock-ai/agent/paper-training",
        "/api/data/observability/notify",
    ))


@app.middleware("http")
async def disable_ui_asset_cache(request, call_next):
    origin = request.headers.get("origin", "")
    is_loopback_preflight = (
        request.method == "OPTIONS"
        and origin in {"http://127.0.0.1:8000", "http://localhost:8000"}
    )
    if not is_loopback_preflight:
        denied = await local_runtime_security.authorize_async(request)
        if denied is not None:
            return denied
    observe_api = _is_critical_slo_api_request(request.method, request.url.path) and "/stream" not in request.url.path
    started_monotonic = time.monotonic() if observe_api else None
    try:
        response = await call_next(request)
    except Exception:
        if started_monotonic is not None:
            try:
                get_agent_run_runtime().record_slo_observation(
                    "api.request",
                    latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
                    success=False,
                )
            except Exception:
                logger.exception("Unable to persist failed API SLO observation")
        raise
    if started_monotonic is not None:
        try:
            observation = get_agent_run_runtime().record_slo_observation(
                "api.request",
                latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
                success=response.status_code < 500,
            )
            response.headers["X-Stock-AI-SLO-API"] = str(observation["status"])
        except Exception:
            # API responses must not be rewritten after their business action
            # has completed.  Preserve the failure visibly in the local log
            # and let the SLO dashboard remain fail-closed without a sample.
            logger.exception("Unable to persist API SLO observation")
            response.headers["X-Stock-AI-SLO-API"] = "recording_failed"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.include_router(open_stock_ai_router)
app.include_router(codex_router)
app.include_router(agent_router)
app.include_router(data_platform_router)
app.include_router(broker_router)
app.include_router(market_intelligence_router)
app.include_router(ui_contract_router)
app.include_router(autonomous_trading_router)


@app.get("/", include_in_schema=False)
def index():
    source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    runtime_bootstrap = (
        f'<meta name="stock-ai-runtime-session" content="{local_runtime_security.token}" />'
        '<script src="/static/js/core/runtime-session.js?v=20260718-local-security-v2"></script>'
    )
    source = source.replace("</head>", f"  {runtime_bootstrap}\n</head>", 1)
    return HTMLResponse(source, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='8' fill='#0f172a'/>"
        "<rect x='2' y='2' width='28' height='28' rx='7' fill='#2563eb'/>"
        "<text x='16' y='21' text-anchor='middle' font-size='13' "
        "font-family='Arial' font-weight='700' fill='#f8fafc'>TW</text>"
        "</svg>"
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "system_id": "stock-ai-system",
        "app": settings.app_name,
        "environment": settings.app_env,
        "build_commit": os.getenv("STOCK_AI_BUILD_COMMIT", "working-copy"),
        "instance_id": os.getenv("STOCK_AI_INSTANCE_ID", "untracked"),
        "project_root": os.getenv("STOCK_AI_PROJECT_ROOT", str(settings.project_root)),
    }


def _system_settings_overview() -> dict[str, object]:
    """Collect non-secret system page data once, then expose focused views."""
    codex_home = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    skill_roots = [
        ("Codex", codex_home / "skills"),
        ("專案", settings.project_root / "skills"),
        ("AI-Trader", settings.project_root / "external" / "AI-Trader" / "skills"),
    ]
    skill_items: list[dict[str, str]] = []
    seen_skill_paths: set[Path] = set()
    for source, root in skill_roots:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*/SKILL.md")):
            resolved = skill_file.resolve()
            if resolved in seen_skill_paths:
                continue
            seen_skill_paths.add(resolved)
            skill_items.append({"name": skill_file.parent.name, "source": source})

    mcp_names: list[str] = []
    config_path = codex_home / "config.toml"
    if config_path.is_file():
        try:
            with config_path.open("rb") as stream:
                config = tomllib.load(stream)
            servers = config.get("mcp_servers") or config.get("mcpServers") or {}
            if isinstance(servers, dict):
                mcp_names = sorted(str(name) for name in servers)
        except (OSError, tomllib.TOMLDecodeError):
            mcp_names = []

    quote_status = realtime_status().as_dict()
    notification_status = get_notification_channels()
    notification_ready = sum(1 for item in notification_status["items"] if item["configured"])
    return {
        "skills": {
            "count": len(skill_items),
            "items": skill_items[:24],
            "roots": [str(root) for _, root in skill_roots if root.is_dir()],
        },
        "mcp": {
            "count": len(mcp_names),
            "items": mcp_names,
            "bridge_ready": True,
            "transport": "Codex App Server",
            "config_detected": config_path.is_file(),
        },
        "apis": [
            {
                "name": "台股即時行情",
                "provider": quote_status["provider"],
                "configured": quote_status["configured"],
                "detail": quote_status["message"],
            },
            {
                "name": "TWSE / TPEx 官方資料",
                "provider": "OpenAPI",
                "configured": True,
                "detail": "公開資料連線，不需要 API key。",
            },
            {
                "name": "通知服務",
                "provider": "Telegram / LINE",
                "configured": notification_ready > 0,
                "detail": f"{notification_ready}/{notification_status['count']} 個通道已設定；金鑰只由後端環境讀取。",
            },
            {
                "name": "Open Stock AI",
                "provider": "Local API",
                "configured": True,
                "detail": "本機策略、風控與研究 API 已掛載。",
            },
        ],
        "security": "只回傳是否設定，不回傳 API key、Token 或帳號憑證。",
    }


@app.get("/api/system/settings-overview")
def system_settings_overview():
    """Compatibility summary for diagnostics; UI pages use focused endpoints."""
    return _system_settings_overview()


@app.get("/api/system/capability-status")
def system_capability_status():
    """Return the one machine-readable execution/promotion authority."""
    open_stock_ai_settings = load_open_stock_ai_settings()
    promotion_store = None
    database_path = Path(open_stock_ai_settings.sqlite_path).expanduser()
    # The capability endpoint is also the first governance read on a fresh
    # installation.  Initialising the configured file-backed store here makes
    # the promotion authority durable from first boot instead of reporting a
    # misleading "store not configured" state until another subsystem happens
    # to create the SQLite file.  In-memory databases remain explicitly
    # non-durable and therefore fail closed.
    if str(database_path) != ":memory:":
        promotion_store = SQLitePromotionReceiptStore(database_path)
    status = capability_status(
        requested_mode=open_stock_ai_settings.requested_execution_mode,
        requested_live_trading_enabled=open_stock_ai_settings.requested_live_trading_enabled,
        promotion_store=promotion_store,
    )
    runtime_governance = get_runtime_engine(open_stock_ai_settings).governance
    status["runtime_governance"] = (
        runtime_governance.status()
        if runtime_governance is not None
        else {"durable": False, "authorities": {}}
    )
    return status


@app.get("/api/system/settings/skills")
def system_skills_settings():
    """Skills／MCP page data only, without account or API configuration."""
    overview = _system_settings_overview()
    return {
        "skills": overview["skills"],
        "mcp": overview["mcp"],
        "security": overview["security"],
    }


@app.get("/api/system/settings/connections")
def system_connection_settings():
    """Data API and notification health only, without model or tool details."""
    overview = _system_settings_overview()
    return {
        "apis": overview["apis"],
        "security": overview["security"],
    }


@app.get(ui_data_route("catalog"))
@app.get("/api/catalog")
def catalog():
    return load_catalog()


@app.get("/api/system/requirements")
def system_requirements():
    return stock_app_requirement_contract()


@app.get("/api/system/schedule")
def system_schedule(
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
):
    return schedule_guard_status(
        as_of=as_of,
        broker_api_connected=broker_api_connected,
        authorized_realtime_feed=authorized_realtime_feed,
        live_ordering_enabled=live_ordering_enabled,
    )


@app.get("/api/system/source-policy")
def system_source_policy():
    return source_policy_status()


@app.post("/api/system/source-policy/evaluate")
def system_source_policy_evaluate(payload: dict = Body(default_factory=dict)):
    return evaluate_source_policy(payload)


@app.get("/api/system/official-derivatives")
def system_official_derivatives():
    return official_derivatives_status()


@app.get("/api/system/official-events")
def system_official_events(symbol: str | None = None):
    return official_events_status(symbol=symbol)


@app.get("/api/system/update-plan")
def system_update_plan(
    phase: str | None = None,
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
):
    return build_update_plan(
        phase=phase,
        as_of=as_of,
        broker_api_connected=broker_api_connected,
        authorized_realtime_feed=authorized_realtime_feed,
        live_ordering_enabled=live_ordering_enabled,
    )


@app.post("/api/system/update-run")
def system_update_run(
    phase: str | None = None,
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
):
    return dry_run_update(
        phase=phase,
        as_of=as_of,
        broker_api_connected=broker_api_connected,
        authorized_realtime_feed=authorized_realtime_feed,
        live_ordering_enabled=live_ordering_enabled,
    )


@app.get(ui_data_route("securities_master"))
@app.get("/api/securities/master")
def securities_master(q: str = "", market: str = "all", limit: int = 200):
    sync = securities_master_status()
    items = list_securities_master(
        q=q,
        market=market,
        limit=max(1, min(limit, 5000)),
        include_lifecycle=True,
    )
    return {
        "count": len(items),
        "items": [item.model_dump() for item in items],
        "sync": sync,
    }


@app.post(ui_data_route("securities_master_refresh"))
@app.post("/api/securities/master/refresh")
def refresh_securities_master():
    return securities_master_status(refresh=True)


@app.get("/api/watchlist/default")
def default_watchlist(limit: int = 10):
    del limit
    return {
        "count": 0,
        "items": [],
        "universe": {
            "source": "none",
            "symbols": [],
            "count": 0,
            "message": "No default watchlist is configured; add a user watchlist explicitly.",
        },
    }


@app.get(ui_data_route("watchlist_overview"))
@app.get("/api/watchlist/overview")
def watchlist_overview(limit: int = 10):
    return get_watchlist_overview(limit=max(1, min(limit, 20)))


@app.get(ui_data_route("sources"))
@app.get("/api/sources")
def sources():
    return list_supported_sources()


@app.get(ui_data_route("overview_indices"))
@app.get("/api/overview/indices")
def overview_indices():
    return get_overview_indices()


@app.get("/api/twse/openapi/inventory")
def twse_openapi_inventory(q: str = ""):
    return search_twse_openapi(q)


@app.get("/api/twse/openapi/fetch")
def twse_openapi_fetch(path: str, limit: int = 100):
    try:
        return fetch_twse_openapi_path(path, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(ui_data_route("realtime_status"))
@app.get("/api/realtime/status")
def realtime_marketdata_status():
    return realtime_status().as_dict()


@app.get(ui_data_route("realtime_quote"))
@app.get("/api/realtime/quote/{symbol}")
async def realtime_quote(symbol: str):
    try:
        payload = await fetch_realtime_quote(symbol)
        if isinstance(payload.get("data"), dict):
            get_intraday_candle_store().record_quote(payload["data"])
        return payload
    except RealtimeNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RealtimeProviderError as exc:
        # Reachability is a transient market-data state, not a broken detail
        # page. Keep the labeled official close visible without presenting it
        # as realtime or generating a failed browser request.
        return {
            "provider": realtime_status().provider,
            "available": False,
            "degraded": True,
            "symbol": symbol,
            "data": None,
            "message": str(exc),
        }


@app.get(ui_data_route("realtime_stream"))
@app.get("/api/realtime/stream/{symbol}")
def realtime_stream(symbol: str, channels: str | None = None):
    selected = [c.strip() for c in channels.split(",") if c.strip()] if channels else None
    try:
        # Validate before returning StreamingResponse so missing credentials fail immediately.
        st = realtime_status()
        if not st.enabled:
            raise RealtimeNotConfigured(st.message)
        return StreamingResponse(
            sse_stream(symbol, selected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    except RealtimeNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(ui_data_route("intraday_candle_status"))
@app.get("/api/intraday/candles/status")
def intraday_candles_status():
    return intraday_candle_status()


@app.get(ui_data_route("intraday_candle_dates"))
@app.get("/api/intraday/candles/{symbol}/dates")
def intraday_candles_dates(symbol: str):
    try:
        return get_intraday_candle_store().available_dates(symbol)
    except IntradayCandleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(ui_data_route("intraday_candles"))
@app.get("/api/intraday/candles/{symbol}")
async def intraday_candles(
    symbol: str,
    date: str | None = None,
    timeframe: int = 1,
    refresh: bool = False,
    as_of: datetime | None = None,
):
    try:
        return await intraday_candle_query(
            symbol,
            trading_date=date,
            timeframe=timeframe,
            refresh=refresh,
            as_of=as_of,
        )
    except (IntradayCandleError, InvalidIntradayTimeframe, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(ui_data_route("entities_search"))
@app.get("/api/entities/search")
def entities_search(q: str = ""):
    items = search_entities(q)
    return {"items": [e.model_dump() for e in items], "count": len(items)}


@app.get(ui_data_route("market_summary"))
@app.get("/api/market/{symbol}/summary")
def market_summary(symbol: str):
    summary = get_market_detail_summary(symbol)
    if not summary:
        raise HTTPException(status_code=404, detail=f"No market summary for symbol: {symbol}")
    agent_event_bus.publish(
        "market.quote.updated",
        {"symbol": symbol, "summary": summary},
    )
    return summary


@app.get(ui_data_route("market_history"))
@app.get("/api/market/{symbol}/history")
def market_history(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int = 5000,
    refresh: bool | None = None,
    allow_fallback: bool = True,
    price_basis: str = "unadjusted",
    refresh_adjustments: bool | None = None,
    require_complete_adjustment: bool = True,
):
    try:
        payload = get_price_history_payload(
            symbol,
            start=start,
            end=end,
            cursor=cursor,
            limit=limit,
            refresh=refresh,
            allow_fallback=allow_fallback,
            price_basis=price_basis,
            refresh_adjustments=refresh_adjustments,
            require_complete_adjustment=require_complete_adjustment,
        )
    except DailyHistoryQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        **payload,
        "points": [point.model_dump() for point in payload["points"]],
    }


@app.get(ui_data_route("institutional_flow"))
@app.get("/api/flow/institutional")
def institutional_flow(symbol: str | None = None, date: str | None = None, limit: int = 100):
    items = list_institutional_flows(symbol=symbol, date=date, limit=max(1, min(limit, 2000)))
    return {"count": len(items), "items": [item.model_dump() for item in items]}


@app.get(ui_data_route("margin"))
@app.get("/api/flow/margin")
def margin_trading(symbol: str | None = None, limit: int = 100):
    items = list_margin_trading(symbol=symbol, limit=max(1, min(limit, 2000)))
    return {"count": len(items), "items": [item.model_dump() for item in items]}


@app.get(ui_data_route("chip_history"))
@app.get("/api/flow/chip/history")
def chip_history(symbol: str, days: int = 20, refresh: bool = False):
    return query_chip_history(symbol, days=days, refresh=refresh)


@app.get(ui_data_route("short_daytrade_history"))
@app.get("/api/flow/chip/short-daytrade")
def short_daytrade_history(
    symbol: str,
    days: int = 14,
    refresh: bool = False,
):
    try:
        return query_short_daytrade_history(
            _paper_trade_store().store,
            symbol,
            days=max(1, min(days, 60)),
            refresh=refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("tdcc_holding_history"))
@app.get("/api/flow/chip/tdcc-history")
def tdcc_holding_history(
    symbol: str,
    weeks: int = 52,
    refresh: bool = False,
):
    try:
        return query_tdcc_holding_history(
            symbol,
            weeks=max(1, min(weeks, 104)),
            refresh=refresh,
            db_path=_paper_trade_store().store.path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("revenue"))
@app.get("/api/fundamentals/revenue")
def monthly_revenue(symbol: str | None = None, limit: int = 100):
    items = list_monthly_revenues(symbol=symbol, limit=max(1, min(limit, 2000)))
    return {"count": len(items), "items": [item.model_dump() for item in items]}


@app.get(ui_data_route("revenue_history"))
@app.get("/api/fundamentals/revenue/history")
def monthly_revenue_history(
    symbol: str,
    start_period: str = "2010-01",
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = 240,
):
    try:
        return query_monthly_revenue_history(
            symbol,
            start_period=start_period,
            end_period=end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=max(1, min(limit, 240)),
        )
    except MonthlyRevenueHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(ui_data_route("revenue_history_sync"))
@app.post("/api/fundamentals/revenue/history/sync")
def monthly_revenue_history_sync(payload: dict = Body(default_factory=dict)):
    try:
        return backfill_monthly_revenue_history(
            str(payload.get("symbol") or ""),
            start_period=str(payload.get("start_period") or "2010-01"),
            end_period=(
                str(payload["end_period"])
                if payload.get("end_period")
                else None
            ),
            market_segment=(
                str(payload["market_segment"])
                if payload.get("market_segment")
                else None
            ),
            force_refresh=bool(payload.get("force_refresh", False)),
            max_workers=int(payload.get("max_workers") or 4),
        )
    except (
        MonthlyRevenueHistoryError,
        MonthlyRevenueSourceError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("income_statement_history"))
@app.get("/api/fundamentals/income-statement/history")
def income_statement_history(
    symbol: str,
    start_period: str = "2013-Q1",
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = 64,
):
    try:
        return query_income_statement_history(
            symbol,
            start_period=start_period,
            end_period=end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=max(1, min(limit, 64)),
        )
    except IncomeStatementHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(ui_data_route("income_statement_history_sync"))
@app.post("/api/fundamentals/income-statement/history/sync")
def income_statement_history_sync(payload: dict = Body(default_factory=dict)):
    try:
        return backfill_income_statement_history(
            str(payload.get("symbol") or ""),
            start_period=str(payload.get("start_period") or "2013-Q1"),
            end_period=(
                str(payload["end_period"])
                if payload.get("end_period")
                else None
            ),
            market_segment=(
                str(payload["market_segment"])
                if payload.get("market_segment")
                else None
            ),
            force_refresh=bool(payload.get("force_refresh", False)),
            max_workers=int(payload.get("max_workers") or 4),
        )
    except (
        IncomeStatementHistoryError,
        IncomeStatementSourceError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("balance_sheet_history"))
@app.get("/api/fundamentals/balance-sheet/history")
def balance_sheet_history(
    symbol: str,
    start_period: str = "2013-Q1",
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = 64,
):
    try:
        return query_balance_sheet_history(
            symbol,
            start_period=start_period,
            end_period=end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=max(1, min(limit, 64)),
        )
    except BalanceSheetHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(ui_data_route("balance_sheet_history_sync"))
@app.post("/api/fundamentals/balance-sheet/history/sync")
def balance_sheet_history_sync(payload: dict = Body(default_factory=dict)):
    try:
        return backfill_balance_sheet_history(
            str(payload.get("symbol") or ""),
            start_period=str(payload.get("start_period") or "2013-Q1"),
            end_period=(
                str(payload["end_period"])
                if payload.get("end_period")
                else None
            ),
            market_segment=(
                str(payload["market_segment"])
                if payload.get("market_segment")
                else None
            ),
            force_refresh=bool(payload.get("force_refresh", False)),
            max_workers=int(payload.get("max_workers") or 4),
        )
    except (
        BalanceSheetHistoryError,
        BalanceSheetSourceError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("cash_flow_history"))
@app.get("/api/fundamentals/cash-flow/history")
def cash_flow_history(
    symbol: str,
    start_period: str = "2013-Q1",
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = 64,
):
    try:
        return query_cash_flow_history(
            symbol,
            start_period=start_period,
            end_period=end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=max(1, min(limit, 64)),
        )
    except CashFlowHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(ui_data_route("cash_flow_history_sync"))
@app.post("/api/fundamentals/cash-flow/history/sync")
def cash_flow_history_sync(payload: dict = Body(default_factory=dict)):
    try:
        return backfill_cash_flow_history(
            str(payload.get("symbol") or ""),
            start_period=str(payload.get("start_period") or "2013-Q1"),
            end_period=(
                str(payload["end_period"])
                if payload.get("end_period")
                else None
            ),
            market_segment=(
                str(payload["market_segment"])
                if payload.get("market_segment")
                else None
            ),
            force_refresh=bool(payload.get("force_refresh", False)),
            max_workers=int(payload.get("max_workers") or 4),
        )
    except (
        CashFlowHistoryError,
        CashFlowSourceError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("financial_ratio_history"))
@app.get("/api/fundamentals/ratios/history")
def financial_ratio_history(
    symbol: str,
    start_period: str = "2013-Q1",
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = 64,
):
    try:
        return query_financial_ratio_history(
            symbol,
            start_period=start_period,
            end_period=end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=max(1, min(limit, 64)),
        )
    except FinancialRatioHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("growth_history"))
@app.get("/api/fundamentals/growth/history")
def growth_history(
    symbol: str,
    monthly_start_period: str = "2024-01",
    monthly_end_period: str | None = None,
    quarterly_start_period: str = "2013-Q1",
    quarterly_end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
):
    try:
        return query_growth_history(
            symbol,
            monthly_start_period=monthly_start_period,
            monthly_end_period=monthly_end_period,
            quarterly_start_period=quarterly_start_period,
            quarterly_end_period=quarterly_end_period,
            market_segment=market_segment,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
        )
    except (
        GrowthHistoryError,
        MonthlyRevenueHistoryError,
        IncomeStatementHistoryError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("financial_revision_history"))
@app.get("/api/fundamentals/revisions/history")
def financial_revision_history(
    symbol: str,
    statement_kind: str,
    period: str,
    market_segment: str | None = None,
    source_id: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
):
    try:
        return query_financial_revision_history(
            symbol,
            statement_kind=statement_kind,
            period=period,
            market_segment=market_segment,
            source_id=source_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
        )
    except FinancialRevisionHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("industry_metrics"))
@app.get("/api/fundamentals/industry-metrics")
def industry_metrics(
    symbol: str,
    period: str | None = None,
    industry: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
):
    try:
        return query_industry_metrics(
            symbol,
            period=period,
            industry=industry,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
        )
    except IndustryMetricsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("financial_guidance"))
@app.get("/api/fundamentals/guidance")
def financial_guidance(symbol: str):
    try:
        return query_financial_guidance(symbol)
    except GuidanceTrackingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("financial_anomalies"))
@app.get("/api/fundamentals/anomalies")
def financial_anomalies(symbol: str, end_period: str | None = None):
    try:
        return query_financial_anomalies(symbol, end_period=end_period)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("basic_valuation"))
@app.get("/api/fundamentals/valuation/basic")
def basic_valuation(symbol: str, end_period: str | None = None):
    try:
        return query_basic_valuation(
            symbol,
            end_period=end_period,
            store=_paper_trade_store().store,
        )
    except (BasicValuationError, ValueError) as exc:
        # Missing same-period statements are an expected data-coverage state,
        # not a malformed client request. Return a renderable contract so the
        # individual and market workspaces never leave an error-shaped hole.
        return {
            "schema_version": "stock_ai.basic_valuation.v1",
            "symbol": symbol.strip().upper(),
            "status": "unavailable",
            "unavailable_reason": str(exc),
            "metrics": [],
            "available_count": 0,
        }


@app.get(ui_data_route("valuation_percentiles"))
@app.get("/api/fundamentals/valuation/percentiles")
def valuation_percentiles(
    symbol: str,
    refresh: bool = False,
    months: int = 120,
):
    try:
        return query_valuation_percentiles(
            _paper_trade_store().store,
            symbol,
            refresh=refresh,
            months=max(1, min(months, 120)),
        )
    except (ValuationPercentileError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("peer_comparison"))
@app.get("/api/fundamentals/valuation/peers")
def peer_comparison(
    symbol: str,
    peers: str = "",
    period: str | None = None,
    refresh: bool = False,
):
    try:
        return query_peer_comparison(
            symbol,
            peers=[item.strip() for item in peers.split(",") if item.strip()],
            period=period,
            refresh=refresh,
        )
    except PeerComparisonError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("dcf_valuation"))
@app.get("/api/fundamentals/valuation/dcf")
def dcf_valuation(
    base_revenue: float,
    gross_margin_percent: float,
    fcf_conversion_percent: float,
    revenue_growth_percent: float,
    discount_rate_percent: float,
    terminal_growth_percent: float,
    net_debt: float,
    shares_outstanding: float,
    forecast_years: int = 5,
):
    try:
        return build_dcf_valuation(
            base_revenue=base_revenue,
            gross_margin_percent=gross_margin_percent,
            fcf_conversion_percent=fcf_conversion_percent,
            revenue_growth_percent=revenue_growth_percent,
            discount_rate_percent=discount_rate_percent,
            terminal_growth_percent=terminal_growth_percent,
            net_debt=net_debt,
            shares_outstanding=shares_outstanding,
            forecast_years=forecast_years,
        )
    except DCFValuationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("valuation_model_policy"))
@app.get("/api/fundamentals/valuation/model-policy")
def valuation_model_policy(
    industry_profile: str,
    profitable: bool = True,
    positive_free_cash_flow: bool = True,
    pays_dividend: bool = True,
    asset_heavy: bool = False,
    high_growth: bool = False,
):
    try:
        return build_valuation_model_policy(
            industry_profile=industry_profile,
            profitable=profitable,
            positive_free_cash_flow=positive_free_cash_flow,
            pays_dividend=pays_dividend,
            asset_heavy=asset_heavy,
            high_growth=high_growth,
            evidence=[
                {
                    "category": "model_assumption",
                    "field": "company_characteristics",
                    "value": {
                        "profitable": profitable,
                        "positive_free_cash_flow": positive_free_cash_flow,
                        "pays_dividend": pays_dividend,
                        "asset_heavy": asset_heavy,
                        "high_growth": high_growth,
                    },
                    "source": "user_input",
                }
            ],
        )
    except ValuationModelPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/official/tdcc/holding-distribution")
def tdcc_holding_distribution(symbol: str | None = None):
    return tdcc_holding_distribution_contract(symbol=symbol)


@app.post("/api/official/tdcc/holding-distribution/import")
def tdcc_holding_distribution_import(payload: dict = Body(default_factory=dict)):
    return import_tdcc_holding_distribution(payload)


@app.post("/api/official/tdcc/holding-distribution/import-csv")
def tdcc_holding_distribution_import_csv(payload: dict = Body(default_factory=dict)):
    return import_tdcc_holding_distribution_csv(payload)


@app.get("/api/official/taifex/derivatives-summary")
def taifex_derivatives_summary():
    return taifex_derivatives_summary_contract()


@app.post("/api/official/taifex/derivatives-summary/import")
def taifex_derivatives_import(payload: dict = Body(default_factory=dict)):
    return import_taifex_derivatives(payload)


@app.post("/api/official/taifex/derivatives-summary/import-csv")
def taifex_derivatives_import_csv(payload: dict = Body(default_factory=dict)):
    return import_taifex_derivatives_csv(payload)


@app.get("/api/official/mops/company-events")
def mops_company_events(symbol: str | None = None):
    return official_events_status(symbol=symbol)


@app.post("/api/official/mops/company-events/import")
def mops_company_events_import(payload: dict = Body(default_factory=dict)):
    result = import_mops_company_events(payload)
    clear_market_event_caches()
    return result


@app.post("/api/official/mops/company-events/import-csv")
def mops_company_events_import_csv(payload: dict = Body(default_factory=dict)):
    result = import_mops_company_events_csv(payload)
    clear_market_event_caches()
    return result


@app.get(ui_data_route("market_events"))
@app.get("/api/market/{symbol}/events")
def market_events(symbol: str, limit: int = 20):
    items = get_market_events(symbol, limit=max(1, min(limit, 50)))
    result = {"symbol": symbol, "count": len(items), "items": [item.model_dump() for item in items]}
    agent_event_bus.publish("market.event.updated", result)
    return result


@app.get(ui_data_route("market_anomalies"))
@app.get("/api/market/{symbol}/anomalies")
def market_anomalies(symbol: str, limit: int = 100):
    return TradingAnomalyStore(_paper_trade_store().store).list_events(
        symbol=symbol,
        limit=limit,
    )


@app.post(ui_data_route("market_anomalies_scan"))
@app.post("/api/market/{symbol}/anomalies/scan")
def market_anomalies_scan(symbol: str, payload: dict = Body(default_factory=dict)):
    try:
        return scan_symbol_anomalies(
            _paper_trade_store().store,
            symbol,
            start=payload.get("start"),
            end=payload.get("end"),
            refresh=bool(payload.get("refresh", True)),
        )
    except (TradingAnomalyError, DailyHistoryQueryError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(ui_data_route("market_anomaly_tracking"))
@app.post("/api/market/{symbol}/anomalies/{event_id}/tracking")
def market_anomaly_tracking(
    symbol: str,
    event_id: str,
    payload: dict = Body(default_factory=dict),
):
    anomaly_store = TradingAnomalyStore(_paper_trade_store().store)
    event = next(
        (
            item
            for item in anomaly_store.list_events(symbol=symbol, limit=500)["items"]
            if item["event_id"] == event_id
        ),
        None,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="trading anomaly event was not found")
    try:
        return anomaly_store.track(
            event_id=event_id,
            status=str(payload.get("status") or ""),
            note=(
                str(payload["note"]).strip()
                if payload.get("note") is not None
                else None
            ),
        )
    except TradingAnomalyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _paper_trade_store():
    trade_store = get_runtime_engine().pipeline.trade_store
    if trade_store is None:
        raise HTTPException(status_code=503, detail="paper_account_unavailable")
    return trade_store


@app.get(ui_data_route("corporate_actions"))
@app.get("/api/market/{symbol}/corporate-actions")
def corporate_actions(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
):
    return _paper_trade_store().corporate_actions(
        symbol=symbol,
        start=start,
        end=end,
        limit=max(1, min(limit, 1000)),
    )


@app.post("/api/official/corporate-actions/import")
def corporate_actions_import(payload: dict = Body(default_factory=dict)):
    items = payload.get("items")
    if items is None:
        items = [payload]
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HTTPException(status_code=422, detail="items_must_be_an_array_of_objects")
    try:
        return _paper_trade_store().import_corporate_actions(items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("trading_restrictions"))
@app.get("/api/market/{symbol}/trading-restrictions")
def trading_restrictions(
    symbol: str,
    as_of: str | None = None,
    active_only: bool = False,
    limit: int = 100,
):
    try:
        return _paper_trade_store().trading_restrictions(
            symbol=symbol,
            as_of=as_of,
            active_only=active_only,
            limit=max(1, min(limit, 1000)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/official/trading-restrictions/import")
def trading_restrictions_import(payload: dict = Body(default_factory=dict)):
    items = payload.get("items")
    if items is None:
        items = [payload]
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HTTPException(status_code=422, detail="items_must_be_an_array_of_objects")
    try:
        return _paper_trade_store().import_trading_restrictions(items)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("market_liquidity"))
@app.get("/api/market/{symbol}/liquidity")
def market_liquidity(
    symbol: str,
    window_sessions: int = 20,
    order_quantity_shares: int | None = None,
    refresh: bool = True,
):
    try:
        return assess_symbol_liquidity(
            _paper_trade_store().store,
            symbol,
            window_sessions=window_sessions,
            order_quantity_shares=order_quantity_shares,
            refresh=refresh,
        )
    except (DailyHistoryQueryError, LiquidityAssessmentError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(ui_data_route("news"))
@app.get("/api/news/center")
def news_center(symbol: str | None = None, category: str | None = None, limit: int = 20):
    result = get_news_center(
        symbol=symbol,
        category=category,
        limit=max(1, min(limit, 60)),
    )
    agent_event_bus.publish(
        "news.updated",
        {"symbol": symbol, "category": category, "result": result},
    )
    return result


@app.get(ui_data_route("news_history_coverage"))
@app.get("/api/news/history-coverage")
def news_history_coverage():
    return get_news_history_coverage_store().status_summary()


@app.get(ui_data_route("market_overview"))
@app.get("/api/market/{symbol}/overview")
def market_overview(symbol: str):
    return get_market_overview(symbol)


@app.get(ui_data_route("daily_reports"))
@app.get("/api/reports/daily")
def daily_report(limit: int = 5, symbols: str | None = None):
    explicit_symbols = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in str(symbols or "").split(",")
            if value.strip()
        )
    )
    universe = (
        UniverseSnapshot(source="explicit_symbols", symbols=explicit_symbols)
        if explicit_symbols
        else UniverseSnapshot.empty()
    )
    return generate_daily_report(
        universe=universe,
        limit=max(1, min(limit, 20)),
    ).model_dump()


@app.get("/api/notifications/channels")
def notification_channels():
    return get_notification_channels()


@app.get("/api/notifications/previews")
def notification_previews(symbol: str | None = None):
    return get_notification_previews(symbol=symbol)


@app.post("/api/notifications/send")
def notification_send(payload: NotificationSendRequest):
    return send_notification(payload)


@app.get(ui_data_route("linkage"))
@app.get("/api/linkage")
def linkage(source: str, target: str):
    return explain_linkage(source, target)


@app.get("/api/trading/preview")
def trading_preview(symbol: str, side: str = "buy", quantity_lots: int = 1):
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    return get_trading_workspace(symbol=symbol, side=side, quantity_lots=max(1, min(quantity_lots, 999))).model_dump()


@app.get("/api/trading/assistant")
def trading_assistant(symbol: str):
    return get_ai_trading_workspace(symbol=symbol).model_dump()


@app.get("/api/risk/summary")
def risk_summary(symbol: str, side: str = "buy", quantity_lots: int = 1):
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    return get_risk_workspace(symbol=symbol, side=side, quantity_lots=max(1, min(quantity_lots, 999))).model_dump()


@app.get("/api/assets/summary")
def assets_summary(limit: int = 4):
    workspace = get_asset_workspace(limit=max(1, min(limit, 10)))
    return {
        "meta": workspace.meta.model_dump(),
        "summary": workspace.summary.model_dump(),
        "positions_count": len(workspace.positions),
    }


@app.get("/api/assets/positions")
def assets_positions(limit: int = 4):
    workspace = get_asset_workspace(limit=max(1, min(limit, 10)))
    result = {
        "meta": workspace.meta.model_dump(),
        "count": len(workspace.positions),
        "items": [item.model_dump() for item in workspace.positions],
    }
    agent_event_bus.publish("portfolio.updated", result)
    return result


@app.post(ui_data_route("question"), response_model=QueryResponse)
@app.post("/api/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    route, answer, data, sources = await answer_question(req.question)
    return QueryResponse(route=route, answer=answer, data=data, sources=sources)


@app.post(ui_data_route("screener"))
@app.post("/api/screener")
def screener(req: ScreenerRequest):
    symbols = tuple(dict.fromkeys(value.strip().upper() for value in req.symbols if value.strip()))
    try:
        universe = (
            UniverseSnapshot.empty()
            if req.universe_source in {"explicit_symbols", "workflow_parameters"} and not symbols
            else resolve_universe(
                UniverseRequest(
                    source=req.universe_source,
                    symbols=symbols,
                    filters=req.filters,
                    limit=req.limit,
                )
            )
        )
    except (UniverseResolutionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        rows = run_screener(req.conditions, symbols=universe.symbols)
    except ScreenerConditionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "items": [r.model_dump() for r in rows],
        "count": len(rows),
        "conditions": req.conditions,
        "universe": {
            "source": universe.source,
            "symbols": list(universe.symbols),
            "count": universe.count,
            "filters": universe.filters,
            "created_at": universe.created_at,
        },
    }
