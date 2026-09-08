from __future__ import annotations

import calendar
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.storage.sqlite_store import SQLiteStore

from .basic_valuation import fetch_official_daily_valuation
from .data_platform.source_registry import source_endpoint
from .realtime_data import normalize_symbol


VALUATION_PERCENTILE_SCHEMA_VERSION = "stock_ai.valuation_percentiles.v1"
WINDOW_MONTHS = {"1y": 12, "3y": 36, "5y": 60, "10y": 120}
METRICS = {
    "pe": "本益比 PE",
    "pb": "股價淨值比 PB",
    "dividend_yield": "殖利率",
}


class ValuationPercentileError(ValueError):
    pass


def _number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("%", "").strip()
    if text.casefold() in {"", "-", "--", "n/a", "na"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _roc_date(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 7:
        return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])).isoformat()
    if len(digits) == 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    raise ValuationPercentileError(f"unsupported exchange date: {value}")


def _month_targets(latest: date, months: int = 120) -> list[date]:
    targets: list[date] = []
    year, month = latest.year, latest.month
    for offset in range(months - 1, -1, -1):
        index = year * 12 + month - 1 - offset
        target_year, target_month_index = divmod(index, 12)
        target_month = target_month_index + 1
        last_day = calendar.monthrange(target_year, target_month)[1]
        target = date(target_year, target_month, last_day)
        targets.append(min(target, latest) if offset == 0 else target)
    return targets


def _field_index(fields: list[Any], *candidates: str) -> int | None:
    normalized = [str(value).replace(" ", "") for value in fields]
    for candidate in candidates:
        for index, field in enumerate(normalized):
            if candidate in field:
                return index
    return None


def parse_twse_month(
    payload: dict[str, Any],
    *,
    symbol: str,
    source_url: str,
) -> dict[str, Any] | None:
    fields = list(payload.get("fields") or [])
    rows = list(payload.get("data") or [])
    if not rows:
        return None
    date_index = _field_index(fields, "日期")
    pe_index = _field_index(fields, "本益比")
    pb_index = _field_index(fields, "股價淨值比")
    yield_index = _field_index(fields, "殖利率")
    fiscal_index = _field_index(fields, "財報年/季")
    if date_index is None:
        return None
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            parsed.append(
                {
                    "date": _roc_date(row[date_index]),
                    "pe": _number(row[pe_index]) if pe_index is not None else None,
                    "pb": _number(row[pb_index]) if pb_index is not None else None,
                    "dividend_yield": (
                        _number(row[yield_index]) if yield_index is not None else None
                    ),
                    "fiscal_period": (
                        str(row[fiscal_index]) if fiscal_index is not None else None
                    ),
                    "raw": {"fields": fields, "row": row},
                }
            )
        except (IndexError, ValuationPercentileError):
            continue
    if not parsed:
        return None
    item = max(parsed, key=lambda value: value["date"])
    return {
        **item,
        "symbol": normalize_symbol(symbol),
        "source_id": "twse_official_web",
        "source_url": source_url,
    }


def parse_tpex_day(
    payload: dict[str, Any],
    *,
    symbol: str,
    source_url: str,
) -> dict[str, Any] | None:
    tables = payload.get("tables") or []
    if not tables:
        return None
    table = tables[0]
    code = normalize_symbol(symbol).split(".", 1)[0]
    row = next(
        (item for item in table.get("data") or [] if str(item[0]).strip() == code),
        None,
    )
    if not row:
        return None
    return {
        "date": _roc_date(str(table.get("date") or "").replace("/", "")),
        "symbol": normalize_symbol(symbol),
        "pe": _number(row[2]),
        "dividend_yield": _number(row[5]),
        "pb": _number(row[6]),
        "fiscal_period": str(row[7]) if len(row) > 7 else None,
        "source_id": "tpex_official_web",
        "source_url": source_url,
        "raw": {"fields": table.get("fields"), "row": row},
    }


def percentile_rank(values: list[float], current: float) -> float | None:
    finite = [float(value) for value in values]
    if not finite:
        return None
    less = sum(value < current for value in finite)
    equal = sum(value == current for value in finite)
    return round((less + 0.5 * equal) / len(finite) * 100, 2)


def _classification(percentile: float | None) -> str:
    if percentile is None:
        return "unavailable"
    if percentile <= 25:
        return "relative_low"
    if percentile >= 75:
        return "relative_high"
    return "middle_range"


class ValuationHistoryStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        with self.store._connect() as conn:
            conn.execute(
                """
                create table if not exists valuation_history_samples (
                    symbol text not null,
                    valuation_date text not null,
                    source_id text not null,
                    pe real,
                    pb real,
                    dividend_yield real,
                    fiscal_period text,
                    source_url text not null,
                    raw_hash text not null,
                    raw_json text not null,
                    acquired_at text not null,
                    primary key (symbol, valuation_date, source_id)
                )
                """
            )
            conn.commit()

    def import_sample(self, sample: dict[str, Any]) -> None:
        raw_json = json.dumps(
            sample.get("raw") or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.store._connect() as conn:
            conn.execute(
                """
                insert into valuation_history_samples (
                    symbol, valuation_date, source_id, pe, pb, dividend_yield,
                    fiscal_period, source_url, raw_hash, raw_json, acquired_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(symbol, valuation_date, source_id) do update set
                    pe=excluded.pe,
                    pb=excluded.pb,
                    dividend_yield=excluded.dividend_yield,
                    fiscal_period=excluded.fiscal_period,
                    source_url=excluded.source_url,
                    raw_hash=excluded.raw_hash,
                    raw_json=excluded.raw_json,
                    acquired_at=excluded.acquired_at
                """,
                (
                    normalize_symbol(sample["symbol"]),
                    sample["date"],
                    sample["source_id"],
                    sample.get("pe"),
                    sample.get("pb"),
                    sample.get("dividend_yield"),
                    sample.get("fiscal_period"),
                    sample["source_url"],
                    sha256(raw_json.encode()).hexdigest(),
                    raw_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def query(self, symbol: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from valuation_history_samples
                 where symbol=?
                 order by valuation_date
                """,
                (normalize_symbol(symbol),),
            ).fetchall()
        return [
            {
                "symbol": row["symbol"],
                "date": row["valuation_date"],
                "source_id": row["source_id"],
                "pe": row["pe"],
                "pb": row["pb"],
                "dividend_yield": row["dividend_yield"],
                "fiscal_period": row["fiscal_period"],
                "source_url": row["source_url"],
                "raw_hash": row["raw_hash"],
                "acquired_at": row["acquired_at"],
            }
            for row in rows
        ]


def _fetch_twse_month(symbol: str, target: date) -> dict[str, Any] | None:
    code = normalize_symbol(symbol).split(".", 1)[0]
    url = source_endpoint("twse_monthly_valuation_history")
    response = default_external_transport_guard().call_sync(
        f"source:twse_official_web:valuation_history:{code}",
        lambda: httpx.get(
            url,
            params={
                "response": "json",
                "date": target.strftime("%Y%m%d"),
                "stockNo": code,
            },
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "StockAI/0.1"},
        ),
    )
    response.raise_for_status()
    return parse_twse_month(
        response.json(),
        symbol=symbol,
        source_url=str(response.url),
    )


def _fetch_tpex_month(symbol: str, target: date) -> dict[str, Any] | None:
    url = source_endpoint("tpex_monthly_valuation_history")
    for offset in range(10):
        candidate = target - timedelta(days=offset)
        if candidate.month != target.month:
            break
        roc = f"{candidate.year - 1911:03d}/{candidate.month:02d}/{candidate.day:02d}"
        response = default_external_transport_guard().call_sync(
            f"source:tpex_official_web:valuation_history:{normalize_symbol(symbol).split('.', 1)[0]}",
            lambda: httpx.get(
                url,
                params={"l": "zh-tw", "o": "json", "d": roc, "c": ""},
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": "StockAI/0.1"},
            ),
        )
        response.raise_for_status()
        sample = parse_tpex_day(
            response.json(),
            symbol=symbol,
            source_url=str(response.url),
        )
        if sample:
            return sample
    return None


def sync_valuation_history(
    store: SQLiteStore,
    symbol: str,
    *,
    latest_date: str,
    months: int = 120,
    max_workers: int = 6,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    latest = date.fromisoformat(latest_date)
    targets = _month_targets(latest, months=max(1, min(int(months), 120)))
    fetcher = _fetch_tpex_month if normalized.endswith(".TWO") else _fetch_twse_month
    history_store = ValuationHistoryStore(store)
    existing = history_store.query(normalized)
    existing_months = {str(item["date"])[:7] for item in existing}
    pending = [
        target for target in targets
        if target.strftime("%Y-%m") not in existing_months
    ]
    fetched = 0
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8))) as executor:
        futures = {executor.submit(fetcher, normalized, target): target for target in pending}
        for future in as_completed(futures):
            target = futures[future]
            try:
                sample = future.result()
                if sample:
                    history_store.import_sample(sample)
                    fetched += 1
                else:
                    failures.append({"month": target.strftime("%Y-%m"), "error": "no_sample"})
            except Exception as exc:
                failures.append({"month": target.strftime("%Y-%m"), "error": str(exc)})
    available_count = len(history_store.query(normalized))
    return {
        "requested_month_count": len(targets),
        "cached_month_count": len(existing_months),
        "fetched_month_count": fetched,
        "stored_month_count": available_count,
        "failed_months": sorted(failures, key=lambda item: item["month"]),
        "sampling": "last_available_exchange_observation_per_calendar_month",
    }


def build_valuation_percentiles(
    *,
    symbol: str,
    samples: list[dict[str, Any]],
    sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda item: item["date"])
    latest = ordered[-1] if ordered else None
    metric_items: list[dict[str, Any]] = []
    for metric, label in METRICS.items():
        current = _number((latest or {}).get(metric))
        windows: list[dict[str, Any]] = []
        for window, months in WINDOW_MONTHS.items():
            subset = ordered[-months:]
            values = [
                float(item[metric])
                for item in subset
                if item.get(metric) is not None
            ]
            rank = percentile_rank(values, current) if current is not None else None
            windows.append(
                {
                    "window": window,
                    "requested_months": months,
                    "sample_count": len(values),
                    "start_date": subset[0]["date"] if subset else None,
                    "end_date": subset[-1]["date"] if subset else None,
                    "percentile": rank,
                    "classification": _classification(rank),
                    "minimum": min(values) if values else None,
                    "maximum": max(values) if values else None,
                    "status": (
                        "complete"
                        if rank is not None and len(subset) >= months
                        else "partial"
                        if rank is not None
                        else "unavailable"
                    ),
                }
            )
        metric_items.append(
            {
                "code": metric,
                "label": label,
                "current_value": current,
                "unit": "percent" if metric == "dividend_yield" else "times",
                "windows": windows,
            }
        )
    all_windows = [
        window for metric in metric_items for window in metric["windows"]
    ]
    return {
        "schema_version": VALUATION_PERCENTILE_SCHEMA_VERSION,
        "symbol": normalize_symbol(symbol),
        "latest_date": latest.get("date") if latest else None,
        "status": (
            "complete"
            if all(window["status"] == "complete" for window in all_windows)
            else "partial"
            if ordered
            else "insufficient_history"
        ),
        "sampling": "last_available_exchange_observation_per_calendar_month",
        "percentile_method": "midrank_empirical_percentile",
        "metrics": metric_items,
        "coverage": {
            "sample_count": len(ordered),
            "first_date": ordered[0]["date"] if ordered else None,
            "last_date": ordered[-1]["date"] if ordered else None,
            "source_ids": sorted({item["source_id"] for item in ordered}),
            "raw_hashes_preserved": bool(ordered)
            and all(bool(item.get("raw_hash")) for item in ordered),
        },
        "sync": sync,
        "truthfulness": {
            "monthly_sampling_disclosed": True,
            "missing_values_excluded_not_zero": True,
            "supported_metrics": list(METRICS),
            "unsupported_historical_metrics": ["ps", "ev_to_ebitda", "fcf_yield"],
            "unsupported_reason": (
                "Official exchange history provides PE, PB and dividend yield. "
                "PS, EV/EBITDA and FCF Yield require a separate historical PIT "
                "statement reconstruction and are not fabricated."
            ),
        },
    }


def query_valuation_percentiles(
    store: SQLiteStore,
    symbol: str,
    *,
    refresh: bool = False,
    months: int = 120,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValuationPercentileError("symbol is required")
    history_store = ValuationHistoryStore(store)
    sync = None
    if refresh:
        official = fetch_official_daily_valuation(normalized)
        sync = sync_valuation_history(
            store,
            normalized,
            latest_date=str(official["date"]),
            months=months,
        )
    return build_valuation_percentiles(
        symbol=normalized,
        samples=history_store.query(normalized),
        sync=sync,
    )
