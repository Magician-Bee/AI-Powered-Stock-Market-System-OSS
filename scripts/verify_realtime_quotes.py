#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from stock_ai import main as main_module
from stock_ai import realtime_quotes


ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo("Asia/Taipei")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def mis_quote(
    *,
    observed_at: datetime,
    price: str,
    volume: str,
) -> dict:
    row = {
        "c": "2330",
        "n": "台積電",
        "nf": "台灣積體電路製造股份有限公司",
        "ex": "tse",
        "d": observed_at.strftime("%Y%m%d"),
        "t": observed_at.strftime("%H:%M:%S"),
        "tlong": str(int(observed_at.timestamp() * 1000)),
        "y": "99.0",
        "o": "99.5",
        "h": "101.0",
        "l": "98.5",
        "z": price,
        "tv": "3",
        "v": volume,
        "u": "108.5",
        "w": "89.1",
        "b": "99.5_99.0_98.5_98.0_97.5_",
        "g": "10_20_30_40_50_",
        "a": "100.0_100.5_101.0_101.5_102.0_",
        "f": "11_21_31_41_51_",
    }
    return realtime_quotes._parse_mis_row(
        row,
        {
            "queryTime": {
                "sysDate": observed_at.strftime("%Y%m%d"),
                "sysTime": observed_at.strftime("%H:%M:%S"),
            },
            "userDelay": 5000,
        },
        received_at=observed_at + timedelta(seconds=5),
    )


def fugle_quote(observed_at: datetime) -> dict:
    timestamp = int(observed_at.timestamp() * 1_000_000)
    payload = {
        "date": observed_at.date().isoformat(),
        "exchange": "TWSE",
        "market": "TSE",
        "symbol": "2330",
        "name": "台積電",
        "referencePrice": 99,
        "previousClose": 99,
        "openPrice": 99.5,
        "highPrice": 101,
        "lowPrice": 98.5,
        "lastPrice": 100,
        "lastSize": 3,
        "change": 1,
        "changePercent": 1.0101,
        "bids": [
            {"price": 99.5 - index * 0.5, "size": 10 + index}
            for index in range(5)
        ],
        "asks": [
            {"price": 100 + index * 0.5, "size": 11 + index}
            for index in range(5)
        ],
        "total": {
            "tradeValue": 120_000_000,
            "tradeVolume": 1200,
            "tradeVolumeAtBid": 500,
            "tradeVolumeAtAsk": 650,
            "transaction": 230,
            "time": timestamp,
        },
        "lastTrade": {
            "bid": 99.5,
            "ask": 100,
            "price": 100,
            "size": 3,
            "time": timestamp,
            "serial": 123,
        },
        "tradingHalt": {"isHalted": False},
        "isContinuous": True,
        "serial": 123,
        "lastUpdated": timestamp,
    }
    return realtime_quotes._normalize_fugle_quote(
        payload,
        received_at=observed_at + timedelta(milliseconds=200),
    )


async def continuous_updates(first: dict, second: dict) -> list[dict]:
    queued = [first, second]

    async def fake_fetch(_symbol: str) -> dict:
        data = queued.pop(0)
        return {
            "schema_version": realtime_quotes.REALTIME_QUOTE_SCHEMA_VERSION,
            "provider": "twse_mis",
            "symbol": "2330",
            "received_at": data["received_at"],
            "data": data,
        }

    async def no_sleep(_seconds: float) -> None:
        return None

    original_fetch = realtime_quotes.fetch_twse_mis_quote
    original_sleep = realtime_quotes.asyncio.sleep
    realtime_quotes.fetch_twse_mis_quote = fake_fetch
    realtime_quotes.asyncio.sleep = no_sleep
    try:
        stream = realtime_quotes.stream_twse_mis(
            "2330", poll_interval_seconds=0
        )
        values = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return values
    finally:
        realtime_quotes.fetch_twse_mis_quote = original_fetch
        realtime_quotes.asyncio.sleep = original_sleep


def api_snapshot(quote: dict) -> tuple[int, dict]:
    async def fake_fetch(_symbol: str) -> dict:
        return {
            "schema_version": realtime_quotes.REALTIME_QUOTE_SCHEMA_VERSION,
            "provider": quote["provider"],
            "symbol": quote["symbol"],
            "received_at": quote["received_at"],
            "data": quote,
        }

    original = main_module.fetch_realtime_quote
    main_module.fetch_realtime_quote = fake_fetch
    try:
        response = TestClient(main_module.app).get("/api/realtime/quote/2330")
        return response.status_code, response.json()
    finally:
        main_module.fetch_realtime_quote = original


def main() -> int:
    observed = datetime(2026, 7, 27, 9, 1, tzinfo=TAIPEI)
    first = mis_quote(observed_at=observed, price="100", volume="1200")
    second = mis_quote(
        observed_at=observed + timedelta(seconds=5),
        price="100.5",
        volume="1204",
    )
    licensed = fugle_quote(observed + timedelta(minutes=1))
    events = asyncio.run(continuous_updates(first, second))
    api_status, api_payload = api_snapshot(second)

    for provider_quote in (first, licensed):
        require(
            provider_quote["schema_version"]
            == realtime_quotes.REALTIME_QUOTE_SCHEMA_VERSION,
            "provider did not emit the canonical quote contract",
        )
        require(
            provider_quote["last_price"] is not None,
            "latest trade is missing",
        )
        require(
            provider_quote["best_bid"] is not None
            and provider_quote["best_ask"] is not None,
            "best bid/ask is missing",
        )
        require(
            provider_quote["complete_five_levels"] is True,
            "five-level order book is incomplete",
        )
        require(
            provider_quote["total_volume_lots"] is not None,
            "cumulative volume is missing",
        )
        require(
            provider_quote["trading_status"] == "trading",
            "trading status is not explicit",
        )

    require(
        [item["message"]["data"]["last_price"] for item in events]
        == [100.0, 100.5],
        "stream did not emit both price updates",
    )
    require(
        events[1]["message"]["data"]["total_volume_lots"] == 1204,
        "stream did not emit updated cumulative volume",
    )
    require(api_status == 200, "snapshot API did not return HTTP 200")
    require(
        api_payload["data"]["schema_version"]
        == realtime_quotes.REALTIME_QUOTE_SCHEMA_VERSION,
        "snapshot API lost the canonical contract",
    )

    market_chart = (
        ROOT / "src/stock_ai/ui/static/js/features/market-chart.js"
    ).read_text(encoding="utf-8")
    index_html = (
        ROOT / "src/stock_ai/ui/static/index.html"
    ).read_text(encoding="utf-8")
    for label in (
        "交易狀態",
        "最佳委買 / 委賣",
        "委買五檔",
        "委賣五檔",
        "累計成交量 / 更新",
    ):
        require(label in market_chart, f"UI is missing {label}")
    require(
        "20260727-realtime-quote-v1" in index_html,
        "UI cache version was not advanced",
    )

    print(
        json.dumps(
            {
                "schema_version": "stock_ai.stock_001_verification.v1",
                "status": "passed",
                "providers": {
                    "twse_mis": {
                        "authorized": first["authorized"],
                        "trading_status": first["trading_status"],
                        "bid_levels": first["bid_levels"],
                        "ask_levels": first["ask_levels"],
                    },
                    "fugle": {
                        "authorized": licensed["authorized"],
                        "trading_status": licensed["trading_status"],
                        "bid_levels": licensed["bid_levels"],
                        "ask_levels": licensed["ask_levels"],
                    },
                },
                "continuous_updates": {
                    "events": len(events),
                    "prices": [
                        item["message"]["data"]["last_price"]
                        for item in events
                    ],
                    "volumes": [
                        item["message"]["data"]["total_volume_lots"]
                        for item in events
                    ],
                },
                "api": {
                    "/api/realtime/quote/2330": api_status,
                    "schema_version": api_payload["data"]["schema_version"],
                },
                "ui": "contract_present",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
