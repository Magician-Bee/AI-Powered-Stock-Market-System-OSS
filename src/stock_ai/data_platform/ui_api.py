from __future__ import annotations

from pathlib import Path
from typing import Any


UI_DATA_API_PREFIX = "/api/data/ui/v1"

_ROUTES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "catalog": ("GET", "/catalog", "/api/catalog", ("data_catalog",)),
    "entities_search": (
        "GET",
        "/entities/search",
        "/api/entities/search",
        ("security_database",),
    ),
    "question": ("POST", "/query", "/api/query", ("research_question",)),
    "screener": ("POST", "/screener", "/api/screener", ("screener",)),
    "linkage": ("GET", "/linkage", "/api/linkage", ("linkage",)),
    "securities_master": (
        "GET",
        "/securities/master",
        "/api/securities/master",
        ("home", "watchlist", "stock"),
    ),
    "securities_master_refresh": (
        "POST",
        "/securities/master/refresh",
        "/api/securities/master/refresh",
        ("data_catalog",),
    ),
    "watchlist_overview": (
        "GET",
        "/watchlist/overview",
        "/api/watchlist/overview",
        ("home", "watchlist"),
    ),
    "sources": ("GET", "/sources", "/api/sources", ("home",)),
    "overview_indices": (
        "GET",
        "/overview/indices",
        "/api/overview/indices",
        ("home", "market_overview"),
    ),
    "realtime_status": (
        "GET",
        "/realtime/status",
        "/api/realtime/status",
        ("stock", "realtime_monitor"),
    ),
    "realtime_quote": (
        "GET",
        "/realtime/quote/{symbol}",
        "/api/realtime/quote/{symbol}",
        ("stock", "realtime_monitor"),
    ),
    "realtime_stream": (
        "GET",
        "/realtime/stream/{symbol}",
        "/api/realtime/stream/{symbol}",
        ("stock", "realtime_monitor"),
    ),
    "intraday_candle_status": (
        "GET",
        "/intraday/candles/status",
        "/api/intraday/candles/status",
        ("stock", "realtime_monitor"),
    ),
    "intraday_candle_dates": (
        "GET",
        "/intraday/candles/{symbol}/dates",
        "/api/intraday/candles/{symbol}/dates",
        ("stock",),
    ),
    "intraday_candles": (
        "GET",
        "/intraday/candles/{symbol}",
        "/api/intraday/candles/{symbol}",
        ("stock", "realtime_monitor"),
    ),
    "market_summary": (
        "GET",
        "/market/{symbol}/summary",
        "/api/market/{symbol}/summary",
        ("stock",),
    ),
    "market_history": (
        "GET",
        "/market/{symbol}/history",
        "/api/market/{symbol}/history",
        ("stock",),
    ),
    "market_events": (
        "GET",
        "/market/{symbol}/events",
        "/api/market/{symbol}/events",
        ("stock", "news"),
    ),
    "corporate_actions": (
        "GET",
        "/market/{symbol}/corporate-actions",
        "/api/market/{symbol}/corporate-actions",
        ("stock", "paper_account"),
    ),
    "trading_restrictions": (
        "GET",
        "/market/{symbol}/trading-restrictions",
        "/api/market/{symbol}/trading-restrictions",
        ("stock", "paper_account"),
    ),
    "market_liquidity": (
        "GET",
        "/market/{symbol}/liquidity",
        "/api/market/{symbol}/liquidity",
        ("stock", "paper_account"),
    ),
    "market_anomalies": (
        "GET",
        "/market/{symbol}/anomalies",
        "/api/market/{symbol}/anomalies",
        ("stock", "realtime_monitor"),
    ),
    "market_anomalies_scan": (
        "POST",
        "/market/{symbol}/anomalies/scan",
        "/api/market/{symbol}/anomalies/scan",
        ("stock", "realtime_monitor"),
    ),
    "market_anomaly_tracking": (
        "POST",
        "/market/{symbol}/anomalies/{event_id}/tracking",
        "/api/market/{symbol}/anomalies/{event_id}/tracking",
        ("stock", "realtime_monitor"),
    ),
    "market_overview": (
        "GET",
        "/market/{symbol}/overview",
        "/api/market/{symbol}/overview",
        ("stock",),
    ),
    "institutional_flow": (
        "GET",
        "/flow/institutional",
        "/api/flow/institutional",
        ("home", "flow", "stock"),
    ),
    "margin": (
        "GET",
        "/flow/margin",
        "/api/flow/margin",
        ("flow", "fundamentals", "stock"),
    ),
    "chip_history": (
        "GET",
        "/flow/chip/history",
        "/api/flow/chip/history",
        ("flow", "stock", "watchlist", "screener"),
    ),
    "short_daytrade_history": (
        "GET",
        "/flow/chip/short-daytrade",
        "/api/flow/chip/short-daytrade",
        ("flow", "stock"),
    ),
    "tdcc_holding_history": (
        "GET",
        "/flow/chip/tdcc-history",
        "/api/flow/chip/tdcc-history",
        ("flow", "stock"),
    ),
    "revenue": (
        "GET",
        "/fundamentals/revenue",
        "/api/fundamentals/revenue",
        ("fundamentals", "stock"),
    ),
    "revenue_history": (
        "GET",
        "/fundamentals/revenue/history",
        "/api/fundamentals/revenue/history",
        ("fundamentals", "stock"),
    ),
    "revenue_history_sync": (
        "POST",
        "/fundamentals/revenue/history/sync",
        "/api/fundamentals/revenue/history/sync",
        ("fundamentals", "stock", "data_catalog"),
    ),
    "income_statement_history": (
        "GET",
        "/fundamentals/income-statement/history",
        "/api/fundamentals/income-statement/history",
        ("fundamentals", "stock"),
    ),
    "income_statement_history_sync": (
        "POST",
        "/fundamentals/income-statement/history/sync",
        "/api/fundamentals/income-statement/history/sync",
        ("fundamentals", "stock", "data_catalog"),
    ),
    "balance_sheet_history": (
        "GET",
        "/fundamentals/balance-sheet/history",
        "/api/fundamentals/balance-sheet/history",
        ("fundamentals", "stock"),
    ),
    "balance_sheet_history_sync": (
        "POST",
        "/fundamentals/balance-sheet/history/sync",
        "/api/fundamentals/balance-sheet/history/sync",
        ("fundamentals", "stock", "data_catalog"),
    ),
    "cash_flow_history": (
        "GET",
        "/fundamentals/cash-flow/history",
        "/api/fundamentals/cash-flow/history",
        ("fundamentals", "stock"),
    ),
    "cash_flow_history_sync": (
        "POST",
        "/fundamentals/cash-flow/history/sync",
        "/api/fundamentals/cash-flow/history/sync",
        ("fundamentals", "stock", "data_catalog"),
    ),
    "financial_ratio_history": (
        "GET",
        "/fundamentals/ratios/history",
        "/api/fundamentals/ratios/history",
        ("fundamentals", "stock"),
    ),
    "growth_history": (
        "GET",
        "/fundamentals/growth/history",
        "/api/fundamentals/growth/history",
        ("fundamentals", "stock"),
    ),
    "financial_revision_history": (
        "GET",
        "/fundamentals/revisions/history",
        "/api/fundamentals/revisions/history",
        ("fundamentals", "stock", "research"),
    ),
    "industry_metrics": (
        "GET",
        "/fundamentals/industry-metrics",
        "/api/fundamentals/industry-metrics",
        ("fundamentals", "stock", "research"),
    ),
    "financial_guidance": (
        "GET",
        "/fundamentals/guidance",
        "/api/fundamentals/guidance",
        ("fundamentals", "stock", "research"),
    ),
    "financial_anomalies": (
        "GET",
        "/fundamentals/anomalies",
        "/api/fundamentals/anomalies",
        ("fundamentals", "stock", "research"),
    ),
    "basic_valuation": (
        "GET",
        "/fundamentals/valuation/basic",
        "/api/fundamentals/valuation/basic",
        ("fundamentals", "stock", "research"),
    ),
    "valuation_percentiles": (
        "GET",
        "/fundamentals/valuation/percentiles",
        "/api/fundamentals/valuation/percentiles",
        ("fundamentals", "stock", "research"),
    ),
    "peer_comparison": (
        "GET",
        "/fundamentals/valuation/peers",
        "/api/fundamentals/valuation/peers",
        ("fundamentals", "stock", "research"),
    ),
    "dcf_valuation": (
        "GET",
        "/fundamentals/valuation/dcf",
        "/api/fundamentals/valuation/dcf",
        ("fundamentals", "stock", "research"),
    ),
    "valuation_model_policy": (
        "GET",
        "/fundamentals/valuation/model-policy",
        "/api/fundamentals/valuation/model-policy",
        ("fundamentals", "stock", "research"),
    ),
    "news": (
        "GET",
        "/news/center",
        "/api/news/center",
        ("home", "news", "stock"),
    ),
    "news_history_coverage": (
        "GET",
        "/news/history-coverage",
        "/api/news/history-coverage",
        ("news", "data_catalog"),
    ),
    "daily_reports": (
        "GET",
        "/reports/daily",
        "/api/reports/daily",
        ("home", "stock"),
    ),
}


def ui_data_route(name: str) -> str:
    try:
        return f"{UI_DATA_API_PREFIX}{_ROUTES[name][1]}"
    except KeyError as exc:
        raise ValueError(f"Unknown unified UI data route: {name}") from exc


def unified_data_api_contract() -> dict[str, Any]:
    items = [
        {
            "route_id": route_id,
            "method": definition[0],
            "path": ui_data_route(route_id),
            "compatibility_path": definition[2],
            "consumers": list(definition[3]),
            "connector_access": "backend_only",
        }
        for route_id, definition in _ROUTES.items()
    ]
    consumers = sorted(
        {
            consumer
            for definition in _ROUTES.values()
            for consumer in definition[3]
        }
    )
    return {
        "schema_version": "stock_ai.unified_ui_data_api.v1",
        "prefix": UI_DATA_API_PREFIX,
        "route_count": len(items),
        "consumer_count": len(consumers),
        "consumers": consumers,
        "items": items,
        "legacy_routes_are_compatibility_only": True,
        "ui_connector_access": False,
        "status": "enforced",
    }


def audit_ui_data_access(static_root: str | Path) -> dict[str, Any]:
    root = Path(static_root).expanduser().resolve()
    javascript_files = sorted(root.rglob("*.js"))
    violations: list[dict[str, str]] = []
    compatibility_paths = {
        definition[2]
        for definition in _ROUTES.values()
    }
    for path in javascript_files:
        text = path.read_text(encoding="utf-8")
        for compatibility_path in sorted(compatibility_paths):
            if compatibility_path in text:
                violations.append(
                    {
                        "file": str(path.relative_to(root)),
                        "compatibility_path": compatibility_path,
                    }
                )
    return {
        "schema_version": "stock_ai.unified_ui_data_api_audit.v1",
        "status": "passed" if not violations else "failed",
        "scanned_file_count": len(javascript_files),
        "violation_count": len(violations),
        "violations": violations,
        "required_prefix": UI_DATA_API_PREFIX,
    }
