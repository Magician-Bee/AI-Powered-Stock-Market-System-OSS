import asyncio
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from xml.etree import ElementTree
import json
import hashlib
import inspect
import logging

import httpx
import yaml

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.types import MissingSymbolError, UniverseSnapshot

from .config import get_settings
from .data_quality_contracts import DataQuality, DecisionDataQualityReceipt
from .data_platform.identity import entity_id_for_symbol
from .data_platform.source_registry import get_source_registry, source_endpoint
from .daily_history import DailyHistoryQueryError, query_daily_history
from .models import (
    AITradePlanCard,
    AITradingAssistantWorkspace,
    AssetWorkspace,
    BrokerConnectionStatus,
    Entity,
    EventItem,
    LinkageExplanation,
    MarketSummary,
    NotificationPreview,
    OrderCostEstimate,
    PricePoint,
    ReadonlyWorkspaceMeta,
    RiskSummary,
    RiskWorkspace,
    ScreenerItem,
    TradingPreview,
    TradingWorkspace,
)
from .official_events import list_mops_company_events
from .realtime_data import fetch_yahoo_history, fetch_yahoo_summary, normalize_symbol
from .realtime_quotes import fetch_twse_mis_quote
from .taiwan_official import is_taiwan_code, normalize_taiwan_code, official_history, official_summary_payload, search_taiwan_official
from .shared_quality_store import SharedDecisionQualityStore
from .phase1_data import list_institutional_flows, list_monthly_revenues
from .valuation_percentiles import ValuationHistoryStore, build_valuation_percentiles


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _decision_quality_store():
    return SharedDecisionQualityStore()


def _persist_decision_quality_receipt(
    receipt: DecisionDataQualityReceipt,
    *,
    surface: str,
) -> None:
    _decision_quality_store().save(receipt, surface=surface)


from .twse_openapi import fetch_twse_openapi_path
from .screener_conditions import evaluate_conditions, parse_conditions

OVERVIEW_INDEX_SPECS: list[dict[str, str]] = [
    {"symbol": "^TWII", "label": "加權指數"},
    {"symbol": "^TWOII", "label": "櫃買指數"},
    {"symbol": "TX=F", "label": "台指期"},
    {"symbol": "^DJI", "label": "道瓊"},
    {"symbol": "^IXIC", "label": "NASDAQ"},
    {"symbol": "^GSPC", "label": "S&P 500"},
    {"symbol": "^SOX", "label": "費半"},
]


TAIWAN_SEARCH_ALIASES = {
    "台積電": "2330",
    "鴻海": "2317",
    "廣達": "2382",
    "聯發科": "2454",
    "台達電": "2308",
    "富邦金": "2881",
    "國泰金": "2882",
    "中華電": "2412",
    "南亞": "1303",
    "元大台灣50": "0050",
}

def _debug_report(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    """Compatibility no-op: production code never posts diagnostics to a fixed port."""
    return None


def load_catalog() -> dict[str, Any]:
    path: Path = get_settings().catalog_file
    if not path.exists():
        return {"error": f"catalog not found: {path}"}
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_supported_sources() -> dict[str, Any]:
    registry = get_source_registry()
    items = [
        {
            "id": source.source_id,
            "name": source.display_name,
            "category": source.authority,
            "market_scope": (
                ["taiwan", "global"]
                if source.source_id in {"yahoo_finance", "google_news"}
                else ["taiwan"]
            ),
            "status": "active" if source.active else "disabled",
            "coverage": source.domains,
            "reliability_note": (
                f"Tier {source.reliability_tier}; {source.license_status}"
            ),
        }
        for source in sorted(
            registry.sources.values(),
            key=lambda item: (item.priority, item.source_id),
        )
    ]
    return {
        "schema_version": "stock_ai.supported_sources.v2",
        "count": len(items),
        "items": items,
        "active_ids": [item["id"] for item in items if item["status"] == "active"],
        "planned_ids": [],
    }


def _looks_like_symbol(q: str) -> bool:
    s = q.strip().upper().replace(" ", "")
    return bool(s) and (any(ch.isdigit() for ch in s) or "." in s or s.startswith("^") or s.isascii())


def search_entities(q: str = ""):
    """Real securities search only: official Taiwan search first, Yahoo for global tickers.

    No demo/sample securities are returned. Empty search is an empty result rather
    than an implicit popular-stock Universe.
    """
    query = q.strip()
    results: list[Entity] = []

    if not query:
        return []

    try:
        results.extend(search_taiwan_official(query, limit=30))
    except Exception as exc:
        logger.debug("Official Taiwan entity search unavailable: %s", exc, exc_info=True)

    if results:
        return results

    alias_code = TAIWAN_SEARCH_ALIASES.get(query)
    if alias_code:
        try:
            results.extend(search_taiwan_official(alias_code, limit=30))
        except Exception as exc:
            logger.debug("Official alias entity search unavailable: %s", exc, exc_info=True)
        if results:
            return results

    # Realtime-only UI rule: do not surface Yahoo/global symbols unless a true
    # intraday provider is wired for them. A clickable result implies the detail
    # panel can render live quote/chart fields, so with the current TWSE MIS
    # provider we only expose Taiwan official search results.
    return []


def get_entity(symbol: str):
    try:
        if is_taiwan_code(symbol):
            return official_summary_payload(symbol)["entity"]
    except Exception as exc:
        logger.debug("Official entity lookup unavailable: %s", exc, exc_info=True)
    try:
        return fetch_yahoo_summary(symbol)["entity"]
    except Exception:
        return None


def _linked_factors_for(entity: Entity) -> list[str]:
    if entity.market == "taiwan":
        return ["加權指數", "櫃買指數", "USD/TWD", "NASDAQ", "SOX", "US10Y"]
    return ["S&P 500", "NASDAQ", "US10Y", "DXY"]


def _event_sort_key(event: EventItem) -> datetime:
    raw = (event.event_time or "").strip()
    for parser in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ):
        try:
            parsed = parser(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _event_source_priority(event: EventItem) -> int:
    event_type = (event.event_type or "").lower()
    source = get_source_registry().identify_url(event.source_url)
    if source and source.source_id == "mops":
        return 3
    if source and source.authority.startswith("official_exchange"):
        return 2
    if event_type in {"material_event", "announcement"}:
        return 1
    return 0


def _row_matches_symbol(row: dict[str, Any], symbol: str, entity_name: str | None = None) -> bool:
    code = normalize_taiwan_code(symbol) if is_taiwan_code(symbol) else normalize_symbol(symbol)
    base_tokens = {
        code.upper(),
        symbol.strip().upper(),
        symbol.strip().upper().replace(".TW", "").replace(".TWO", ""),
    }
    if entity_name:
        base_tokens.add(entity_name.strip().upper())
    haystack = " ".join(str(v or "").upper() for v in row.values())
    return any(token and token in haystack for token in base_tokens)


def _event_from_row(
    row: dict[str, Any],
    *,
    event_id_prefix: str,
    index: int,
    symbol: str,
    event_type: str,
    default_url: str,
    confidence: float,
) -> EventItem | None:
    title_keys = ("標題", "主旨", "事實發生日", "公司名稱", "名稱", "headline", "title")
    title = next((str(row.get(key)).strip() for key in title_keys if row.get(key)), "").strip()
    if not title:
        values = [str(v).strip() for v in row.values() if str(v or "").strip()]
        title = " / ".join(values[:2]) if values else ""
    if not title:
        return None
    time_keys = ("發言日期", "發言時間", "日期", "eventDate", "time", "date")
    event_time = next((str(row.get(key)).strip() for key in time_keys if row.get(key)), "")
    summary_parts = [f"{k}: {v}" for k, v in row.items() if str(v or "").strip()][:6]
    return EventItem(
        event_id=f"{event_id_prefix}-{normalize_symbol(symbol)}-{index}",
        event_time=event_time or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        related_symbols=[normalize_symbol(symbol)],
        event_type=event_type,
        title=title,
        summary="；".join(summary_parts)[:500],
        sentiment="neutral",
        estimated_impact_direction="mixed",
        confidence=confidence,
        source_url=default_url,
    )


def _fetch_twse_symbol_events(symbol: str, entity_name: str | None = None, limit: int = 10) -> list[EventItem]:
    if not is_taiwan_code(symbol):
        return []
    endpoint_defs = [
        ("/opendata/t187ap04_L", "material_event", "twse-material", 0.82),
        ("/announcement/notice", "announcement", "twse-notice", 0.7),
        ("/announcement/punish", "announcement", "twse-punish", 0.7),
        ("/news/newsList", "news", "twse-news", 0.6),
        ("/news/eventList", "news", "twse-event", 0.55),
    ]
    events: list[EventItem] = []
    for path, event_type, prefix, confidence in endpoint_defs:
        try:
            rows = fetch_twse_openapi_path(path, limit=200).get("data") or []
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        matched = [row for row in rows if isinstance(row, dict) and _row_matches_symbol(row, symbol, entity_name)]
        for idx, row in enumerate(matched[:limit], start=1):
            item = _event_from_row(
                row,
                event_id_prefix=prefix,
                index=idx,
                symbol=symbol,
                event_type=event_type,
                default_url=source_endpoint("twse_dynamic_openapi", path=path),
                confidence=confidence,
            )
            if item:
                events.append(item)
    return events


def _fetch_twse_symbol_events_brief(symbol: str, entity_name: str | None = None, limit: int = 3) -> list[EventItem]:
    if not is_taiwan_code(symbol):
        return []
    endpoint_defs = [
        ("/opendata/t187ap04_L", "material_event", "twse-material", 0.82),
        ("/news/newsList", "news", "twse-news", 0.6),
    ]
    events: list[EventItem] = []
    for path, event_type, prefix, confidence in endpoint_defs:
        try:
            rows = fetch_twse_openapi_path(path, limit=120).get("data") or []
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        matched = [row for row in rows if isinstance(row, dict) and _row_matches_symbol(row, symbol, entity_name)]
        for idx, row in enumerate(matched[:limit], start=1):
            item = _event_from_row(
                row,
                event_id_prefix=prefix,
                index=idx,
                symbol=symbol,
                event_type=event_type,
                default_url=source_endpoint("twse_dynamic_openapi", path=path),
                confidence=confidence,
            )
            if item:
                events.append(item)
                if len(events) >= limit:
                    return events
    return events


def _fetch_google_news_events(symbol: str, entity_name: str | None = None, limit: int = 8) -> list[EventItem]:
    query_tokens = [symbol.strip()]
    if entity_name:
        query_tokens.append(entity_name.strip())
    if is_taiwan_code(symbol):
        query_tokens.extend(["台股", "股票"])
    query = " ".join(token for token in query_tokens if token)
    url = source_endpoint("google_news_search", query=quote_plus(query))
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"}) as client:
            response = default_external_transport_guard().call_sync(
                "source:google_news:events",
                lambda: client.get(url),
            )
            xml_text = response.text
        root = ElementTree.fromstring(xml_text)
    except Exception:
        return []
    items: list[EventItem] = []
    for idx, node in enumerate(root.findall(".//item")[:limit], start=1):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        event_time = (node.findtext("pubDate") or "").strip()
        summary = (node.findtext("description") or title or "Google News item").strip()
        # A feed item without the original publisher time can be shown only
        # after a user explicitly requests a live refresh; it cannot become
        # historical research evidence, so do not manufacture ``now`` here.
        if not title or not event_time:
            continue
        items.append(
            EventItem(
                event_id=f"google-news-{normalize_symbol(symbol)}-{idx}",
                event_time=event_time,
                related_symbols=[normalize_symbol(symbol)],
                event_type="news",
                title=title,
                summary=summary[:500],
                sentiment="neutral",
                estimated_impact_direction="mixed",
                confidence=0.4,
                source_url=link or str(get_source_registry().source("google_news").base_url),
            )
        )
    return items


def get_market_events(symbol: str, limit: int = 20) -> list[EventItem]:
    entity = get_entity(symbol)
    entity_name = entity.name if entity else None
    events: list[EventItem] = []
    events.extend(list_mops_company_events(symbol=symbol, limit=limit))
    events.extend(_fetch_twse_symbol_events(symbol, entity_name=entity_name, limit=limit))
    try:
        events.extend(fetch_yahoo_summary(symbol).get("events", [])[:limit])
    except Exception as exc:
        logger.debug("Optional Yahoo event source unavailable: %s", exc, exc_info=True)
    events.extend(_fetch_google_news_events(symbol, entity_name=entity_name, limit=limit))

    deduped: list[EventItem] = []
    seen: set[str] = set()
    for item in sorted(events, key=lambda event: (_event_source_priority(event), _event_sort_key(event)), reverse=True):
        key = f"{item.source_url}|{item.title}".strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


@lru_cache(maxsize=256)
def _cached_brief_events(symbol: str, entity_name: str | None, limit: int) -> tuple[EventItem, ...]:
    events = list_mops_company_events(symbol=symbol, limit=limit)
    if len(events) < limit:
        events.extend(_fetch_twse_symbol_events_brief(symbol, entity_name=entity_name, limit=limit - len(events)))
    return tuple(sorted(events, key=_event_sort_key, reverse=True)[:limit])


def get_market_events_brief(symbol: str, limit: int = 3, entity_name: str | None = None) -> list[EventItem]:
    if entity_name is None:
        entity = get_entity(symbol)
        entity_name = entity.name if entity else None
    return list(_cached_brief_events(symbol, entity_name, limit))


def clear_market_event_caches() -> None:
    _cached_brief_events.cache_clear()


def get_market_overview(symbol: str) -> dict[str, Any]:
    summary = get_market_summary(symbol)
    return {
        "symbol": symbol,
        "summary": summary.model_dump() if summary else None,
        "history": [p.model_dump() for p in get_price_history(symbol)],
        "events": [e.model_dump() for e in get_market_events(symbol)],
        "sources": list_supported_sources(),
    }


def _summary_from_real_payload(
    payload: dict[str, Any],
    *,
    persist_quality_receipt: bool = False,
    cross_source_observation: dict[str, Any] | None = None,
) -> MarketSummary:
    entity = payload["entity"]
    points = payload["history"]
    latest = payload["latest"]
    if len(points) >= 2:
        trend = "uptrend" if latest.close > points[0].close else "downtrend"
    else:
        trend = "unknown"
    summary = MarketSummary(
        entity=entity,
        latest_price=latest,
        change_percent=payload["change_percent"],
        trend=trend,
        fundamentals=None,
        flow=None,
        events=payload.get("events", []),
        linked_factors=_linked_factors_for(entity),
        data_source=payload["source"],
        data_timestamp=payload["data_timestamp"],
        freshness_note=payload["freshness_note"],
        reliability_note=payload["reliability_note"],
        provider_id="tpex_openapi" if str(entity.exchange).upper() == "TPEX" else "twse_openapi",
        connector_id="stock_ai.taiwan_official.official_summary_payload",
        quote_kind="official_close",
        authorized=True,
        realtime=False,
        delayed=False,
        official_close=True,
        max_age_seconds=345600,
    )
    return _attach_market_summary_quality_receipt(
        summary,
        persist=persist_quality_receipt,
        cross_source_observation=cross_source_observation,
    )


def _attach_market_summary_quality_receipt(
    summary: MarketSummary,
    *,
    persist: bool = False,
    cross_source_observation: dict[str, Any] | None = None,
) -> MarketSummary:
    """Bind the legacy summary surface to the shared quality receipt contract.

    MarketSummary keeps the primary official/MIS quote authoritative.  When a
    same-day independent Yahoo observation is available, it is recorded only as
    quality evidence; it never replaces the primary quote or makes delayed data
    execution-eligible.
    """

    latest = summary.latest_price
    missing_fields = [
        field_name
        for field_name, value in {
            "data_source": summary.data_source,
            "quote_time": latest.date,
            "close": latest.close,
            "volume": latest.volume,
        }.items()
        if value is None or value == ""
    ]
    quality_status = (
        "ready"
        if summary.realtime and not summary.delayed and not missing_fields
        else "partial"
        if not missing_fields
        else "insufficient"
    )
    quality = DataQuality(
        status=quality_status,
        score=1.0 if quality_status == "ready" else 0.6 if quality_status == "partial" else 0.0,
        source=summary.data_source,
        data_as_of=summary.data_timestamp or latest.date,
        fallback=bool(summary.delayed or summary.official_close),
        missing_fields=missing_fields,
        quality_flags=(
            ["cross_source_observed"]
            if cross_source_observation
            and cross_source_observation.get("status") == "observed"
            else ["cross_source_observation_not_available"]
        ),
        source_observation=cross_source_observation or {},
    )
    if cross_source_observation and cross_source_observation.get("status") == "conflict":
        quality.status = "conflict"
        quality.quality_flags = ["cross_source_conflict"]
    summary.data_quality_receipt = DecisionDataQualityReceipt.issue(
        snapshot_id=f"SUMMARY-{summary.entity.symbol}-{latest.date}",
        symbol=summary.entity.symbol,
        decision_at=datetime.now(timezone.utc).isoformat(),
        snapshot_data_as_of=datetime.now(timezone.utc).isoformat(),
        data_quality=quality,
        evidence_ids=[
            f"market-summary:{summary.entity.symbol}",
            f"quote:{summary.data_source}:{latest.date}",
        ],
    )
    if persist:
        _persist_decision_quality_receipt(
            summary.data_quality_receipt,
            surface="market_summary",
        )
    return summary


def _observe_independent_same_day_quote(
    symbol: str,
    *,
    primary_close: float | None,
    primary_date: str | None,
) -> dict[str, Any]:
    """Compare an independent daily observation without changing quote truth."""

    if primary_close is None or not primary_date:
        return {"status": "not_observed", "reason": "primary_quote_missing"}
    primary_day = _observation_day(primary_date)
    if primary_day is None:
        return {"status": "not_observed", "reason": "primary_date_unparseable"}
    try:
        payload = fetch_yahoo_summary(symbol)
        points = payload.get("points") or []
        secondary = next(
            (
                point
                for point in reversed(points)
                if _observation_day(getattr(point, "date", "")) == primary_day
            ),
            None,
        )
        secondary_close = getattr(secondary, "close", None)
        if secondary_close is None:
            return {
                "status": "not_observed",
                "reason": "independent_source_date_not_available",
                "primary_day": primary_day,
                "secondary_source": "yahoo_finance",
            }
        primary_value = float(primary_close)
        secondary_value = float(secondary_close)
        tolerance = max(0.02, abs(primary_value) * 0.01)
        difference = abs(primary_value - secondary_value)
        status = "observed" if difference <= tolerance else "conflict"
        return {
            "status": status,
            "primary_source": "primary_market_quote",
            "secondary_source": str(payload.get("source") or "yahoo_finance"),
            "primary_day": primary_day,
            "secondary_day": _observation_day(getattr(secondary, "date", "")),
            "primary_close": primary_value,
            "secondary_close": secondary_value,
            "absolute_difference": round(difference, 8),
            "tolerance": round(tolerance, 8),
        }
    except Exception as exc:
        return {
            "status": "not_observed",
            "reason": "independent_source_unavailable",
            "primary_day": primary_day,
            "secondary_source": "yahoo_finance",
            "error_type": type(exc).__name__,
        }


def _observation_day(value: object) -> str | None:
    """Normalize ISO, slash-delimited and TW compact quote dates."""

    text = str(value or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) >= 10 and text[4] in "-/" and text[7] in "-/":
        return f"{text[:4]}-{text[5:7]}-{text[8:10]}"
    return None


def get_market_summary(symbol: str, *, include_events: bool = True) -> MarketSummary | None:
    _debug_report("H1", "services.py:get_market_summary", "summary-start", {"symbol": symbol, "is_taiwan_code": is_taiwan_code(symbol)})
    # Realtime-only safety rule for the visible UI and chat answers:
    # Taiwan detail/summary must come from the same intraday payload as the chart.
    # If MIS is unavailable, return None; never fall back to end-of-day official
    # close/history or Yahoo and present it as a current decision input.
    if is_taiwan_code(symbol):
        try:
            rt = asyncio.run(fetch_twse_mis_quote(symbol))["data"]
            _debug_report(
                "H3",
                "services.py:get_market_summary",
                "taiwan-realtime-received",
                {
                    "symbol": symbol,
                    "payload_symbol": rt.get("symbol"),
                    "exchange": rt.get("exchange"),
                    "has_last_price": rt.get("last_price") is not None,
                },
            )
            code = rt.get("symbol") or normalize_taiwan_code(symbol)
            exchange = "TPEx" if rt.get("exchange") == "otc" else "TWSE"
            display_symbol = f"{code}.{'TWO' if exchange == 'TPEx' else 'TW'}"
            ent = Entity(
                entity_id=entity_id_for_symbol(
                    display_symbol,
                    market="taiwan",
                    exchange=exchange,
                    source_code=str(code),
                ),
                symbol=display_symbol,
                name=rt.get("name") or code,
                entity_type="etf" if str(code).startswith("00") else "stock",
                market="taiwan",
                exchange=exchange,
                currency="TWD",
            )
            bid = (rt.get("bids") or [{}])[0].get("price")
            ask = (rt.get("asks") or [{}])[0].get("price")
            has_last_trade = rt.get("last_price") is not None
            price_basis = "last_trade" if has_last_trade else "bid_ask_midpoint"
            display_price = rt.get("last_price") if has_last_trade else ((bid + ask) / 2 if bid is not None and ask is not None else None)
            if display_price is None:
                _debug_report("H3", "services.py:get_market_summary", "taiwan-display-price-missing", {"symbol": symbol, "bid": bid, "ask": ask})
                return None
            latest = PricePoint(
                date=f"{rt.get('date', '')} {rt.get('time', '')}".strip(),
                open=rt.get("open") or display_price,
                high=rt.get("high") or display_price,
                low=rt.get("low") or display_price,
                close=display_price,
                volume=rt.get("total_volume_shares") or 0,
            )
            change_percent = rt.get("change_percent")
            if change_percent is None and rt.get("previous_close"):
                change_percent = round((display_price - rt.get("previous_close")) / rt.get("previous_close") * 100, 4)
            summary = MarketSummary(
                entity=ent,
                latest_price=latest,
                change_percent=change_percent if change_percent is not None else 0.0,
                trend=f"intraday_realtime_{price_basis}",
                fundamentals=None,
                flow=None,
                # A screener only needs the quote payload and its quality
                # receipt.  Event aggregation fans out to several official
                # and auxiliary endpoints per symbol, so keep it out of the
                # bounded realtime scan path while preserving the detailed
                # summary behavior for the dashboard and stock page.
                events=get_market_events(symbol, limit=10) if include_events else [],
                linked_factors=_linked_factors_for(ent),
                data_source=f"TWSE MIS public intraday quote endpoint ({price_basis})",
                data_timestamp=rt.get("received_at"),
                freshness_note=(
                    f"盤中公開網頁行情，更新間隔約 {int((rt.get('user_delay_ms') or 5000)/1000)} 秒；"
                    f"頁面上列出的行情參數只使用此即時 payload。價格基準={price_basis}；"
                    "若沒有最後成交價，系統只可標示委買委賣中價，不可冒充成交/收盤價。"
                ),
                reliability_note="免費公開網頁端點適合個人本機觀看；商業轉散布需正式授權。",
                provider_id="twse_mis",
                connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
                quote_kind=price_basis,
                authorized=False,
                realtime=has_last_trade,
                delayed=False,
                official_close=False,
                max_age_seconds=max(15, int((rt.get("user_delay_ms") or 5000) / 1000) * 3),
                limit_up=rt.get("limit_up"),
                limit_down=rt.get("limit_down"),
                trading_state=str(rt.get("trading_status") or "unknown"),
                buy_liquidity_confirmed=bool(rt.get("asks")),
                sell_liquidity_confirmed=bool(rt.get("bids")),
            )
            return _attach_market_summary_quality_receipt(
                summary,
                persist=True,
                cross_source_observation=_observe_independent_same_day_quote(
                    display_symbol,
                    primary_close=display_price,
                    primary_date=latest.date,
                ),
            )
        except Exception as exc:
            _debug_report("H3", "services.py:get_market_summary", "taiwan-summary-exception", {"symbol": symbol, "error": str(exc)})
            return None

    # Non-Taiwan/global: no current realtime provider is connected in this app.
    # Return no summary rather than showing delayed Yahoo/latest-available data.
    _debug_report("H1", "services.py:get_market_summary", "non-taiwan-summary-unsupported", {"symbol": symbol})
    return None


def get_market_detail_summary(symbol: str) -> MarketSummary | None:
    """Return a usable detail summary without mislabeling delayed data.

    Trading decisions keep using ``get_market_summary`` and therefore only
    receive realtime Taiwan quotes. Detail pages must also open outside market
    hours or during a temporary MIS outage, so they may use the latest official
    exchange close while preserving its source and freshness labels.
    """
    realtime = get_market_summary(symbol)
    if realtime is not None:
        return realtime
    if not is_taiwan_code(symbol):
        return None
    try:
        payload = official_summary_payload(symbol)
        latest = payload["latest"]
        return _summary_from_real_payload(
            payload,
            persist_quality_receipt=True,
            cross_source_observation=_observe_independent_same_day_quote(
                symbol,
                primary_close=latest.close,
                primary_date=latest.date,
            ),
        )
    except Exception as exc:
        _debug_report(
            "H3",
            "services.py:get_market_detail_summary",
            "official-detail-fallback-failed",
            {"symbol": symbol, "error": str(exc)},
        )
        return None


def get_execution_price_summary(symbol: str) -> MarketSummary | None:
    """Return a price summary suitable for the local Paper Broker.

    The dashboard correctly prefers a live bid/ask midpoint when no last trade
    has printed yet.  That midpoint is useful research context but it is not a
    fillable execution price.  A local paper order must instead use the latest
    signed official close when the realtime surface lacks an actual trade.
    This preserves the UI's live display while making the paper lane usable at
    auction/opening gaps and outside normal trading hours.
    """

    detail = get_market_detail_summary(symbol)
    if detail is not None and str(detail.quote_kind or "") in {"last_trade", "official_close"}:
        return detail
    if not is_taiwan_code(symbol):
        return detail
    try:
        payload = official_summary_payload(symbol)
        latest = payload["latest"]
        return _summary_from_real_payload(
            payload,
            persist_quality_receipt=True,
            cross_source_observation=_observe_independent_same_day_quote(
                symbol,
                primary_close=latest.close,
                primary_date=latest.date,
            ),
        )
    except Exception as exc:
        _debug_report(
            "H3",
            "services.py:get_execution_price_summary",
            "official-execution-fallback-failed",
            {"symbol": symbol, "error": str(exc)},
        )
        return detail


def get_overview_indices() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for spec in OVERVIEW_INDEX_SPECS:
        symbol = spec["symbol"]
        label = spec["label"]
        try:
            payload = fetch_yahoo_summary(symbol)
            points = payload.get("points") or []
            latest = points[-1] if points else None
            prev = points[-2] if len(points) >= 2 else latest
            latest_close = float(latest.close) if latest else None
            prev_close = float(prev.close) if prev else None
            change_pct = round((latest_close - prev_close) / prev_close * 100, 2) if latest_close is not None and prev_close not in (None, 0) else 0.0
            items.append(
                {
                    "symbol": symbol,
                    "label": label,
                    "priceText": f"{latest_close:,.2f}" if latest_close is not None else "-",
                    "changeText": f"{change_pct:+.2f}%",
                    "changeClass": "tw-red" if change_pct >= 0 else "tw-green",
                    "source": payload.get("source") or "Yahoo Finance latest-available",
                    "available": latest_close is not None,
                }
            )
        except Exception as exc:
            items.append(
                {
                    "symbol": symbol,
                    "label": label,
                    "priceText": "-",
                    "changeText": "-",
                    "changeClass": "tw-green",
                    "source": f"暫時無法取得：{type(exc).__name__}",
                    "available": False,
                }
            )
    return {"count": len(items), "items": items}


def get_price_history_payload(
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    cursor: str | None = None,
    limit: int = 5000,
    refresh: bool | None = None,
    allow_fallback: bool = True,
    price_basis: str = "unadjusted",
    refresh_adjustments: bool | None = None,
    require_complete_adjustment: bool = True,
) -> dict[str, Any]:
    if (
        start is not None
        or end is not None
        or cursor is not None
        or refresh is not None
        or price_basis != "unadjusted"
        or refresh_adjustments is not None
    ):
        if not start:
            raise DailyHistoryQueryError(
                "start is required for a complete historical range query"
            )
        return query_daily_history(
            symbol,
            start=start,
            end=end or date.today().isoformat(),
            cursor=cursor,
            limit=limit,
            refresh=True if refresh is None else refresh,
            allow_fallback=allow_fallback,
            price_basis=price_basis,
            refresh_adjustments=refresh_adjustments,
            require_complete_adjustment=require_complete_adjustment,
        )
    official_points = []
    errors: list[str] = []
    try:
        if is_taiwan_code(symbol):
            official_points = official_history(symbol)
            if len(official_points) >= 20:
                points = official_points
                source = "TWSE/TPEx official monthly history"
            else:
                points = []
                source = ""
        else:
            points = []
            source = ""
    except Exception as exc:
        points = []
        source = ""
        errors.append(f"official:{type(exc).__name__}")

    # Yahoo is a secondary continuity source only when the official monthly
    # history is temporarily unavailable or genuinely shorter than the chart.
    if not points:
        try:
            yahoo_points = fetch_yahoo_history(symbol)["points"]
            if len(yahoo_points) > len(official_points):
                points = yahoo_points
                source = "Yahoo Finance historical fallback"
        except Exception as exc:
            errors.append(f"fallback:{type(exc).__name__}")
    if not points:
        points = official_points
        source = "TWSE/TPEx official latest available"

    count = len(points)
    indicators = {name: count >= window for name, window in {"MA5": 5, "MA10": 10, "MA20": 20, "MA60": 60, "BOLL(20,2)": 20, "MACD": 35}.items()}
    return {
        "symbol": symbol,
        "points": points,
        "source": source,
        "point_count": count,
        "history_start": points[0].date if points else None,
        "history_end": points[-1].date if points else None,
        "available_indicators": indicators,
        "quality": "ready" if count >= 60 else "limited" if count else "unavailable",
        "note": (
            "歷史資料足以計算 MA60。"
            if count >= 60
            else f"目前只有 {count} 根有效日 K；只顯示資料量足夠的技術指標，可能是近期上市或來源暫時不完整。"
        ),
        "errors": errors,
    }


def get_price_history(symbol: str):
    return get_price_history_payload(symbol)["points"]


def explain_linkage(source: str, target: str) -> LinkageExplanation:
    target_symbol = normalize_symbol(target)
    source_u = source.strip().upper()
    direction = "unknown"
    confidence = 0.35
    mechanism = "目前只有基礎規則說明，尚未完成以歷史資料驗證的個股連動模型；系統不會把相關性硬說成因果。"
    path = [source, "市場變數", target_symbol]
    evidence = ["rule_template_only", "需要補接歷史相關性/事件研究"]
    if source_u in {"SOX", "^SOX", "費半"}:
        direction = "mixed"
        confidence = 0.55
        mechanism = "費半代表全球半導體風險偏好；對台股半導體可能有資金與情緒連動，但個股仍需看公司基本面與當日事件。"
        path = ["SOX/費半", "全球半導體風險偏好", "台股電子/半導體", target_symbol]
        evidence = ["產業指數連動規則", "尚未完成即時計量驗證"]
    elif source_u in {"US10Y", "美債", "利率"}:
        direction = "mixed"
        confidence = 0.5
        mechanism = "美債殖利率影響折現率與外資風險偏好，可能影響高估值科技股，但方向需看當時成長預期與市場情緒。"
        path = ["US10Y", "折現率/風險偏好", "成長股估值", target_symbol]
        evidence = ["總經傳導規則", "尚未完成即時計量驗證"]
    return LinkageExplanation(source=source, target=target_symbol, direction=direction, confidence=confidence, mechanism=mechanism, path=path, evidence=evidence, affected_symbols=[])


_PERSISTED_SCREENER_FIELDS = frozenset(
    {
        "revenue_yoy",
        "institutional_buy_5d",
        "pe_percentile",
        "avg_turnover_20d",
        "sma_60",
    }
)


def _screener_pe_percentile(symbol: str) -> tuple[float | None, dict[str, Any]]:
    """Read the cached five-year PE percentile without causing a market refresh."""

    try:
        from open_stock_ai.runtime import get_runtime_engine

        trade_store = get_runtime_engine().pipeline.trade_store
        if trade_store is None:
            return None, {"status": "unavailable", "reason": "paper_store_unavailable"}
        samples = ValuationHistoryStore(trade_store.store).query(symbol)
        payload = build_valuation_percentiles(symbol=symbol, samples=samples)
    except Exception as exc:
        return None, {
            "status": "unavailable",
            "reason": f"valuation_cache_read_failed:{type(exc).__name__}",
        }
    pe = next((item for item in payload["metrics"] if item["code"] == "pe"), None)
    window = next(
        (item for item in (pe or {}).get("windows", []) if item["window"] == "5y"),
        None,
    )
    value = (window or {}).get("percentile")
    if value is None:
        return None, {
            "status": "unavailable",
            "reason": "pe_percentile_5y_unavailable",
            "sample_count": (window or {}).get("sample_count", 0),
            "source_ids": payload.get("coverage", {}).get("source_ids", []),
        }
    return float(value), {
        "status": str((window or {}).get("status") or "partial"),
        "source_ids": payload.get("coverage", {}).get("source_ids", []),
        "data_as_of": payload.get("latest_date"),
        "sample_count": (window or {}).get("sample_count"),
        "percentile_window": "5y",
        "percentile_method": payload.get("percentile_method"),
    }


def _screener_persisted_values(
    symbol: str,
    *,
    fields: set[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return only requested non-quote screen facts plus their source receipt.

    The realtime scan remains bounded: these factors use already persisted
    official/contracted records only. A missing cache or incomplete history is
    represented as unavailable, so it cannot silently make a condition pass.
    """

    values: dict[str, Any] = {}
    receipt: dict[str, dict[str, Any]] = {}

    if "revenue_yoy" in fields:
        try:
            rows = list_monthly_revenues(symbol=symbol, limit=12, allow_network=False)
            latest = max(rows, key=lambda item: str(item.period), default=None)
            value = latest.yoy_change_percent if latest is not None else None
            if value is not None:
                values["revenue_yoy"] = float(value)
                receipt["revenue_yoy"] = {
                    "status": "available",
                    "source": latest.source,
                    "data_as_of": latest.published_at or latest.report_date or latest.period,
                    "period": latest.period,
                    "publication_time_status": latest.publication_time_status,
                }
            else:
                receipt["revenue_yoy"] = {
                    "status": "unavailable",
                    "reason": "official_revenue_yoy_not_cached",
                }
        except Exception as exc:
            receipt["revenue_yoy"] = {
                "status": "unavailable",
                "reason": f"official_revenue_cache_read_failed:{type(exc).__name__}",
            }

    if "institutional_buy_5d" in fields:
        try:
            rows = sorted(
                list_institutional_flows(symbol=symbol, limit=20, allow_network=False),
                key=lambda item: str(item.trade_date),
                reverse=True,
            )
            sessions: list[Any] = []
            seen_dates: set[str] = set()
            for row in rows:
                if row.trade_date in seen_dates:
                    continue
                seen_dates.add(row.trade_date)
                sessions.append(row)
                if len(sessions) == 5:
                    break
            if len(sessions) == 5:
                values["institutional_buy_5d"] = float(
                    sum(item.total_institutional_net for item in sessions)
                )
                receipt["institutional_buy_5d"] = {
                    "status": "available",
                    "source": sorted({item.source for item in sessions}),
                    "data_as_of": sessions[0].trade_date,
                    "session_count": len(sessions),
                    "session_start": sessions[-1].trade_date,
                }
            else:
                receipt["institutional_buy_5d"] = {
                    "status": "unavailable",
                    "reason": "five_distinct_official_institutional_sessions_not_cached",
                    "session_count": len(sessions),
                }
        except Exception as exc:
            receipt["institutional_buy_5d"] = {
                "status": "unavailable",
                "reason": f"official_institutional_cache_read_failed:{type(exc).__name__}",
            }

    if fields.intersection({"avg_turnover_20d", "sma_60"}):
        try:
            end = date.today()
            history = query_daily_history(
                symbol,
                start=(end - timedelta(days=150)).isoformat(),
                end=end.isoformat(),
                limit=120,
                refresh=False,
                allow_fallback=False,
            )
            points = list(history.get("points") or [])
            source_meta = {
                "source": history.get("source_ids", []),
                "data_as_of": getattr(points[-1], "date", None) if points else None,
                "range_complete": history.get("range_complete"),
                "fallback_count": history.get("fallback_count"),
                "price_basis": history.get("price_basis"),
            }
            if "avg_turnover_20d" in fields:
                turnovers = [
                    float(item.turnover)
                    for item in points[-20:]
                    if getattr(item, "turnover", None) is not None
                ]
                if len(turnovers) == 20:
                    values["avg_turnover_20d"] = sum(turnovers) / len(turnovers)
                    receipt["avg_turnover_20d"] = {
                        "status": "available",
                        **source_meta,
                        "session_count": len(turnovers),
                    }
                else:
                    receipt["avg_turnover_20d"] = {
                        "status": "unavailable",
                        **source_meta,
                        "reason": "twenty_official_turnover_sessions_not_cached",
                        "session_count": len(turnovers),
                    }
            if "sma_60" in fields:
                closes = [
                    float(item.close)
                    for item in points[-60:]
                    if getattr(item, "close", None) is not None
                ]
                if len(closes) == 60:
                    values["sma_60"] = sum(closes) / len(closes)
                    receipt["sma_60"] = {
                        "status": "available",
                        **source_meta,
                        "session_count": len(closes),
                    }
                else:
                    receipt["sma_60"] = {
                        "status": "unavailable",
                        **source_meta,
                        "reason": "sixty_official_close_sessions_not_cached",
                        "session_count": len(closes),
                    }
        except Exception as exc:
            for field in fields.intersection({"avg_turnover_20d", "sma_60"}):
                receipt[field] = {
                    "status": "unavailable",
                    "reason": f"official_daily_history_cache_read_failed:{type(exc).__name__}",
                }

    if "pe_percentile" in fields:
        value, detail = _screener_pe_percentile(symbol)
        receipt["pe_percentile"] = detail
        if value is not None:
            values["pe_percentile"] = value

    return values, receipt


def run_screener(
    conditions: list[str] | None = None,
    *,
    symbols: list[str] | tuple[str, ...] | None = None,
):
    parsed_conditions = parse_conditions(conditions)
    persisted_fields = {
        condition.field
        for condition in parsed_conditions
        if condition.field in _PERSISTED_SCREENER_FIELDS
    }
    persisted_fields.update(
        str(condition.value)
        for condition in parsed_conditions
        if condition.value_is_field and str(condition.value) in _PERSISTED_SCREENER_FIELDS
    )
    decision_at = datetime.now(timezone.utc).isoformat()
    snapshot_payload = {
        "conditions": list(conditions or []),
        "symbols": list(symbols or ()),
        "decision_at": decision_at,
    }
    snapshot_id = "SCREEN-" + hashlib.sha256(
        json.dumps(snapshot_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    # Realtime-only quote universe. Do not mix Yahoo/global/latest-available or
    # historical volume ratios into a table labelled as current.
    items: list[ScreenerItem] = []
    try:
        summary_supports_event_switch = "include_events" in inspect.signature(get_market_summary).parameters
    except (TypeError, ValueError):
        summary_supports_event_switch = True
    for symbol in symbols or ():
        # Keep lightweight test doubles and downstream integrations that still
        # expose the original one-argument callable compatible. The production
        # callable takes the keyword and avoids event fan-out.
        summary = (
            get_market_summary(symbol, include_events=False)
            if summary_supports_event_switch
            else get_market_summary(symbol)
        )
        if not summary:
            continue
        latest = summary.latest_price
        values = {
            "change_percent": summary.change_percent,
            "close": latest.close,
            "volume": latest.volume,
            "exchange": summary.entity.exchange,
            "trading_state": summary.trading_state,
            "is_etf": summary.entity.entity_type == "etf",
            "realtime": summary.realtime,
            "buy_liquidity_confirmed": summary.buy_liquidity_confirmed,
            "sell_liquidity_confirmed": summary.sell_liquidity_confirmed,
        }
        persisted_values, persisted_receipt = _screener_persisted_values(
            summary.entity.symbol,
            fields=persisted_fields,
        )
        values.update(persisted_values)
        evaluations = evaluate_conditions(values, parsed_conditions)
        if not all(item.passed for item in evaluations):
            continue
        quality_flags: list[str] = []
        if summary.delayed:
            quality_flags.append("quote_delayed")
        if not summary.realtime:
            quality_flags.append("realtime_not_confirmed")
        if summary.freshness_note:
            quality_flags.append(str(summary.freshness_note))
        missing_fields = [
            field_name
            for field_name, value in {
                "data_source": summary.data_source,
                "quote_time": latest.date,
                "close": latest.close,
                "volume": latest.volume,
            }.items()
            if value is None or value == ""
        ]
        quality_status = (
            "ready"
            if summary.realtime and not summary.delayed and not missing_fields
            else "partial"
            if not missing_fields
            else "insufficient"
        )
        quality_score = 1.0 if quality_status == "ready" else 0.6 if quality_status == "partial" else 0.0
        quality = DataQuality(
            status=quality_status,
            score=quality_score,
            source=summary.data_source,
            data_as_of=summary.data_timestamp or latest.date,
            fallback=bool(summary.delayed or summary.data_source in {"demo", ""}),
            missing_fields=missing_fields,
            quality_flags=sorted(set(quality_flags)),
        )
        quality_receipt = DecisionDataQualityReceipt.issue(
            snapshot_id=snapshot_id,
            symbol=summary.entity.symbol,
            decision_at=decision_at,
            snapshot_data_as_of=decision_at,
            data_quality=quality,
            evidence_ids=[
                f"market-summary:{summary.entity.symbol}",
                f"quote:{summary.data_source}:{latest.date}",
            ],
        )
        _persist_decision_quality_receipt(
            quality_receipt,
            surface="screener",
        )
        # The old score was a hidden ``change_percent`` heuristic.  A screen is
        # now ranked solely by its disclosed condition match; the change is a
        # deterministic tie-breaker and never masquerades as a strategy score.
        score = 100.0 if parsed_conditions else 0.0
        reasons = [f"即時來源：{summary.data_source}", f"報價時間：{latest.date}"]
        reasons.extend(f"條件通過：{item.condition.display()}" for item in evaluations)
        if summary.change_percent > 0:
            reasons.append("即時源漲跌幅為正")
        elif summary.change_percent < 0:
            reasons.append("即時源漲跌幅為負")
        items.append(
            ScreenerItem(
                symbol=summary.entity.symbol,
                name=summary.entity.name,
                score=round(score, 1),
                reasons=reasons,
                metrics={
                    "change_percent": summary.change_percent,
                    "display_price": latest.close,
                    "quote_time": latest.date,
                    "freshness_note": summary.freshness_note,
                    "source": summary.data_source,
                    "condition_receipt": [item.receipt() for item in evaluations],
                    "condition_field_receipt": persisted_receipt,
                    "condition_match_score": score,
                },
                data_quality_receipt=quality_receipt,
            )
        )
    return sorted(
        items,
        key=lambda item: (item.score, float(item.metrics.get("change_percent") or 0.0), item.symbol),
        reverse=True,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _unique_text(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _build_readonly_meta(
    data_sources: list[str],
    fallback: dict[str, Any] | None = None,
    extra_limitations: list[str] | None = None,
) -> ReadonlyWorkspaceMeta:
    limitations = [
        "未接券商 API，所有交易相關資料皆為唯讀預覽，不可送出正式委託。",
        "若即時行情暫時不可用，系統會退回最近可用收盤或本機預設欄位，並於 fallback 明確標示。",
        "資產與部位來自本機 Paper OMS 模擬帳本，不代表券商真實庫存與損益。",
    ]
    if extra_limitations:
        limitations.extend(extra_limitations)
    return ReadonlyWorkspaceMeta(
        generated_at=_now_iso(),
        data_sources=_unique_text(data_sources),
        limitations=_unique_text(limitations),
        fallback=fallback or {},
    )


def _preview_broker_status() -> BrokerConnectionStatus:
    return BrokerConnectionStatus(
        provider_name="Broker API not connected",
        connected=False,
        can_submit_orders=False,
        mode="preview_only",
        note="目前僅提供交易預覽、風控檢查與資產骨架，不提供真實送單能力。",
    )


def _latest_reference_snapshot(symbol: str) -> dict[str, Any]:
    summary = get_market_summary(symbol)
    if summary:
        return {
            "symbol": summary.entity.symbol,
            "name": summary.entity.name,
            "industry": summary.entity.industry,
            "price": float(summary.latest_price.close),
            "price_source": summary.data_source,
            "timestamp": summary.data_timestamp or summary.latest_price.date or _now_iso(),
            "fallback_fields": {"reference_price": "realtime_quote"},
        }

    entity = get_entity(symbol)
    points = get_price_history(symbol)
    if points:
        latest = points[-1]
        return {
            "symbol": entity.symbol if entity else normalize_symbol(symbol),
            "name": entity.name if entity else normalize_symbol(symbol),
            "industry": entity.industry if entity else None,
            "price": float(latest.close),
            "price_source": "Latest available official history close (preview fallback)",
            "timestamp": latest.date or _now_iso(),
            "fallback_fields": {"reference_price": "latest_available_close"},
        }

    raise ValueError(f"No reference price available for symbol: {symbol}")


def _estimate_order_cost(reference_price: float, side: str, quantity_lots: int) -> OrderCostEstimate:
    quantity_shares = max(1, quantity_lots) * 1000
    gross_amount = round(reference_price * quantity_shares, 2)
    estimated_fee = round(max(20.0, gross_amount * 0.001425 * 0.6), 2) if gross_amount else 0.0
    estimated_tax = round(gross_amount * 0.003, 2) if side == "sell" else 0.0
    estimated_total = round(gross_amount + estimated_fee, 2) if side == "buy" else round(gross_amount - estimated_fee - estimated_tax, 2)
    return OrderCostEstimate(
        gross_amount=gross_amount,
        estimated_fee=estimated_fee,
        estimated_tax=estimated_tax,
        estimated_total=estimated_total,
    )


def get_asset_workspace(limit: int = 4) -> AssetWorkspace:
    """Use the persistent Paper OMS account for assets, risk and trading previews."""
    from .paper_asset_workspace import get_paper_asset_workspace

    return get_paper_asset_workspace(limit=limit)


from .central_risk_adapter import build_risk_summary as _build_risk_summary


def get_risk_workspace(symbol: str | None = None, side: str = "buy", quantity_lots: int = 1) -> RiskWorkspace:
    if not str(symbol or "").strip():
        raise MissingSymbolError("Risk Workspace requires an explicit symbol")
    asset_workspace = get_asset_workspace(limit=4)
    snapshot = _latest_reference_snapshot(symbol)
    summary = _build_risk_summary(
        asset_workspace=asset_workspace,
        symbol=snapshot["symbol"],
        side=side,
        quantity_lots=max(1, quantity_lots),
        reference_price=snapshot["price"],
        industry=snapshot.get("industry"),
    )
    meta = _build_readonly_meta(
        data_sources=asset_workspace.meta.data_sources + [snapshot["price_source"], "open_stock_ai.risk_engine"],
        fallback={"reference_price": snapshot["fallback_fields"], "risk_rules": "central_risk_engine_order_preview"},
        extra_limitations=["此頁使用中央 RiskEngine 與 Paper OMS 帳本；仍是模擬預覽，不會送往券商。"],
    )
    return RiskWorkspace(
        meta=meta,
        summary=summary,
        preview_symbol=snapshot["symbol"],
        preview_side=side,  # type: ignore[arg-type]
        position_snapshot=asset_workspace.positions,
    )


def get_trading_workspace(symbol: str | None = None, side: str = "buy", quantity_lots: int = 1) -> TradingWorkspace:
    if not str(symbol or "").strip():
        raise MissingSymbolError("Trading Workspace requires an explicit symbol")
    quantity_lots = max(1, quantity_lots)
    asset_workspace = get_asset_workspace(limit=4)
    broker_status = _preview_broker_status()
    snapshot = _latest_reference_snapshot(symbol)
    estimated_costs = _estimate_order_cost(snapshot["price"], side, quantity_lots)
    available_cash_before = asset_workspace.summary.cash_available
    available_cash_after = round(
        available_cash_before - estimated_costs.estimated_total if side == "buy" else available_cash_before + estimated_costs.estimated_total,
        2,
    )
    risk_summary = _build_risk_summary(
        asset_workspace=asset_workspace,
        symbol=snapshot["symbol"],
        side=side,
        quantity_lots=quantity_lots,
        reference_price=snapshot["price"],
        industry=snapshot.get("industry"),
    )
    preview = TradingPreview(
        symbol=snapshot["symbol"],
        name=snapshot["name"],
        side=side,  # type: ignore[arg-type]
        quantity_lots=quantity_lots,
        quantity_shares=quantity_lots * 1000,
        reference_price=snapshot["price"],
        estimated_fill_price=snapshot["price"],
        available_cash_before=available_cash_before,
        available_cash_after=available_cash_after,
        estimated_costs=estimated_costs,
        limitation_note="僅供委託預覽與費用估算，未接券商 API 前不得視為可正式送單。",
        fallback_fields={
            "reference_price": snapshot["fallback_fields"],
            "broker_submission": "disabled",
            "portfolio_snapshot": asset_workspace.meta.fallback,
        },
    )
    meta = _build_readonly_meta(
        data_sources=asset_workspace.meta.data_sources + [snapshot["price_source"], "taiwan_fee_tax_preview_rules"],
        fallback={"reference_price": snapshot["fallback_fields"], "order_submission": "disabled_without_broker_api"},
    )
    return TradingWorkspace(
        meta=meta,
        broker_status=broker_status,
        preview=preview,
        risk_summary=risk_summary,
        asset_summary=asset_workspace.summary,
        positions=asset_workspace.positions,
    )


def _signal_to_action(signal: str) -> str:
    return {
        "buy": "buy",
        "watch": "watch",
        "avoid": "avoid",
    }.get(str(signal or "").lower(), "hold")


def _score_to_rule_score(score: float) -> float:
    return round(max(-1.0, min(1.0, score / 5.0)), 3)


def get_ai_trading_workspace(symbol: str | None = None) -> AITradingAssistantWorkspace:
    from .mvp_features import get_notification_previews, get_watchlist_overview
    from .phase1_data import generate_daily_report

    if not str(symbol or "").strip():
        raise MissingSymbolError("Trading Assistant Workspace requires an explicit symbol")
    focus_symbol = str(symbol).strip().upper()
    universe = UniverseSnapshot(source="explicit_symbols", symbols=(focus_symbol,))
    daily_report = generate_daily_report(universe=universe, limit=3)
    watchlist = get_watchlist_overview(limit=5, universe=universe)
    watch_items = watchlist.get("items", [])
    focus_pick = next((item for item in daily_report.picks if item.symbol == focus_symbol), None)
    focus_watch = next((item for item in watch_items if item["symbol"] == focus_symbol), {})
    focus_name = focus_pick.name if focus_pick else str(focus_watch.get("name") or focus_symbol)
    notification_items = [
        item if isinstance(item, NotificationPreview) else NotificationPreview(**item)
        for item in get_notification_previews(symbol=focus_symbol).get("items", [])
    ]
    market_summary = get_market_summary(focus_symbol)
    brief_events = get_market_events_brief(focus_symbol, limit=2, entity_name=focus_name)
    intraday_change = float(focus_watch.get("change_percent") or (market_summary.change_percent if market_summary else 0.0))
    conflict_note = None
    if focus_pick and focus_pick.signal == "buy" and intraday_change < 0:
        conflict_note = "每日報告偏多，但盤中價格表現偏弱，建議降槓桿或改列觀察。"
    elif focus_watch and "漲跌幅異常" in focus_watch.get("alert_flags", []) and "法人偏多" not in focus_watch.get("alert_flags", []):
        conflict_note = "盤中波動較大且未見同步籌碼優勢，訊號需保守解讀。"

    cards = [
        AITradePlanCard(
            session="pre_market",
            symbol=focus_symbol,
            name=focus_name,
            action_bias=_signal_to_action(focus_pick.signal if focus_pick else "watch"),  # type: ignore[arg-type]
            rule_score=_score_to_rule_score(focus_pick.score if focus_pick else 0.0),
            technical_reasons=[
                f"每日報告分數 {focus_pick.score:.2f}" if focus_pick else "暫無每日報告分數，改用觀察模式。",
                f"市場趨勢 {market_summary.trend}" if market_summary else "即時摘要不可用，改看最近可用市場資料。",
            ],
            flow_reasons=[
                reason for reason in (focus_pick.reasons if focus_pick else []) if "法人" in reason or "籌碼" in reason
            ] or ["等待法人與量能資料進一步確認。"],
            fundamental_reasons=[
                reason for reason in (focus_pick.reasons if focus_pick else []) if "營收" in reason or "基本面" in reason
            ] or [f"月營收年增 {focus_watch.get('revenue_yoy')}%" if focus_watch.get("revenue_yoy") is not None else "尚無新基本面亮點。"],
            event_reasons=[event.title for event in brief_events] or ["暫無明顯事件催化。"],
            risk_reasons=(focus_pick.risk_factors if focus_pick else []) or ["建議搭配風控摘要檢查單筆曝險。"],
            data_sources=(focus_pick.data_sources if focus_pick else []) or daily_report.source_snapshot[:4],
            as_of=daily_report.generated_at,
            conflict_note=conflict_note,
        ),
        AITradePlanCard(
            session="intraday",
            symbol=focus_symbol,
            name=focus_name,
            action_bias=("buy" if intraday_change > 0 and "法人偏多" in focus_watch.get("alert_flags", []) else "watch"),  # type: ignore[arg-type]
            rule_score=round(max(-1.0, min(1.0, intraday_change / 10.0)), 3),
            technical_reasons=[
                f"盤中/最近可用價格 {focus_watch.get('latest_price')}" if focus_watch.get("latest_price") is not None else "盤中價格暫不可用。",
                f"漲跌幅 {intraday_change:+.2f}%",
            ],
            flow_reasons=[
                f"法人淨額 {focus_watch.get('institutional_net')}" if focus_watch.get("institutional_net") is not None else "法人即時/近端資料不足。",
                "自選股提醒：" + ",".join(focus_watch.get("alert_flags", [])[:2]) if focus_watch.get("alert_flags") else "自選股暫無異常提醒。",
            ],
            fundamental_reasons=[
                f"營收年增 {focus_watch.get('revenue_yoy')}%" if focus_watch.get("revenue_yoy") is not None else "營收資料待補。",
            ],
            event_reasons=[
                str(focus_watch.get("latest_news_title")) if focus_watch.get("latest_news_title") else "暫無最新新聞標題。"
            ],
            risk_reasons=[
                "波動放大時應降低口數。",
                "若風控摘要出現 block，應停止委託預覽往下操作。",
            ],
            data_sources=[str(source) for source in focus_watch.get("data_sources", []) if source] or ["watchlist_overview"],
            as_of=market_summary.data_timestamp if market_summary and market_summary.data_timestamp else _now_iso(),
            conflict_note=conflict_note,
        ),
        AITradePlanCard(
            session="post_market",
            symbol=focus_symbol,
            name=focus_name,
            action_bias=_signal_to_action(focus_pick.signal if focus_pick else "hold"),  # type: ignore[arg-type]
            rule_score=0.0,
            technical_reasons=["收盤後建議重新檢查當日波動、委託預覽與資產配置變化。"],
            flow_reasons=["通知預覽可用來驗證收盤後提醒文案與自選股熱點。"],
            fundamental_reasons=[daily_report.summary],
            event_reasons=[item.title for item in notification_items[:2]] or ["暫無通知預覽。"],
            risk_reasons=["所有量化規則參考皆為讀取型彙整，不代表確定結論或投資建議。"],
            data_sources=daily_report.source_snapshot[:4] + ["notification_previews"],
            as_of=_now_iso(),
            conflict_note="若多來源方向不一致，應優先以風控上限與人工判讀處理。",
        ),
    ]
    meta = _build_readonly_meta(
        data_sources=daily_report.source_snapshot + [str(source) for source in focus_watch.get("data_sources", []) if source] + ["notification_previews"],
        fallback={
            "market_snapshot": "use daily report/watchlist data when realtime summary is unavailable",
            "assistant_cards": "heuristics derived from existing A-H/M modules",
        },
        extra_limitations=["本頁為非模型規則與多來源彙整，不代表 AI 判斷、成功機率或投資報酬。"],
    )
    market_snapshot = {
        "symbol": focus_symbol,
        "name": focus_name,
        "latest_price": market_summary.latest_price.close if market_summary else focus_watch.get("latest_price"),
        "change_percent": market_summary.change_percent if market_summary else focus_watch.get("change_percent"),
        "trend": market_summary.trend if market_summary else "preview_only",
        "event_titles": [event.title for event in brief_events],
        "source": market_summary.data_source if market_summary else "watchlist_overview fallback",
        "timestamp": market_summary.data_timestamp if market_summary else _now_iso(),
    }
    return AITradingAssistantWorkspace(
        meta=meta,
        focus_symbol=focus_symbol,
        report_title=daily_report.title,
        cards=cards,
        notification_previews=notification_items,
        watchlist_items=watch_items[:5],
        market_snapshot=market_snapshot,
    )
