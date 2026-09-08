from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import median
from typing import Any, Callable

from .basic_valuation import fetch_official_daily_valuation
from .balance_sheet import backfill_balance_sheet_history
from .data_platform.service import MarketDataPlatform, get_market_data_platform, stable_entity_id
from .financial_ratios import query_financial_ratio_history
from .income_statement import (
    backfill_income_statement_history,
    latest_conservatively_available_period,
)
from .realtime_data import normalize_symbol
from .taiwan_official import tpex_companies, tpex_quotes, twse_companies, twse_quotes


PEER_COMPARISON_SCHEMA_VERSION = "stock_ai.peer_comparison.v1"
VALUATION_FIELDS = {
    "pe": ("本益比 PE", "times"),
    "pb": ("股價淨值比 PB", "times"),
    "dividend_yield_percent": ("殖利率", "percent"),
}
OPERATING_FIELDS = {
    "gross_margin_percent": ("毛利率", "percent"),
    "operating_margin_percent": ("營業利益率", "percent"),
    "net_margin_percent": ("稅後淨利率", "percent"),
    "roe_percent": ("ROE", "percent"),
    "debt_ratio_percent": ("負債比", "percent"),
}


class PeerComparisonError(ValueError):
    pass


def _number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("%", "").strip()
    if text.casefold() in {"", "-", "--", "n/a", "na"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _resolved_entity(platform: MarketDataPlatform, symbol: str) -> dict[str, Any]:
    resolved = platform.resolve_entity(symbol)
    entity = dict(resolved.get("entity") or {})
    if not entity:
        raise PeerComparisonError(f"{symbol}: security master entity not found")
    return entity


def _official_stock_snapshot(symbol: str) -> list[dict[str, Any]]:
    """Read the relevant official stock master while the warehouse is cold."""

    suffix = ".TWO" if symbol.endswith(".TWO") else ".TW"
    if suffix == ".TW":
        company_rows = twse_companies()
        quote_codes = {str(item.get("Code") or "").strip() for item in twse_quotes()}
        code_key, name_key, industry_key, exchange = "公司代號", "公司簡稱", "產業別", "TWSE"
    else:
        company_rows = tpex_companies()
        quote_codes = {str(item.get("SecuritiesCompanyCode") or "").strip() for item in tpex_quotes()}
        code_key, name_key, industry_key, exchange = "SecuritiesCompanyCode", "CompanyAbbreviation", "SecuritiesIndustryCode", "TPEx"
    records: list[dict[str, Any]] = []
    for row in company_rows:
        code = str(row.get(code_key) or "").strip()
        if not code or not code.isdigit() or code.startswith("00"):
            continue
        name = str(row.get(name_key) or row.get("公司名稱") or row.get("CompanyName") or code).strip()
        records.append({
            "entity_id": stable_entity_id(market="taiwan", exchange=exchange, source_code=code),
            "symbol": f"{code}{suffix}",
            "name": name,
            "canonical_name": name,
            "exchange": exchange,
            "industry": str(row.get(industry_key) or "").strip(),
            "entity_type": "stock",
            "trading_status": "active" if code in quote_codes else "listed_pending_quote",
            "metadata": {"short_name": name},
        })
    return records


def peer_universe(
    symbol: str,
    *,
    platform: MarketDataPlatform | None = None,
    candidate_limit: int = 30,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise PeerComparisonError("symbol is required")
    comparison_items = service.securities(market="stock", limit=100_000)
    target = next(
        (dict(item) for item in comparison_items if item.get("symbol") == normalized),
        {},
    )
    source = "Unified Market Warehouse security_master"
    if not target and platform is None:
        comparison_items = _official_stock_snapshot(normalized)
        target = next(
            (dict(item) for item in comparison_items if item.get("symbol") == normalized),
            {},
        )
        source = "TWSE / TPEx official security-master snapshot (warehouse synchronization in progress)"
    if not target:
        target = _resolved_entity(service, normalized)
    if target.get("entity_type") != "stock":
        raise PeerComparisonError("peer comparison supports ordinary stocks only")
    industry = str(target.get("industry") or "").strip()
    if not industry:
        raise PeerComparisonError("official security master industry is unavailable")
    exchange = str(target.get("exchange") or "")
    candidates = [
        dict(item)
        for item in comparison_items
        if item.get("symbol") != normalized
        and str(item.get("industry") or "").strip() == industry
        and item.get("entity_type") == "stock"
        and item.get("trading_status") == "active"
    ]
    candidates.sort(
        key=lambda item: (
            0 if str(item.get("exchange") or "") == exchange else 1,
            str(item.get("symbol") or ""),
        )
    )
    return {
        "target": {
            "entity_id": target.get("entity_id"),
            "symbol": normalized,
            "name": (target.get("metadata") or {}).get("short_name")
            or target.get("canonical_name"),
            "exchange": exchange,
            "industry": industry,
        },
        "industry": industry,
        "candidate_count": len(candidates),
        "candidates": candidates[: max(1, min(int(candidate_limit), 100))],
        "selection_contract": {
            "source": source,
            "required_entity_type": "stock",
            "required_lifecycle_status": "active",
            "required_industry_match": "exact official security-master industry code",
            "preference_order": [
                "same exchange first",
                "symbol ascending for deterministic display only",
            ],
            "comparison_inclusion": "explicit user-selected symbols only",
            "no_hidden_peer_selection": True,
        },
        "_comparison_items": [dict(item) for item in comparison_items],
    }


def _latest_ratio(
    symbol: str,
    period: str,
    ratio_fetcher: Callable[..., dict[str, Any]],
    platform: MarketDataPlatform,
    refresh: bool,
) -> dict[str, Any]:
    payload = ratio_fetcher(
        symbol,
        start_period=period,
        end_period=period,
        limit=1,
        platform=platform,
    )
    items = list(payload.get("items") or [])
    if not items and refresh and ratio_fetcher is query_financial_ratio_history:
        backfill_income_statement_history(
            symbol,
            start_period=period,
            end_period=period,
            platform=platform,
        )
        backfill_balance_sheet_history(
            symbol,
            start_period=period,
            end_period=period,
            platform=platform,
        )
        payload = ratio_fetcher(
            symbol,
            start_period=period,
            end_period=period,
            limit=1,
            platform=platform,
        )
        items = list(payload.get("items") or [])
    return dict(items[0]) if items else {}


def _company_snapshot(
    item: dict[str, Any],
    *,
    period: str,
    platform: MarketDataPlatform,
    valuation_fetcher: Callable[[str], dict[str, Any]],
    ratio_fetcher: Callable[..., dict[str, Any]],
    refresh: bool,
) -> dict[str, Any]:
    symbol = str(item["symbol"])
    issues: list[str] = []
    try:
        official = valuation_fetcher(symbol)
    except Exception as exc:  # official/network failures remain visible per company
        official = {}
        issues.append(f"official_valuation: {exc}")
    try:
        ratio = _latest_ratio(
            symbol,
            period,
            ratio_fetcher,
            platform,
            refresh,
        )
        if not ratio:
            issues.append(
                f"official_financial_ratios: no data for requested period {period}"
            )
    except Exception as exc:
        ratio = {}
        issues.append(f"official_financial_ratios: {exc}")
    valuation = {
        field: _number(official.get(field)) for field in VALUATION_FIELDS
    }
    operating = {
        field: _number(ratio.get(field)) for field in OPERATING_FIELDS
    }
    return {
        "symbol": symbol,
        "name": item.get("name"),
        "entity_id": item.get("entity_id"),
        "exchange": item.get("exchange"),
        "industry": item.get("industry"),
        "valuation_date": official.get("date"),
        "financial_period": ratio.get("period") or period,
        "valuation": valuation,
        "operating": operating,
        "status": (
            "complete"
            if all(value is not None for value in (*valuation.values(), *operating.values()))
            else "partial"
        ),
        "issues": issues,
        "provenance": {
            "valuation": {
                "source_id": official.get("source_id"),
                "source_url": official.get("source_url"),
                "method": "official_exchange_reported",
            },
            "operating": ratio.get("source_comparison"),
        },
    }


def _benchmarks(
    companies: list[dict[str, Any]],
    *,
    target_symbol: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for section, fields in (
        ("valuation", VALUATION_FIELDS),
        ("operating", OPERATING_FIELDS),
    ):
        for field, (label, unit) in fields.items():
            observations = [
                (str(company["symbol"]), company[section].get(field))
                for company in companies
                if company[section].get(field) is not None
            ]
            target = next(
                (float(value) for symbol, value in observations if symbol == target_symbol),
                None,
            )
            values = [float(value) for _, value in observations]
            rank = None
            if target is not None and values:
                below = sum(value < target for value in values)
                equal = sum(value == target for value in values)
                rank = round((below + equal / 2) / len(values) * 100, 2)
            output.append(
                {
                    "section": section,
                    "field": field,
                    "label": label,
                    "unit": unit,
                    "target_value": target,
                    "peer_median": round(median(values), 4) if values else None,
                    "target_percentile": rank,
                    "sample_count": len(values),
                    "interpretation": (
                        "descriptive_only; a higher percentile is not labeled better"
                    ),
                }
            )
    return output


def query_peer_comparison(
    symbol: str,
    *,
    peers: list[str] | tuple[str, ...] | None = None,
    period: str | None = None,
    platform: MarketDataPlatform | None = None,
    valuation_fetcher: Callable[[str], dict[str, Any]] | None = None,
    ratio_fetcher: Callable[..., dict[str, Any]] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    universe = peer_universe(symbol, platform=platform)
    target = dict(universe["target"])
    normalized_peers: list[str] = []
    for peer in peers or []:
        normalized = normalize_symbol(str(peer))
        if normalized and normalized != target["symbol"] and normalized not in normalized_peers:
            normalized_peers.append(normalized)
    if len(normalized_peers) > 5:
        raise PeerComparisonError("at most five peer symbols are allowed")
    all_candidates = {
        str(item.get("symbol")): item
        for item in universe.get("_comparison_items") or service.securities(market="stock", limit=100_000)
        if item.get("symbol")
    }
    invalid: list[dict[str, str]] = []
    selected: list[dict[str, Any]] = []
    for peer in normalized_peers:
        item = all_candidates.get(peer)
        if not item:
            invalid.append({"symbol": peer, "reason": "security_master_not_found"})
        elif item.get("entity_type") != "stock" or item.get("trading_status") != "active":
            invalid.append({"symbol": peer, "reason": "not_active_ordinary_stock"})
        elif str(item.get("industry") or "") != str(universe["industry"]):
            invalid.append({"symbol": peer, "reason": "official_industry_mismatch"})
        else:
            selected.append(dict(item))
    target_item = {
        **target,
        "trading_status": "active",
        "entity_type": "stock",
    }
    requested_period = period or latest_conservatively_available_period()
    companies: list[dict[str, Any]] = []
    if selected:
        members = [target_item, *selected]
        with ThreadPoolExecutor(max_workers=min(4, len(members))) as executor:
            futures = {
                executor.submit(
                    _company_snapshot,
                    item,
                    period=requested_period,
                    platform=service,
                    valuation_fetcher=valuation_fetcher
                    or fetch_official_daily_valuation,
                    ratio_fetcher=ratio_fetcher or query_financial_ratio_history,
                    refresh=refresh,
                ): str(item["symbol"])
                for item in members
            }
            by_symbol: dict[str, dict[str, Any]] = {}
            for future in as_completed(futures):
                by_symbol[futures[future]] = future.result()
        companies = [by_symbol[str(item["symbol"])] for item in members]
    return {
        "schema_version": PEER_COMPARISON_SCHEMA_VERSION,
        "symbol": target["symbol"],
        "industry": universe["industry"],
        "period": requested_period,
        "status": (
            "complete"
            if selected and all(item["status"] == "complete" for item in companies)
            else "partial"
            if selected
            else "selection_required"
        ),
        "selection": {
            **universe["selection_contract"],
            "requested_peers": normalized_peers,
            "accepted_peers": [str(item["symbol"]) for item in selected],
            "rejected_peers": invalid,
            "candidate_count": universe["candidate_count"],
            "candidate_preview": universe["candidates"],
        },
        "companies": companies,
        "benchmarks": _benchmarks(companies, target_symbol=target["symbol"]),
        "truthfulness": {
            "same_official_industry_required": True,
            "user_selection_preserved": True,
            "valuation_basis": "official exchange values on each disclosed valuation date",
            "operating_basis": "same requested official financial-statement period",
            "missing_values_not_zero": True,
            "percentiles_are_descriptive_not_recommendations": True,
        },
    }
