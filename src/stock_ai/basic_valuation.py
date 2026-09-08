from __future__ import annotations

from datetime import date
from threading import RLock
from typing import Any

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.storage.sqlite_store import SQLiteStore

from .balance_sheet import query_balance_sheet_history
from .cash_flow_statement import query_cash_flow_history
from .daily_history import query_daily_history
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .data_platform.source_registry import source_endpoint
from .income_statement import (
    EARLIEST_ARCHIVE_PERIOD,
    latest_conservatively_available_period,
    query_income_statement_history,
)
from .liquidity import fetch_official_share_revision
from .realtime_data import normalize_symbol


BASIC_VALUATION_SCHEMA_VERSION = "stock_ai.basic_valuation.v1"
_OFFICIAL_DAILY_VALUATION_ROWS: dict[str, list[dict[str, Any]]] = {}
_OFFICIAL_DAILY_VALUATION_LOCK = RLock()


class BasicValuationError(ValueError):
    pass


def _number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if text in {"", "-", "--", "N/A"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _roc_date(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    if len(text) != 7:
        raise BasicValuationError("official valuation date is invalid")
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7])).isoformat()


def _official_daily_valuation_rows(dataset: str) -> list[dict[str, Any]]:
    """Fetch each exchange's daily valuation report once per app process.

    The report contains every listed security. Peer comparison validates several
    symbols from the same report, so downloading it once per company creates
    needless network delay without improving provenance.
    """

    with _OFFICIAL_DAILY_VALUATION_LOCK:
        cached = _OFFICIAL_DAILY_VALUATION_ROWS.get(dataset)
        if cached is not None:
            return cached
        url = source_endpoint(dataset)
        response = default_external_transport_guard().call_sync(
            f"source:twse_openapi:{dataset}",
            lambda: httpx.get(
                url, timeout=20, headers={"User-Agent": "StockAI/0.1"}
            ),
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise BasicValuationError("official exchange daily valuation response is invalid")
        normalized_rows = [dict(item) for item in rows if isinstance(item, dict)]
        _OFFICIAL_DAILY_VALUATION_ROWS[dataset] = normalized_rows
        return normalized_rows


def _metric(
    code: str,
    label: str,
    value: float | None,
    *,
    unit: str,
    formula: str,
    numerator: float | None = None,
    denominator: float | None = None,
    unavailable_reason: str | None = None,
    method: str = "calculated",
) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "value": round(value, 4) if value is not None else None,
        "unit": unit,
        "status": "available" if value is not None else "unavailable",
        "method": method,
        "formula": formula,
        "numerator": numerator,
        "denominator": denominator,
        "unavailable_reason": None if value is not None else unavailable_reason,
    }


def _divide(
    numerator: float | None,
    denominator: float | None,
    *,
    positive_denominator: bool = True,
) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator == 0 or (positive_denominator and denominator < 0):
        return None
    return numerator / denominator


def build_basic_valuation(
    *,
    symbol: str,
    valuation_date: str,
    price: float | None,
    issued_common_shares: int | None,
    income: dict[str, Any],
    balance: dict[str, Any],
    cash_flow: dict[str, Any],
    official_daily: dict[str, Any],
    price_source_ids: list[str],
    share_revision: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    quarter = int(income.get("quarter") or 0)
    annualization_factor = 4 / quarter if quarter in {1, 2, 3, 4} else None
    market_cap = (
        float(price) * int(issued_common_shares) / 1000
        if price is not None and issued_common_shares
        else None
    )

    def annualized(field: str, record: dict[str, Any]) -> float | None:
        value = record.get(field)
        return (
            float(value) * float(annualization_factor)
            if value is not None and annualization_factor is not None
            else None
        )

    revenue = annualized("revenue", income)
    net_income = annualized("net_income", income)
    operating_income = annualized("operating_income", income)
    depreciation_and_amortization = annualized(
        "depreciation_and_amortization", cash_flow
    )
    free_cash_flow = annualized("free_cash_flow", cash_flow)
    ebitda = (
        operating_income + depreciation_and_amortization
        if operating_income is not None and depreciation_and_amortization is not None
        else None
    )
    debt = _number(balance.get("interest_bearing_debt"))
    cash = _number(balance.get("cash_and_cash_equivalents"))
    enterprise_value = (
        market_cap + debt - cash
        if market_cap is not None and debt is not None and cash is not None
        else None
    )
    equity = _number(balance.get("total_equity"))

    pe = _divide(market_cap, net_income)
    pb = _divide(market_cap, equity)
    ps = _divide(market_cap, revenue)
    ev_ebitda = _divide(enterprise_value, ebitda)
    fcf_ratio = _divide(free_cash_flow, market_cap, positive_denominator=True)
    fcf_yield = fcf_ratio * 100 if fcf_ratio is not None else None
    official_yield = _number(official_daily.get("dividend_yield_percent"))
    metrics = [
        _metric(
            "pe",
            "本益比 PE",
            pe,
            unit="times",
            formula="market_cap / annualized_net_income",
            numerator=market_cap,
            denominator=net_income,
            unavailable_reason="需要正數年化淨利、官方收盤價與已發行普通股數",
        ),
        _metric(
            "pb",
            "股價淨值比 PB",
            pb,
            unit="times",
            formula="market_cap / total_equity",
            numerator=market_cap,
            denominator=equity,
            unavailable_reason="需要正數股東權益、官方收盤價與已發行普通股數",
        ),
        _metric(
            "ps",
            "股價營收比 PS",
            ps,
            unit="times",
            formula="market_cap / annualized_revenue",
            numerator=market_cap,
            denominator=revenue,
            unavailable_reason="需要正數年化營收、官方收盤價與已發行普通股數",
        ),
        _metric(
            "dividend_yield",
            "現金股利殖利率",
            official_yield,
            unit="percent",
            formula="official_exchange_daily_dividend_yield",
            unavailable_reason="交易所當日資料未提供可用殖利率",
            method="official_reported",
        ),
        _metric(
            "ev_to_ebitda",
            "EV/EBITDA",
            ev_ebitda,
            unit="times",
            formula=(
                "(market_cap + interest_bearing_debt - cash) / "
                "(annualized_operating_income + annualized_depreciation_and_amortization)"
            ),
            numerator=enterprise_value,
            denominator=ebitda,
            unavailable_reason="需要正數 EBITDA、現金、有息負債、市值與折舊攤銷",
        ),
        _metric(
            "fcf_yield",
            "自由現金流殖利率",
            fcf_yield,
            unit="percent",
            formula="annualized_free_cash_flow / market_cap * 100",
            numerator=free_cash_flow,
            denominator=market_cap,
            unavailable_reason="需要自由現金流、官方收盤價與已發行普通股數",
        ),
    ]
    return {
        "schema_version": BASIC_VALUATION_SCHEMA_VERSION,
        "symbol": normalized,
        "valuation_date": valuation_date,
        "financial_period": income.get("period"),
        "status": (
            "complete"
            if all(item["status"] == "available" for item in metrics)
            else "partial"
        ),
        "available_count": sum(item["status"] == "available" for item in metrics),
        "metrics": metrics,
        "inputs": {
            "price_twd": price,
            "issued_common_shares": issued_common_shares,
            "market_cap_thousand_twd": market_cap,
            "annualized_revenue_thousand_twd": revenue,
            "annualized_net_income_thousand_twd": net_income,
            "annualized_operating_income_thousand_twd": operating_income,
            "annualized_depreciation_and_amortization_thousand_twd": depreciation_and_amortization,
            "ebitda_thousand_twd": ebitda,
            "annualized_free_cash_flow_thousand_twd": free_cash_flow,
            "cash_thousand_twd": cash,
            "interest_bearing_debt_thousand_twd": debt,
            "enterprise_value_thousand_twd": enterprise_value,
            "total_equity_thousand_twd": equity,
            "annualization_factor": annualization_factor,
        },
        "cross_check": {
            "official_pe": _number(official_daily.get("pe")),
            "official_pb": _number(official_daily.get("pb")),
            "official_dividend_yield_percent": official_yield,
            "note": (
                "PE/PB calculated from the disclosed input set can differ from the "
                "exchange daily values because statement scope and trailing-period "
                "definitions differ; both are preserved rather than overwritten."
            ),
        },
        "provenance": {
            "price_source_ids": price_source_ids,
            "share_revision": share_revision,
            "official_daily_valuation": official_daily,
            "income_statement": {
                key: income.get(key)
                for key in ("period", "source_id", "source_url", "available_at")
            },
            "balance_sheet": {
                key: balance.get(key)
                for key in ("period", "source_id", "source_url", "available_at")
            },
            "cash_flow_statement": {
                key: cash_flow.get(key)
                for key in ("period", "source_id", "source_url", "available_at")
            },
        },
        "truthfulness": {
            "same_financial_period_required": True,
            "missing_values_are_not_zero": True,
            "total_liabilities_not_used_as_debt": True,
            "historical_replay": False,
            "point_in_time_note": (
                "This endpoint is a current valuation snapshot. Historical percentile "
                "replay is delivered separately by VAL-002."
            ),
        },
    }


def fetch_official_daily_valuation(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    is_tpex = normalized.endswith(".TWO")
    dataset = "tpex_daily_valuation" if is_tpex else "twse_daily_valuation"
    url = source_endpoint(dataset)
    rows = _official_daily_valuation_rows(dataset)
    code_field = "SecuritiesCompanyCode" if is_tpex else "Code"
    row = next(
        (item for item in rows if str(item.get(code_field) or "").strip() == code),
        None,
    )
    if not row:
        raise BasicValuationError("official exchange daily valuation row was not found")
    return {
        "date": _roc_date(row.get("Date")),
        "source_id": "tpex_openapi" if is_tpex else "twse_openapi",
        "source_url": url,
        "pe": _number(row.get("PriceEarningRatio") if is_tpex else row.get("PEratio")),
        "pb": _number(row.get("PriceBookRatio") if is_tpex else row.get("PBratio")),
        "dividend_yield_percent": _number(
            row.get("YieldRatio") if is_tpex else row.get("DividendYield")
        ),
        "dividend_per_share_twd": (
            _number(row.get("DividendPerShare")) if is_tpex else None
        ),
        "raw": row,
    }


def query_basic_valuation(
    symbol: str,
    *,
    store: SQLiteStore,
    end_period: str | None = None,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise BasicValuationError("symbol is required")
    service = platform or get_market_data_platform()
    end = end_period or latest_conservatively_available_period()
    income_result = query_income_statement_history(
        normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end,
        limit=8, platform=service,
    )
    balance_result = query_balance_sheet_history(
        normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end,
        limit=8, platform=service,
    )
    cash_result = query_cash_flow_history(
        normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end,
        limit=8, platform=service,
    )
    incomes = {item["period"]: item for item in income_result.get("items") or []}
    balances = {item["period"]: item for item in balance_result.get("items") or []}
    cash_flows = {item["period"]: item for item in cash_result.get("items") or []}
    periods = sorted(set(incomes) & set(balances) & set(cash_flows), reverse=True)
    if not periods:
        raise BasicValuationError("no same-period income, balance and cash-flow statements")
    period = periods[0]
    official_daily = fetch_official_daily_valuation(normalized)
    valuation_date = str(official_daily["date"])
    history = query_daily_history(
        normalized, start=valuation_date, end=valuation_date, limit=1,
        refresh=True, allow_fallback=False,
    )
    point = (history.get("points") or [None])[0]
    price = float(point.close) if point is not None else None
    share_revision, share_warning = fetch_official_share_revision(
        store, normalized, refresh=True
    )
    result = build_basic_valuation(
        symbol=normalized,
        valuation_date=valuation_date,
        price=price,
        issued_common_shares=(
            int(share_revision["issued_common_shares"]) if share_revision else None
        ),
        income=incomes[period],
        balance=balances[period],
        cash_flow=cash_flows[period],
        official_daily=official_daily,
        price_source_ids=list(history.get("source_ids") or []),
        share_revision=share_revision,
    )
    result["warnings"] = [share_warning] if share_warning else []
    return result
