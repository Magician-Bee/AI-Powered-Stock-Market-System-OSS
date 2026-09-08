from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.agent_runtime.runtime_paths import AgentRuntimePaths
from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .config import get_settings
from .data_platform.source_registry import source_endpoint
from .taiwan_official import normalize_taiwan_code


INTRADAY_CANDLE_SCHEMA_VERSION = "stock_ai.intraday_candle.v1"
INTRADAY_CANDLES_SCHEMA_VERSION = "stock_ai.intraday_candles.v1"
INTRADAY_CANDLE_IMPORT_SCHEMA_VERSION = "stock_ai.intraday_candle_import.v1"
SUPPORTED_TIMEFRAMES = (1, 5, 15, 30, 60)
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
REGULAR_SESSION_OPEN = time(9, 0)
REGULAR_SESSION_CLOSE = time(13, 30)
REGULAR_SESSION_MINUTES = 270


class IntradayCandleError(RuntimeError):
    pass


class InvalidIntradayTimeframe(IntradayCandleError):
    pass


@dataclass(frozen=True)
class IntradaySource:
    source_id: str
    source_kind: str
    priority: int
    authorized: bool
    note: str


FUGLE_SOURCE = IntradaySource(
    source_id="fugle_marketdata",
    source_kind="licensed_provider_candle",
    priority=100,
    authorized=True,
    note="Fugle licensed one-minute candles.",
)
YAHOO_SOURCE = IntradaySource(
    source_id="yahoo_finance",
    source_kind="auxiliary_research_candle",
    priority=30,
    authorized=False,
    note="Yahoo Finance auxiliary one-minute candles; research display only.",
)
TWSE_QUOTE_SOURCE = IntradaySource(
    source_id="twse_mis",
    source_kind="public_quote_sample",
    priority=10,
    authorized=False,
    note="TWSE MIS quote samples observed while the application was running.",
)
FUGLE_QUOTE_SOURCE = IntradaySource(
    source_id="fugle_marketdata",
    source_kind="licensed_quote_sample",
    priority=20,
    authorized=True,
    note="Fugle licensed trade samples observed while the application was running.",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise IntradayCandleError("candle timestamp is required")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise IntradayCandleError(
                f"invalid candle timestamp: {value}"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TIMEZONE)
    return parsed.astimezone(TAIPEI_TIMEZONE)


def _canonical_symbol(symbol: str, exchange: str | None = None) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        raise IntradayCandleError("symbol is required")
    if raw.endswith((".TW", ".TWO")):
        return raw
    code = normalize_taiwan_code(raw)
    normalized_exchange = str(exchange or "").strip().upper()
    suffix = ".TWO" if normalized_exchange in {"OTC", "TPEX"} else ".TW"
    return f"{code}{suffix}"


def _float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise IntradayCandleError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise IntradayCandleError(f"{field} must be finite")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _minute_floor(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _session_bounds(trading_date: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(trading_date)
    session_open = datetime.combine(
        day, REGULAR_SESSION_OPEN, tzinfo=TAIPEI_TIMEZONE
    )
    session_close = datetime.combine(
        day, REGULAR_SESSION_CLOSE, tzinfo=TAIPEI_TIMEZONE
    )
    return session_open, session_close


def normalize_one_minute_candle(
    candle: dict[str, Any],
    *,
    source: IntradaySource,
    observed_at: datetime,
) -> dict[str, Any]:
    provider_timestamp = _parse_datetime(
        candle.get("bucket_start", candle.get("date"))
    )
    bucket_start = _minute_floor(provider_timestamp)
    if candle.get("timestamp_semantics") == "period_end":
        session_open, session_close = _session_bounds(
            provider_timestamp.date().isoformat()
        )
        if session_open < provider_timestamp <= session_close:
            bucket_start -= timedelta(minutes=1)
    bucket_end = bucket_start + timedelta(minutes=1)
    symbol = _canonical_symbol(
        str(candle.get("symbol") or ""),
        str(candle.get("exchange") or ""),
    )
    open_price = _float(candle.get("open"), field="open")
    high_price = _float(candle.get("high"), field="high")
    low_price = _float(candle.get("low"), field="low")
    close_price = _float(candle.get("close"), field="close")
    if high_price < max(open_price, low_price, close_price):
        raise IntradayCandleError("high must be at least open, low and close")
    if low_price > min(open_price, high_price, close_price):
        raise IntradayCandleError("low must be at most open, high and close")
    volume_lots = _float(candle.get("volume", 0), field="volume")
    if volume_lots < 0:
        raise IntradayCandleError("volume must be non-negative")
    raw_payload = candle.get("raw", candle)
    raw_hash = _content_hash(raw_payload)
    normalized = {
        "schema_version": INTRADAY_CANDLE_SCHEMA_VERSION,
        "symbol": symbol,
        "trading_date": bucket_start.date().isoformat(),
        "bucket_start": bucket_start.isoformat(timespec="seconds"),
        "bucket_end": bucket_end.isoformat(timespec="seconds"),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume_lots": volume_lots,
        "turnover": _optional_float(candle.get("turnover")),
        "average": _optional_float(candle.get("average")),
        "opening_cumulative_volume_lots": _optional_float(
            candle.get("opening_cumulative_volume_lots")
        ),
        "closing_cumulative_volume_lots": _optional_float(
            candle.get("closing_cumulative_volume_lots")
        ),
        "source_id": source.source_id,
        "source_kind": source.source_kind,
        "source_priority": source.priority,
        "authorized": source.authorized,
        "is_final": bool(candle.get("is_final")),
        "sequence": (
            int(candle["sequence"])
            if candle.get("sequence") is not None
            else None
        ),
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ),
        "raw_payload_hash": raw_hash,
        "raw_payload": raw_payload,
        "quality": {
            "source_note": source.note,
            "timestamp_semantics": candle.get(
                "timestamp_semantics", "period_start"
            ),
            "provider_timestamp": provider_timestamp.isoformat(
                timespec="seconds"
            ),
            "volume_complete": bool(
                candle.get(
                    "volume_complete",
                    source.source_kind != "public_quote_sample",
                )
            ),
            **(
                candle.get("quality")
                if isinstance(candle.get("quality"), dict)
                else {}
            ),
        },
    }
    normalized["revision_id"] = (
        "ICR-"
        + _content_hash(
            {
                "symbol": symbol,
                "bucket_start": normalized["bucket_start"],
                "source_id": source.source_id,
                "raw_payload_hash": raw_hash,
                "ohlcv": [
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    volume_lots,
                ],
            }
        )[:32]
    )
    return normalized


class IntradayCandleStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_migrations(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ManagedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        configure_connection(connection)
        return connection

    def record_candles(
        self,
        candles: Iterable[dict[str, Any]],
        *,
        source: IntradaySource,
        is_complete: bool | dict[str, bool],
        response_payload: Any | None = None,
        requested_at: datetime | None = None,
        completed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested = requested_at or _utc_now()
        completed = completed_at or _utc_now()
        normalized = [
            normalize_one_minute_candle(
                dict(candle),
                source=source,
                observed_at=completed,
            )
            for candle in candles
        ]
        normalized.sort(key=lambda item: item["bucket_start"])
        by_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for candle in normalized:
            by_day[(candle["symbol"], candle["trading_date"])].append(candle)

        inserted = 0
        receipt_ids: list[str] = []
        ingested_at = completed.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        )
        with self._connect() as connection:
            apply_migrations(connection)
            for candle in normalized:
                cursor = connection.execute(
                    """
                    insert or ignore into intraday_candle_revisions(
                        revision_id, symbol, trading_date, bucket_start,
                        bucket_end, open, high, low, close, volume_lots,
                        turnover, average, opening_cumulative_volume_lots,
                        closing_cumulative_volume_lots, source_id,
                        source_kind, source_priority, authorized, is_final,
                        sequence, observed_at, ingested_at, raw_payload_hash,
                        raw_payload_json, quality_json
                    ) values (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        candle["revision_id"],
                        candle["symbol"],
                        candle["trading_date"],
                        candle["bucket_start"],
                        candle["bucket_end"],
                        candle["open"],
                        candle["high"],
                        candle["low"],
                        candle["close"],
                        candle["volume_lots"],
                        candle["turnover"],
                        candle["average"],
                        candle["opening_cumulative_volume_lots"],
                        candle["closing_cumulative_volume_lots"],
                        candle["source_id"],
                        candle["source_kind"],
                        candle["source_priority"],
                        int(candle["authorized"]),
                        int(candle["is_final"]),
                        candle["sequence"],
                        candle["observed_at"],
                        ingested_at,
                        candle["raw_payload_hash"],
                        json.dumps(
                            candle["raw_payload"],
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                        json.dumps(
                            candle["quality"],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                inserted += cursor.rowcount

            response_hash = _content_hash(
                response_payload
                if response_payload is not None
                else [item["raw_payload_hash"] for item in normalized]
            )
            for (symbol, trading_date), day_candles in by_day.items():
                day_complete = (
                    bool(is_complete.get(trading_date))
                    if isinstance(is_complete, dict)
                    else bool(is_complete)
                )
                receipt_payload = {
                    "schema_version": INTRADAY_CANDLE_IMPORT_SCHEMA_VERSION,
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "source_id": source.source_id,
                    "source_priority": source.priority,
                    "source_timeframe_minutes": 1,
                    "candle_count": len(day_candles),
                    "is_complete": day_complete,
                    "requested_at": requested.astimezone(timezone.utc).isoformat(
                        timespec="microseconds"
                    ),
                    "completed_at": ingested_at,
                    "response_hash": response_hash,
                    "metadata": metadata or {},
                }
                import_id = "ICI-" + _content_hash(receipt_payload)[:32]
                receipt_ids.append(import_id)
                connection.execute(
                    """
                    insert or ignore into intraday_candle_import_receipts(
                        import_id, symbol, trading_date, source_id,
                        source_priority, source_timeframe_minutes, candle_count,
                        is_complete, requested_at, completed_at, response_hash,
                        metadata_json
                    ) values (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        import_id,
                        symbol,
                        trading_date,
                        source.source_id,
                        source.priority,
                        len(day_candles),
                        int(day_complete),
                        receipt_payload["requested_at"],
                        receipt_payload["completed_at"],
                        response_hash,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                    ),
                )
            connection.commit()
        return {
            "schema_version": INTRADAY_CANDLE_IMPORT_SCHEMA_VERSION,
            "source_id": source.source_id,
            "candle_count": len(normalized),
            "inserted_revision_count": inserted,
            "trading_dates": sorted(
                {item["trading_date"] for item in normalized}
            ),
            "receipt_ids": receipt_ids,
        }

    def _latest_rows(
        self,
        symbol: str,
        trading_date: str,
        *,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        canonical = _canonical_symbol(symbol)
        cutoff = (
            as_of.astimezone(timezone.utc).isoformat(timespec="microseconds")
            if as_of
            else None
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                  from (
                    select r.*,
                           row_number() over (
                               partition by r.bucket_start
                               order by r.source_priority desc,
                                        r.is_final desc,
                                        r.observed_at desc,
                                        r.ingested_at desc,
                                        r.revision_id desc
                           ) as selection_rank
                      from intraday_candle_revisions r
                     where r.symbol=? and r.trading_date=?
                       and (? is null or r.ingested_at <= ?)
                  )
                 where selection_rank=1
                 order by bucket_start
                """,
                (canonical, trading_date, cutoff, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def available_dates(self, symbol: str) -> dict[str, Any]:
        canonical = _canonical_symbol(symbol)
        with self._connect() as connection:
            rows = connection.execute(
                """
                select trading_date,
                       count(distinct bucket_start) as revision_bucket_count,
                       count(*) as revision_count,
                       max(ingested_at) as latest_ingested_at
                  from intraday_candle_revisions
                 where symbol=?
                 group by trading_date
                 order by trading_date desc
                """,
                (canonical,),
            ).fetchall()
        return {
            "schema_version": INTRADAY_CANDLES_SCHEMA_VERSION,
            "symbol": canonical,
            "dates": [dict(row) for row in rows],
            "count": len(rows),
        }

    def _latest_receipt(
        self,
        symbol: str,
        trading_date: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        cutoff = (
            as_of.astimezone(timezone.utc).isoformat(timespec="microseconds")
            if as_of
            else None
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                select *
                  from intraday_candle_import_receipts
                 where symbol=? and trading_date=?
                   and (? is null or completed_at <= ?)
                 order by source_priority desc, completed_at desc, import_id desc
                 limit 1
                """,
                (
                    _canonical_symbol(symbol),
                    trading_date,
                    cutoff,
                    cutoff,
                ),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["is_complete"] = bool(result["is_complete"])
        return result

    def reconstruct(
        self,
        symbol: str,
        trading_date: str,
        *,
        timeframe: int,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise InvalidIntradayTimeframe(
                f"timeframe must be one of {SUPPORTED_TIMEFRAMES}"
            )
        date.fromisoformat(trading_date)
        canonical = _canonical_symbol(symbol)
        rows = self._latest_rows(
            canonical, trading_date, as_of=as_of
        )

        session_open, session_close = _session_bounds(trading_date)
        grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            bucket = _parse_datetime(row["bucket_start"])
            if not session_open <= bucket < session_close:
                continue
            elapsed = int((bucket - session_open).total_seconds() // 60)
            aggregate_start = session_open + timedelta(
                minutes=(elapsed // timeframe) * timeframe
            )
            grouped[aggregate_start].append(row)

        points: list[dict[str, Any]] = []
        for bucket_start in sorted(grouped):
            members = sorted(
                grouped[bucket_start], key=lambda item: item["bucket_start"]
            )
            volumes = [float(item["volume_lots"]) for item in members]
            turnover_values = [
                float(item["turnover"])
                for item in members
                if item["turnover"] is not None
            ]
            volume_total = sum(volumes)
            average_members = [
                item
                for item in members
                if item["average"] is not None
                and float(item["volume_lots"]) > 0
            ]
            average_volume = sum(
                float(item["volume_lots"]) for item in average_members
            )
            weighted_average = (
                sum(
                    float(item["average"]) * float(item["volume_lots"])
                    for item in average_members
                )
                / average_volume
                if average_volume > 0
                else None
            )
            aggregate_end = min(
                bucket_start + timedelta(minutes=timeframe), session_close
            )
            source_ids = sorted({str(item["source_id"]) for item in members})
            points.append(
                {
                    "schema_version": INTRADAY_CANDLE_SCHEMA_VERSION,
                    "symbol": canonical,
                    "trading_date": trading_date,
                    "timeframe_minutes": timeframe,
                    "date": bucket_start.isoformat(timespec="seconds"),
                    "bucket_start": bucket_start.isoformat(
                        timespec="seconds"
                    ),
                    "bucket_end": aggregate_end.isoformat(timespec="seconds"),
                    "open": float(members[0]["open"]),
                    "high": max(float(item["high"]) for item in members),
                    "low": min(float(item["low"]) for item in members),
                    "close": float(members[-1]["close"]),
                    "volume": volume_total,
                    "volume_lots": volume_total,
                    "turnover": (
                        sum(turnover_values) if turnover_values else None
                    ),
                    "average": weighted_average,
                    "one_minute_count": len(members),
                    "expected_one_minute_count": int(
                        (aggregate_end - bucket_start).total_seconds() // 60
                    ),
                    "is_final": all(bool(item["is_final"]) for item in members),
                    "source_ids": source_ids,
                    "authorized": all(
                        bool(item["authorized"]) for item in members
                    ),
                    "revision_ids": [
                        str(item["revision_id"]) for item in members
                    ],
                }
            )

        selected_minutes = {
            _parse_datetime(row["bucket_start"]) for row in rows
        }
        missing_minutes = REGULAR_SESSION_MINUTES - len(
            {
                minute
                for minute in selected_minutes
                if session_open <= minute < session_close
            }
        )
        receipt = self._latest_receipt(
            canonical,
            trading_date,
            as_of=as_of,
        )
        source_ids = sorted({str(row["source_id"]) for row in rows})
        complete_import = bool(receipt and receipt["is_complete"])
        return {
            "schema_version": INTRADAY_CANDLES_SCHEMA_VERSION,
            "symbol": canonical,
            "trading_date": trading_date,
            "timeframe_minutes": timeframe,
            "available_timeframes": list(SUPPORTED_TIMEFRAMES),
            "reconstructable": bool(rows),
            "reconstruction_status": (
                "complete"
                if rows and complete_import
                else "partial"
                if rows
                else "no_data"
            ),
            "reconstruction_algorithm": (
                "latest source-priority one-minute revisions grouped from "
                "Asia/Taipei 09:00 regular-session anchor"
            ),
            "source_one_minute_count": len(rows),
            "candle_count": len(points),
            "missing_session_minute_count": max(0, missing_minutes),
            "source_ids": source_ids,
            "authorized": bool(rows)
            and all(bool(row["authorized"]) for row in rows),
            "latest_import_receipt": receipt,
            "as_of": (
                as_of.astimezone(timezone.utc).isoformat(
                    timespec="microseconds"
                )
                if as_of
                else None
            ),
            "points": points,
            "note": (
                "Every timeframe is rebuilt from persisted one-minute "
                "revisions; missing minutes are never forward-filled."
            ),
        }

    def record_quote(self, quote: dict[str, Any]) -> dict[str, Any] | None:
        if quote.get("schema_version") != "stock_ai.realtime_quote.v1":
            return None
        price = _optional_float(quote.get("last_price"))
        timestamp = quote.get("exchange_timestamp")
        if price is None or not timestamp:
            return None
        observed = _parse_datetime(timestamp)
        bucket_start = _minute_floor(observed)
        symbol = _canonical_symbol(
            str(quote.get("symbol") or ""),
            str(quote.get("exchange") or ""),
        )
        trading_date = bucket_start.date().isoformat()
        source = (
            FUGLE_QUOTE_SOURCE
            if quote.get("provider") == "fugle"
            else TWSE_QUOTE_SOURCE
        )
        with self._connect() as connection:
            prior_row = connection.execute(
                """
                select *
                  from intraday_candle_revisions
                 where symbol=? and trading_date=? and bucket_start=?
                   and source_id=? and source_kind=?
                 order by observed_at desc, ingested_at desc, revision_id desc
                 limit 1
                """,
                (
                    symbol,
                    trading_date,
                    bucket_start.isoformat(timespec="seconds"),
                    source.source_id,
                    source.source_kind,
                ),
            ).fetchone()
        existing = [dict(prior_row)] if prior_row is not None else []
        total_volume = _optional_float(quote.get("total_volume_lots"))
        last_size = _optional_float(quote.get("last_trade_size_lots")) or 0.0
        if existing:
            prior = existing[-1]
            opening_cumulative = _optional_float(
                prior.get("opening_cumulative_volume_lots")
            )
            open_price = float(prior["open"])
            high_price = max(float(prior["high"]), price)
            low_price = min(float(prior["low"]), price)
        else:
            opening_cumulative = (
                max(0.0, total_volume - last_size)
                if total_volume is not None
                else None
            )
            open_price = high_price = low_price = price
        volume = (
            max(0.0, total_volume - opening_cumulative)
            if total_volume is not None and opening_cumulative is not None
            else last_size
        )
        candle = {
            "symbol": symbol,
            "exchange": quote.get("exchange"),
            "bucket_start": bucket_start.isoformat(timespec="seconds"),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": price,
            "volume": volume,
            "opening_cumulative_volume_lots": opening_cumulative,
            "closing_cumulative_volume_lots": total_volume,
            "sequence": quote.get("sequence"),
            "is_final": False,
            "volume_complete": False,
            "quality": {
                "derived_from": quote["schema_version"],
                "sample_only": True,
            },
            "raw": quote,
        }
        return self.record_candles(
            [candle],
            source=source,
            is_complete=False,
            response_payload=quote,
            completed_at=observed,
            metadata={"mode": "quote_sample"},
        )


def _database_path() -> Path:
    override = os.getenv("STOCK_AI_MARKET_DATA_DB")
    if override:
        return Path(override)
    return AgentRuntimePaths.discover().database


@lru_cache(maxsize=8)
def _cached_store(path: str) -> IntradayCandleStore:
    return IntradayCandleStore(path)


def get_intraday_candle_store() -> IntradayCandleStore:
    return _cached_store(str(_database_path().expanduser().resolve()))


def clear_intraday_candle_store_cache() -> None:
    _cached_store.cache_clear()


def normalize_fugle_candles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        output.append(
            {
                "symbol": row.get("symbol") or payload.get("symbol"),
                "exchange": row.get("exchange") or payload.get("exchange"),
                "date": row.get("date"),
                "timestamp_semantics": "period_end",
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "average": row.get("average"),
                "is_final": bool(row.get("isFinal", True)),
                "volume_complete": True,
                "raw": row,
            }
        )
    return output


def normalize_fugle_stream_candle(
    payload: dict[str, Any],
) -> dict[str, Any]:
    rows = normalize_fugle_candles({"data": [payload]})
    if not rows:
        raise IntradayCandleError("Fugle candle event has no data")
    rows[0]["is_final"] = bool(payload.get("isFinal", False))
    return rows[0]


async def fetch_fugle_candles(
    symbol: str, *, trading_date: str | None = None
) -> dict[str, Any]:
    settings = get_settings()
    api_key = settings.fugle_marketdata_api_key
    if not api_key:
        raise IntradayCandleError("缺少 FUGLE_MARKETDATA_API_KEY。")
    code = normalize_taiwan_code(symbol)
    target_date = (
        date.fromisoformat(trading_date)
        if trading_date
        else datetime.now(TAIPEI_TIMEZONE).date()
    )
    today = datetime.now(TAIPEI_TIMEZONE).date()
    if target_date == today:
        url = source_endpoint("fugle_intraday_candles", symbol=code)
        params = {"timeframe": "1", "sort": "asc"}
    else:
        url = source_endpoint("fugle_historical_candles", symbol=code)
        params = {
            "from": target_date.isoformat(),
            "to": target_date.isoformat(),
            "timeframe": "1",
            "sort": "asc",
        }
    requested_at = _utc_now()
    async def load():
        async with httpx.AsyncClient(timeout=20) as client:
            return await client.get(
                url, params=params, headers={"X-API-KEY": api_key}
            )

    response = await default_external_transport_guard().call(
        f"source:fugle_marketdata:intraday_candles:{code}:{target_date.isoformat()}",
        load,
    )
    if response.status_code in {401, 403}:
        raise IntradayCandleError("Fugle 分 K API 授權失敗。")
    if response.status_code == 429:
        raise IntradayCandleError("Fugle 分 K API 速率限制 429。")
    if response.status_code >= 400:
        raise IntradayCandleError(
            f"Fugle 分 K API 回應 {response.status_code}: "
            f"{response.text[:200]}"
        )
    payload = response.json()
    rows = normalize_fugle_candles(payload)
    matching = [
        row
        for row in rows
        if _parse_datetime(row["date"]).date() == target_date
    ]
    import_result = get_intraday_candle_store().record_candles(
        matching,
        source=FUGLE_SOURCE,
        is_complete=target_date < today,
        response_payload=payload,
        requested_at=requested_at,
        completed_at=_utc_now(),
        metadata={
            "endpoint": url,
            "requested_date": target_date.isoformat(),
            "provider_timeframe": 1,
        },
    )
    return {
        "provider": FUGLE_SOURCE.source_id,
        "requested_date": target_date.isoformat(),
        "received_count": len(matching),
        "import": import_result,
    }


def fetch_yahoo_candles(
    symbol: str, *, trading_date: str | None = None
) -> dict[str, Any]:
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - dependency is installed in app
        raise IntradayCandleError("yfinance is not installed") from exc

    canonical = _canonical_symbol(symbol)
    requested_at = _utc_now()
    history = yf.Ticker(canonical).history(
        period="7d",
        interval="1m",
        auto_adjust=False,
        prepost=False,
    )
    if history is None or history.empty:
        raise IntradayCandleError(
            f"Yahoo Finance has no one-minute candles for {canonical}"
        )
    target = date.fromisoformat(trading_date) if trading_date else None
    rows: list[dict[str, Any]] = []
    for index, row in history.iterrows():
        timestamp = (
            index.to_pydatetime()
            if hasattr(index, "to_pydatetime")
            else _parse_datetime(index)
        )
        timestamp = _parse_datetime(timestamp)
        if target is not None and timestamp.date() != target:
            continue
        values = {
            key: _optional_float(row.get(key))
            for key in ("Open", "High", "Low", "Close", "Volume")
        }
        if any(values[key] is None for key in ("Open", "High", "Low", "Close")):
            continue
        rows.append(
            {
                "symbol": canonical,
                "bucket_start": timestamp.isoformat(timespec="seconds"),
                "open": values["Open"],
                "high": values["High"],
                "low": values["Low"],
                "close": values["Close"],
                "volume": max(0.0, float(values["Volume"] or 0) / 1000),
                "is_final": timestamp < datetime.now(TAIPEI_TIMEZONE).replace(
                    second=0, microsecond=0
                ),
                "volume_complete": True,
                "raw": {
                    "timestamp": timestamp.isoformat(timespec="seconds"),
                    **values,
                },
            }
        )
    if not rows:
        requested = trading_date or "latest seven-day window"
        raise IntradayCandleError(
            f"Yahoo Finance has no one-minute candles for {canonical} "
            f"on {requested}"
        )
    today = datetime.now(TAIPEI_TIMEZONE).date()
    import_result = get_intraday_candle_store().record_candles(
        rows,
        source=YAHOO_SOURCE,
        is_complete={
            _parse_datetime(row["bucket_start"]).date().isoformat():
            _parse_datetime(row["bucket_start"]).date() < today
            for row in rows
        },
        response_payload=[row["raw"] for row in rows],
        requested_at=requested_at,
        completed_at=_utc_now(),
        metadata={
            "dataset_id": "yahoo_intraday_chart",
            "period": "7d",
            "interval": "1m",
            "research_only": True,
        },
    )
    return {
        "provider": YAHOO_SOURCE.source_id,
        "requested_date": trading_date,
        "received_count": len(rows),
        "import": import_result,
    }


async def refresh_intraday_candles(
    symbol: str, *, trading_date: str | None = None
) -> dict[str, Any]:
    if get_settings().fugle_marketdata_api_key:
        return await fetch_fugle_candles(
            symbol, trading_date=trading_date
        )
    return await asyncio.to_thread(
        fetch_yahoo_candles,
        symbol,
        trading_date=trading_date,
    )


async def intraday_candle_query(
    symbol: str,
    *,
    trading_date: str | None,
    timeframe: int,
    refresh: bool,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidIntradayTimeframe(
            f"timeframe must be one of {SUPPORTED_TIMEFRAMES}"
        )
    store = get_intraday_candle_store()
    refresh_result: dict[str, Any] | None = None
    refresh_error: str | None = None
    if refresh:
        try:
            refresh_result = await refresh_intraday_candles(
                symbol, trading_date=trading_date
            )
        except Exception as exc:
            refresh_error = f"{type(exc).__name__}: {exc}"

    canonical = _canonical_symbol(symbol)
    dates = store.available_dates(canonical)
    selected_date = trading_date
    if selected_date is None and dates["dates"]:
        selected_date = str(dates["dates"][0]["trading_date"])
    if selected_date is None:
        selected_date = datetime.now(TAIPEI_TIMEZONE).date().isoformat()
    result = store.reconstruct(
        canonical,
        selected_date,
        timeframe=timeframe,
        as_of=as_of,
    )
    result["available_dates"] = [
        item["trading_date"] for item in dates["dates"]
    ]
    result["refresh"] = {
        "requested": refresh,
        "result": refresh_result,
        "error": refresh_error,
    }
    return result


def intraday_candle_status() -> dict[str, Any]:
    return {
        "schema_version": INTRADAY_CANDLES_SCHEMA_VERSION,
        "storage_schema_version": 25,
        "base_timeframe_minutes": 1,
        "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
        "session_timezone": "Asia/Taipei",
        "session_open": "09:00",
        "session_close": "13:30",
        "regular_session_minutes": REGULAR_SESSION_MINUTES,
        "reconstruction": "persisted revisioned 1m candles",
        "sources": [
            {
                "source_id": FUGLE_SOURCE.source_id,
                "priority": FUGLE_SOURCE.priority,
                "authorized": FUGLE_SOURCE.authorized,
                "configured": bool(
                    get_settings().fugle_marketdata_api_key
                ),
            },
            {
                "source_id": YAHOO_SOURCE.source_id,
                "priority": YAHOO_SOURCE.priority,
                "authorized": YAHOO_SOURCE.authorized,
                "configured": True,
                "research_only": True,
            },
            {
                "source_id": TWSE_QUOTE_SOURCE.source_id,
                "priority": TWSE_QUOTE_SOURCE.priority,
                "authorized": TWSE_QUOTE_SOURCE.authorized,
                "configured": True,
                "sample_only": True,
            },
            {
                "source_id": FUGLE_QUOTE_SOURCE.source_id,
                "priority": FUGLE_QUOTE_SOURCE.priority,
                "authorized": FUGLE_QUOTE_SOURCE.authorized,
                "configured": bool(
                    get_settings().fugle_marketdata_api_key
                ),
                "sample_only": True,
            },
        ],
    }
