from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from open_stock_ai.agent_runtime import default_external_transport_guard

from .config import get_settings
from .models import NewsItem, NotificationChannelStatus, NotificationDeliveryResult, NotificationPreview, NotificationSendRequest, WatchlistOverviewItem
from open_stock_ai.types import UniverseSnapshot

from .phase1_data import (
    generate_daily_report,
    list_institutional_flows,
    list_margin_trading,
    list_monthly_revenues,
    securities_for_universe,
)
from .realtime_quotes import fetch_twse_mis_quote
from .services import get_market_events, get_market_events_brief

OFFICIAL_EVENT_TYPES = {"material_event", "announcement"}
POSITIVE_KEYWORDS = ("創高", "成長", "利多", "買超", "擴產", "上修", "調升", "受惠", "大漲", "突破")
NEGATIVE_KEYWORDS = ("下修", "利空", "跌停", "風險", "調降", "虧損", "衰退", "賣超", "重挫", "違約")
POLICY_KEYWORDS = ("政策", "法規", "金管會", "央行", "法案", "關稅", "監理", "補助")
ANALYST_KEYWORDS = ("評等", "目標價", "券商", "投顧", "研究報告", "晨報")
MACRO_KEYWORDS = ("通膨", "利率", "殖利率", "美元", "匯率", "VIX", "原油", "黃金", "就業", "GDP")
INTERNATIONAL_KEYWORDS = ("美股", "NASDAQ", "S&P", "道瓊", "ADR", "費半", "國際", "Fed", "聯準會")
INDUSTRY_KEYWORDS = ("半導體", "AI 伺服器", "航運", "鋼鐵", "塑化", "生技", "金融", "觀光", "ETF")
NEWS_SOURCE_TEXT_PATTERN = re.compile(
    r"(Yahoo\s*財經|Yahoo股市|news\.cnyes\.com|sinotrade\.com\.tw|Google News|MoneyDJ|工商時報|經濟日報|鉅亨網|udn\.com|ctee\.com\.tw|The Information|優分析UAnalyze|今周刊)",
    re.IGNORECASE,
)
NEWS_SOURCE_TAIL_PATTERN = re.compile(
    r"\s*(?:[-|｜]|來源[:：])\s*(Yahoo\s*財經|Yahoo股市|news\.cnyes\.com|sinotrade\.com\.tw|Google News|MoneyDJ|工商時報|經濟日報|鉅亨網|udn\.com|ctee\.com\.tw|The Information|優分析UAnalyze|今周刊)\s*$",
    re.IGNORECASE,
)
GENERIC_NEWS_SOURCE_TAIL_PATTERN = re.compile(
    r"\s*(?:[-|｜]|來源[:：])\s*([\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}\.(?:com|tw|net|org)|[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}(?:新聞網|新聞|財經|股市|日報|時報|News|Information))\s*$",
    re.IGNORECASE,
)
GENERIC_NEWS_SOURCE_END_PATTERN = re.compile(
    r"\s*([\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}\.(?:com|tw|net|org)|[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}(?:新聞網|新聞|財經|股市|日報|時報|News|Information))\s*$",
    re.IGNORECASE,
)


def _strip_news_source_tails(text: str) -> str:
    while True:
        cleaned = NEWS_SOURCE_TAIL_PATTERN.sub("", text).strip()
        cleaned = GENERIC_NEWS_SOURCE_TAIL_PATTERN.sub("", cleaned).strip()
        cleaned = GENERIC_NEWS_SOURCE_END_PATTERN.sub("", cleaned).strip()
        if cleaned == text:
            return cleaned
        text = cleaned


def _debug_report(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    """Compatibility no-op: production code never posts diagnostics to a fixed port."""
    return None


def _watchlist_debug_report(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    """Compatibility no-op: production code never posts diagnostics to a fixed port."""
    return None


def _hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _source_name(url: str, event_type: str) -> str:
    host = _hostname(url).lower()
    if "mops.twse.com.tw" in host:
        return "MOPS"
    if "twse.com.tw" in host or "openapi.twse.com.tw" in host:
        return "TWSE OpenAPI"
    if "news.google.com" in host:
        return "Google News"
    if "yahoo.com" in host:
        return "Yahoo 財經"
    if "cnyes.com" in host:
        return "鉅亨網"
    if "moneydj.com" in host:
        return "MoneyDJ"
    if "udn.com" in host:
        return "經濟日報"
    if "ctee.com.tw" in host:
        return "工商時報"
    return event_type or "市場來源"


def _is_official_verified(event_type: str, url: str) -> bool:
    host = _hostname(url).lower()
    return event_type in OFFICIAL_EVENT_TYPES or "twse.com.tw" in host or "mops.twse.com.tw" in host


def _credibility_score(event_type: str, url: str) -> float:
    host = _hostname(url).lower()
    if _is_official_verified(event_type, url):
        return 0.92
    if "yahoo.com" in host:
        return 0.68
    if "cnyes.com" in host or "moneydj.com" in host or "udn.com" in host or "ctee.com.tw" in host:
        return 0.62
    if "news.google.com" in host:
        return 0.45
    return 0.5


def _classify_category(title: str, summary: str, event_type: str) -> str:
    text = f"{title} {summary} {event_type}".lower()
    if any(keyword.lower() in text for keyword in POLICY_KEYWORDS):
        return "policy"
    if any(keyword.lower() in text for keyword in ANALYST_KEYWORDS):
        return "analyst"
    if any(keyword.lower() in text for keyword in MACRO_KEYWORDS):
        return "macro"
    if any(keyword.lower() in text for keyword in INTERNATIONAL_KEYWORDS):
        return "international"
    if any(keyword.lower() in text for keyword in INDUSTRY_KEYWORDS):
        return "industry"
    return "company"


def _derive_sentiment(title: str, summary: str, default: str) -> str:
    text = f"{title} {summary}"
    if any(keyword in text for keyword in POSITIVE_KEYWORDS):
        return "positive"
    if any(keyword in text for keyword in NEGATIVE_KEYWORDS):
        return "negative"
    return default


def _clean_news_title(value: str) -> str:
    text = str(value or "").replace("&nbsp;", " ").replace("&amp;", "&").strip()
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&lt;[^&]*&gt;", " ", text, flags=re.IGNORECASE)
    text = _strip_news_source_tails(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -|｜/")


def _clean_news_summary(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"&lt;a\s+[^&]*&gt;([\s\S]*?)&lt;/a&gt;", r" \1 ", text, flags=re.IGNORECASE)
    text = re.sub(r"<a\s+[^>]*>([\s\S]*?)</a>", r" \1 ", text, flags=re.IGNORECASE)
    for pattern in (r"https?://\S+", r"<[^>]+>", r"&lt;[^&]*&gt;"):
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("\\n", " ")
        .replace("\n", " ")
        .strip()
    )
    text = re.sub(r"\b[A-Za-z0-9.-]+\.(?:com|tw|net|org)\b\s*(?:--|[-:：])?\s*", " ", text, flags=re.IGNORECASE)
    text = NEWS_SOURCE_TEXT_PATTERN.sub(" ", text)
    text = _strip_news_source_tails(text)
    text = re.sub(r"\s*來源[:：]\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return "" if "<a href" in text.lower() else text[:280]


def build_news_item(event: Any) -> NewsItem:
    clean_title = _clean_news_title(event.title)
    category = _classify_category(clean_title, event.summary, event.event_type)
    sentiment = _derive_sentiment(clean_title, event.summary, event.sentiment)
    clean_summary = _clean_news_summary(event.summary)
    return NewsItem(
        news_id=event.event_id,
        title=clean_title,
        source=_source_name(event.source_url, event.event_type),
        published_at=event.event_time,
        related_symbols=event.related_symbols,
        category=category,  # type: ignore[arg-type]
        sentiment=sentiment,  # type: ignore[arg-type]
        impact=event.estimated_impact_direction,
        credibility=_credibility_score(event.event_type, event.source_url),
        official_verified=_is_official_verified(event.event_type, event.source_url),
        summary=clean_summary,
        source_url=event.source_url,
    )


def get_news_center(
    symbol: str | None = None,
    category: str | None = None,
    limit: int = 30,
    *,
    universe: UniverseSnapshot | None = None,
) -> dict[str, Any]:
    if symbol:
        symbols = [symbol]
        resolved_universe = UniverseSnapshot(source="explicit_symbols", symbols=(symbol,))
    else:
        resolved_universe = universe or UniverseSnapshot.empty()
        symbols = list(resolved_universe.symbols)
    _debug_report("H3", "mvp_features.py:get_news_center", "news-center-start", {"symbol": symbol, "category": category, "limit": limit, "symbol_count": len(symbols)})
    items: list[NewsItem] = []
    seen: set[str] = set()
    per_symbol_limit = max(3, min(limit, 8))
    for sym in symbols:
        for event in get_market_events(sym, limit=per_symbol_limit):
            news = build_news_item(event)
            if category and news.category != category:
                continue
            key = f"{news.title}|{news.source_url}".lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(news)
    items.sort(key=lambda item: (item.official_verified, item.credibility, item.published_at), reverse=True)
    items = items[:limit]
    counts = Counter(item.category for item in items)
    return {
        "count": len(items),
        "items": [item.model_dump() for item in items],
        "category_counts": dict(counts),
        "symbol_scope": symbols,
        "universe": {
            "source": resolved_universe.source,
            "symbols": list(resolved_universe.symbols),
            "count": resolved_universe.count,
            "filters": resolved_universe.filters,
            "created_at": resolved_universe.created_at,
        },
    }


async def _fetch_watchlist_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    async def _safe(symbol: str) -> tuple[str, dict[str, Any] | None]:
        try:
            payload = await fetch_twse_mis_quote(symbol)
            return symbol, payload.get("data")
        except Exception:
            return symbol, None

    rows = await asyncio.gather(*[_safe(symbol) for symbol in symbols])
    return {symbol: payload for symbol, payload in rows if payload}


def get_watchlist_overview(
    limit: int = 10,
    *,
    universe: UniverseSnapshot | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    _watchlist_debug_report("H1", "mvp_features.py:get_watchlist_overview", "watchlist-overview-start", {"limit": limit})
    try:
        phase_started = time.perf_counter()
        resolved_universe = universe or UniverseSnapshot.empty()
        watchlist = securities_for_universe(resolved_universe, limit=limit)
        _watchlist_debug_report(
            "H1",
            "mvp_features.py:get_watchlist_overview",
            "watchlist-overview-default-watchlist-ready",
            {"watchlist_count": len(watchlist), "elapsed_ms": round((time.perf_counter() - phase_started) * 1000, 1)},
        )
        symbols = [item.symbol for item in watchlist]
        phase_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as executor:
            quotes_future = executor.submit(asyncio.run, _fetch_watchlist_quotes(symbols))
            flows_future = executor.submit(list_institutional_flows, limit=2000)
            margins_future = executor.submit(list_margin_trading, limit=5000)
            revenues_future = executor.submit(list_monthly_revenues, limit=5000)
            quotes = quotes_future.result()
            flows = {item.symbol: item for item in flows_future.result()}
            margins = {item.symbol: item for item in margins_future.result()}
            revenues = {item.symbol: item for item in revenues_future.result()}
        _watchlist_debug_report(
            "H1",
            "mvp_features.py:get_watchlist_overview",
            "watchlist-overview-sources-ready",
            {
                "watchlist_count": len(watchlist),
                "quote_count": len(quotes),
                "flow_count": len(flows),
                "margin_count": len(margins),
                "revenue_count": len(revenues),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "parallel_sources_elapsed_ms": round((time.perf_counter() - phase_started) * 1000, 1),
            },
        )
        items: list[WatchlistOverviewItem] = []
        for security in watchlist:
            quote = quotes.get(security.symbol, {})
            flow = flows.get(security.symbol)
            margin = margins.get(security.symbol)
            revenue = revenues.get(security.symbol)
            events = get_market_events_brief(security.symbol, limit=1, entity_name=security.name)
            alert_flags: list[str] = []
            if quote and abs(float(quote.get("change_percent") or 0)) >= 2:
                alert_flags.append("漲跌幅異常")
            if flow and flow.total_institutional_net > 0:
                alert_flags.append("法人偏多")
            if margin and margin.margin_balance > margin.margin_previous_balance:
                alert_flags.append("融資升溫")
            if revenue and revenue.yoy_change_percent > 0:
                alert_flags.append("營收年增")
            if events:
                alert_flags.append("近期新聞")
            items.append(
                WatchlistOverviewItem(
                    symbol=security.symbol,
                    name=security.name,
                    exchange=security.exchange,
                    industry=security.industry,
                    latest_price=float(quote["last_price"]) if quote and quote.get("last_price") is not None else None,
                    change_percent=float(quote["change_percent"]) if quote and quote.get("change_percent") is not None else None,
                    total_volume_lots=int(quote["total_volume_lots"]) if quote and quote.get("total_volume_lots") is not None else None,
                    institutional_net=flow.total_institutional_net if flow else None,
                    margin_balance=margin.margin_balance if margin else None,
                    revenue_yoy=revenue.yoy_change_percent if revenue else None,
                    latest_news_title=_clean_news_title(events[0].title) if events else None,
                    latest_news_url=events[0].source_url if events else None,
                    alert_flags=alert_flags or ["觀察中"],
                    data_sources=[
                        "TWSE MIS" if quote else "TWSE MIS unavailable",
                        flow.source if flow else "T86 unavailable",
                        margin.source if margin else "MI_MARGN unavailable",
                        revenue.source if revenue else "t187ap05_L unavailable",
                    ],
                )
            )
        result = {
            "count": len(items),
            "items": [item.model_dump() for item in items],
            "universe": {
                "source": resolved_universe.source,
                "symbols": list(resolved_universe.symbols),
                "count": resolved_universe.count,
                "filters": resolved_universe.filters,
                "created_at": resolved_universe.created_at,
            },
        }
        _watchlist_debug_report(
            "H1",
            "mvp_features.py:get_watchlist_overview",
            "watchlist-overview-done",
            {"count": len(items), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
        return result
    except Exception as exc:
        _watchlist_debug_report(
            "H3",
            "mvp_features.py:get_watchlist_overview",
            "watchlist-overview-error",
            {"error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
        raise


def get_notification_channels() -> dict[str, Any]:
    settings = get_settings()
    telegram_ready = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    line_ready = bool(settings.line_access_token and settings.line_target_id)
    items = [
        NotificationChannelStatus(
            channel="telegram",
            configured=telegram_ready,
            enabled=telegram_ready,
            mode="ready" if telegram_ready else "preview_only",
            target_hint=settings.telegram_chat_id[-4:] if settings.telegram_chat_id else None,
            note="可用 /api/notifications/send 明確 dry_run=false 發送；預設仍是 dry-run。",
        ),
        NotificationChannelStatus(
            channel="line",
            configured=line_ready,
            enabled=line_ready,
            mode="ready" if line_ready else "preview_only",
            target_hint=settings.line_target_id[-4:] if settings.line_target_id else None,
            note="可用 /api/notifications/send 明確 dry_run=false 發送；預設仍是 dry-run。",
        ),
    ]
    return {"count": len(items), "items": [item.model_dump() for item in items]}


def get_notification_previews(symbol: str | None = None) -> dict[str, Any]:
    universe = (
        UniverseSnapshot(source="explicit_symbols", symbols=(symbol,))
        if symbol
        else UniverseSnapshot.empty()
    )
    report = generate_daily_report(universe=universe, limit=3)
    related_symbols = [item.symbol for item in report.picks[:3]]
    previews: list[NotificationPreview] = [
        NotificationPreview(
            category="daily_report",
            title="量化規則每日報告",
            body="；".join(f"{item.name}{item.symbol} {item.signal} 分數{item.score}" for item in report.picks[:3]) or "今日暫無選股候選。",
            channels=["telegram", "line"],
            related_symbols=related_symbols,
        )
    ]
    if symbol:
        news_center = get_news_center(symbol=symbol, limit=3)
        if news_center["items"]:
            top_news = news_center["items"][0]
            previews.append(
                NotificationPreview(
                    category="market_news",
                    title=f"{symbol} 新聞提醒",
                    body=f"{top_news['title']}｜來源 {top_news['source']}｜分類 {top_news['category']}",
                    channels=["telegram", "line"],
                    related_symbols=[symbol],
                )
            )
    watchlist = get_watchlist_overview(limit=5, universe=universe)
    hot_alerts = [item for item in watchlist["items"] if "漲跌幅異常" in item["alert_flags"] or "法人偏多" in item["alert_flags"]][:3]
    if hot_alerts:
        previews.append(
            NotificationPreview(
                category="watchlist_alert",
                title="自選股提醒",
                body="；".join(f"{item['name']}{item['symbol']} {','.join(item['alert_flags'][:2])}" for item in hot_alerts),
                channels=["telegram", "line"],
                related_symbols=[item["symbol"] for item in hot_alerts],
            )
        )
    return {"count": len(previews), "items": [item.model_dump() for item in previews]}


def send_notification(request: NotificationSendRequest | dict[str, Any]) -> dict[str, Any]:
    payload = request if isinstance(request, NotificationSendRequest) else NotificationSendRequest(**request)
    message = _notification_message(payload.title, payload.body, payload.related_symbols)
    results = [_send_notification_channel(channel, message, payload.dry_run) for channel in payload.channels]
    return {
        "schema_version": "stock_ai.notification_delivery.v1",
        "dry_run": payload.dry_run,
        "requested_channels": payload.channels,
        "sent_count": sum(1 for item in results if item.sent),
        "attempted_count": sum(1 for item in results if item.attempted),
        "items": [item.model_dump() for item in results],
    }


def _notification_message(title: str, body: str, related_symbols: list[str]) -> str:
    symbols = f"\n相關股票：{', '.join(related_symbols[:8])}" if related_symbols else ""
    text = f"{title.strip()}\n{body.strip()}{symbols}".strip()
    return text[:3500]


def _send_notification_channel(channel: str, message: str, dry_run: bool) -> NotificationDeliveryResult:
    settings = get_settings()
    if channel == "telegram":
        configured = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        target_hint = settings.telegram_chat_id[-4:] if settings.telegram_chat_id else None
        if dry_run or not configured:
            return NotificationDeliveryResult(
                channel="telegram",
                configured=configured,
                attempted=False,
                sent=False,
                mode="dry_run" if dry_run else "not_configured",
                target_hint=target_hint,
                detail="dry-run preview only." if dry_run else "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.",
            )
        return _post_telegram(settings.telegram_bot_token or "", settings.telegram_chat_id or "", message, target_hint)

    configured = bool(settings.line_access_token and settings.line_target_id)
    target_hint = settings.line_target_id[-4:] if settings.line_target_id else None
    if dry_run or not configured:
        return NotificationDeliveryResult(
            channel="line",
            configured=configured,
            attempted=False,
            sent=False,
            mode="dry_run" if dry_run else "not_configured",
            target_hint=target_hint,
            detail="dry-run preview only." if dry_run else "Missing LINE_ACCESS_TOKEN or LINE_TARGET_ID.",
        )
    return _post_line(settings.line_access_token or "", settings.line_target_id or "", message, target_hint)


def _post_telegram(token: str, chat_id: str, message: str, target_hint: str | None) -> NotificationDeliveryResult:
    body = json.dumps({"chat_id": chat_id, "text": message, "disable_web_page_preview": True}).encode("utf-8")
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"}, method="POST")
    return _execute_notification_request("telegram", req, target_hint)


def _post_line(token: str, target_id: str, message: str, target_hint: str | None) -> NotificationDeliveryResult:
    body = json.dumps({"to": target_id, "messages": [{"type": "text", "text": message}]}).encode("utf-8")
    req = Request(
        "https://api.line.me/v2/bot/message/push",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    return _execute_notification_request("line", req, target_hint)


def _execute_notification_request(channel: str, req: Request, target_hint: str | None) -> NotificationDeliveryResult:
    try:
        response = default_external_transport_guard().call_sync(
            f"notification:{channel}",
            lambda: urlopen(req, timeout=8),
        )
        with response as resp:
            status_code = getattr(resp, "status", 200)
        return NotificationDeliveryResult(
            channel=channel,
            configured=True,
            attempted=True,
            sent=200 <= status_code < 300,
            mode="ready" if 200 <= status_code < 300 else "error",
            target_hint=target_hint,
            status_code=status_code,
            detail="Delivered." if 200 <= status_code < 300 else "Provider returned a non-success status.",
        )
    except HTTPError as exc:
        return NotificationDeliveryResult(
            channel=channel,
            configured=True,
            attempted=True,
            sent=False,
            mode="error",
            target_hint=target_hint,
            status_code=exc.code,
            detail=f"Provider HTTP error: {exc.reason or exc.code}",
        )
    except (TimeoutError, URLError, OSError) as exc:
        return NotificationDeliveryResult(
            channel=channel,
            configured=True,
            attempted=True,
            sent=False,
            mode="error",
            target_hint=target_hint,
            detail=f"Provider request failed: {exc.__class__.__name__}",
        )
