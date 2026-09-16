from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from open_stock_ai.types import UniverseSnapshot
from open_stock_ai.agent_runtime import (
    ExternalTransportAdmissionError,
    default_external_transport_guard,
)

from .data_platform import get_market_data_platform
from .data_platform.security_loader import OfficialSecurityMasterLoader
from .data_platform.service import stable_entity_id
from .data_platform.source_registry import get_source_registry, source_endpoint
from .data_platform.warehouse import standard_warehouse_domain
from .models import DailyReport, DailySelectionItem, InstitutionalFlowItem, MarginTradingItem, RevenueItem, SecurityMasterItem
from .taiwan_official import (
    TWSE_COMPANIES,
    TWSE_ALL_QUOTES,
    TPEX_COMPANIES,
    TPEX_QUOTES,
    _float,
    _int,
    clear_official_caches,
    is_taiwan_code,
    latest_official_quote,
    normalize_taiwan_code,
    official_cache_status,
    roc_to_iso,
    tpex_companies,
    tpex_quotes,
    twse_companies,
    twse_quotes,
)

_SOURCE_REGISTRY = get_source_registry()
TWSE_OPENAPI_BASE = str(_SOURCE_REGISTRY.source("twse_openapi").base_url)
TWSE_MARGIN_PATH = f"/{_SOURCE_REGISTRY.dataset('twse_margin').endpoint_path}"
TWSE_REVENUE_PATH = f"/{_SOURCE_REGISTRY.dataset('twse_revenue').endpoint_path}"
TWSE_T86_URL = source_endpoint("twse_institutional_flow", date="{date}")
WARRANT_CODE = re.compile(r"\d{5}[A-Z]$", re.I)
logger = logging.getLogger(__name__)


def _debug_report(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    """Compatibility no-op: production code never posts diagnostics to a fixed port."""
    return None


def _daily_debug_report(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    """Compatibility no-op: production code never posts diagnostics to a fixed port."""
    return None


def _get_json(url: str, timeout: int = 20) -> Any:
    dataset = _SOURCE_REGISTRY.identify_dataset_url(url)
    strategy = dataset.failure_strategy if dataset else None
    attempts = strategy.max_attempts if strategy else 1
    effective_timeout = strategy.timeout_seconds if strategy else timeout
    backoff = strategy.backoff_seconds if strategy else []
    dataset_scope = (
        f"source:{dataset.source_id}:{dataset.dataset_id}"
        if dataset
        else f"source:unregistered:phase1_data:{url}"
    )

    def load_with_retries() -> Any:
        for attempt in range(attempts):
            current_url = url
            try:
                for _redirect in range(4):
                    req = Request(
                        current_url,
                        headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"},
                    )
                    try:
                        with urlopen(req, timeout=effective_timeout) as response:
                            return json.loads(response.read().decode("utf-8-sig"))
                    except HTTPError as exc:
                        location = exc.headers.get("Location") if exc.headers else None
                        if exc.code in {301, 302, 303, 307, 308} and location:
                            current_url = urljoin(current_url, location)
                            continue
                        raise
                raise HTTPError(current_url, 508, "Too many redirects", {}, None)
            except (HTTPError, URLError, TimeoutError):
                if attempt >= attempts - 1:
                    raise
                delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0
                if delay:
                    time.sleep(delay)
        raise RuntimeError("Registered source retry loop exited unexpectedly")

    return default_external_transport_guard().call_sync(dataset_scope, load_with_retries)


def _fetch_twse_openapi_rows(path: str) -> list[dict[str, Any]]:
    data = _get_json(f"{TWSE_OPENAPI_BASE}{path}")
    return data if isinstance(data, list) else []


@lru_cache(maxsize=2)
def _cached_margin_rows(_five_minute_bucket: int) -> list[dict[str, Any]]:
    try:
        return _fetch_twse_openapi_rows(TWSE_MARGIN_PATH)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ExternalTransportAdmissionError):
        return []


def _format_trade_date(raw: str | None = None) -> str:
    if raw:
        return roc_to_iso(raw)
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\u3000", " ").strip()


def _match_query(texts: list[str], query: str) -> bool:
    if not query:
        return True
    q = query.strip().lower()
    return any(q in _clean_text(text).lower() for text in texts if text)


def _normalize_symbol_filter(symbol: str | None) -> str:
    if not symbol:
        return ""
    if is_taiwan_code(symbol):
        return normalize_taiwan_code(symbol)
    return symbol.strip().upper()


def _entity_id_for_symbol(symbol: str, source_id: str) -> str:
    platform = get_market_data_platform()
    resolution = platform.resolve_entity(
        symbol,
        identifier_type="display_symbol",
    )
    if resolution["status"] == "resolved":
        return str(resolution["entity"]["entity_id"])
    code = normalize_taiwan_code(symbol)
    exchange = "TPEx" if symbol.upper().endswith(".TWO") else "TWSE"
    return stable_entity_id(market="taiwan", exchange=exchange, source_code=code)


def _read_persisted(
    dataset: str,
    *,
    symbol: str | None,
    model,
    limit: int,
) -> list[Any]:
    platform = get_market_data_platform()
    entity_id = _entity_id_for_symbol(symbol, "twse_openapi") if symbol else None
    domain = standard_warehouse_domain(dataset)
    if domain is None:
        rows = platform.query(dataset=dataset, entity_id=entity_id, limit=max(limit, 5000))
        payloads = [row.payload for row in rows]
    else:
        rows = platform.standard_query(
            domain,
            dataset=dataset,
            entity_id=entity_id,
            limit=max(limit, 5000),
        )
        payloads = [row["record"] for row in rows]
    return [model.model_validate(payload) for payload in payloads[:limit]]


def _persist_records(
    dataset: str,
    *,
    source_id: str,
    items: list[Any],
    observation_field: str,
    request_url: str,
) -> None:
    if not items:
        return
    records = [item.model_dump(mode="json") for item in items]
    temporal_options: dict[str, Any]
    if dataset == "revenues_monthly":
        def period_start(row: dict[str, Any]) -> str:
            return f"{row['period']}-01"

        def period_end(row: dict[str, Any]) -> str:
            year, month = (int(value) for value in str(row["period"]).split("-", 1))
            return f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"

        temporal_options = {
            "time_basis": "fiscal_period",
            "fiscal_period_for": lambda row: str(row["period"]),
            "period_start_for": period_start,
            "period_end_for": period_end,
            "published_at_for": lambda row: (
                str(row["published_at"]) if row.get("published_at") else None
            ),
            "available_at_for": lambda row: (
                str(row.get("available_at") or row.get("acquired_at"))
                if row.get("available_at") or row.get("acquired_at")
                else None
            ),
            "effective_at_for": period_end,
        }
        observation_key_for = lambda row: str(row["period"])
        observed_at_for = period_end
    else:
        temporal_options = {
            "time_basis": "trade_date",
            "trade_date_for": lambda row: str(row[observation_field]),
            "published_at_for": None,
            "available_at_for": lambda _row: None,
            "effective_at_for": lambda row: str(row[observation_field]),
        }
        observation_key_for = lambda row: str(row[observation_field])
        observed_at_for = lambda row: str(row[observation_field])
    get_market_data_platform().ingest_records(
        source_id=source_id,
        dataset=dataset,
        records=records,
        entity_id_for=lambda row: _entity_id_for_symbol(str(row["symbol"]), source_id),
        observation_key_for=observation_key_for,
        observed_at_for=observed_at_for,
        request_url=request_url,
        transformation_id=f"stock_ai.{dataset}_normalizer.v1",
        **temporal_options,
    )


def _record_persistence_failure(
    *,
    dataset: str,
    source_id: str,
    error: Exception,
) -> None:
    """Keep compatibility reads available while making warehouse failures observable."""
    logger.warning(
        "Unified data persistence failed for %s/%s: %s",
        source_id,
        dataset,
        error,
        exc_info=True,
    )
    get_market_data_platform().warehouse.save_checkpoint(
        source_id=source_id,
        dataset=dataset,
        partition_key="all",
        status="failed",
        error={"type": type(error).__name__, "message": str(error)},
        metadata={"compatibility_result_returned": True},
    )


def list_securities_master(
    q: str = "",
    market: str = "all",
    limit: int = 200,
    *,
    include_lifecycle: bool = False,
) -> list[SecurityMasterItem]:
    # UI reads must stay local and bounded.  Refreshing the official security
    # master is an explicit write operation exposed by the dedicated refresh
    # endpoint; doing it on every GET previously made the dashboard wait more
    # than its four-second budget and created avoidable database churn.
    if include_lifecycle:
        try:
            stored = get_market_data_platform().securities(
                query=q,
                market=market,
                limit=limit,
            )
            return [SecurityMasterItem.model_validate(row) for row in stored]
        except Exception:
            logger.warning(
                "Stored security master read unavailable; explicit refresh required",
                exc_info=True,
            )
            return []

    market_filter = market.strip().lower()
    margin_rows = _cached_margin_rows(int(time.time() // 300))
    margin_codes = {_clean_text(row.get("股票代號")) for row in margin_rows if _clean_text(row.get("股票代號"))}
    items: list[SecurityMasterItem] = []

    if market_filter in {"all", "taiwan", "twse", "listed"}:
        companies = {_clean_text(row.get("公司代號")): row for row in twse_companies() if _clean_text(row.get("公司代號"))}
        quotes = {_clean_text(row.get("Code")): row for row in twse_quotes() if _clean_text(row.get("Code"))}
        for code in sorted(set(companies) | set(quotes)):
            company = companies.get(code, {})
            quote = quotes.get(code, {})
            name = _clean_text(quote.get("Name") or company.get("公司簡稱") or company.get("公司名稱"))
            legal_name = _clean_text(company.get("公司名稱"))
            if not code or not _match_query([code, name, legal_name], q):
                continue
            is_etf = code.startswith("00")
            items.append(
                SecurityMasterItem(
                    symbol=f"{code}.TW",
                    name=name or legal_name or code,
                    market="taiwan",
                    exchange="TWSE",
                    listing_type="listed",
                    industry=_clean_text(company.get("產業別")) or ("ETF" if is_etf else None),
                    trade_unit=1000,
                    day_trade_eligible=None,
                    margin_eligible=code in margin_codes,
                    short_eligible=code in margin_codes,
                    is_etf=is_etf,
                    is_warrant=bool(WARRANT_CODE.fullmatch(code)),
                    list_date=roc_to_iso(_clean_text(company.get("上市日期"))) if company.get("上市日期") else None,
                    trading_status="active" if quote else "listed_pending_quote",
                    source=f"{TWSE_COMPANIES} + {TWSE_ALL_QUOTES} + {TWSE_OPENAPI_BASE}{TWSE_MARGIN_PATH}",
                )
            )

    if market_filter in {"all", "taiwan", "tpex", "otc"}:
        companies = {
            _clean_text(row.get("SecuritiesCompanyCode")): row
            for row in tpex_companies()
            if _clean_text(row.get("SecuritiesCompanyCode"))
        }
        quotes = {
            _clean_text(row.get("SecuritiesCompanyCode")): row
            for row in tpex_quotes()
            if _clean_text(row.get("SecuritiesCompanyCode"))
        }
        for code in sorted(set(companies) | set(quotes)):
            company = companies.get(code, {})
            quote = quotes.get(code, {})
            name = _clean_text(quote.get("CompanyName") or company.get("CompanyAbbreviation") or company.get("CompanyName"))
            legal_name = _clean_text(company.get("CompanyName"))
            if not code or not _match_query([code, name, legal_name], q):
                continue
            is_etf = code.startswith("00")
            items.append(
                SecurityMasterItem(
                    symbol=f"{code}.TWO",
                    name=name or legal_name or code,
                    market="taiwan",
                    exchange="TPEx",
                    listing_type="otc",
                    industry=_clean_text(company.get("SecuritiesIndustryCode")) or ("ETF" if is_etf else None),
                    trade_unit=1000,
                    day_trade_eligible=None,
                    margin_eligible=None,
                    short_eligible=None,
                    is_etf=is_etf,
                    is_warrant=bool(WARRANT_CODE.fullmatch(code)),
                    list_date=roc_to_iso(_clean_text(company.get("DateOfListing"))) if company.get("DateOfListing") else None,
                    trading_status="active" if quote else "listed_pending_quote",
                    source=f"{TPEX_COMPANIES} + {TPEX_QUOTES}",
                )
            )

    fallback_items = items[:limit]
    try:
        platform = get_market_data_platform()
        sync = platform.sync_security_master_payloads(
            twse_companies=twse_companies(),
            twse_quotes=twse_quotes(),
            tpex_companies=tpex_companies(),
            tpex_quotes=tpex_quotes(),
        )
        platform.warehouse.save_checkpoint(
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="all",
            cursor_value=str(sync.get("snapshot_hash") or sync.get("acquired_at") or ""),
            status="succeeded",
            metadata={
                "compatibility_result_returned": True,
                "sync_status": sync.get("status"),
                "entity_count": sync.get("count"),
            },
        )
        stored = (
            platform.securities(query=q, market=market, limit=limit)
            if include_lifecycle
            else []
        )
        fallback_by_symbol = {item.symbol.upper(): item for item in fallback_items}
        if stored:
            output: list[SecurityMasterItem] = []
            for row in stored:
                fallback = fallback_by_symbol.get(str(row.get("symbol") or "").upper())
                if fallback:
                    row = {
                        **row,
                        "margin_eligible": fallback.margin_eligible,
                        "short_eligible": fallback.short_eligible,
                    }
                output.append(SecurityMasterItem.model_validate(row))
            return output
        if not include_lifecycle:
            return [
                item.model_copy(
                    update={
                        "entity_id": (
                            platform.resolve_entity(
                                item.symbol,
                                identifier_type="display_symbol",
                            ).get("entity")
                            or {}
                        ).get("entity_id")
                    }
                )
                for item in fallback_items
            ]
    except Exception as exc:
        # The caller still receives the just-fetched official snapshot, while
        # the data-platform checkpoint/health endpoint exposes persistence
        # failures instead of relabeling fallback data as warehouse data.
        _record_persistence_failure(
            dataset="security_master",
            source_id="twse_openapi",
            error=exc,
        )
    return fallback_items


def securities_master_status(*, refresh: bool = False) -> dict[str, Any]:
    platform = None
    if not refresh:
        try:
            platform = get_market_data_platform()
            lifecycle = platform.warehouse.lifecycle_summary()
            stored_count = sum(lifecycle.get("by_entity_type", {}).values())
            active = lifecycle.get("by_status", {}).get("active", 0)
            return {
                "count": stored_count, "by_exchange": lifecycle.get("by_exchange", {}),
                "active": active,
                "pending_quote": lifecycle.get("by_status", {}).get("pre_listing", max(0, stored_count - active)),
                "lifecycle": lifecycle, "cache": official_cache_status(),
                "refresh_policy": {"security_master_seconds": 3600, "market_snapshot_seconds": 300,
                                   "history_seconds": 900, "classification_refresh": "explicit_only",
                                   "refresh_endpoint": "/api/securities/master/refresh"},
                "sources": OfficialSecurityMasterLoader.source_urls(),
                "data_platform": {
                    "status": "cached" if stored_count else "empty",
                    "sync": {"status": "not_requested"},
                    # GET returns bounded local availability, including an
                    # empty warehouse. It never starts a remote refresh.
                    "warehouse": {"status": "available", "database_path": str(platform.warehouse.path),
                                  "tables": {"entities": stored_count}},
                },
            }
        except Exception as exc:
            logger.warning(
                "Stored security master status unavailable; explicit refresh required",
                exc_info=True,
            )
            return {"count": 0, "by_exchange": {}, "active": 0, "pending_quote": 0, "lifecycle": {},
                    "data_platform": {"status": "unavailable", "sync": {"status": "not_requested"},
                                      "error": {"type": type(exc).__name__, "reason": "local_security_master_unavailable"}},
                    "refresh_policy": {"classification_refresh": "explicit_only",
                                       "refresh_endpoint": "/api/securities/master/refresh"}}
    if refresh:
        clear_official_caches()
        _cached_margin_rows.cache_clear()
    platform_status: dict[str, Any]
    lifecycle: dict[str, Any] = {}
    try:
        platform = platform or get_market_data_platform()
        # The loader owns source isolation and retains each attempted payload.
        # Fetching four company/quote sources here first made one transport
        # failure abort the entire explicit refresh before catalogue identity
        # checkpoints could be recovered or persisted.
        sync = OfficialSecurityMasterLoader(platform).run(force=True)
        lifecycle = platform.warehouse.lifecycle_summary()
        stored_count = sum(lifecycle.get("by_entity_type", {}).values())
        platform_status = {
            "status": sync.get("status", "unknown"),
            "sync": sync,
            "warehouse": {
                "status": "available",
                "database_path": str(getattr(platform.warehouse, "path", "")),
                "tables": {"entities": stored_count},
            },
        }
    except Exception as exc:
        if platform is not None:
            try:
                lifecycle = platform.warehouse.lifecycle_summary()
            except Exception:
                lifecycle = {}
        platform_status = {
            "status": "failed",
            "sync": {"status": "failed"},
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    stored_count = sum(lifecycle.get("by_entity_type", {}).values())
    active = lifecycle.get("by_status", {}).get("active", 0)
    return {
        "count": stored_count,
        "by_exchange": lifecycle.get("by_exchange", {}),
        "active": active,
        "pending_quote": lifecycle.get("by_status", {}).get("pre_listing", 0),
        "lifecycle": lifecycle,
        "cache": official_cache_status(),
        "refresh_policy": {
            "security_master_seconds": 3600,
            "market_snapshot_seconds": 300,
            "history_seconds": 900,
        },
        "sources": OfficialSecurityMasterLoader.source_urls(),
        "data_platform": platform_status,
    }


def list_institutional_flows(
    symbol: str | None = None,
    date: str | None = None,
    limit: int = 100,
    *,
    allow_network: bool = True,
) -> list[InstitutionalFlowItem]:
    if not allow_network:
        # A whole-market scan must remain bounded even while an official
        # endpoint is slow. It can use already-persisted official records,
        # but must not turn one delayed optional factor into a stuck UI.
        return _read_persisted(
            "institutional_flows",
            symbol=symbol,
            model=InstitutionalFlowItem,
            limit=limit,
        )
    date_key = (date or datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")).replace("-", "")
    _debug_report("H2", "phase1_data.py:list_institutional_flows", "institutional-flow-start", {"symbol": symbol, "date": date, "limit": limit, "date_key": date_key})
    try:
        data = _get_json(TWSE_T86_URL.format(date=date_key))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ExternalTransportAdmissionError):
        return _read_persisted(
            "institutional_flows",
            symbol=symbol,
            model=InstitutionalFlowItem,
            limit=limit,
        )
    trade_date = _format_trade_date(data.get("date") or date_key)
    rows = data.get("data") or []
    _debug_report("H2", "phase1_data.py:list_institutional_flows", "institutional-flow-rows-fetched", {"trade_date": trade_date, "row_count": len(rows)})
    target = _normalize_symbol_filter(symbol)
    items: list[InstitutionalFlowItem] = []
    for row in rows:
        code = _clean_text(row[0] if len(row) > 0 else "")
        if target and code != target:
            continue
        _debug_report("H2", "phase1_data.py:list_institutional_flows", "institutional-flow-row-parse", {"code": code, "row_len": len(row), "sample": row[:5]})
        dealer_self_buy = _int(row[12] if len(row) > 12 else 0)
        dealer_self_sell = _int(row[13] if len(row) > 13 else 0)
        dealer_hedge_buy = _int(row[15] if len(row) > 15 else 0)
        dealer_hedge_sell = _int(row[16] if len(row) > 16 else 0)
        items.append(
            InstitutionalFlowItem(
                trade_date=trade_date,
                symbol=f"{code}.TW",
                name=_clean_text(row[1] if len(row) > 1 else code),
                foreign_buy=_int(row[2] if len(row) > 2 else 0),
                foreign_sell=_int(row[3] if len(row) > 3 else 0),
                foreign_net=_int(row[4] if len(row) > 4 else 0),
                foreign_dealer_buy=_int(row[5] if len(row) > 5 else 0),
                foreign_dealer_sell=_int(row[6] if len(row) > 6 else 0),
                foreign_dealer_net=_int(row[7] if len(row) > 7 else 0),
                trust_buy=_int(row[8] if len(row) > 8 else 0),
                trust_sell=_int(row[9] if len(row) > 9 else 0),
                trust_net=_int(row[10] if len(row) > 10 else 0),
                dealer_buy=dealer_self_buy + dealer_hedge_buy,
                dealer_sell=dealer_self_sell + dealer_hedge_sell,
                dealer_net=_int(row[11] if len(row) > 11 else 0),
                dealer_hedge_net=_int(row[17] if len(row) > 17 else 0),
                total_institutional_net=_int(row[18] if len(row) > 18 else 0),
                source="TWSE official T86 daily institutional flow",
            )
        )
    try:
        _persist_records(
            "institutional_flows",
            source_id="twse_openapi",
            items=items,
            observation_field="trade_date",
            request_url=TWSE_T86_URL.format(date=date_key),
        )
        persisted = _read_persisted(
            "institutional_flows",
            symbol=symbol,
            model=InstitutionalFlowItem,
            limit=limit,
        )
        if persisted:
            return persisted
    except Exception as exc:
        _record_persistence_failure(
            dataset="institutional_flows",
            source_id="twse_openapi",
            error=exc,
        )
    _debug_report("H2", "phase1_data.py:list_institutional_flows", "institutional-flow-done", {"item_count": len(items), "target": target})
    return items[:limit]


def list_margin_trading(symbol: str | None = None, limit: int = 100) -> list[MarginTradingItem]:
    try:
        rows = _fetch_twse_openapi_rows(TWSE_MARGIN_PATH)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ExternalTransportAdmissionError):
        return _read_persisted(
            "margin_trading",
            symbol=symbol,
            model=MarginTradingItem,
            limit=limit,
        )
    target = _normalize_symbol_filter(symbol)
    trade_date = _format_trade_date()
    items: list[MarginTradingItem] = []
    for row in rows:
        code = _clean_text(row.get("股票代號"))
        if target and code != target:
            continue
        items.append(
            MarginTradingItem(
                trade_date=trade_date,
                symbol=f"{code}.TW",
                name=_clean_text(row.get("股票名稱")),
                margin_buy=_int(row.get("融資買進")),
                margin_sell=_int(row.get("融資賣出")),
                margin_cash_redemption=_int(row.get("融資現金償還")),
                margin_previous_balance=_int(row.get("融資前日餘額")),
                margin_balance=_int(row.get("融資今日餘額")),
                margin_limit=_int(row.get("融資限額")),
                short_buy=_int(row.get("融券買進")),
                short_sell=_int(row.get("融券賣出")),
                short_cash_redemption=_int(row.get("融券現券償還")),
                short_previous_balance=_int(row.get("融券前日餘額")),
                short_balance=_int(row.get("融券今日餘額")),
                short_limit=_int(row.get("融券限額")),
                offsetting=_int(row.get("資券互抵")),
                note=_clean_text(row.get("註記")) or None,
                source=f"TWSE OpenAPI {TWSE_MARGIN_PATH}",
            )
        )
    try:
        _persist_records(
            "margin_trading",
            source_id="twse_openapi",
            items=items,
            observation_field="trade_date",
            request_url=f"{TWSE_OPENAPI_BASE}{TWSE_MARGIN_PATH}",
        )
        persisted = _read_persisted(
            "margin_trading",
            symbol=symbol,
            model=MarginTradingItem,
            limit=limit,
        )
        if persisted:
            return persisted
    except Exception as exc:
        _record_persistence_failure(
            dataset="margin_trading",
            source_id="twse_openapi",
            error=exc,
        )
    return items[:limit]


def list_monthly_revenues(
    symbol: str | None = None,
    limit: int = 100,
    *,
    allow_network: bool = True,
) -> list[RevenueItem]:
    if not allow_network:
        # See list_institutional_flows: deterministic scans never block on an
        # optional current export. A dedicated source refresh can repopulate
        # this persisted official cache separately.
        return _read_persisted(
            "revenues_monthly",
            symbol=symbol,
            model=RevenueItem,
            limit=limit,
        )
    try:
        rows = _fetch_twse_openapi_rows(TWSE_REVENUE_PATH)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ExternalTransportAdmissionError):
        return _read_persisted(
            "revenues_monthly",
            symbol=symbol,
            model=RevenueItem,
            limit=limit,
        )
    target = _normalize_symbol_filter(symbol)
    items: list[RevenueItem] = []
    acquired_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        code = _clean_text(row.get("公司代號"))
        if target and code != target:
            continue
        period_raw = _clean_text(row.get("資料年月"))
        period = f"{int(period_raw[:3]) + 1911:04d}-{period_raw[3:5]}" if len(period_raw) == 5 and period_raw.isdigit() else period_raw
        report_date = roc_to_iso(_clean_text(row.get("出表日期")))
        source_url = f"{TWSE_OPENAPI_BASE}{TWSE_REVENUE_PATH}"
        items.append(
            RevenueItem(
                report_date=report_date,
                report_date_semantics="openapi_export_date",
                period=period,
                symbol=f"{code}.TW",
                name=_clean_text(row.get("公司名稱")),
                industry=_clean_text(row.get("產業別")) or None,
                current_revenue=_float(row.get("營業收入-當月營收")),
                previous_revenue=_float(row.get("營業收入-上月營收")),
                last_year_revenue=_float(row.get("營業收入-去年當月營收")),
                mom_change_percent=_float(row.get("營業收入-上月比較增減(%)")),
                yoy_change_percent=_float(row.get("營業收入-去年同月增減(%)")),
                ytd_revenue=_float(row.get("累計營業收入-當月累計營收")),
                last_ytd_revenue=_float(row.get("累計營業收入-去年累計營收")),
                ytd_change_percent=_float(row.get("累計營業收入-前期比較增減(%)")),
                note=_clean_text(row.get("備註")) or None,
                source=f"TWSE OpenAPI {TWSE_REVENUE_PATH}",
                source_id="twse_openapi",
                source_url=source_url,
                source_market="sii",
                unit="thousand_twd",
                acquired_at=acquired_at,
                published_at=None,
                available_at=acquired_at,
                publication_time_status="not_provided_by_current_openapi",
                growth_source="official_disclosed",
            )
        )
    try:
        _persist_records(
            "revenues_monthly",
            source_id="twse_openapi",
            items=items,
            observation_field="report_date",
            request_url=f"{TWSE_OPENAPI_BASE}{TWSE_REVENUE_PATH}",
        )
        persisted = _read_persisted(
            "revenues_monthly",
            symbol=symbol,
            model=RevenueItem,
            limit=limit,
        )
        if persisted:
            return persisted
    except Exception as exc:
        _record_persistence_failure(
            dataset="revenues_monthly",
            source_id="twse_openapi",
            error=exc,
        )
    return items[:limit]


def securities_for_universe(
    universe: UniverseSnapshot,
    *,
    limit: int | None = None,
) -> list[SecurityMasterItem]:
    symbols = list(universe.symbols[:limit] if limit is not None else universe.symbols)
    if not symbols:
        return []
    master = {item.symbol.upper(): item for item in list_securities_master(limit=5000, include_lifecycle=True)}
    items: list[SecurityMasterItem] = []
    for symbol in symbols:
        item = master.get(symbol.upper())
        if item is not None:
            items.append(item)
            continue
        exchange = "TPEx" if symbol.upper().endswith(".TWO") else "TWSE" if symbol.upper().endswith(".TW") else "UNKNOWN"
        items.append(
            SecurityMasterItem(
                symbol=symbol,
                name=symbol,
                market="taiwan" if exchange != "UNKNOWN" else "unknown",
                exchange=exchange,
                listing_type="unknown",
                trading_status="unknown",
                source=f"UniverseSnapshot:{universe.source}; security metadata unavailable",
            )
        )
    return items


def generate_daily_report(universe: UniverseSnapshot, limit: int = 5) -> DailyReport:
    from .services import get_market_events_brief

    started = time.perf_counter()
    _daily_debug_report(
        "H2",
        "phase1_data.py:generate_daily_report",
        "daily-report-start",
        {"limit": limit, "universe_source": universe.source, "universe_count": universe.count},
    )
    try:
        picks: list[DailySelectionItem] = []
        watchlist = securities_for_universe(universe, limit=limit)
        generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        if not watchlist:
            return DailyReport(
                generated_at=generated_at,
                title="量化規則每日報告",
                summary="尚未指定 Universe，本輪未分析任何股票。",
                picks=[],
                source_snapshot=[],
                universe={
                    "source": universe.source,
                    "symbols": list(universe.symbols),
                    "count": universe.count,
                    "filters": universe.filters,
                    "created_at": universe.created_at,
                },
            )
        _daily_debug_report("H2", "phase1_data.py:generate_daily_report", "daily-report-watchlist-ready", {"watchlist_count": len(watchlist), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        with ThreadPoolExecutor(max_workers=3) as executor:
            flows_future = executor.submit(list_institutional_flows, limit=2000)
            margins_future = executor.submit(list_margin_trading, limit=5000)
            revenues_future = executor.submit(list_monthly_revenues, limit=5000)
            flows_by_symbol = {item.symbol: item for item in flows_future.result()}
            margins_by_symbol = {item.symbol: item for item in margins_future.result()}
            revenues_by_symbol = {item.symbol: item for item in revenues_future.result()}
        _daily_debug_report(
            "H2",
            "phase1_data.py:generate_daily_report",
            "daily-report-sources-ready",
            {
                "flow_count": len(flows_by_symbol),
                "margin_count": len(margins_by_symbol),
                "revenue_count": len(revenues_by_symbol),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        for security in watchlist:
            item_started = time.perf_counter()
            latest = None
            price_change_percent = 0.0
            price_source = "TWSE official quote"
            if is_taiwan_code(security.symbol):
                try:
                    _entity, latest, price_change_percent, price_source, _exchange = latest_official_quote(security.symbol)
                except Exception:
                    latest = None
                    price_change_percent = 0.0
            flow_item = flows_by_symbol.get(security.symbol)
            margin_item = margins_by_symbol.get(security.symbol)
            revenue_item = revenues_by_symbol.get(security.symbol)
            flow = [flow_item] if flow_item else []
            margin = [margin_item] if margin_item else []
            revenue = [revenue_item] if revenue_item else []
            events = get_market_events_brief(security.symbol, limit=2, entity_name=security.name)
            _daily_debug_report(
                "H2",
                "phase1_data.py:generate_daily_report",
                "daily-report-item-data-ready",
                {
                    "symbol": security.symbol,
                    "has_latest": latest is not None,
                    "price_change_percent": price_change_percent,
                    "flow_count": len(flow),
                    "margin_count": len(margin),
                    "revenue_count": len(revenue),
                    "event_count": len(events),
                    "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 1),
                },
            )

            score = 0.0
            reasons: list[str] = []
            risks: list[str] = []
            sources = [price_source]

            if latest and price_change_percent > 0:
                score += 1.0
                reasons.append(f"最新官方收盤漲跌幅 {price_change_percent:.2f}%")
            else:
                risks.append("股價短線動能轉弱")

            if flow:
                sources.append(flow[0].source)
                if flow[0].total_institutional_net > 0:
                    score += 1.0
                    reasons.append(f"三大法人合計買超 {flow[0].total_institutional_net:,} 股")
                else:
                    risks.append("三大法人近期偏賣超")

            if revenue:
                sources.append(revenue[0].source)
                if revenue[0].yoy_change_percent > 0:
                    score += 1.0
                    reasons.append(f"月營收年增 {revenue[0].yoy_change_percent:.2f}%")
                else:
                    risks.append("月營收年增轉弱")

            if margin:
                sources.append(margin[0].source)
                if margin[0].margin_balance > margin[0].margin_previous_balance:
                    risks.append("融資餘額增加，短線籌碼偏熱")
                else:
                    reasons.append("融資餘額未明顯升高")

            if events:
                reasons.append(f"近期待關注事件 {len(events)} 則")
                sources.extend(sorted({event.source_url for event in events[:2]}))

            signal = "buy" if score >= 2.5 else "watch" if score >= 1 else "avoid"
            picks.append(
                DailySelectionItem(
                    symbol=security.symbol,
                    name=security.name,
                    score=round(score, 2),
                    signal=signal,
                    reasons=reasons[:4] or ["等待更多資料交叉驗證"],
                    risk_factors=risks[:4] or ["暫無重大風險訊號"],
                    data_sources=sources[:6],
                    event_count=len(events),
                )
            )
            _daily_debug_report(
                "H2",
                "phase1_data.py:generate_daily_report",
                "daily-report-item-done",
                {"symbol": security.symbol, "score": round(score, 2), "elapsed_ms": round((time.perf_counter() - item_started) * 1000, 1)},
            )

        picks.sort(key=lambda item: item.score, reverse=True)
        report = DailyReport(
            generated_at=generated_at,
            title="量化規則每日報告",
            summary="本報告以官方日線、三大法人、融資融券、月營收與近期事件交叉評分；這是非模型規則分析。",
            picks=picks[:limit],
            source_snapshot=[
                TWSE_COMPANIES,
                TPEX_COMPANIES,
                f"{TWSE_OPENAPI_BASE}{TWSE_MARGIN_PATH}",
                f"{TWSE_OPENAPI_BASE}{TWSE_REVENUE_PATH}",
                TWSE_T86_URL.format(date=datetime.now(timezone.utc).astimezone().strftime('%Y%m%d')),
            ],
            universe={
                "source": universe.source,
                "symbols": list(universe.symbols),
                "count": universe.count,
                "filters": universe.filters,
                "created_at": universe.created_at,
            },
        )
        _daily_debug_report("H2", "phase1_data.py:generate_daily_report", "daily-report-done", {"pick_count": len(report.picks), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        return report
    except Exception as exc:
        _daily_debug_report("H3", "phase1_data.py:generate_daily_report", "daily-report-error", {"error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)})
        raise
