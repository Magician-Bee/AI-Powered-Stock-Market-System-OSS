from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from .data_platform.source_registry import source_endpoint
from .realtime_data import normalize_symbol
from .taiwan_official import tpex_history_range


SHORT_DAYTRADE_SCHEMA_VERSION = "stock_ai.short_daytrade_history.v1"
TWSE_SHORT_URL = source_endpoint("twse_borrowed_short")
TWSE_DAYTRADE_URL = source_endpoint("twse_daytrade")
TWSE_DAILY_URL = source_endpoint("twse_daily_volume")
TPEX_SHORT_URL = source_endpoint("tpex_borrowed_short")
TPEX_DAYTRADE_URL = source_endpoint("tpex_daytrade")


def _number(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").strip() or 0))
    except ValueError:
        return 0


def _iso_date(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 7:
        digits = f"{int(digits[:3]) + 1911:04d}{digits[3:]}"
    if len(digits) != 8:
        raise ValueError(f"unsupported official trade date: {value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _report_table(payload: dict[str, Any], field: str) -> dict[str, Any]:
    return next(
        (
            table
            for table in payload.get("tables") or []
            if field in (table.get("fields") or [])
        ),
        {},
    )


def _normalized_symbol(symbol: str) -> tuple[str, str]:
    normalized = normalize_symbol(symbol)
    return normalized, normalized.split(".", 1)[0]


def _short_record(
    *,
    payload: dict[str, Any],
    row: list[Any],
    fields: list[Any],
    symbol: str,
) -> dict[str, Any]:
    normalized, _code = _normalized_symbol(symbol)
    return {
        "trade_date": _iso_date(payload.get("date")),
        "symbol": normalized,
        "name": str(row[1]).strip(),
        "borrowed_sell_previous_balance": _number(row[8]),
        "borrowed_sell": _number(row[9]),
        "borrowed_return": _number(row[10]),
        "borrowed_adjustment": _number(row[11]),
        "borrowed_sell_balance": _number(row[12]),
        "borrowed_sell_next_limit": _number(row[13]),
        "raw": {"fields": fields, "row": row},
    }


def parse_twse_short_balance(
    payload: dict[str, Any],
    symbol: str,
) -> dict[str, Any] | None:
    _normalized, code = _normalized_symbol(symbol)
    fields = list(payload.get("fields") or [])
    row = next(
        (
            item
            for item in payload.get("data") or []
            if item and str(item[0]).strip() == code
        ),
        None,
    )
    if not row or len(row) < 14:
        return None
    return _short_record(
        payload=payload,
        row=row,
        fields=fields,
        symbol=symbol,
    )


def parse_tpex_short_balance(
    payload: dict[str, Any],
    symbol: str,
) -> dict[str, Any] | None:
    _normalized, code = _normalized_symbol(symbol)
    table = _report_table(payload, "次一營業日可借券賣出限額")
    row = next(
        (
            item
            for item in table.get("data") or []
            if item and str(item[0]).strip() == code
        ),
        None,
    )
    if not row or len(row) < 14:
        return None
    return _short_record(
        payload=payload,
        row=row,
        fields=list(table.get("fields") or []),
        symbol=symbol,
    )


def _daytrade_record(
    *,
    payload: dict[str, Any],
    row: list[Any],
    fields: list[Any],
    symbol: str,
    total_volume: int | None,
    raw_volume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized, _code = _normalized_symbol(symbol)
    daytrade_volume = _number(row[3])
    return {
        "trade_date": _iso_date(payload.get("date")),
        "symbol": normalized,
        "name": str(row[1]).strip(),
        "daytrade_volume": daytrade_volume,
        "daytrade_buy_value": _number(row[4]),
        "daytrade_sell_value": _number(row[5]),
        "total_traded_volume": total_volume,
        "daytrade_ratio_percent": (
            round(daytrade_volume / total_volume * 100, 4)
            if total_volume
            else None
        ),
        "suspension_note": str(row[2] or "").strip() or None,
        "raw": {
            "daytrade_fields": fields,
            "daytrade_row": row,
            "daily_volume": raw_volume,
        },
    }


def parse_twse_daytrade(
    daytrade_payload: dict[str, Any],
    daily_payload: dict[str, Any],
    symbol: str,
) -> dict[str, Any] | None:
    _normalized, code = _normalized_symbol(symbol)
    day_table = _report_table(daytrade_payload, "當日沖銷交易成交股數")
    day_row = next(
        (
            item
            for item in day_table.get("data") or []
            if item and str(item[0]).strip() == code
        ),
        None,
    )
    volume_table = _report_table(daily_payload, "成交股數")
    volume_row = next(
        (
            item
            for item in volume_table.get("data") or []
            if item and str(item[0]).strip() == code
        ),
        None,
    )
    if not day_row:
        return None
    total_volume = _number(volume_row[2]) if volume_row and len(volume_row) > 2 else None
    return _daytrade_record(
        payload=daytrade_payload,
        row=day_row,
        fields=list(day_table.get("fields") or []),
        symbol=symbol,
        total_volume=total_volume,
        raw_volume={
            "fields": volume_table.get("fields"),
            "row": volume_row,
        },
    )


def parse_tpex_daytrade(
    payload: dict[str, Any],
    symbol: str,
    *,
    total_volume: int | None,
) -> dict[str, Any] | None:
    _normalized, code = _normalized_symbol(symbol)
    table = _report_table(payload, "當日沖銷交易成交股數")
    row = next(
        (
            item
            for item in table.get("data") or []
            if item and str(item[0]).strip() == code
        ),
        None,
    )
    if not row:
        return None
    return _daytrade_record(
        payload=payload,
        row=row,
        fields=list(table.get("fields") or []),
        symbol=symbol,
        total_volume=total_volume,
        raw_volume={
            "source": "tpex_trading_stock",
            "volume": total_volume,
        },
    )


class ShortDaytradeStore:
    def __init__(self, store: SQLiteStore | str | Path):
        self.path = Path(getattr(store, "path", store))
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            conn.execute(
                """
                create table if not exists chip_short_daytrade_history (
                    symbol text not null,
                    trade_date text not null,
                    payload_json text not null,
                    raw_hash text not null,
                    acquired_at text not null,
                    primary key(symbol, trade_date)
                )
                """
            )
            conn.commit()

    def save(self, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            conn.execute(
                """
                insert into chip_short_daytrade_history
                    (symbol, trade_date, payload_json, raw_hash, acquired_at)
                values (?, ?, ?, ?, ?)
                on conflict(symbol, trade_date) do update set
                    payload_json=excluded.payload_json,
                    raw_hash=excluded.raw_hash,
                    acquired_at=excluded.acquired_at
                """,
                (
                    payload["symbol"],
                    payload["trade_date"],
                    payload_json,
                    sha256(payload_json.encode()).hexdigest(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def query(self, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            rows = conn.execute(
                """
                select payload_json, raw_hash, acquired_at
                from chip_short_daytrade_history
                where symbol=?
                order by trade_date desc
                limit ?
                """,
                (normalize_symbol(symbol), max(1, min(int(limit), 365))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for payload_json, raw_hash, acquired_at in reversed(rows):
            item = json.loads(payload_json)
            item["raw_hash"] = raw_hash
            item["acquired_at"] = acquired_at
            items.append(item)
        return items

    def borrowed_short_items(self, symbol: str, limit: int = 60) -> list[dict[str, Any]]:
        """Project captured public borrow activity for chip PIT accounting.

        Published borrow activity is useful chip context, but it does not
        establish a borrow locate, fee, recall policy, or executable short
        capacity.
        """

        normalized = normalize_symbol(symbol)
        source_id = "tpex_official_web" if normalized.endswith(".TWO") else "twse_official_web"
        projected: list[dict[str, Any]] = []
        for item in self.query(normalized, limit=limit):
            borrowed = item.get("borrowed")
            if not isinstance(borrowed, dict):
                continue
            projected.append({
                **borrowed,
                "trade_date": item.get("trade_date"),
                "symbol": normalized,
                "source_id": source_id,
                "acquired_at": item.get("acquired_at"),
                "source_urls": list(item.get("source_urls") or []),
                "raw_hash": item.get("raw_hash"),
                "record_kind": "published_borrow_activity",
            })
        return projected


def _fetch_date(
    symbol: str,
    target: date,
    *,
    tpex_total_volume: int | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_symbol(symbol)
    date_key = target.strftime("%Y%m%d")
    headers = {"User-Agent": "Mozilla/5.0 StockAI/1.0"}
    guard = default_external_transport_guard()
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers=headers,
    ) as client:
        if normalized.endswith(".TWO"):
            tpex_date = target.strftime("%Y/%m/%d")
            short_response = guard.call_sync(
                f"source:tpex:short_balance:{date_key}",
                lambda: client.get(
                    TPEX_SHORT_URL,
                    params={"date": tpex_date, "response": "json"},
                ),
            )
            day_response = guard.call_sync(
                f"source:tpex:daytrade:{date_key}",
                lambda: client.get(
                    TPEX_DAYTRADE_URL,
                    params={
                        "type": "Daily",
                        "date": tpex_date,
                        "response": "json",
                    },
                ),
            )
            for response in (short_response, day_response):
                response.raise_for_status()
            short = parse_tpex_short_balance(short_response.json(), normalized)
            daytrade = parse_tpex_daytrade(
                day_response.json(),
                normalized,
                total_volume=tpex_total_volume,
            )
            source_urls = [str(short_response.url), str(day_response.url)]
            exchange = "TPEx"
        else:
            short_response = guard.call_sync(
                f"source:twse:short_balance:{date_key}",
                lambda: client.get(
                    TWSE_SHORT_URL,
                    params={"response": "json", "date": date_key},
                ),
            )
            day_response = guard.call_sync(
                f"source:twse:daytrade:{date_key}",
                lambda: client.get(
                    TWSE_DAYTRADE_URL,
                    params={
                        "response": "json",
                        "date": date_key,
                        "selectType": "All",
                    },
                ),
            )
            daily_response = guard.call_sync(
                f"source:twse:daily_volume:{date_key}",
                lambda: client.get(
                    TWSE_DAILY_URL,
                    params={
                        "response": "json",
                        "date": date_key,
                        "type": "ALLBUT0999",
                    },
                ),
            )
            for response in (short_response, day_response, daily_response):
                response.raise_for_status()
            short = parse_twse_short_balance(short_response.json(), normalized)
            daytrade = parse_twse_daytrade(
                day_response.json(),
                daily_response.json(),
                normalized,
            )
            source_urls = [
                str(short_response.url),
                str(day_response.url),
                str(daily_response.url),
            ]
            exchange = "TWSE"
    if not short and not daytrade:
        return None
    return {
        "trade_date": (short or daytrade)["trade_date"],
        "symbol": normalized,
        "name": (short or daytrade).get("name"),
        "exchange": exchange,
        "borrowed": short,
        "daytrade": daytrade,
        "source_urls": source_urls,
    }


def _heat(ratio: float | None) -> dict[str, Any]:
    if ratio is None:
        return {"level": "unavailable", "label": "資料不足"}
    if ratio >= 40:
        return {"level": "elevated", "label": "高熱度"}
    if ratio >= 25:
        return {"level": "active", "label": "活躍"}
    return {"level": "normal", "label": "一般"}


def _decorate_and_summarize(
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_dates = [
        item["trade_date"]
        for item in reversed(items)
        if item.get("daytrade")
    ][:2]
    recent_cutoff = date.today() - timedelta(days=7)
    for item in items:
        daytrade = item.get("daytrade")
        if not daytrade:
            continue
        ratio = daytrade.get("daytrade_ratio_percent")
        daytrade["heat"] = _heat(float(ratio) if ratio is not None else None)
        item_date = date.fromisoformat(item["trade_date"])
        daytrade["revision_status"] = (
            "provisional_t_plus_2"
            if item["trade_date"] in latest_dates and item_date >= recent_cutoff
            else "published"
        )

    borrowed_items = [item["borrowed"] for item in items if item.get("borrowed")]
    daytrade_items = [item["daytrade"] for item in items if item.get("daytrade")]
    latest_borrowed = borrowed_items[-1] if borrowed_items else None
    latest_daytrade = daytrade_items[-1] if daytrade_items else None
    prior_ratio = next(
        (
            item.get("daytrade_ratio_percent")
            for item in reversed(daytrade_items[:-1])
            if item.get("daytrade_ratio_percent") is not None
        ),
        None,
    )
    ratios = [
        float(item["daytrade_ratio_percent"])
        for item in daytrade_items[-5:]
        if item.get("daytrade_ratio_percent") is not None
    ]
    return {
        "borrowed": {
            "latest_balance": (
                latest_borrowed.get("borrowed_sell_balance")
                if latest_borrowed
                else None
            ),
            "latest_balance_change": (
                latest_borrowed.get("borrowed_sell_balance", 0)
                - latest_borrowed.get("borrowed_sell_previous_balance", 0)
                if latest_borrowed
                else None
            ),
            "recent_5d_sold": sum(
                int(item.get("borrowed_sell") or 0)
                for item in borrowed_items[-5:]
            ),
            "recent_5d_returned": sum(
                int(item.get("borrowed_return") or 0)
                for item in borrowed_items[-5:]
            ),
        },
        "daytrade": {
            "latest_ratio_percent": (
                latest_daytrade.get("daytrade_ratio_percent")
                if latest_daytrade
                else None
            ),
            "ratio_change_percentage_points": (
                round(
                    float(latest_daytrade["daytrade_ratio_percent"])
                    - float(prior_ratio),
                    4,
                )
                if latest_daytrade
                and latest_daytrade.get("daytrade_ratio_percent") is not None
                and prior_ratio is not None
                else None
            ),
            "recent_5d_average_ratio_percent": (
                round(sum(ratios) / len(ratios), 4) if ratios else None
            ),
            "heat": (
                latest_daytrade.get("heat")
                if latest_daytrade
                else _heat(None)
            ),
        },
    }


def query_short_daytrade_history(
    store: SQLiteStore,
    symbol: str,
    *,
    days: int = 14,
    refresh: bool = False,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized.endswith((".TW", ".TWO")):
        raise ValueError("short/day-trade history supports explicit .TW or .TWO symbols")
    day_count = max(1, min(int(days), 60))
    history = ShortDaytradeStore(store)
    sync: dict[str, Any] | None = None
    if refresh:
        targets = [
            date.today() - timedelta(days=offset)
            for offset in range(day_count)
        ]
        tpex_volume_by_date: dict[str, int] = {}
        tpex_volume_coverage: list[dict[str, Any]] | None = None
        if normalized.endswith(".TWO"):
            code = normalized.split(".", 1)[0]
            points, tpex_volume_coverage = tpex_history_range(
                code,
                min(targets).isoformat(),
                max(targets).isoformat(),
            )
            tpex_volume_by_date = {
                point.date: point.volume
                for point in points
            }
        stored = 0
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    _fetch_date,
                    normalized,
                    target,
                    tpex_total_volume=tpex_volume_by_date.get(
                        target.isoformat()
                    ),
                ): target
                for target in targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    item = future.result()
                    if item:
                        history.save(item)
                        stored += 1
                except Exception as exc:
                    failures.append(
                        {
                            "date": target.isoformat(),
                            "error_type": type(exc).__name__,
                        }
                    )
        sync = {
            "requested_calendar_days": len(targets),
            "stored_trading_days": stored,
            "failed_dates": sorted(
                failures,
                key=lambda item: item["date"],
            ),
            "tpex_volume_coverage": tpex_volume_coverage,
        }
    items = history.query(normalized, limit=day_count)
    summary = _decorate_and_summarize(items)
    complete_items = [
        item
        for item in items
        if item.get("borrowed") and item.get("daytrade")
    ]
    return {
        "schema_version": SHORT_DAYTRADE_SCHEMA_VERSION,
        "symbol": normalized,
        "exchange": "TPEx" if normalized.endswith(".TWO") else "TWSE",
        "status": (
            "complete"
            if items and len(complete_items) == len(items)
            else "partial"
            if items
            else "no_data"
        ),
        "items": items,
        "summary": summary,
        "coverage": {
            "count": len(items),
            "complete_count": len(complete_items),
            "first_date": items[0]["trade_date"] if items else None,
            "last_date": items[-1]["trade_date"] if items else None,
        },
        "sync": sync,
        "sources": {
            "borrowed_short": (
                "TPEx official margin/sbl"
                if normalized.endswith(".TWO")
                else "TWSE official TWT93U"
            ),
            "daytrade": (
                "TPEx official intraday/stat"
                if normalized.endswith(".TWO")
                else "TWSE official TWTB4U"
            ),
            "total_volume": (
                "TPEx official afterTrading/tradingStock"
                if normalized.endswith(".TWO")
                else "TWSE official MI_INDEX"
            ),
        },
        "truthfulness": {
            "borrowed_sell_is_separate_from_margin_short": True,
            "daytrade_ratio_formula": (
                "official daytrade volume / official total traded volume * 100"
            ),
            "daytrade_recent_observations_may_be_revised_through_t_plus_2": True,
            "heat_method": {
                "kind": "mechanical_display_band_not_investment_signal",
                "normal": "ratio < 25%",
                "active": "25% <= ratio < 40%",
                "elevated": "ratio >= 40%",
            },
            "missing_dates_not_zero_filled": True,
            "raw_hash_scope": (
                "normalized record containing the official source rows"
            ),
            "raw_payload_hash_preserved": (
                all(bool(item.get("raw_hash")) for item in items)
                if items
                else False
            ),
        },
    }
