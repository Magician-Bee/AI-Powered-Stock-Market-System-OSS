from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
import sqlite3
from typing import Any

from open_stock_ai.config.settings import load_settings
from open_stock_ai.storage.migrations import ManagedSQLiteConnection
from open_stock_ai.types import UniverseRequest, UniverseSnapshot

from .phase1_data import list_securities_master
from .taiwan_official import _int, tpex_quotes, twse_quotes


class UniverseResolutionError(ValueError):
    """A requested Universe cannot be resolved without inventing membership."""


def resolve_universe(request: UniverseRequest | None) -> UniverseSnapshot:
    """Resolve a formally requested symbol Universe with provenance.

    `None` means no Universe, and deliberately returns an empty snapshot. Sources
    that require unavailable ranking data fail explicitly instead of falling back
    to a popular-stock list.
    """

    if request is None:
        return UniverseSnapshot.empty()
    if request.source == "explicit_symbols":
        return UniverseSnapshot(
            source=request.source,
            symbols=request.symbols[: request.limit],
            filters=dict(request.filters),
        )
    if request.source == "user_watchlist":
        symbols, provenance = _user_watchlist_symbols(request.filters)
        return _provider_snapshot(request, symbols, provenance)
    if request.source == "portfolio_positions":
        symbols, provenance = _portfolio_position_symbols()
        return _provider_snapshot(request, symbols, provenance)
    if request.source == "workflow_parameters":
        symbols, provenance = _workflow_parameter_symbols(request)
        return _provider_snapshot(request, symbols, provenance)
    if request.source == "tool_discovered":
        symbols, provenance = _tool_discovered_symbols(request)
        return _provider_snapshot(request, symbols, provenance)

    if request.source == "top_by_volume":
        market_filter = str(request.filters.get("market") or "all").strip().casefold()
        ranked: list[tuple[int, str]] = []
        if market_filter in {"all", "taiwan", "twse", "listed"}:
            ranked.extend(
                (
                    _int(row.get("TradeVolume")),
                    f"{str(row.get('Code') or '').strip()}.TW",
                )
                for row in twse_quotes()
                if str(row.get("Code") or "").strip()
            )
        if market_filter in {"all", "taiwan", "tpex", "otc"}:
            ranked.extend(
                (
                    _int(row.get("TradingShares")),
                    f"{str(row.get('SecuritiesCompanyCode') or '').strip()}.TWO",
                )
                for row in tpex_quotes()
                if str(row.get("SecuritiesCompanyCode") or "").strip()
            )
        ranked = sorted(ranked, key=lambda item: (-item[0], item[1]))
        if not ranked:
            raise UniverseResolutionError(
                "top_by_volume received no attributed official quote rows; no fallback Universe is allowed"
            )
        filters = dict(request.filters)
        filters.update(
            {
                "ranking_metric": "official_trade_volume_shares",
                "ranking_sources": ["TWSE_ALL_QUOTES", "TPEX_DAILY_QUOTES"],
            }
        )
        return UniverseSnapshot(
            source=request.source,
            symbols=tuple(symbol for _volume, symbol in ranked[: request.limit]),
            filters=filters,
        )

    if request.source == "top_by_market_cap":
        raise UniverseResolutionError(
            f"{request.source} requires an attributed ranking provider; no fallback Universe is allowed"
        )

    market = {
        "all_twse_active": "twse",
        "all_tpex_active": "tpex",
        "sector_members": str(request.filters.get("market") or "all"),
        "screening_query": str(request.filters.get("market") or "all"),
    }.get(request.source, "all")
    query = str(request.filters.get("query") or "") if request.source == "screening_query" else ""
    securities = list_securities_master(q=query, market=market, limit=5000, include_lifecycle=True)
    if request.source == "sector_members":
        sector = str(request.filters.get("sector") or "").strip().casefold()
        if not sector:
            raise UniverseResolutionError("sector_members requires filters.sector")
        securities = [
            item
            for item in securities
            if sector in str(item.industry or "").casefold()
        ]
    securities = [item for item in securities if item.trading_status == "active"]
    return UniverseSnapshot(
        source=request.source,
        symbols=tuple(item.symbol for item in securities[: request.limit]),
        filters=dict(request.filters),
    )


def universe_payload(snapshot: UniverseSnapshot) -> dict[str, Any]:
    return asdict(snapshot) | {"count": snapshot.count}


def universe_source_options() -> dict[str, Any]:
    """Describe live selectable Universe providers without resolving a scan."""

    settings = load_settings()
    db_path = Path(settings.sqlite_path).expanduser()
    watchlists: list[dict[str, Any]] = []
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path, factory=ManagedSQLiteConnection) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    select w.watchlist_id, w.name, w.user_id, count(s.symbol) as symbol_count
                      from user_watchlists w
                      left join user_watchlist_symbols s on s.watchlist_id = w.watchlist_id
                     group by w.watchlist_id, w.name, w.user_id
                     order by w.updated_at desc, w.name
                    """
                ).fetchall()
                watchlists = [
                    {
                        "watchlist_id": str(row["watchlist_id"]),
                        "name": str(row["name"]),
                        "user_id": str(row["user_id"]),
                        "symbol_count": int(row["symbol_count"] or 0),
                    }
                    for row in rows
                ]
        except sqlite3.Error:
            watchlists = []
    try:
        from .agent_service import get_agent_run_runtime

        workflows = [
            {
                "workflow_id": item.get("workflow_id"),
                "name": item.get("name"),
                "version": item.get("version"),
            }
            for item in get_agent_run_runtime().list_workflows()
        ]
    except Exception:
        workflows = []
    try:
        portfolio_symbols, _provenance = _portfolio_position_symbols()
    except UniverseResolutionError:
        portfolio_symbols = []
    return {
        "schema_version": "stock_ai.universe_source_options.v1",
        "sources": [
            "explicit_symbols",
            "user_watchlist",
            "portfolio_positions",
            "top_by_volume",
            "sector_members",
            "workflow_parameters",
            "tool_discovered",
        ],
        "watchlists": watchlists,
        "configured_watchlist_count": len(settings.watchlist),
        "portfolio_symbols": portfolio_symbols,
        "workflows": workflows,
    }


def _provider_snapshot(
    request: UniverseRequest,
    symbols: list[str] | tuple[str, ...],
    provenance: dict[str, Any],
) -> UniverseSnapshot:
    normalized = tuple(
        dict.fromkeys(
            _normalize_universe_symbol(symbol)
            for symbol in symbols
            if _normalize_universe_symbol(symbol)
        )
    )
    filters = {
        key: value
        for key, value in request.filters.items()
        if key not in {"tool_result", "tool_results", "workflow_parameters"}
    }
    filters["resolution"] = provenance
    return UniverseSnapshot(
        source=request.source,
        symbols=normalized[: request.limit],
        filters=filters,
    )


def _user_watchlist_symbols(filters: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    settings = load_settings()
    symbols = [
        str(item.get("symbol") or "")
        for item in settings.watchlist
        if isinstance(item, dict) and item.get("symbol")
    ]
    sources = ["config.open_stock_ai.watchlist"] if symbols else []
    db_path = Path(settings.sqlite_path).expanduser()
    persisted: list[str] = []
    if db_path.is_file():
        try:
            with sqlite3.connect(db_path, factory=ManagedSQLiteConnection) as conn:
                conn.row_factory = sqlite3.Row
                clauses: list[str] = []
                parameters: list[Any] = []
                if filters.get("watchlist_id"):
                    clauses.append("w.watchlist_id = ?")
                    parameters.append(str(filters["watchlist_id"]))
                if filters.get("user_id"):
                    clauses.append("w.user_id = ?")
                    parameters.append(str(filters["user_id"]))
                where = f"where {' and '.join(clauses)}" if clauses else ""
                rows = conn.execute(
                    f"""
                    select s.symbol
                      from user_watchlist_symbols s
                      join user_watchlists w on w.watchlist_id = s.watchlist_id
                      {where}
                     order by w.updated_at desc, s.added_at, s.symbol
                    """,
                    parameters,
                ).fetchall()
                persisted = [str(row["symbol"]) for row in rows if row["symbol"]]
        except sqlite3.Error:
            persisted = []
    if persisted:
        sources.append("sqlite.user_watchlists")
    resolved = list(dict.fromkeys([*persisted, *symbols]))
    if not resolved:
        raise UniverseResolutionError(
            "user_watchlist has no persisted or configured symbols; no fallback Universe is allowed"
        )
    return resolved, {
        "provider": "user_watchlist_store",
        "sources": sources,
        "persisted_count": len(persisted),
        "configured_count": len(symbols),
    }


def _portfolio_position_symbols() -> tuple[list[str], dict[str, Any]]:
    try:
        from open_stock_ai.runtime import get_runtime_engine

        engine = get_runtime_engine()
        trade_store = getattr(engine.pipeline, "trade_store", None)
        exposure = trade_store.exposure() if trade_store is not None else {}
    except Exception as exc:
        raise UniverseResolutionError(
            f"portfolio_positions provider failed: {type(exc).__name__}: {exc}"
        ) from exc
    position_map = exposure.get("symbols") if isinstance(exposure, dict) else {}
    symbols = [
        str(symbol)
        for symbol, item in (position_map or {}).items()
        if isinstance(item, dict)
        and float(item.get("quantity") or item.get("position_size_pct") or 0) > 0
    ]
    if not symbols:
        raise UniverseResolutionError(
            "portfolio_positions contains no open paper positions; no fallback Universe is allowed"
        )
    return symbols, {
        "provider": "paper_oms_positions",
        "source_of_truth": exposure.get("source_of_truth"),
        "position_count": len(symbols),
    }


def _workflow_parameter_symbols(
    request: UniverseRequest,
) -> tuple[list[str], dict[str, Any]]:
    symbols = list(request.symbols)
    workflow_id = str(request.filters.get("workflow_id") or "").strip()
    workflow: dict[str, Any] | None = None
    if workflow_id:
        try:
            from .agent_service import get_agent_run_runtime

            workflow = get_agent_run_runtime().get_workflow(workflow_id)
        except Exception as exc:
            raise UniverseResolutionError(
                f"workflow_parameters provider failed: {type(exc).__name__}: {exc}"
            ) from exc
        if workflow is None:
            raise UniverseResolutionError(f"Workflow not found: {workflow_id}")
        symbols.extend(_symbols_from_structured_payload(workflow))
    supplied_parameters = request.filters.get("workflow_parameters")
    if supplied_parameters is not None:
        symbols.extend(_symbols_from_structured_payload(supplied_parameters))
    if not symbols:
        raise UniverseResolutionError(
            "workflow_parameters did not contain symbol or symbols fields"
        )
    return symbols, {
        "provider": "agent_workflow_store",
        "workflow_id": workflow_id or None,
        "parameter_symbol_count": len(symbols),
    }


def _tool_discovered_symbols(
    request: UniverseRequest,
) -> tuple[list[str], dict[str, Any]]:
    symbols = list(request.symbols)
    payloads: list[Any] = []
    if "tool_result" in request.filters:
        payloads.append(request.filters["tool_result"])
    if isinstance(request.filters.get("tool_results"), list):
        payloads.extend(request.filters["tool_results"])
    run_id = str(request.filters.get("run_id") or "").strip()
    if run_id:
        try:
            from .agent_service import get_agent_run_runtime

            run = get_agent_run_runtime().get_run(run_id)
        except Exception as exc:
            raise UniverseResolutionError(
                f"tool_discovered run provider failed: {type(exc).__name__}: {exc}"
            ) from exc
        if run is None:
            raise UniverseResolutionError(f"Agent run not found: {run_id}")
        result = run.get("result") if isinstance(run.get("result"), dict) else {}
        payloads.extend(
            item.get("result")
            for item in result.get("tool_trace") or []
            if isinstance(item, dict) and item.get("ok") is True
        )
    for payload in payloads:
        symbols.extend(_symbols_from_structured_payload(payload))
    if not symbols:
        raise UniverseResolutionError(
            "tool_discovered requires a real tool result containing symbol or symbols fields"
        )
    return symbols, {
        "provider": "validated_tool_results",
        "run_id": run_id or None,
        "tool_result_count": len(payloads),
        "discovered_count": len(symbols),
    }


def _symbols_from_structured_payload(value: Any) -> list[str]:
    symbols: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in {"symbol", "ticker"} and isinstance(item, str):
                symbols.append(item)
            elif normalized_key in {"symbols", "tickers", "universe"} and isinstance(item, list):
                symbols.extend(str(entry) for entry in item if isinstance(entry, str))
            elif isinstance(item, (dict, list)):
                symbols.extend(_symbols_from_structured_payload(item))
    elif isinstance(value, list):
        for item in value:
            symbols.extend(_symbols_from_structured_payload(item))
    return symbols


def _normalize_universe_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if re.fullmatch(r"\d{4,6}", symbol):
        return f"{symbol}.TW"
    if re.fullmatch(r"[A-Z0-9^][A-Z0-9.^_-]{0,31}", symbol):
        return symbol
    return ""
