from __future__ import annotations

"""Identity-bound official daily candles, independent of the legacy warehouse.

No synthetic or vendor fallback is used. Each month retains the official raw
response, request identity and hash. This loader intentionally does not assert
corporate-action, historical-vintage, cost, or full-universe certification.
"""

import asyncio
from datetime import date, datetime, timezone
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import tempfile
import time as clock
from threading import Event, Lock
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_stock_ai.strategy.provenance import content_hash


_ENDPOINTS = {
    "TW": ("twse_official_web", "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"),
    "TWO": ("tpex_official_web", "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"),
}

# Space real exchange requests across concurrent research jobs. The TWSE CDN
# can answer a burst with a same-URL 307/308 loop even though the month exists.
_NETWORK_REQUEST_INTERVAL_SECONDS = 0.25
_NETWORK_REQUEST_LOCK = Lock()
_NEXT_NETWORK_REQUEST_AT = 0.0


def _months(start: date, end: date) -> list[str]:
    year, month = start.year, start.month
    months: list[str] = []
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def _check_cancelled(stop_event: Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        # Cancellation is control flow, never a source-verification failure or
        # a successful partial range. It bypasses the monthly Exception guard.
        raise asyncio.CancelledError("official_history_cancelled")


def _fetch_once(url: str, timeout: float, *, stop_event: Event | None = None) -> bytes:
    global _NEXT_NETWORK_REQUEST_AT
    _check_cancelled(stop_event)
    with _NETWORK_REQUEST_LOCK:
        _check_cancelled(stop_event)
        wait = _NEXT_NETWORK_REQUEST_AT - clock.monotonic()
        if wait > 0:
            if stop_event is None:
                clock.sleep(wait)
            else:
                stop_event.wait(wait)
                _check_cancelled(stop_event)
        _NEXT_NETWORK_REQUEST_AT = clock.monotonic() + _NETWORK_REQUEST_INTERVAL_SECONDS
    _check_cancelled(stop_event)
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 StockAI internal research"}), timeout=timeout) as response:
        if urlparse(response.geturl()).hostname != urlparse(url).hostname:
            raise ValueError("official_response_redirected_to_other_host")
        return response.read(4_000_000)


def _fetch(url: str, timeout: float, *, stop_event: Event | None = None) -> tuple[bytes, str]:
    kwargs = {"stop_event": stop_event} if stop_event is not None else {}
    try:
        return _fetch_once(url, timeout, **kwargs), url
    except HTTPError as exc:
        parsed = urlparse(url)
        if exc.code not in {307, 308} or parsed.hostname != "www.twse.com.tw" or parsed.path != "/rwd/zh/afterTrading/STOCK_DAY":
            raise
        # The official rwd endpoint can redirect to its own URL forever for
        # an otherwise available month. The same exchange's legacy endpoint
        # was independently verified; retain its actual request URL as proof.
        query = parse_qs(parsed.query)
        legacy = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?" + urlencode(
            {"response": "json", "date": query["date"][0], "stockNo": query["stockNo"][0]})
        _check_cancelled(stop_event)
        return _fetch_once(legacy, timeout, **kwargs), legacy


def parse_official_month(
    payload: dict[str, Any], *, symbol: str, month: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check response identity/date and normalize TWSE shares or TPEx lots."""
    match = re.fullmatch(r"(\d{4,6})\.(TW|TWO)", symbol)
    if not match:
        raise ValueError("canonical_taiwan_instrument_identity_required")
    code, venue = match.groups()
    if str(payload.get("stat") or "").upper() != "OK":
        raise ValueError("official_status_not_ok")
    response_date = str(payload.get("date") or "")
    if not response_date.startswith(month):
        raise ValueError("official_response_month_mismatch")
    volume_multiplier = turnover_multiplier = 1
    if venue == "TW":
        title = str(payload.get("title") or "")
        if not re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", title):
            raise ValueError("official_response_symbol_mismatch")
        values = payload.get("data") or []
        identity = {"response_title": title, "response_date": response_date}
    else:
        if str(payload.get("code") or "") != code:
            raise ValueError("official_response_symbol_mismatch")
        tables = payload.get("tables") or []
        if len(tables) != 1 or not isinstance(tables[0], dict):
            raise ValueError("official_tpex_table_contract_changed")
        fields = tables[0].get("fields") or []
        if len(fields) < 7:
            raise ValueError("official_tpex_volume_units_unverified")
        volume_field = re.sub(r"\s+", "", str(fields[1]))
        turnover_field = re.sub(r"\s+", "", str(fields[2]))
        if "張數" in volume_field or "仟股" in volume_field or "千股" in volume_field:
            volume_multiplier = 1000
        elif "股數" in volume_field:
            volume_multiplier = 1
        else:
            raise ValueError("official_tpex_volume_units_unverified")
        if "仟元" in turnover_field or "千元" in turnover_field:
            turnover_multiplier = 1000
        elif "元" in turnover_field:
            turnover_multiplier = 1
        else:
            raise ValueError("official_tpex_turnover_units_unverified")
        values = tables[0].get("data") or []
        identity = {"response_code": code, "response_name": payload.get("name"),
                    "response_date": response_date}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in values:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("official_ohlcv_field_contract_changed")
        year, mm, dd = map(int, str(row[0]).split("/"))
        observed = date(year + 1911 if year < 1911 else year, mm, dd)
        if observed.strftime("%Y%m") != month or observed.isoformat() in seen:
            raise ValueError("official_duplicate_or_wrong_month_bar")
        seen.add(observed.isoformat())
        def number(value: Any) -> float:
            result = float(str(value).replace(",", ""))
            if not isfinite(result):
                raise ValueError("official_nonfinite_measurement")
            return result
        item = {"date": observed.isoformat(), "volume": number(row[1]) * (1 if venue == "TW" else volume_multiplier),
                "turnover": number(row[2]) * (1 if venue == "TW" else turnover_multiplier),
                "open": number(row[3]), "high": number(row[4]),
                "low": number(row[5]), "close": number(row[6])}
        if item["volume"] <= 0 or not 0 < item["low"] <= min(item["open"],item["close"]) <= max(item["open"],item["close"]) <= item["high"]:
            raise ValueError("official_invalid_ohlcv_bar")
        rows.append(item)
    if not rows:
        raise ValueError("official_month_has_no_verified_candles")
    return sorted(rows, key=lambda row: row["date"]), identity


def _retain_rejected_raw(raw: bytes, *, cache: Path | None) -> dict[str, Any]:
    """Keep rejected bytes outside the accepted-month cache, once per hash."""
    if cache is None:
        return {"raw_retention_status": "not_configured"}
    digest = hashlib.sha256(raw).hexdigest()
    path = cache / "rejected-responses" / (digest + ".bin")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + digest + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            try:
                # Publish complete bytes without replacing another writer's
                # evidence. Repeated identical rejections reuse one object.
                os.link(temporary, path)
            except FileExistsError:
                pass
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("rejected_raw_content_hash_mismatch")
        return {"raw_retention_status": "retained", "rejected_raw_path": str(path.resolve())}
    except (OSError, ValueError) as exc:
        return {"raw_retention_status": "failed", "raw_retention_error": f"{type(exc).__name__}:{exc}"}
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # The retained blob or explicit failure remains authoritative.


def _write_verified_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_official_candles(
    symbol: str, *, start: str, end: str, cache_dir: str | Path | None = None,
    max_months: int = 12, timeout_seconds: float = 10.0,
    fetch_bytes: Callable[[str, float], bytes | tuple[bytes, str]] | None = None,
    stop_event: Event | None = None,
) -> dict[str, Any]:
    """Load a bounded range; callers cache/rotate a small number per cycle.

    Closed-month cache entries are reused only after request/hash/identity
    verification. The current month is refreshed. Failures remain explicit;
    coverage gaps never turn into invented bars or an alternate-source series.
    The Host may cancel further work with ``stop_event``. An in-flight request
    retains its original timeout; received bytes finish their usual local
    verification/retention before cancellation propagates to the caller.
    """
    if stop_event is not None and not isinstance(stop_event, Event):
        raise TypeError("official_history_stop_event_must_be_threading_event")
    _check_cancelled(stop_event)
    match = re.fullmatch(r"(\d{4,6})\.(TW|TWO)", symbol)
    if not match:
        raise ValueError("canonical_taiwan_instrument_identity_required")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid_history_range")
    months = _months(first, last)
    if not 1 <= max_months <= 120 or len(months) > max_months:
        raise ValueError("bounded_official_month_budget_exceeded")
    code, venue = match.groups()
    source_id, endpoint = _ENDPOINTS[venue]
    cache = Path(cache_dir) if cache_dir is not None else None
    fetcher = fetch_bytes or _fetch
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    current_month = datetime.now(timezone.utc).strftime("%Y%m")
    consecutive_network_failures = 0
    network_stopped = False
    # Current operational evidence remains available even if old archived
    # endpoints fail. Stop after three failures without discarding newer rows.
    for month in reversed(months):
        _check_cancelled(stop_event)
        params = ({"date": month + "01", "stockNo": code, "response": "json"} if venue == "TW"
                  else {"code": code, "date": f"{month[:4]}/{month[4:]}/01", "id": "", "response": "json"})
        url = endpoint + "?" + urlencode(params)
        receipt: dict[str, Any] = {"symbol": symbol, "month": month, "source_id": source_id, "url": url}
        path = cache / f"{symbol}-{month}.json" if cache else None
        raw = None
        network_attempted = False
        try:
            cached = json.loads(path.read_text()) if path and path.is_file() and month < current_month else None
            if cached is not None:
                receipt["cache_hit"] = True
                raw = cached["raw_json"].encode("utf-8")
                receipt.update(raw_sha256=hashlib.sha256(raw).hexdigest(), acquired_at=cached.get("acquired_at"),
                               effective_url=cached.get("effective_url", url))
                if cached.get("url") != url or hashlib.sha256(raw).hexdigest() != cached.get("raw_sha256"):
                    raise ValueError("official_cache_request_or_hash_mismatch")
                acquired_at = cached["acquired_at"]
                effective_url = cached.get("effective_url", url)
            else:
                receipt["cache_hit"] = False
                if network_stopped:
                    raise ValueError("monthly_requests_stopped_after_three_source_failures")
                _check_cancelled(stop_event)
                network_attempted = True
                response = (_fetch(url, timeout_seconds, stop_event=stop_event)
                            if fetch_bytes is None and stop_event is not None else fetcher(url, timeout_seconds))
                if isinstance(response, tuple):
                    response, effective_url = response
                else:
                    effective_url = url
                raw = response
                acquired_at = datetime.now(timezone.utc).isoformat()
                receipt.update(raw_sha256=hashlib.sha256(raw).hexdigest(), acquired_at=acquired_at,
                               effective_url=effective_url)
                if urlparse(effective_url).hostname != urlparse(endpoint).hostname:
                    raise ValueError("official_response_redirected_to_other_host")
            if urlparse(effective_url).hostname != urlparse(endpoint).hostname:
                raise ValueError("official_cache_or_response_host_mismatch")
            points, identity = parse_official_month(json.loads(raw.decode("utf-8-sig")), symbol=symbol, month=month)
            digest = hashlib.sha256(raw).hexdigest()
            selected = [r for r in points if start <= r["date"] <= end]
            rows.extend(selected)
            receipt.update(status="verified", row_count=len(selected), raw_sha256=digest,
                           acquired_at=acquired_at, identity=identity, effective_url=effective_url)
            if path and cached is None:
                cached_payload = {"url": url, "raw_json": raw.decode("utf-8"),
                                  "raw_sha256": digest, "acquired_at": acquired_at,
                                  "effective_url": effective_url}
                try:
                    _write_verified_cache(path, cached_payload)
                    receipt.update(cache_write_status="retained", raw_cache_path=str(path.resolve()))
                except OSError as exc:
                    # Storage availability is not evidence against already
                    # verified received bars; keep the I/O gap explicit.
                    receipt.update(cache_write_status="failed", cache_write_error=f"{type(exc).__name__}:{exc}")
            elif path:
                receipt["raw_cache_path"] = str(path.resolve())
            if network_attempted:
                consecutive_network_failures = 0
        except Exception as exc:
            unavailable = isinstance(exc, OSError) or str(exc) in {
                "official_status_not_ok", "official_month_has_no_verified_candles",
                "monthly_requests_stopped_after_three_source_failures",
            }
            receipt.update(status="rejected", row_count=0, reason=f"{type(exc).__name__}:{exc}",
                           failure_kind="coverage_gap" if unavailable else "verification_failure")
            if isinstance(raw, bytes):
                receipt.update(_retain_rejected_raw(raw, cache=cache))
            if network_attempted:
                consecutive_network_failures += 1
                network_stopped = consecutive_network_failures >= 3
        receipts.append(receipt)
        _check_cancelled(stop_event)
    ordered = sorted(rows, key=lambda row: row["date"])
    receipts.sort(key=lambda receipt: receipt["month"])
    coverage_complete = bool(ordered) and all(r["status"] == "verified" for r in receipts)
    rejected_months = [r["month"] for r in receipts if r["status"] != "verified"]
    verification_failures = [r["month"] for r in receipts if r.get("failure_kind") == "verification_failure"]
    # Verified returned bars remain usable for bounded research through an
    # unavailable month. A contradictory identity, damaged cache or invalid
    # response is not reclassified as a harmless coverage gap.
    returned_source_verified = bool(ordered) and not verification_failures
    evidence = {
        "schema_version": "open_stock_ai.official_candle_source.v1",
        "source_kind": "exchange_official", "source_id": source_id, "symbol": symbol,
        "requested_start": start, "requested_end": end, "price_basis": "unadjusted",
        "source_provenance_verified": returned_source_verified, "instrument_identity_verified": returned_source_verified,
        "coverage_complete": coverage_complete,
        "source_verification_scope": "validated_returned_bars_not_complete_requested_range",
        "verified_start": ordered[0]["date"] if ordered else None,
        "verified_end": ordered[-1]["date"] if ordered else None,
        "rejected_months": rejected_months, "verification_rejected_months": verification_failures,
        "corporate_actions_verified": False, "historical_vintage_verified": False,
        "execution_costs_verified": False,
        "selection_scope": "explicit_symbol_range_not_historical_full_market_universe",
        "data_sha256": content_hash(ordered), "request_receipts_sha256": content_hash(receipts),
    }
    _check_cancelled(stop_event)
    return {"rows": ordered, "data_evidence": evidence, "source_request_receipts": receipts,
            "coverage_complete": coverage_complete,
            "rejected_months": rejected_months}
