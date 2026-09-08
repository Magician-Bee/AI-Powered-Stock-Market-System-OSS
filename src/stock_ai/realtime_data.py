from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import re
from threading import Lock
from typing import Any

from .data_platform.identity import entity_id_for_symbol
from .data_platform.source_registry import source_endpoint
from .models import Entity, EventItem, PricePoint

TAIWAN_NUMERIC = re.compile(r"^\d{4,6}$")
_yahoo_history_locks_guard = Lock()
_yahoo_history_locks: dict[str, Lock] = {}


def normalize_symbol(raw: str) -> str:
    """Normalize user symbols for Yahoo Finance.

    Taiwan listed stocks/ETFs usually use .TW. OTC symbols use .TWO, which can be
    entered explicitly by the user. If the user enters 2330 or 0050, default to
    2330.TW / 0050.TW because that is the common TWSE format.
    """
    s = raw.strip().upper().replace(" ", "")
    aliases = {
        "台積電": "2330.TW",
        "鴻海": "2317.TW",
        "廣達": "2382.TW",
        "元大台灣50": "0050.TW",
        "0050": "0050.TW",
        "費半": "^SOX",
        "SOX": "^SOX",
        "NASDAQ": "^IXIC",
        "S&P500": "^GSPC",
        "SP500": "^GSPC",
    }
    if s in aliases:
        return aliases[s]
    if TAIWAN_NUMERIC.match(s):
        return f"{s}.TW"
    return s


def yahoo_symbol_for_display(symbol: str) -> str:
    return normalize_symbol(symbol)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        # pandas/numpy NaN check without importing numpy directly
        if value != value:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value != value:
            return default
        return int(value)
    except Exception:
        return default


@lru_cache(maxsize=256)
def _fetch_yahoo_history_cached(symbol: str) -> dict[str, Any]:
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - exercised when env missing dep
        raise RuntimeError("yfinance is not installed; run uv sync --extra dev") from exc

    ticker = yf.Ticker(symbol)
    hist = ticker.history(
        period="3mo",
        interval="1d",
        auto_adjust=False,
        timeout=8,
    )
    if hist is None or hist.empty:
        # Common Taiwan fallback for OTC when a numeric code was normalized to
        # TWSE but the security is actually listed on TPEx.
        if symbol.endswith(".TW") and TAIWAN_NUMERIC.match(symbol[:-3]):
            alt = symbol[:-3] + ".TWO"
            ticker = yf.Ticker(alt)
            hist = ticker.history(
                period="3mo",
                interval="1d",
                auto_adjust=False,
                timeout=8,
            )
            if hist is not None and not hist.empty:
                symbol = alt
        if hist is None or hist.empty:
            raise ValueError(f"No Yahoo Finance price history for {symbol}")

    points: list[PricePoint] = []
    for idx, row in hist.tail(60).iterrows():
        point_date = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
        points.append(
            PricePoint(
                date=point_date,
                open=round(_safe_float(row.get("Open")), 4),
                high=round(_safe_float(row.get("High")), 4),
                low=round(_safe_float(row.get("Low")), 4),
                close=round(_safe_float(row.get("Close")), 4),
                volume=_safe_int(row.get("Volume")),
            )
        )
    if len(points) < 2:
        raise ValueError(f"Not enough Yahoo Finance price points for {symbol}")
    return {
        "symbol": symbol,
        "points": points,
        "source": f"Yahoo Finance / yfinance ({symbol})",
        "data_timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def fetch_yahoo_history(raw_symbol: str) -> dict[str, Any]:
    """Fetch only OHLCV history and single-flight concurrent callers per symbol."""
    symbol = normalize_symbol(raw_symbol)
    with _yahoo_history_locks_guard:
        request_lock = _yahoo_history_locks.setdefault(symbol, Lock())
    with request_lock:
        return _fetch_yahoo_history_cached(symbol)


@lru_cache(maxsize=256)
def fetch_yahoo_summary(raw_symbol: str) -> dict[str, Any]:
    """Fetch real market data from Yahoo Finance via yfinance.

    Returns a plain dict so API/service code can decide how to expose missing
    fields. Raises ValueError when Yahoo has no usable price history. This is not
    a trading-grade feed; exchanges may be delayed and Yahoo terms apply.
    """
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - exercised when env missing dep
        raise RuntimeError("yfinance is not installed; run uv sync --extra dev") from exc

    history = fetch_yahoo_history(raw_symbol)
    symbol = history["symbol"]
    points = history["points"]
    ticker = yf.Ticker(symbol)

    info: dict[str, Any] = {}
    try:
        info = ticker.get_info() or {}
    except Exception:
        info = {}

    quote_type = str(info.get("quoteType") or "stock").lower()
    currency = str(info.get("currency") or ("TWD" if symbol.endswith((".TW", ".TWO")) else "USD"))
    exchange = str(info.get("exchange") or info.get("fullExchangeName") or "Yahoo")
    local_names = {
        "2330.TW": "台積電",
        "2317.TW": "鴻海",
        "2382.TW": "廣達",
        "0050.TW": "元大台灣50",
    }
    name = local_names.get(symbol, str(info.get("longName") or info.get("shortName") or symbol))
    market = "taiwan" if symbol.endswith((".TW", ".TWO")) else "global"
    entity_type = "index" if symbol.startswith("^") else "etf" if quote_type == "etf" else "stock"
    sector = info.get("sector")
    industry = info.get("industry")

    entity = Entity(
        entity_id=entity_id_for_symbol(
            symbol,
            market=market,
            exchange=(
                "TPEx"
                if symbol.endswith(".TWO")
                else "TWSE"
                if symbol.endswith(".TW")
                else exchange
            ),
            source_code=symbol.split(".", 1)[0],
        ),
        symbol=symbol,
        name=name,
        entity_type=entity_type,
        market=market,
        exchange=exchange,
        currency=currency,
        sector=sector,
        industry=industry,
        is_active=True,
    )

    events: list[EventItem] = []
    try:
        news_items = getattr(ticker, "news", []) or []
    except Exception:
        news_items = []
    for i, item in enumerate(news_items[:5], start=1):
        content = item.get("content", item) if isinstance(item, dict) else {}
        title = content.get("title") or item.get("title") if isinstance(item, dict) else None
        link = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("clickThroughUrl", {}).get("url") if isinstance(content.get("clickThroughUrl"), dict) else ""
        provider_time = content.get("pubDate") or content.get("displayTime") or datetime.now(timezone.utc).isoformat()
        summary = content.get("summary") or title or "Yahoo Finance news item"
        if title:
            events.append(
                EventItem(
                    event_id=f"yahoo-news-{symbol}-{i}",
                    event_time=str(provider_time),
                    related_symbols=[symbol],
                    event_type="news",
                    title=str(title),
                    summary=str(summary),
                    sentiment="neutral",
                    estimated_impact_direction="mixed",
                    confidence=0.45,
                    source_url=str(
                        link or source_endpoint("yahoo_chart", symbol=symbol)
                    ),
                )
            )

    return {
        "symbol": symbol,
        "entity": entity,
        "points": points,
        "events": events,
        "source": f"Yahoo Finance / yfinance ({symbol})",
        "data_timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "freshness_note": "使用 Yahoo Finance/yfinance 取得最新可得資料；不同交易所可能延遲，非交易所直連即時報價。",
        "reliability_note": "若資料缺漏或延遲，系統會明確顯示不足；不可把缺資料的回答當成投資決策依據。",
    }


def clear_realtime_cache() -> None:
    fetch_yahoo_summary.cache_clear()
    _fetch_yahoo_history_cached.cache_clear()
