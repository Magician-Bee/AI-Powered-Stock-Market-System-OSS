from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from stock_ai.paper_training_api import PaperOrderRequest, paper_training_preview
from stock_ai.realtime_quotes import status as realtime_status
from stock_ai.services import get_price_history_payload

from .contracts import CandidateDetail, WorkspaceContext
from .deep_analysis import select_deep_analysis_symbols
from .service import get_market_intelligence_service


router = APIRouter(tags=["Market Intelligence Workspace"])

_CHART_CACHE_TTL_SECONDS = 60.0
_CHART_CACHE_MAX_ENTRIES = 256
_chart_response_cache: dict[
    tuple[str, str | None, str | None, int],
    tuple[float, dict[str, Any]],
] = {}
_chart_request_locks: dict[tuple[str, str | None, str | None, int], Lock] = {}
_chart_cache_lock = RLock()

# Official TWSE B.12.13 and TPEx V1.33 transmission-spec classifications.
# Keep the upstream code in the response as ``industry_code`` while exposing a
# readable label to the combined TWSE/TPEx decision workspace.
_TWSE_INDUSTRY_LABELS = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技業",
    "35": "綠能環保",
    "36": "數位雲端",
    "37": "運動休閒",
    "38": "居家生活",
    "80": "管理股票",
    "91": "存託憑證",
}


def _industry_label(value: str | None) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return "未分類", None
    code = raw.zfill(2) if raw.isdigit() else raw
    return _TWSE_INDUSTRY_LABELS.get(code, raw), code if raw.isdigit() else None


def _service():
    return get_market_intelligence_service()


def _cached_instrument_chart(
    symbol: str,
    *,
    start: str | None,
    end: str | None,
    limit: int,
) -> dict[str, Any]:
    """Share short-lived chart reads across the homepage, reloads and Agent context."""
    key = (symbol, start, end, limit)
    now = time.monotonic()
    with _chart_cache_lock:
        cached = _chart_response_cache.get(key)
        if cached and now - cached[0] < _CHART_CACHE_TTL_SECONDS:
            return cached[1]
        request_lock = _chart_request_locks.setdefault(key, Lock())

    # A new browser tab and the native WebView can request the same default
    # index at nearly the same time. Recheck after the per-key lock so only one
    # upstream history request is made.
    with request_lock:
        now = time.monotonic()
        with _chart_cache_lock:
            cached = _chart_response_cache.get(key)
            if cached and now - cached[0] < _CHART_CACHE_TTL_SECONDS:
                return cached[1]

        if start:
            payload = get_price_history_payload(
                symbol,
                start=start,
                end=end or date.today().isoformat(),
                limit=limit,
                refresh=False,
                allow_fallback=True,
            )
        else:
            payload = get_price_history_payload(symbol, limit=limit)

        with _chart_cache_lock:
            _chart_response_cache[key] = (time.monotonic(), payload)
            if len(_chart_response_cache) > _CHART_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    _chart_response_cache,
                    key=lambda item: _chart_response_cache[item][0],
                )
                _chart_response_cache.pop(oldest_key, None)
                if oldest_key != key:
                    _chart_request_locks.pop(oldest_key, None)
        return payload


def _bootstrap_snapshot(snapshot) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    selected_symbols: list[str] = []
    compact_rankings: dict[str, list[str]] = {}
    for category, symbols in snapshot.rankings.items():
        compact_rankings[category] = symbols[:30]
        selected_symbols.extend(compact_rankings[category])
    for symbols in snapshot.portfolio_actions.values():
        selected_symbols.extend(symbols)
    selected_symbols = list(dict.fromkeys(selected_symbols))
    classification_counts = {
        category: sum(1 for item in snapshot.candidate_details.values() if not item.is_position and item.market_category == category)
        for category in ("BUY_NOW", "WATCH", "FUTURE_BUY", "AVOID_NOW", "INSUFFICIENT_DATA")
    }
    portfolio_counts = {
        action: len(snapshot.portfolio_actions.get(action, []))
        for action in ("hold", "add", "reduce", "exit")
    }
    universe = snapshot.universe.model_dump(mode="json")
    # Snapshots created before the whole-market contract was introduced do not
    # carry the newer breakdown fields. Keep the UI honest and non-empty while
    # those snapshots are still being served, without inventing per-factor data.
    universe["security_master_count"] = universe.get("security_master_count") or universe.get("resolved_count", 0)
    universe["investable_count"] = universe.get("investable_count") or universe.get("resolved_count", 0)
    universe["ordinary_stock_count"] = universe.get("ordinary_stock_count") or universe.get("investable_count", 0)
    universe["classified_count"] = universe.get("classified_count") or len(snapshot.candidate_details)
    return {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "market": snapshot.market,
        "generated_at": snapshot.generated_at,
        "data_as_of": snapshot.data_as_of,
        "status": snapshot.status,
        "market_regime": snapshot.market_regime.model_dump(mode="json"),
        "universe": universe,
        "rankings": compact_rankings,
        "ranking_counts": {
            category: len(symbols)
            for category, symbols in snapshot.rankings.items()
        },
        "portfolio_actions": snapshot.portfolio_actions,
        "candidate_details": {
            symbol: snapshot.candidate_details[symbol].model_dump(mode="json")
            for symbol in selected_symbols
            if symbol in snapshot.candidate_details
        },
        "data_quality": snapshot.data_quality,
        "model_overlay": snapshot.model_overlay.model_dump(mode="json"),
        "risk_overlay": snapshot.risk_overlay,
        "next_refresh_conditions": snapshot.next_refresh_conditions,
        "scan_statistics": snapshot.scan_statistics,
        "classification_counts": classification_counts,
        "portfolio_action_counts": portfolio_counts,
        "model_analysis_count": len(snapshot.model_overlay.analyzed_symbols),
        "model_receipt_count": len(snapshot.model_receipts),
        "errors": snapshot.errors,
        "details_endpoint": (
            f"/api/intelligence/snapshots/{snapshot.snapshot_id}"
        ),
    }


def _compact_candidates(snapshot, category: str, limit: int = 30) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    symbols = snapshot.rankings.get(category, [])[:limit]
    return [
        snapshot.candidate_details[symbol].model_dump(mode="json")
        for symbol in symbols
        if symbol in snapshot.candidate_details
    ]


def _market_rankings(snapshot) -> dict[str, list[dict[str, Any]]]:
    if snapshot is None:
        return {
            "volume": [],
            "movers": [],
            "industries": [],
            "anomalies": [],
        }
    details = [
        item for item in snapshot.candidate_details.values()
        if not _is_excluded_product(item)
    ]
    executable = [
        item
        for item in details
        if item.data_quality.status in {"ready", "partial"}
        and item.latest_price is not None
    ]
    volume = sorted(
        executable,
        key=lambda item: (-float(item.volume or 0), item.symbol),
    )[:30]
    movers = sorted(
        executable,
        key=lambda item: (-abs(float(item.change_percent or 0)), item.symbol),
    )[:30]
    industry_rows: dict[str, list[CandidateDetail]] = {}
    for item in executable:
        industry_rows.setdefault(item.industry or "未分類", []).append(item)
    industries = [
        {
            "kind": "industry",
            "name": _industry_label(industry)[0],
            "industry_code": _industry_label(industry)[1],
            "member_count": len(items),
            "average_change_percent": round(
                sum(float(item.change_percent or 0) for item in items) / len(items),
                3,
            ),
            "leader_symbol": sorted(
                items,
                key=lambda item: (-float(item.score or 0), item.symbol),
            )[0].symbol,
        }
        for industry, items in industry_rows.items()
        if items
    ]
    industries.sort(
        key=lambda item: (
            -float(item["average_change_percent"]),
            str(item["name"]),
        )
    )
    anomalies = sorted(
        [
            item
            for item in executable
            if item.category == "high_risk"
            or abs(float(item.change_percent or 0)) >= 7
            or item.data_quality.status == "conflict"
        ],
        key=lambda item: (-abs(float(item.change_percent or 0)), item.symbol),
    )[:30]
    return {
        "volume": [item.model_dump(mode="json") for item in volume],
        "movers": [item.model_dump(mode="json") for item in movers],
        "industries": industries[:30],
        "anomalies": [item.model_dump(mode="json") for item in anomalies],
    }


def _market_decision_lists(snapshot) -> dict[str, list[dict[str, Any]]]:
    if snapshot is None:
        return {key: [] for key in ("buy_now", "watch", "future_buy", "avoid_now", "insufficient_data", "sell_reduce")}
    details = [
        item for item in snapshot.candidate_details.values()
        if not _is_excluded_product(item)
    ]
    by_category = {
        key: [item.model_dump(mode="json") for item in details if item.market_category == category]
        for key, category in (
            ("buy_now", "BUY_NOW"), ("watch", "WATCH"), ("future_buy", "FUTURE_BUY"),
            ("avoid_now", "AVOID_NOW"), ("insufficient_data", "INSUFFICIENT_DATA"),
        )
    }
    by_category["sell_reduce"] = [
        item.model_dump(mode="json") for item in details
        if item.is_position and item.portfolio_action in {"reduce", "exit"}
    ]
    for rows in by_category.values():
        rows.sort(key=lambda item: (int(item.get("rank") or 999999), str(item.get("symbol") or "")))
    return by_category


def _is_excluded_product(item: Any) -> bool:
    """Keep API consumers aligned with the scanner for new and legacy snapshots."""
    if any(
        bool(getattr(item, field, False))
        for field in ("is_etf", "is_warrant", "is_managed_stock", "is_special_security")
    ):
        return True
    # Older persisted snapshots predate the product flags. Taiwan ETF codes
    # are conventionally 00xxx (including leveraged/inverse suffixes), so
    # suppress those stale rows until the next whole-market scan refreshes it.
    symbol = str(getattr(item, "symbol", "")).split(".", 1)[0]
    return symbol.startswith("00")


def _saved_navigation_items(snapshot, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        detail = snapshot.candidate_details.get(symbol) if snapshot else None
        if detail is not None and _is_excluded_product(detail):
            continue
        items.append(
            {
                **(detail.model_dump(mode="json") if detail else {}),
                **row,
                "symbol": symbol,
                "name": detail.name if detail else symbol,
            }
        )
    return items


@router.get("/api/workspace/bootstrap")
def workspace_bootstrap(background_tasks: BackgroundTasks) -> dict[str, Any]:
    service = _service()
    snapshot = service.latest_snapshot()
    if snapshot is None:
        scan = service.create_scan(
            {
                "event_type": "workspace_first_open",
                "mode": "deterministic_whole_market",
                "llm_per_symbol": False,
            }
        )
        background_tasks.add_task(service.run_scan, scan["scan_id"])
    context = service.context()
    if snapshot and not context.data.market_snapshot_id:
        context = service.patch_context({"data": {"market_snapshot_id": snapshot.snapshot_id}})
    market_status = realtime_status().as_dict()
    default_symbol = context.selection.symbol or "^TWII"
    try:
        default_chart_payload = _cached_instrument_chart(
            default_symbol,
            start=None,
            end=None,
            limit=5000,
        )
    except Exception as exc:
        default_chart_payload = {
            "symbol": default_symbol,
            "points": [],
            "status": "unavailable",
            "error": {
                "code": "default_chart_unavailable",
                "message": str(exc),
            },
        }
    watchlist_rows = service.store.watchlist_symbols("home-default", limit=30)
    alert_rows = service.store.active_alerts(limit=30)
    ranking_payload = _market_rankings(snapshot)
    decision_payload = _market_decision_lists(snapshot)
    return {
        "schema_version": "stock_ai.workspace_bootstrap.v1",
        "status": snapshot.status if snapshot else "initializing",
        "generated_at": snapshot.generated_at if snapshot else None,
        "market_status": market_status,
        "market_snapshot": _bootstrap_snapshot(snapshot),
        "default_chart": {
            "symbol": default_symbol,
            "fallback_symbol": "^TWII",
            "timeframe": context.timeframe,
            "date_range": context.dateRange,
            "price_basis": context.priceBasis,
            "payload": default_chart_payload,
        },
        "market_navigation": {
            **{
                category: _compact_candidates(snapshot, category, limit=30)
                for category in (
                    "actionable_now",
                    "near_actionable",
                    "wait_for_pullback",
                    "wait_for_breakout",
                    "avoid_now",
                    "high_risk",
                    "insufficient_data",
                )
            },
            **ranking_payload,
            **decision_payload,
            "watchlist": _saved_navigation_items(snapshot, watchlist_rows),
            "alerts": _saved_navigation_items(snapshot, alert_rows),
            "indices": [
                {
                    "symbol": "^TWII",
                    "name": "加權指數",
                    "kind": "index",
                    "source": "workspace_default_benchmark",
                },
                {
                    "symbol": "^TWOII",
                    "name": "櫃買指數",
                    "kind": "index",
                    "source": "workspace_market_benchmark",
                },
            ],
            "institutional": [],
            "institutional_status": {
                "status": "unavailable",
                "message": "本次全市場快照未包含可比較的同日期法人淨買賣超，不以規則分數代替。",
            },
        },
        "watchlist_summary": {
            "count": len(watchlist_rows),
            "items": _saved_navigation_items(snapshot, watchlist_rows),
        },
        "alerts_summary": {
            "count": len(alert_rows),
            "items": _saved_navigation_items(snapshot, alert_rows),
        },
        "market_rankings": ranking_payload,
        "market_decisions": decision_payload,
        "portfolio_actions": (
            {
                action: [
                    snapshot.candidate_details[symbol].model_dump(mode="json")
                    for symbol in symbols
                    if symbol in snapshot.candidate_details
                ]
                for action, symbols in snapshot.portfolio_actions.items()
            }
            if snapshot
            else {"hold": [], "add": [], "reduce": [], "exit": []}
        ),
        "agent_context": context.model_dump(mode="json"),
        "layout_preferences": context.layout.model_dump(mode="json"),
        "data_status": (
            snapshot.data_quality
            if snapshot
            else {
                "status": "initializing",
                "message": "正在建立第一份全市場量化快照；不會顯示虛假候選。",
            }
        ),
    }


@router.get("/api/workspace/context")
def workspace_context() -> dict[str, Any]:
    return _service().context().model_dump(mode="json")


@router.patch("/api/workspace/context")
def patch_workspace_context(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    return _service().patch_context(payload).model_dump(mode="json")


@router.get("/api/workspace/stream")
async def workspace_stream() -> StreamingResponse:
    async def events():
        last_snapshot_id: str | None = None
        for _ in range(120):
            snapshot = _service().latest_snapshot()
            if snapshot and snapshot.snapshot_id != last_snapshot_id:
                last_snapshot_id = snapshot.snapshot_id
                data = json.dumps(
                    {
                        "event": "snapshot.updated",
                        "snapshot_id": snapshot.snapshot_id,
                        "status": snapshot.status,
                        "generated_at": snapshot.generated_at,
                    },
                    ensure_ascii=False,
                )
                yield f"event: snapshot.updated\ndata: {data}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/intelligence/snapshots/latest")
def latest_intelligence_snapshot(
    ensure: bool = Query(default=False),
) -> dict[str, Any]:
    service = _service()
    snapshot = service.latest_snapshot()
    if snapshot is None and ensure:
        scan = service.create_scan({"event_type": "latest_snapshot_missing"})
        snapshot = service.run_scan(scan["scan_id"])
    if snapshot is None:
        raise HTTPException(status_code=404, detail="market_snapshot_initializing")
    return snapshot.model_dump(mode="json")


@router.get("/api/intelligence/snapshots/{snapshot_id}")
def intelligence_snapshot(snapshot_id: str) -> dict[str, Any]:
    snapshot = _service().snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="market_snapshot_not_found")
    return snapshot.model_dump(mode="json")


@router.post("/api/intelligence/scans", status_code=202)
def create_intelligence_scan(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Body(default_factory=dict),
    wait: bool = Query(default=False),
) -> dict[str, Any]:
    service = _service()
    scan = service.create_scan(payload or None)
    if wait:
        snapshot = service.run_scan(scan["scan_id"])
        return {
            **(service.store.scan(scan["scan_id"]) or scan),
            "snapshot": snapshot.model_dump(mode="json"),
        }
    background_tasks.add_task(service.run_scan, scan["scan_id"])
    return scan


@router.get("/api/intelligence/scans/{scan_id}")
def intelligence_scan(scan_id: str) -> dict[str, Any]:
    scan = _service().store.scan(scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="market_scan_not_found")
    return scan


@router.post("/api/intelligence/snapshots/{snapshot_id}/model-overlay")
def update_model_overlay(
    snapshot_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    try:
        if payload.get("status") == "running":
            snapshot = _service().begin_model_overlay(
                snapshot_id,
                provider=payload.get("provider"),
                model_id=payload.get("model_id"),
            )
        else:
            snapshot = _service().apply_model_overlay(
                snapshot_id,
                succeeded=payload.get("succeeded") is True,
                provider=payload.get("provider"),
                model_id=payload.get("model_id"),
                summaries=payload.get("summaries"),
                receipt=payload.get("receipt"),
                error=payload.get("error"),
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="market_snapshot_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return snapshot.model_dump(mode="json")


def _candidate(entity_id: str):
    snapshot = _service().latest_snapshot()
    if snapshot is None:
        return snapshot, None
    key = str(entity_id).strip().upper()
    detail = snapshot.candidate_details.get(key)
    if detail is None:
        for item in snapshot.candidate_details.values():
            if item.symbol.split(".", 1)[0] == key:
                detail = item
                break
    return snapshot, detail


@router.get("/api/instruments/{entity_id}/workspace")
def instrument_workspace(entity_id: str) -> dict[str, Any]:
    snapshot, detail = _candidate(entity_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="instrument_not_in_latest_snapshot")
    context = _service().patch_context(
        {
            "selection": {
                "entity_id": detail.symbol,
                "symbol": detail.symbol,
                "candidate_id": detail.symbol,
            },
            "data": {"market_snapshot_id": snapshot.snapshot_id if snapshot else None},
        }
    )
    return {
        "schema_version": "stock_ai.instrument_workspace.v1",
        "instrument": {
            "symbol": detail.symbol,
            "name": detail.name,
            "exchange": detail.exchange,
            "industry": detail.industry,
        },
        "intelligence": detail.model_dump(mode="json"),
        "context": context.model_dump(mode="json"),
    }


@router.get("/api/instruments/{entity_id}/chart")
def instrument_chart(
    entity_id: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
) -> dict[str, Any]:
    _snapshot, detail = _candidate(entity_id)
    symbol = detail.symbol if detail else entity_id.strip().upper()
    return _cached_instrument_chart(
        symbol,
        start=start,
        end=end,
        limit=limit,
    )


@router.get("/api/instruments/{entity_id}/intelligence")
def instrument_intelligence(entity_id: str) -> dict[str, Any]:
    snapshot, detail = _candidate(entity_id)
    if detail is None or snapshot is None:
        raise HTTPException(status_code=404, detail="instrument_not_in_latest_snapshot")
    return {
        "schema_version": "stock_ai.instrument_intelligence.v1",
        "snapshot_id": snapshot.snapshot_id,
        "detail": detail.model_dump(mode="json"),
    }


@router.get("/api/instruments/{entity_id}/evidence")
def instrument_evidence(entity_id: str) -> dict[str, Any]:
    snapshot, detail = _candidate(entity_id)
    if detail is None or snapshot is None:
        raise HTTPException(status_code=404, detail="instrument_not_in_latest_snapshot")
    return {
        "schema_version": "stock_ai.instrument_evidence.v1",
        "snapshot_id": snapshot.snapshot_id,
        "symbol": detail.symbol,
        "items": [item.model_dump(mode="json") for item in detail.evidence],
        "data_quality": detail.data_quality.model_dump(mode="json"),
        "model_receipts": snapshot.model_receipts,
        "host_risk_status": detail.host_risk_status,
    }


@router.post("/api/alerts", status_code=201)
def create_alert(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    alert_type = str(payload.get("alert_type") or "price").strip()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    return _service().store.create_alert(
        alert_id=f"ALT-{uuid4().hex}",
        symbol=symbol,
        alert_type=alert_type,
        rule=payload.get("rule") if isinstance(payload.get("rule"), dict) else {},
    )


@router.post("/api/comparisons", status_code=201)
def create_comparison(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    symbols = list(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in payload.get("symbols") or []
            if str(symbol).strip()
        )
    )
    if not 2 <= len(symbols) <= 6:
        raise HTTPException(status_code=422, detail="comparison requires 2 to 6 symbols")
    result = _service().store.create_comparison(
        comparison_id=f"CMP-{uuid4().hex}",
        symbols=symbols,
    )
    _service().patch_context({"comparison": {"symbols": symbols}})
    return result


@router.post("/api/watchlists/{watchlist_id}/symbols", status_code=201)
def add_watchlist_symbol(
    watchlist_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    return _service().store.add_watchlist_symbol(
        watchlist_id=watchlist_id or "home-default",
        symbol=symbol,
    )


@router.post("/api/paper-trading/preview")
def workspace_paper_preview(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    request = PaperOrderRequest.model_validate(payload)
    return paper_training_preview(request)
