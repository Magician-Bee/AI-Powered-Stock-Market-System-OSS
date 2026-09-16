from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from stock_ai import realtime_quotes


TAIPEI = ZoneInfo("Asia/Taipei")


def _mis_quote(
    *,
    price: str = "100.0",
    volume: str = "1200",
    observed_at: datetime | None = None,
) -> dict:
    observed = observed_at or datetime(2026, 7, 27, 9, 1, tzinfo=TAIPEI)
    row = {
        "c": "2330",
        "n": "台積電",
        "nf": "台灣積體電路製造股份有限公司",
        "ex": "tse",
        "d": observed.strftime("%Y%m%d"),
        "t": observed.strftime("%H:%M:%S"),
        "tlong": str(int(observed.timestamp() * 1000)),
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
    raw = {
        "queryTime": {
            "sysDate": observed.strftime("%Y%m%d"),
            "sysTime": observed.strftime("%H:%M:%S"),
        },
        "userDelay": 5000,
    }
    return realtime_quotes._parse_mis_row(
        row,
        raw,
        received_at=observed + timedelta(seconds=5),
    )


def _fugle_payload(observed_at: datetime | None = None) -> dict:
    observed = observed_at or datetime(2026, 7, 27, 9, 2, tzinfo=TAIPEI)
    timestamp = int(observed.timestamp() * 1_000_000)
    return {
        "date": observed.date().isoformat(),
        "type": "EQUITY",
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
        "tradingHalt": {"isHalted": False, "time": timestamp},
        "isContinuous": True,
        "serial": 123,
        "lastUpdated": timestamp,
    }


def test_twse_mis_normalizes_complete_stock_001_quote_contract() -> None:
    quote = _mis_quote()

    assert quote["schema_version"] == "stock_ai.realtime_quote.v1"
    assert quote["last_price"] == 100
    assert quote["last_trade_size_lots"] == 3
    assert quote["total_volume_lots"] == 1200
    assert quote["best_bid"] == {"price": 99.5, "size": 10}
    assert quote["best_ask"] == {"price": 100, "size": 11}
    assert quote["bid_levels"] == quote["ask_levels"] == 5
    assert quote["complete_five_levels"] is True
    assert quote["trading_status"] == "trading"
    assert quote["trading_status_source"] == "derived_session_clock"
    assert quote["freshness"] == "live"
    assert quote["is_stale"] is False
    assert quote["authorized"] is False


def test_twse_mis_old_snapshot_is_closed_instead_of_claiming_live_market() -> None:
    old = datetime(2026, 7, 24, 13, 30, tzinfo=TAIPEI)
    quote = _mis_quote(observed_at=old)
    quote = realtime_quotes._parse_mis_row(
        quote["raw"],
        {"userDelay": 5000},
        received_at=datetime(2026, 7, 27, 8, 45, tzinfo=TAIPEI),
    )

    assert quote["trading_status"] == "closed"
    assert quote["freshness"] == "closed"
    assert quote["is_market_open"] is False


def test_fugle_rest_payload_uses_same_quote_contract_and_provider_flags() -> None:
    observed = datetime(2026, 7, 27, 9, 2, tzinfo=TAIPEI)
    quote = realtime_quotes._normalize_fugle_quote(
        _fugle_payload(observed),
        received_at=observed + timedelta(milliseconds=200),
    )

    assert quote["schema_version"] == "stock_ai.realtime_quote.v1"
    assert quote["provider"] == "fugle"
    assert quote["authorized"] is True
    assert quote["last_price"] == 100
    assert quote["last_trade_size_lots"] == 3
    assert quote["total_volume_lots"] == 1200
    assert quote["turnover"] == 120_000_000
    assert quote["inner_volume_lots"] == 500
    assert quote["outer_volume_lots"] == 650
    assert quote["complete_five_levels"] is True
    assert quote["trading_status"] == "trading"
    assert quote["trading_status_source"] == "provider_flag"

    halted_payload = _fugle_payload(observed)
    halted_payload["tradingHalt"] = {"isHalted": True}
    halted = realtime_quotes._normalize_fugle_quote(
        halted_payload,
        received_at=observed + timedelta(milliseconds=200),
    )
    assert halted["trading_status"] == "halted"
    assert halted["is_market_open"] is False


def test_fugle_trade_and_book_events_merge_into_one_canonical_quote() -> None:
    observed = datetime(2026, 7, 27, 9, 2, tzinfo=TAIPEI)
    current = realtime_quotes._normalize_fugle_quote(
        _fugle_payload(observed),
        received_at=observed + timedelta(milliseconds=100),
    )
    trade_time = observed + timedelta(seconds=1)
    merged_trade = realtime_quotes._merge_fugle_stream_quote(
        current,
        "trades",
        {
            "symbol": "2330",
            "exchange": "TWSE",
            "price": 100.5,
            "size": 4,
            "volume": 1204,
            "time": int(trade_time.timestamp() * 1_000_000),
            "serial": 124,
            "isContinuous": True,
        },
        received_at=trade_time + timedelta(milliseconds=100),
    )
    assert merged_trade is not None
    assert merged_trade["last_price"] == 100.5
    assert merged_trade["last_trade_size_lots"] == 4
    assert merged_trade["total_volume_lots"] == 1204
    assert merged_trade["sequence"] == 124

    merged_book = realtime_quotes._merge_fugle_stream_quote(
        merged_trade,
        "books",
        {
            "symbol": "2330",
            "exchange": "TWSE",
            "bids": [{"price": 100, "size": 20}],
            "asks": [{"price": 100.5, "size": 21}],
            "time": int(trade_time.timestamp() * 1_000_000),
            "serial": 125,
        },
        received_at=trade_time + timedelta(milliseconds=200),
    )
    assert merged_book is not None
    assert merged_book["best_bid"] == {"price": 100, "size": 20}
    assert merged_book["best_ask"] == {"price": 100.5, "size": 21}
    assert merged_book["last_price"] == 100.5
    assert merged_book["sequence"] == 125


def test_twse_stream_continuously_emits_changed_quote(monkeypatch) -> None:
    first = _mis_quote(price="100", volume="1200")
    second = _mis_quote(
        price="100.5",
        volume="1204",
        observed_at=datetime(2026, 7, 27, 9, 1, 5, tzinfo=TAIPEI),
    )
    queued = [first, second]

    async def fake_fetch(_symbol: str) -> dict:
        data = queued.pop(0)
        return {
            "schema_version": "stock_ai.realtime_quote.v1",
            "provider": "twse_mis",
            "symbol": "2330",
            "received_at": data["received_at"],
            "data": data,
        }

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(realtime_quotes, "fetch_twse_mis_quote", fake_fetch)
    monkeypatch.setattr(realtime_quotes.asyncio, "sleep", no_sleep)
    async def collect() -> tuple[dict, dict]:
        stream = realtime_quotes.stream_twse_mis(
            "2330", poll_interval_seconds=0
        )
        event_one = await anext(stream)
        event_two = await anext(stream)
        await stream.aclose()
        return event_one, event_two

    event_one, event_two = asyncio.run(collect())

    assert event_one["message"]["event"] == "quote"
    assert event_two["message"]["event"] == "quote"
    assert event_one["message"]["data"]["last_price"] == 100
    assert event_two["message"]["data"]["last_price"] == 100.5
    assert event_two["message"]["data"]["total_volume_lots"] == 1204
    assert (
        event_two["schema_version"]
        == "stock_ai.realtime_stream_event.v1"
    )


def test_fugle_websocket_handshake_uses_external_transport_guard(monkeypatch) -> None:
    calls = []

    class FakeConnection:
        async def __aenter__(self):
            calls.append("handshake")
            return "fake-websocket"

        async def __aexit__(self, exception_type, exception, traceback):
            calls.append(("close", exception_type))

    class FakeGuard:
        async def call(self, scope, operation):
            calls.append(("guard", scope))
            return await operation()

    monkeypatch.setattr(realtime_quotes.websockets, "connect", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr(realtime_quotes, "default_external_transport_guard", lambda: FakeGuard())

    async def exercise():
        async with realtime_quotes._guarded_fugle_websocket() as websocket:
            assert websocket == "fake-websocket"

    asyncio.run(exercise())

    assert calls == [
        ("guard", realtime_quotes.FUGLE_WEBSOCKET_SCOPE),
        "handshake",
        ("close", None),
    ]


def test_fugle_websocket_context_honors_cleanup_exception_suppression(monkeypatch) -> None:
    class SuppressingConnection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exception_type, exception, traceback):
            return exception_type is ValueError

    class PassthroughGuard:
        async def call(self, _scope, operation):
            return await operation()

    monkeypatch.setattr(realtime_quotes.websockets, "connect", lambda *args, **kwargs: SuppressingConnection())
    monkeypatch.setattr(realtime_quotes, "default_external_transport_guard", lambda: PassthroughGuard())

    async def exercise():
        async with realtime_quotes._guarded_fugle_websocket():
            raise ValueError("handled by websocket context")

    asyncio.run(exercise())


def test_realtime_status_publishes_stock_001_capabilities() -> None:
    payload = realtime_quotes.status().as_dict()

    assert payload["quote_schema_version"] == "stock_ai.realtime_quote.v1"
    assert payload["stream_schema_version"] == "stock_ai.realtime_stream_event.v1"
    assert payload["stream_transport"] == "sse"
    assert set(payload["capabilities"]) == {
        "last_trade",
        "best_bid_ask",
        "five_level_order_book",
        "cumulative_volume",
        "trading_status",
        "continuous_updates",
    }
