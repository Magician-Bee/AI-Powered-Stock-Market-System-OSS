from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

import httpx
import websockets

from open_stock_ai.agent_runtime import default_external_transport_guard

from .config import get_settings
from .data_platform.source_registry import get_source_registry, source_endpoint
from .market_calendar import default_taiwan_market_calendar
from .taiwan_official import normalize_taiwan_code

FUGLE_REST_BASE = str(get_source_registry().source("fugle_marketdata").base_url)
FUGLE_WS_URL = source_endpoint("fugle_stream")
TWSE_MIS_URL = source_endpoint("twse_mis_quote")
REALTIME_QUOTE_SCHEMA_VERSION = "stock_ai.realtime_quote.v1"
REALTIME_STREAM_SCHEMA_VERSION = "stock_ai.realtime_stream_event.v1"
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
FUGLE_CHANNELS = {"trades", "books", "candles", "aggregates", "indices"}
FUGLE_WEBSOCKET_SCOPE = "source:fugle_stream:websocket"


class RealtimeNotConfigured(RuntimeError):
    pass


class RealtimeProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RealtimeStatus:
    enabled: bool
    provider: str
    configured: bool
    channels: list[str]
    websocket: str | None = None
    rest_base: str | None = None
    message: str = ""
    update_interval_ms: int | None = None
    quote_schema_version: str = REALTIME_QUOTE_SCHEMA_VERSION
    stream_schema_version: str = REALTIME_STREAM_SCHEMA_VERSION
    stream_transport: str = "sse"
    capabilities: tuple[str, ...] = (
        "last_trade",
        "best_bid_ask",
        "five_level_order_book",
        "cumulative_volume",
        "trading_status",
        "continuous_updates",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "configured": self.configured,
            "channels": self.channels,
            "websocket": self.websocket,
            "rest_base": self.rest_base,
            "message": self.message,
            "update_interval_ms": self.update_interval_ms,
            "quote_schema_version": self.quote_schema_version,
            "stream_schema_version": self.stream_schema_version,
            "stream_transport": self.stream_transport,
            "capabilities": list(self.capabilities),
        }


def _channels() -> list[str]:
    return [c.strip() for c in get_settings().realtime_stream_channels.split(",") if c.strip()]


def status() -> RealtimeStatus:
    settings = get_settings()
    provider = settings.realtime_quote_provider.strip().lower()
    channels = _channels()
    if provider in {"", "disabled", "none", "off"}:
        return RealtimeStatus(False, "disabled", False, channels, message="即時行情源尚未設定。")
    if provider in {"twse_mis", "mis", "free_twse", "twse_free"}:
        return RealtimeStatus(
            enabled=True,
            provider="twse_mis",
            configured=True,
            channels=["quote", "books"],
            rest_base=TWSE_MIS_URL,
            message="TWSE MIS 公開網頁行情報價端點；約每 5 秒更新，無 API key。適合個人本機使用，不等同正式授權轉散布行情。",
            update_interval_ms=5000,
        )
    if provider == "fugle":
        configured = bool(settings.fugle_marketdata_api_key)
        return RealtimeStatus(
            enabled=configured,
            provider="fugle",
            configured=configured,
            channels=channels,
            websocket=FUGLE_WS_URL,
            rest_base=FUGLE_REST_BASE,
            message="Fugle/Fubon-compatible WebSocket 授權即時行情" if configured else "缺少 FUGLE_MARKETDATA_API_KEY。",
            update_interval_ms=None,
        )
    return RealtimeStatus(False, provider, False, channels, message=f"尚未支援 provider: {provider}")


def normalize_stream_symbol(symbol: str) -> str:
    return normalize_taiwan_code(symbol)


def _to_float(v: Any) -> float | None:
    s = str(v or "").strip().replace(",", "")
    if s in {"", "-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    return int(f) if f is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _epoch_datetime(value: Any) -> datetime | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    absolute = abs(numeric)
    if absolute >= 100_000_000_000_000:
        numeric /= 1_000_000
    elif absolute >= 100_000_000_000:
        numeric /= 1_000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(
            TAIPEI_TIMEZONE
        )
    except (OverflowError, OSError, ValueError):
        return None


def _market_datetime(date_value: Any, time_value: Any) -> datetime | None:
    raw_date = str(date_value or "").strip().replace("-", "").replace("/", "")
    raw_time = str(time_value or "").strip()
    if len(raw_date) != 8 or not raw_date.isdigit():
        return None
    digits = "".join(char for char in raw_time if char.isdigit())
    if len(digits) < 6:
        return None
    try:
        return datetime.strptime(
            f"{raw_date}{digits[:6]}", "%Y%m%d%H%M%S"
        ).replace(tzinfo=TAIPEI_TIMEZONE)
    except ValueError:
        return None


def _trading_status(
    exchange_timestamp: datetime | None,
    received_at: datetime,
    *,
    trading_halt: bool = False,
    is_trial: bool = False,
    is_delayed_open: bool = False,
    is_delayed_close: bool = False,
    is_open: bool = False,
    is_close: bool = False,
    is_continuous: bool = False,
) -> tuple[str, str]:
    if trading_halt:
        return "halted", "provider_flag"
    if is_delayed_open:
        return "delayed_open", "provider_flag"
    if is_delayed_close:
        return "delayed_close", "provider_flag"
    if is_trial:
        return "trial", "provider_flag"
    if is_close:
        return "closed", "provider_flag"
    if is_open or is_continuous:
        return "trading", "provider_flag"
    if exchange_timestamp is None:
        return "unknown", "unavailable"

    local_exchange = exchange_timestamp.astimezone(TAIPEI_TIMEZONE)
    local_received = received_at.astimezone(TAIPEI_TIMEZONE)
    if local_exchange.date() != local_received.date():
        return "closed", "derived_session_clock"
    calendar_session = default_taiwan_market_calendar().session_at(local_exchange)["session"]
    if calendar_session == "pre_open":
        return "pre_open", "derived_session_clock"
    if calendar_session == "trading":
        return "trading", "derived_session_clock"
    if calendar_session == "closing_auction":
        return "closing_auction", "derived_session_clock"
    return "closed", "derived_session_clock"


def _freshness(
    exchange_timestamp: datetime | None,
    received_at: datetime,
    *,
    trading_status: str,
    max_age_ms: int,
) -> tuple[int | None, bool, str]:
    if exchange_timestamp is None:
        return None, True, "unknown"
    age_ms = max(
        0,
        int(
            (
                received_at.astimezone(timezone.utc)
                - exchange_timestamp.astimezone(timezone.utc)
            ).total_seconds()
            * 1000
        ),
    )
    if trading_status == "closed":
        return age_ms, False, "closed"
    stale = age_ms > max_age_ms
    return age_ms, stale, "stale" if stale else "live"


def _normalize_levels(levels: Any) -> list[dict[str, Any]]:
    if not isinstance(levels, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in levels[:5]:
        if not isinstance(raw, dict):
            continue
        price = _to_float(raw.get("price"))
        size = _to_int(raw.get("size", raw.get("volume")))
        if price is None:
            continue
        output.append({"price": price, "size": size})
    return output


def _split_levels(raw_prices: Any, raw_sizes: Any) -> list[dict[str, Any]]:
    prices = [x for x in str(raw_prices or "").split("_") if x != ""]
    sizes = [x for x in str(raw_sizes or "").split("_") if x != ""]
    out = []
    for i, price in enumerate(prices[:5]):
        out.append({"price": _to_float(price), "size": _to_int(sizes[i]) if i < len(sizes) else None})
    return out


def _book_summary(
    bids: list[dict[str, Any]], asks: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "best_bid": bids[0] if bids else None,
        "best_ask": asks[0] if asks else None,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "complete_five_levels": len(bids) == 5 and len(asks) == 5,
    }


def _parse_mis_row(
    row: dict[str, Any],
    raw: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    received = received_at or _now()
    previous_close = _to_float(row.get("y"))
    last_price = _to_float(row.get("z")) or _to_float(row.get("pz"))
    # During some sessions TWSE MIS returns '-' for last trade; keep it null rather than inventing.
    change = (last_price - previous_close) if last_price is not None and previous_close else None
    change_percent = (change / previous_close * 100) if change is not None and previous_close else None
    date = str(row.get("d") or row.get("^") or raw.get("queryTime", {}).get("sysDate") or "")
    time = str(row.get("t") or row.get("%") or raw.get("queryTime", {}).get("sysTime") or "")
    exchange_timestamp = (
        _epoch_datetime(row.get("tlong")) or _market_datetime(date, time)
    )
    trading_status, trading_status_source = _trading_status(
        exchange_timestamp, received
    )
    update_interval_ms = _to_int(raw.get("userDelay")) or 5000
    quote_age_ms, is_stale, freshness = _freshness(
        exchange_timestamp,
        received,
        trading_status=trading_status,
        max_age_ms=max(update_interval_ms * 3, 20_000),
    )
    bids = _split_levels(row.get("b"), row.get("g"))
    asks = _split_levels(row.get("a"), row.get("f"))
    total_volume_lots = _to_int(row.get("v"))
    last_trade_size_lots = _to_int(row.get("tv"))
    return {
        "schema_version": REALTIME_QUOTE_SCHEMA_VERSION,
        "provider": "twse_mis",
        "source": "TWSE MIS public quote endpoint",
        "authorized": False,
        "realtime": True,
        "symbol": row.get("c"),
        "name": row.get("n"),
        "full_name": row.get("nf"),
        "exchange": row.get("ex"),
        "market_session": "regular_lot",
        "date": date,
        "time": time,
        "exchange_timestamp": (
            exchange_timestamp.isoformat(timespec="milliseconds")
            if exchange_timestamp
            else None
        ),
        "last_updated_ms": _to_int(row.get("tlong")),
        "sequence": _to_int(row.get("tlong")),
        "reference_price": previous_close,
        "previous_close": previous_close,
        "open": _to_float(row.get("o")),
        "high": _to_float(row.get("h")),
        "low": _to_float(row.get("l")),
        "last_price": last_price,
        "last_trade_size_lots": last_trade_size_lots,
        "last_trade_size_shares": (
            last_trade_size_lots * 1000
            if last_trade_size_lots is not None
            else None
        ),
        "change": round(change, 4) if change is not None else None,
        "change_percent": round(change_percent, 4) if change_percent is not None else None,
        "limit_up": _to_float(row.get("u")),
        "limit_down": _to_float(row.get("w")),
        "total_volume_lots": total_volume_lots,
        "total_volume_shares": (
            total_volume_lots * 1000 if total_volume_lots is not None else None
        ),
        "turnover": None,
        "inner_volume_lots": None,
        "outer_volume_lots": None,
        "transaction_count": None,
        "bids": bids,
        "asks": asks,
        **_book_summary(bids, asks),
        "trading_status": trading_status,
        "trading_status_source": trading_status_source,
        "is_market_open": trading_status
        in {"trading", "closing_auction", "delayed_close"},
        "freshness": freshness,
        "quote_age_ms": quote_age_ms,
        "is_stale": is_stale,
        "raw": row,
        "query_time": raw.get("queryTime"),
        "user_delay_ms": update_interval_ms,
        "received_at": received.isoformat(timespec="milliseconds"),
        "note": "免費公開網頁端點約每 5 秒更新；適合個人本機觀看。若要商業轉散布仍需依 TWSE 資訊使用規範處理。",
    }


async def _fetch_mis_once(code: str, exchange: str) -> dict[str, Any] | None:
    ex_ch = f"{exchange}_{code}.tw"
    params = {"ex_ch": ex_ch, "json": "1", "delay": "0"}
    headers = {
        "User-Agent": "Mozilla/5.0 StockAI/0.1",
        "Referer": source_endpoint("twse_mis_referer", symbol=code),
    }
    async def load():
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            return await client.get(TWSE_MIS_URL, params=params)

    res = await default_external_transport_guard().call(
        f"source:twse_mis:quote:{exchange}:{code}",
        load,
    )
    if res.status_code >= 400:
        raise RealtimeProviderError(f"TWSE MIS 回應 {res.status_code}: {res.text[:200]}")
    text = res.text.strip()
    data = json.loads(text)
    rows = data.get("msgArray") or []
    if not rows:
        return None
    if data.get("rtcode") != "0000":
        raise RealtimeProviderError(f"TWSE MIS rtcode={data.get('rtcode')} message={data.get('rtmessage')}")
    return _parse_mis_row(rows[0], data)


async def fetch_twse_mis_quote(symbol: str) -> dict[str, Any]:
    code = normalize_stream_symbol(symbol)
    preferred = []
    if symbol.upper().endswith(".TWO"):
        preferred = ["otc", "tse"]
    elif symbol.upper().endswith(".TW"):
        preferred = ["tse", "otc"]
    else:
        preferred = ["tse", "otc"]
    last_err: Exception | None = None
    for ex in preferred:
        try:
            parsed = await _fetch_mis_once(code, ex)
            if parsed:
                return {
                    "schema_version": REALTIME_QUOTE_SCHEMA_VERSION,
                    "provider": "twse_mis",
                    "symbol": code,
                    "source": "TWSE MIS public quote endpoint",
                    "received_at": parsed["received_at"],
                    "data": parsed,
                }
        except Exception as exc:
            last_err = exc
    if last_err:
        raise RealtimeProviderError(str(last_err)) from last_err
    raise RealtimeProviderError(f"TWSE MIS 查無即時報價: {symbol}")


def require_fugle_key() -> str:
    settings = get_settings()
    if settings.realtime_quote_provider.strip().lower() != "fugle":
        raise RealtimeNotConfigured("REALTIME_QUOTE_PROVIDER 需設為 fugle 才能啟用 Fugle/Fubon 授權 WebSocket 行情。")
    key = settings.fugle_marketdata_api_key
    if not key:
        raise RealtimeNotConfigured("缺少 FUGLE_MARKETDATA_API_KEY。")
    return key


def _normalize_fugle_quote(
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    received = received_at or _now()
    total = payload.get("total") if isinstance(payload.get("total"), dict) else {}
    last_trade = (
        payload.get("lastTrade")
        if isinstance(payload.get("lastTrade"), dict)
        else {}
    )
    trading_halt = (
        payload.get("tradingHalt")
        if isinstance(payload.get("tradingHalt"), dict)
        else {}
    )
    exchange_timestamp = (
        _epoch_datetime(payload.get("lastUpdated"))
        or _epoch_datetime(last_trade.get("time"))
        or _epoch_datetime(total.get("time"))
    )
    trading_status, trading_status_source = _trading_status(
        exchange_timestamp,
        received,
        trading_halt=bool(trading_halt.get("isHalted")),
        is_trial=bool(payload.get("isTrial")),
        is_delayed_open=bool(payload.get("isDelayedOpen")),
        is_delayed_close=bool(payload.get("isDelayedClose")),
        is_open=bool(payload.get("isOpen")),
        is_close=bool(payload.get("isClose")),
        is_continuous=bool(payload.get("isContinuous")),
    )
    quote_age_ms, is_stale, freshness = _freshness(
        exchange_timestamp,
        received,
        trading_status=trading_status,
        max_age_ms=20_000,
    )
    bids = _normalize_levels(payload.get("bids"))
    asks = _normalize_levels(payload.get("asks"))
    previous_close = _to_float(
        payload.get("previousClose", payload.get("referencePrice"))
    )
    last_price = _to_float(
        last_trade.get(
            "price", payload.get("lastPrice", payload.get("closePrice"))
        )
    )
    change = _to_float(payload.get("change"))
    if change is None and last_price is not None and previous_close:
        change = last_price - previous_close
    change_percent = _to_float(payload.get("changePercent"))
    if change_percent is None and change is not None and previous_close:
        change_percent = change / previous_close * 100
    total_volume_lots = _to_int(total.get("tradeVolume"))
    last_trade_size_lots = _to_int(
        last_trade.get("size", payload.get("lastSize"))
    )
    exchange_time = (
        exchange_timestamp.strftime("%H:%M:%S")
        if exchange_timestamp is not None
        else ""
    )
    return {
        "schema_version": REALTIME_QUOTE_SCHEMA_VERSION,
        "provider": "fugle",
        "source": "Fugle licensed market data",
        "authorized": True,
        "realtime": True,
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "full_name": payload.get("name"),
        "exchange": payload.get("exchange"),
        "market": payload.get("market"),
        "market_session": "regular_lot",
        "date": str(payload.get("date") or ""),
        "time": exchange_time,
        "exchange_timestamp": (
            exchange_timestamp.isoformat(timespec="milliseconds")
            if exchange_timestamp
            else None
        ),
        "last_updated_ms": _to_int(payload.get("lastUpdated")),
        "sequence": _to_int(
            payload.get("serial", last_trade.get("serial"))
        ),
        "reference_price": _to_float(payload.get("referencePrice")),
        "previous_close": previous_close,
        "open": _to_float(payload.get("openPrice")),
        "high": _to_float(payload.get("highPrice")),
        "low": _to_float(payload.get("lowPrice")),
        "last_price": last_price,
        "last_trade_size_lots": last_trade_size_lots,
        "last_trade_size_shares": (
            last_trade_size_lots * 1000
            if last_trade_size_lots is not None
            else None
        ),
        "change": round(change, 4) if change is not None else None,
        "change_percent": (
            round(change_percent, 4) if change_percent is not None else None
        ),
        "limit_up": None,
        "limit_down": None,
        "is_limit_up": bool(payload.get("isLimitUpPrice")),
        "is_limit_down": bool(payload.get("isLimitDownPrice")),
        "total_volume_lots": total_volume_lots,
        "total_volume_shares": (
            total_volume_lots * 1000 if total_volume_lots is not None else None
        ),
        "turnover": _to_float(total.get("tradeValue")),
        "inner_volume_lots": _to_int(total.get("tradeVolumeAtBid")),
        "outer_volume_lots": _to_int(total.get("tradeVolumeAtAsk")),
        "transaction_count": _to_int(total.get("transaction")),
        "bids": bids,
        "asks": asks,
        **_book_summary(bids, asks),
        "trading_status": trading_status,
        "trading_status_source": trading_status_source,
        "is_market_open": trading_status
        in {"trading", "closing_auction", "delayed_close"},
        "freshness": freshness,
        "quote_age_ms": quote_age_ms,
        "is_stale": is_stale,
        "raw": payload,
        "query_time": None,
        "user_delay_ms": None,
        "received_at": received.isoformat(timespec="milliseconds"),
        "note": "Fugle/Fubon-compatible licensed realtime market data.",
    }


async def fetch_fugle_quote(symbol: str) -> dict[str, Any]:
    api_key = require_fugle_key()
    code = normalize_stream_symbol(symbol)
    url = source_endpoint("fugle_intraday_quote", symbol=code)
    async def load():
        async with httpx.AsyncClient(timeout=10) as client:
            return await client.get(url, headers={"X-API-KEY": api_key})

    res = await default_external_transport_guard().call(
        f"source:fugle_marketdata:quote:{code}",
        load,
    )
    if res.status_code == 401:
        raise RealtimeProviderError("即時行情 API 驗證失敗，請檢查授權 API key。")
    if res.status_code == 429:
        raise RealtimeProviderError("即時行情 API 速率限制 429。")
    if res.status_code >= 400:
        raise RealtimeProviderError(f"即時行情 API 回應 {res.status_code}: {res.text[:200]}")
    received = _now()
    data = _normalize_fugle_quote(res.json(), received_at=received)
    return {
        "schema_version": REALTIME_QUOTE_SCHEMA_VERSION,
        "provider": "fugle",
        "symbol": code,
        "source": "Fugle/Fubon-compatible licensed market data REST",
        "received_at": received.isoformat(timespec="milliseconds"),
        "data": data,
    }


async def fetch_realtime_quote(symbol: str) -> dict[str, Any]:
    provider = get_settings().realtime_quote_provider.strip().lower()
    if provider in {"twse_mis", "mis", "free_twse", "twse_free"}:
        return await fetch_twse_mis_quote(symbol)
    if provider == "fugle":
        return await fetch_fugle_quote(symbol)
    raise RealtimeNotConfigured(status().message)


def _merge_fugle_stream_quote(
    current: dict[str, Any] | None,
    channel: str,
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> dict[str, Any] | None:
    received = received_at or _now()
    if channel == "aggregates":
        return _normalize_fugle_quote(payload, received_at=received)
    if channel not in {"trades", "books"}:
        return current

    if current is None:
        seed: dict[str, Any] = {
            "symbol": payload.get("symbol"),
            "exchange": payload.get("exchange"),
            "market": payload.get("market"),
            "lastUpdated": payload.get("time"),
            "serial": payload.get("serial"),
        }
        if channel == "trades":
            seed.update(
                {
                    "lastPrice": payload.get("price"),
                    "lastSize": payload.get("size"),
                    "lastTrade": {
                        key: payload.get(key)
                        for key in ("bid", "ask", "price", "size", "time", "serial")
                        if payload.get(key) is not None
                    },
                    "total": {"tradeVolume": payload.get("volume")},
                }
            )
        else:
            seed.update(
                {"bids": payload.get("bids"), "asks": payload.get("asks")}
            )
        for flag in (
            "isTrial",
            "isDelayedOpen",
            "isDelayedClose",
            "isContinuous",
            "isOpen",
            "isClose",
        ):
            if flag in payload:
                seed[flag] = payload[flag]
        return _normalize_fugle_quote(seed, received_at=received)

    merged = dict(current)
    exchange_timestamp = _epoch_datetime(payload.get("time"))
    status, status_source = _trading_status(
        exchange_timestamp,
        received,
        is_trial=bool(payload.get("isTrial")),
        is_delayed_open=bool(payload.get("isDelayedOpen")),
        is_delayed_close=bool(payload.get("isDelayedClose")),
        is_open=bool(payload.get("isOpen")),
        is_close=bool(payload.get("isClose")),
        is_continuous=bool(payload.get("isContinuous")),
    )
    if channel == "trades":
        merged["last_price"] = _to_float(payload.get("price"))
        merged["last_trade_size_lots"] = _to_int(payload.get("size"))
        merged["last_trade_size_shares"] = (
            merged["last_trade_size_lots"] * 1000
            if merged["last_trade_size_lots"] is not None
            else None
        )
        if payload.get("volume") is not None:
            merged["total_volume_lots"] = _to_int(payload.get("volume"))
            merged["total_volume_shares"] = (
                merged["total_volume_lots"] * 1000
                if merged["total_volume_lots"] is not None
                else None
            )
        previous_close = _to_float(merged.get("previous_close"))
        if merged["last_price"] is not None and previous_close:
            change = merged["last_price"] - previous_close
            merged["change"] = round(change, 4)
            merged["change_percent"] = round(
                change / previous_close * 100, 4
            )
    else:
        merged["bids"] = _normalize_levels(payload.get("bids"))
        merged["asks"] = _normalize_levels(payload.get("asks"))
        merged.update(_book_summary(merged["bids"], merged["asks"]))

    merged["symbol"] = payload.get("symbol") or merged.get("symbol")
    merged["exchange"] = payload.get("exchange") or merged.get("exchange")
    merged["market"] = payload.get("market") or merged.get("market")
    merged["sequence"] = _to_int(payload.get("serial"))
    merged["last_updated_ms"] = _to_int(payload.get("time"))
    merged["exchange_timestamp"] = (
        exchange_timestamp.isoformat(timespec="milliseconds")
        if exchange_timestamp
        else merged.get("exchange_timestamp")
    )
    merged["time"] = (
        exchange_timestamp.strftime("%H:%M:%S")
        if exchange_timestamp
        else merged.get("time")
    )
    merged["trading_status"] = status
    merged["trading_status_source"] = status_source
    merged["is_market_open"] = status in {
        "trading",
        "closing_auction",
        "delayed_close",
    }
    quote_age_ms, is_stale, freshness = _freshness(
        exchange_timestamp,
        received,
        trading_status=status,
        max_age_ms=20_000,
    )
    merged["quote_age_ms"] = quote_age_ms
    merged["is_stale"] = is_stale
    merged["freshness"] = freshness
    merged["received_at"] = received.isoformat(timespec="milliseconds")
    merged["raw_stream_event"] = {
        "channel": channel,
        "payload": payload,
    }
    return merged


@asynccontextmanager
async def _guarded_fugle_websocket():
    """Apply the external transport policy to the actual WS handshake."""

    connection = websockets.connect(FUGLE_WS_URL, ping_interval=20, ping_timeout=20)
    guard = default_external_transport_guard()
    websocket = await guard.call(FUGLE_WEBSOCKET_SCOPE, connection.__aenter__)
    try:
        yield websocket
    except BaseException:
        exception_type, exception, traceback = sys.exc_info()
        if not await connection.__aexit__(exception_type, exception, traceback):
            raise
    else:
        await connection.__aexit__(None, None, None)


async def stream_fugle(symbol: str, channels: list[str] | None = None) -> AsyncIterator[dict[str, Any]]:
    api_key = require_fugle_key()
    code = normalize_stream_symbol(symbol)
    requested = channels or _channels() or ["trades", "books", "aggregates"]
    normalized_channels: list[str] = []
    for channel in requested:
        candidates = ["aggregates"] if channel == "quote" else [channel]
        for candidate in candidates:
            if candidate in FUGLE_CHANNELS and candidate not in normalized_channels:
                normalized_channels.append(candidate)
    if not normalized_channels:
        normalized_channels = ["trades", "books", "aggregates"]

    latest_quote: dict[str, Any] | None = None
    try:
        snapshot = await fetch_fugle_quote(code)
        latest_quote = snapshot["data"]
        yield {
            "schema_version": REALTIME_STREAM_SCHEMA_VERSION,
            "provider": "fugle",
            "symbol": code,
            "received_at": snapshot["received_at"],
            "message": {
                "event": "snapshot",
                "channel": "quote",
                "data": latest_quote,
            },
        }
    except Exception as exc:
        yield {
            "schema_version": REALTIME_STREAM_SCHEMA_VERSION,
            "provider": "fugle",
            "symbol": code,
            "received_at": _now().isoformat(timespec="milliseconds"),
            "message": {
                "event": "provider_error",
                "channel": "quote",
                "error": f"{type(exc).__name__}: {exc}",
                "last_good_data": None,
            },
        }

    async with _guarded_fugle_websocket() as ws:
        await ws.send(json.dumps({"event": "auth", "data": {"apikey": api_key}}))
        authenticated = False
        subscriptions_sent = False
        while True:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                msg = {"event": "raw", "data": raw}
            event = msg.get("event")
            if event == "authenticated":
                authenticated = True
            elif event == "error" and not authenticated:
                raise RealtimeProviderError(f"即時行情 WebSocket 驗證失敗: {msg.get('data')}")

            received = _now()
            if event == "data" and isinstance(msg.get("data"), dict):
                channel = str(msg.get("channel") or "")
                if channel == "candles":
                    from .intraday_candles import (
                        FUGLE_SOURCE,
                        get_intraday_candle_store,
                        normalize_fugle_stream_candle,
                        normalize_one_minute_candle,
                    )

                    candle = normalize_fugle_stream_candle(msg["data"])
                    canonical_candle = normalize_one_minute_candle(
                        candle,
                        source=FUGLE_SOURCE,
                        observed_at=received,
                    )
                    get_intraday_candle_store().record_candles(
                        [candle],
                        source=FUGLE_SOURCE,
                        is_complete=False,
                        response_payload=msg,
                        completed_at=received,
                        metadata={"transport": "websocket"},
                    )
                    message = {
                        "event": "candle",
                        "channel": channel,
                        "data": {
                            **canonical_candle,
                            "provider": "fugle",
                            "timeframe_minutes": 1,
                        },
                        "upstream_event": msg,
                    }
                else:
                    updated = _merge_fugle_stream_quote(
                        latest_quote,
                        channel,
                        msg["data"],
                        received_at=received,
                    )
                    if updated is not None and channel in {
                        "trades",
                        "books",
                        "aggregates",
                    }:
                        latest_quote = updated
                        from .intraday_candles import (
                            get_intraday_candle_store,
                        )

                        get_intraday_candle_store().record_quote(latest_quote)
                        message = {
                            "event": "quote",
                            "channel": channel,
                            "data": latest_quote,
                            "upstream_event": msg,
                        }
                    else:
                        message = msg
            else:
                message = msg
            yield {
                "schema_version": REALTIME_STREAM_SCHEMA_VERSION,
                "provider": "fugle",
                "symbol": code,
                "received_at": received.isoformat(timespec="milliseconds"),
                "message": message,
            }

            if authenticated and not subscriptions_sent:
                for ch in normalized_channels:
                    await ws.send(json.dumps({"event": "subscribe", "data": {"channel": ch, "symbol": code}}))
                    await asyncio.sleep(0.02)
                subscriptions_sent = True


async def stream_twse_mis(
    symbol: str, *, poll_interval_seconds: float = 5.0
) -> AsyncIterator[dict[str, Any]]:
    code = normalize_stream_symbol(symbol)
    last_key = None
    last_good_data: dict[str, Any] | None = None
    while True:
        try:
            quote = await fetch_twse_mis_quote(code)
            data = quote["data"]
            from .intraday_candles import get_intraday_candle_store

            get_intraday_candle_store().record_quote(data)
            last_good_data = data
            key = (
                data.get("last_updated_ms"),
                data.get("last_price"),
                data.get("last_trade_size_lots"),
                data.get("total_volume_lots"),
                data.get("time"),
                data.get("trading_status"),
                json.dumps(data.get("bids"), sort_keys=True),
                json.dumps(data.get("asks"), sort_keys=True),
            )
            event = "quote" if key != last_key else "heartbeat"
            last_key = key
            yield {
                "schema_version": REALTIME_STREAM_SCHEMA_VERSION,
                "provider": "twse_mis",
                "symbol": code,
                "received_at": _now().isoformat(timespec="milliseconds"),
                "message": {
                    "event": event,
                    "channel": "quote",
                    "data": data,
                },
            }
        except Exception as exc:
            # TWSE MIS is a free public website endpoint; transient empty/timeout responses
            # must not tear down the whole SSE response. Keep the browser connection alive,
            # report the provider error, and retry on the normal cadence.
            yield {
                "schema_version": REALTIME_STREAM_SCHEMA_VERSION,
                "provider": "twse_mis",
                "symbol": code,
                "received_at": _now().isoformat(timespec="milliseconds"),
                "message": {
                    "event": "provider_error",
                    "channel": "quote",
                    "error": f"{type(exc).__name__}: {exc}",
                    "last_good_data": last_good_data,
                },
            }
        await asyncio.sleep(poll_interval_seconds)


async def sse_stream(symbol: str, channels: list[str] | None = None) -> AsyncIterator[str]:
    provider = get_settings().realtime_quote_provider.strip().lower()
    if provider in {"twse_mis", "mis", "free_twse", "twse_free"}:
        yield "retry: 1500\n\n"
        async for msg in stream_twse_mis(symbol):
            event = msg.get("message", {}).get("event", "quote")
            yield f"event: {event}\n"
            sequence = msg.get("message", {}).get("data", {}).get("sequence")
            if sequence is not None:
                yield f"id: {sequence}\n"
            yield "data: " + json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        return
    if provider == "fugle":
        yield "retry: 1500\n\n"
        async for msg in stream_fugle(symbol, channels=channels):
            event = msg.get("message", {}).get("event", "data")
            yield f"event: {event}\n"
            sequence = msg.get("message", {}).get("data", {}).get("sequence")
            if sequence is not None:
                yield f"id: {sequence}\n"
            yield "data: " + json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        return
    raise RealtimeNotConfigured(status().message)
