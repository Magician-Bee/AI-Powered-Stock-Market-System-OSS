from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from threading import RLock
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen, Request
import json
import re

from open_stock_ai.agent_runtime import default_external_transport_guard

from .data_platform.identity import entity_id_for_symbol
from .data_platform.source_registry import get_source_registry, source_endpoint
from .models import Entity, PricePoint

TWSE_COMPANIES = source_endpoint("twse_companies")
TWSE_ALL_QUOTES = source_endpoint("twse_quotes")
TWSE_STOCK_DAY = source_endpoint("twse_stock_day")
TPEX_COMPANIES = source_endpoint("tpex_companies")
TPEX_QUOTES = source_endpoint("tpex_quotes")
TPEX_TRADING_STOCK = source_endpoint("tpex_trading_stock")
TWSE_ETFS = source_endpoint("twse_etfs")
TWSE_WARRANTS = source_endpoint("twse_warrants")
TWSE_INDICES = source_endpoint("twse_indices")
TWSE_DELISTED = source_endpoint("twse_delisted")
TPEX_EMERGING_COMPANIES = source_endpoint("tpex_emerging_companies")
TPEX_EMERGING_QUOTES = source_endpoint("tpex_emerging_quotes")
TPEX_WARRANTS = source_endpoint("tpex_warrants")
TPEX_DELISTED = source_endpoint("tpex_delisted")

TPEX_INDEX_SERIES: tuple[tuple[str, str, str], ...] = (
    ("TPEX", "櫃買指數", "/tpex_index"),
    ("TPEX50", "富櫃50指數", "/tpex50_index"),
    ("TPEX-TRADING", "上櫃日成交量值指數", "/tpex_daily_trading_index"),
    ("TPCGI-RETURN", "上櫃公司治理指數", "/tpcgi_reward_index"),
    ("TPEX-RETURN", "櫃買報酬指數", "/tpex_reward_index"),
    ("TPCI-RETURN", "櫃買薪酬指數", "/tpci_reward_index"),
    ("TPEX-EMP88", "櫃買勞工就業88指數", "/tpex_emp88_reward_index"),
    ("TPHD", "櫃買高殖利率指數", "/tphd_index"),
)

NUMERIC = re.compile(r"^\d{4,6}[A-Z]?$", re.I)

_CACHE_LOCK = RLock()
_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
_CACHE_LOADED_AT: dict[str, str] = {}


def _cached(key: tuple[Any, ...], ttl_seconds: int, loader, *, cache_empty: bool = True):
    """Small TTL cache that never turns a transient empty history into a permanent result."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    value = loader()
    if cache_empty or value:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic() + ttl_seconds, value)
            _CACHE_LOADED_AT[str(key[0])] = datetime.now().astimezone().isoformat(timespec="seconds")
    return value


def clear_official_caches() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_LOADED_AT.clear()


def official_cache_status() -> dict[str, Any]:
    with _CACHE_LOCK:
        return {"loaded_at": dict(_CACHE_LOADED_AT), "entry_count": len(_CACHE)}


def _get_json(url: str, timeout: int = 20) -> Any:
    dataset = get_source_registry().identify_dataset_url(url)
    strategy = dataset.failure_strategy if dataset else None
    attempts = strategy.max_attempts if strategy else 3
    effective_timeout = strategy.timeout_seconds if strategy else timeout
    backoff = strategy.backoff_seconds if strategy else [0.15, 0.3]
    scope = (
        f"source:{dataset.source_id}:{dataset.dataset_id}"
        if dataset
        else f"source:unregistered:taiwan_official:{url}"
    )

    def load_with_retries() -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"})
            try:
                with urlopen(req, timeout=effective_timeout) as r:
                    return json.loads(r.read().decode("utf-8-sig"))
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0
                    if delay:
                        time.sleep(delay)
        assert last_error is not None
        raise last_error

    return default_external_transport_guard().call_sync(scope, load_with_retries)


def roc_to_iso(raw: str) -> str:
    s = str(raw).strip().replace("/", "")
    if len(s) == 7 and s.isdigit():
        y = int(s[:3]) + 1911
        return f"{y:04d}-{int(s[3:5]):02d}-{int(s[5:7]):02d}"
    if len(s) == 8 and s.isdigit():
        return f"{int(s[:4]):04d}-{int(s[4:6]):02d}-{int(s[6:8]):02d}"
    return str(raw)


def _float(raw: Any) -> float:
    s = str(raw).strip().replace(",", "").replace("+", "")
    if s.lower() in {"", "--", "-", "none", "null", "nan", "n/a"}:
        return 0.0
    return float(s)


def _optional_float(raw: Any) -> float | None:
    s = str(raw).strip().replace(",", "").replace("+", "")
    if s.lower() in {"", "--", "-", "none", "null", "nan", "n/a"}:
        return None
    return float(s)


def _int(raw: Any) -> int:
    s = str(raw).strip().replace(",", "")
    if s.lower() in {"", "--", "-", "none", "null", "nan", "n/a"}:
        return 0
    return int(float(s))


def is_taiwan_code(raw: str) -> bool:
    s = normalize_taiwan_code(raw)
    return bool(NUMERIC.match(s))


def normalize_taiwan_code(raw: str) -> str:
    s = raw.strip().upper().replace(" ", "")
    if s.endswith(".TW") or s.endswith(".TWO"):
        s = s.split(".", 1)[0]
    return s


def twse_companies() -> list[dict[str, Any]]:
    return _cached(("twse_companies",), 3600, lambda: _get_json(TWSE_COMPANIES), cache_empty=False)


def tpex_companies() -> list[dict[str, Any]]:
    return _cached(("tpex_companies",), 3600, lambda: _get_json(TPEX_COMPANIES), cache_empty=False)


def twse_quotes() -> list[dict[str, Any]]:
    return _cached(("twse_quotes",), 300, lambda: _get_json(TWSE_ALL_QUOTES), cache_empty=False)


def tpex_quotes() -> list[dict[str, Any]]:
    return _cached(("tpex_quotes",), 300, lambda: _get_json(TPEX_QUOTES), cache_empty=False)


def twse_etfs() -> list[dict[str, Any]]:
    return _cached(("twse_etfs",), 21600, lambda: _get_json(TWSE_ETFS), cache_empty=False)


def twse_warrants() -> list[dict[str, Any]]:
    return _cached(("twse_warrants",), 21600, lambda: _get_json(TWSE_WARRANTS), cache_empty=False)


def twse_indices() -> list[dict[str, Any]]:
    return _cached(("twse_indices",), 300, lambda: _get_json(TWSE_INDICES), cache_empty=False)


def twse_delisted() -> list[dict[str, Any]]:
    return _cached(("twse_delisted",), 21600, lambda: _get_json(TWSE_DELISTED), cache_empty=False)


def tpex_emerging_companies() -> list[dict[str, Any]]:
    return _cached(
        ("tpex_emerging_companies",),
        21600,
        lambda: _get_json(TPEX_EMERGING_COMPANIES),
        cache_empty=False,
    )


def tpex_emerging_quotes() -> list[dict[str, Any]]:
    return _cached(
        ("tpex_emerging_quotes",),
        300,
        lambda: _get_json(TPEX_EMERGING_QUOTES),
        cache_empty=False,
    )


def tpex_warrants() -> list[dict[str, Any]]:
    return _cached(("tpex_warrants",), 21600, lambda: _get_json(TPEX_WARRANTS), cache_empty=False)


def tpex_indices() -> list[dict[str, Any]]:
    """Return an official index-series master with the latest row as source evidence."""

    def load() -> list[dict[str, Any]]:
        def fetch(definition: tuple[str, str, str]) -> dict[str, Any]:
            code, name, endpoint = definition
            rows = _get_json(source_endpoint("tpex_index", path=endpoint))
            latest = rows[0] if isinstance(rows, list) and rows else {}
            return {
                "IndexCode": code,
                "IndexName": name,
                "Endpoint": endpoint,
                "LatestObservation": latest,
            }

        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(fetch, TPEX_INDEX_SERIES))

    return _cached(("tpex_indices",), 21600, load, cache_empty=False)


def tpex_delisted(*, start_year: int = 1994, end_year: int | None = None) -> list[dict[str, Any]]:
    """Read all available TPEx delisting years from its official website data API."""

    final_year = end_year or datetime.now().year

    def load() -> list[dict[str, Any]]:
        def fetch(year: int) -> list[dict[str, Any]]:
            payload = _get_json(f"{TPEX_DELISTED}?{urlencode({'date': year})}")
            if not isinstance(payload, dict) or str(payload.get("stat", "")).casefold() != "ok":
                return []
            tables = payload.get("tables") or []
            data = tables[0].get("data") if tables and isinstance(tables[0], dict) else []
            output: list[dict[str, Any]] = []
            for row in data or []:
                if not isinstance(row, list) or len(row) < 4:
                    continue
                output.append(
                    {
                        "Code": str(row[0]).strip(),
                        "Company": str(row[1]).strip(),
                        "DelistingDate": str(row[2]).strip(),
                        "Reason": str(row[3]).strip(),
                        "SourceYear": year,
                    }
                )
            return output

        output: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch, year) for year in range(start_year, final_year + 1)]
            for future in as_completed(futures):
                output.extend(future.result())
        return sorted(
            output,
            key=lambda row: (str(row.get("DelistingDate") or ""), str(row.get("Code") or "")),
        )

    return _cached(
        ("tpex_delisted", start_year, final_year),
        21600,
        load,
        cache_empty=False,
    )


def _twse_entity_from_quote(row: dict[str, Any]) -> Entity:
    code = str(row.get("Code", "")).strip()
    name = str(row.get("Name", code)).strip()
    is_etf = code.startswith("00")
    symbol = f"{code}.TW"
    return Entity(
        entity_id=entity_id_for_symbol(
            symbol,
            market="taiwan",
            exchange="TWSE",
            source_code=code,
        ),
        symbol=symbol,
        name=name,
        entity_type="etf" if is_etf else "stock",
        market="taiwan",
        exchange="TWSE",
        currency="TWD",
        sector="ETF" if is_etf else None,
        industry=None,
        is_active=True,
    )


def _tpex_entity_from_quote(row: dict[str, Any]) -> Entity:
    code = str(row.get("SecuritiesCompanyCode", "")).strip()
    name = str(row.get("CompanyName", code)).strip()
    is_etf = code.startswith("00")
    symbol = f"{code}.TWO"
    return Entity(
        entity_id=entity_id_for_symbol(
            symbol,
            market="taiwan",
            exchange="TPEx",
            source_code=code,
        ),
        symbol=symbol,
        name=name,
        entity_type="etf" if is_etf else "stock",
        market="taiwan",
        exchange="TPEx",
        currency="TWD",
        sector="ETF" if is_etf else None,
        industry=None,
        is_active=True,
    )


def search_taiwan_official(q: str, limit: int = 30) -> list[Entity]:
    query = q.strip().lower()
    entities: list[Entity] = []
    seen: set[str] = set()

    for row in twse_quotes():
        code = str(row.get("Code", "")).strip()
        name = str(row.get("Name", "")).strip()
        if not query or query in code.lower() or query in name.lower():
            ent = _twse_entity_from_quote(row)
            entities.append(ent); seen.add(ent.symbol)
            if len(entities) >= limit:
                return entities

    for row in tpex_quotes():
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        name = str(row.get("CompanyName", "")).strip()
        if not query or query in code.lower() or query in name.lower():
            ent = _tpex_entity_from_quote(row)
            if ent.symbol not in seen:
                entities.append(ent); seen.add(ent.symbol)
            if len(entities) >= limit:
                return entities

    # Company directories can find full legal names not present in quote short names.
    for row in twse_companies():
        code = str(row.get("公司代號", "")).strip()
        name = str(row.get("公司簡稱") or row.get("公司名稱") or "").strip()
        legal = str(row.get("公司名稱", "")).strip()
        if query and (query in code.lower() or query in name.lower() or query in legal.lower()):
            symbol = f"{code}.TW"
            ent = Entity(
                entity_id=entity_id_for_symbol(
                    symbol,
                    market="taiwan",
                    exchange="TWSE",
                    source_code=code,
                ),
                symbol=symbol,
                name=name or legal or code,
                entity_type="stock",
                market="taiwan",
                exchange="TWSE",
                currency="TWD",
            )
            if ent.symbol not in seen:
                entities.append(ent); seen.add(ent.symbol)
            if len(entities) >= limit:
                return entities

    for row in tpex_companies():
        code = str(row.get("SecuritiesCompanyCode", "")).strip()
        name = str(row.get("CompanyAbbreviation") or row.get("CompanyName") or "").strip()
        legal = str(row.get("CompanyName", "")).strip()
        if query and (query in code.lower() or query in name.lower() or query in legal.lower()):
            symbol = f"{code}.TWO"
            ent = Entity(
                entity_id=entity_id_for_symbol(
                    symbol,
                    market="taiwan",
                    exchange="TPEx",
                    source_code=code,
                ),
                symbol=symbol,
                name=name or legal or code,
                entity_type="stock",
                market="taiwan",
                exchange="TPEx",
                currency="TWD",
            )
            if ent.symbol not in seen:
                entities.append(ent); seen.add(ent.symbol)
            if len(entities) >= limit:
                return entities

    return entities


def _twse_quote_by_code(code: str) -> dict[str, Any] | None:
    for row in twse_quotes():
        if str(row.get("Code", "")).strip().upper() == code.upper():
            return row
    return None


def _tpex_quote_by_code(code: str) -> dict[str, Any] | None:
    for row in tpex_quotes():
        if str(row.get("SecuritiesCompanyCode", "")).strip().upper() == code.upper():
            return row
    return None


def latest_official_quote(symbol: str) -> tuple[Entity, PricePoint, float, str, str]:
    code = normalize_taiwan_code(symbol)
    row = _twse_quote_by_code(code)
    if row:
        ent = _twse_entity_from_quote(row)
        point = PricePoint(
            date=roc_to_iso(row.get("Date", "")),
            open=_float(row.get("OpeningPrice")),
            high=_float(row.get("HighestPrice")),
            low=_float(row.get("LowestPrice")),
            close=_float(row.get("ClosingPrice")),
            volume=_int(row.get("TradeVolume")),
        )
        change = _float(row.get("Change"))
        prev = point.close - change
        pct = round(change / prev * 100, 2) if prev else 0.0
        return ent, point, pct, "TWSE official openapi STOCK_DAY_ALL", "TWSE"

    row = _tpex_quote_by_code(code)
    if row:
        ent = _tpex_entity_from_quote(row)
        point = PricePoint(
            date=roc_to_iso(row.get("Date", "")),
            open=_float(row.get("Open")),
            high=_float(row.get("High")),
            low=_float(row.get("Low")),
            close=_float(row.get("Close")),
            volume=_int(row.get("TradingShares")),
        )
        change = _float(row.get("Change"))
        prev = point.close - change
        pct = round(change / prev * 100, 2) if prev else 0.0
        return ent, point, pct, "TPEx official openapi tpex_mainboard_quotes", "TPEx"

    raise ValueError(f"Taiwan official quote not found for {symbol}")


def _ym_iter_from_latest(latest_iso: str, months: int = 8) -> list[str]:
    dt = datetime.fromisoformat(latest_iso)
    out = []
    y, m = dt.year, dt.month
    for _ in range(months):
        out.append(f"{y:04d}{m:02d}01")
        m -= 1
        if m == 0:
            y -= 1; m = 12
    return list(reversed(out))


def _ym_iter_between(start_iso: str, end_iso: str) -> list[str]:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    if start > end:
        raise ValueError("history start date must not be after end date")
    out: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}{month:02d}01")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return out


def twse_history_range(
    code: str,
    start_iso: str,
    end_iso: str,
) -> tuple[list[PricePoint], list[dict[str, Any]]]:
    points: list[PricePoint] = []
    coverage: list[dict[str, Any]] = []
    for ym in _ym_iter_between(start_iso, end_iso):
        month = ym[:6]
        url = TWSE_STOCK_DAY + "?" + urlencode(
            {"date": ym, "stockNo": code, "response": "json"}
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            coverage.append(
                {
                    "month": month,
                    "status": "failed",
                    "row_count": 0,
                    "error": type(exc).__name__,
                }
            )
            continue
        month_points: list[PricePoint] = []
        if data.get("stat") == "OK":
            for row in data.get("data", []):
                if len(row) < 7:
                    continue
                point = PricePoint(
                    date=roc_to_iso(row[0]),
                    volume=_int(row[1]),
                    turnover=_optional_float(row[2]) if len(row) > 2 else None,
                    open=_float(row[3]),
                    high=_float(row[4]),
                    low=_float(row[5]),
                    close=_float(row[6]),
                )
                if start_iso <= point.date <= end_iso and point.close > 0:
                    month_points.append(point)
        points.extend(month_points)
        coverage.append(
            {
                "month": month,
                "status": "succeeded",
                "row_count": len(month_points),
                "error": None,
            }
        )
    dedup = {point.date: point for point in points}
    return [dedup[key] for key in sorted(dedup)], coverage


def twse_history(
    code: str,
    latest_iso: str,
    months: int = 8,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[PricePoint]:
    def load() -> list[PricePoint]:
        requested_end = end or latest_iso
        requested_start = start
        if requested_start is None:
            first_month = _ym_iter_from_latest(requested_end, months)[0]
            requested_start = f"{first_month[:4]}-{first_month[4:6]}-01"
        points, _coverage = twse_history_range(code, requested_start, requested_end)
        return points if start is not None or end is not None else points[-160:]

    return _cached(
        ("twse_history", code, latest_iso, months, start, end),
        900,
        load,
        cache_empty=False,
    )


def tpex_history_range(
    code: str,
    start_iso: str,
    end_iso: str,
) -> tuple[list[PricePoint], list[dict[str, Any]]]:
    points: list[PricePoint] = []
    coverage: list[dict[str, Any]] = []
    for ym in _ym_iter_between(start_iso, end_iso):
        month = ym[:6]
        month_date = f"{ym[:4]}/{ym[4:6]}/01"
        url = TPEX_TRADING_STOCK + "?" + urlencode(
            {"code": code, "date": month_date, "id": "", "response": "json"}
        )
        try:
            data = _get_json(url)
        except Exception as exc:
            coverage.append(
                {
                    "month": month,
                    "status": "failed",
                    "row_count": 0,
                    "error": type(exc).__name__,
                }
            )
            continue
        month_points: list[PricePoint] = []
        if str(data.get("stat", "")).upper() == "OK":
            for table in data.get("tables", []):
                for row in table.get("data", []):
                    if len(row) < 7:
                        continue
                    turnover_thousands = _optional_float(row[2])
                    point = PricePoint(
                        date=roc_to_iso(row[0]),
                        # TPEx publishes 成交張數 and 成交仟元. Normalize both
                        # to the canonical shares / TWD contract.
                        volume=_int(row[1]) * 1000,
                        turnover=(
                            turnover_thousands * 1000
                            if turnover_thousands is not None
                            else None
                        ),
                        open=_float(row[3]),
                        high=_float(row[4]),
                        low=_float(row[5]),
                        close=_float(row[6]),
                    )
                    if start_iso <= point.date <= end_iso and point.close > 0:
                        month_points.append(point)
        points.extend(month_points)
        coverage.append(
            {
                "month": month,
                "status": "succeeded",
                "row_count": len(month_points),
                "error": None,
            }
        )
    dedup = {point.date: point for point in points}
    return [dedup[key] for key in sorted(dedup)], coverage


def tpex_history(
    code: str,
    latest_iso: str,
    months: int = 8,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[PricePoint]:
    def load() -> list[PricePoint]:
        requested_end = end or latest_iso
        requested_start = start
        if requested_start is None:
            first_month = _ym_iter_from_latest(requested_end, months)[0]
            requested_start = f"{first_month[:4]}-{first_month[4:6]}-01"
        points, _coverage = tpex_history_range(code, requested_start, requested_end)
        return points if start is not None or end is not None else points[-160:]

    return _cached(
        ("tpex_history", code, latest_iso, months, start, end),
        900,
        load,
        cache_empty=False,
    )


def official_history(symbol: str) -> list[PricePoint]:
    ent, latest, _pct, _src, exchange = latest_official_quote(symbol)
    code = normalize_taiwan_code(ent.symbol)
    if exchange == "TWSE":
        hist = twse_history(code, latest.date)
        if hist:
            return hist
    if exchange == "TPEx":
        hist = tpex_history(code, latest.date)
        if hist:
            return hist
    return [latest]


def official_summary_payload(symbol: str) -> dict[str, Any]:
    ent, latest, pct, source, exchange = latest_official_quote(symbol)
    hist = official_history(ent.symbol)
    if hist and hist[-1].date != latest.date:
        hist.append(latest)
    return {
        "entity": ent,
        "latest": latest,
        "history": hist,
        "change_percent": pct,
        "source": source,
        "data_timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "freshness_note": f"{exchange} 官方公開資料最新可得行情；若需要盤中逐筆/即時低延遲，仍需交易所授權即時資料。",
        "reliability_note": "價格、成交量與K線來自官方公開資料；未接入欄位會明確標示未提供，不會用示範資料補值。",
    }
