from __future__ import annotations

from typing import Any

from .balance_sheet import query_balance_sheet_history
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .income_statement import (
    EARLIEST_ARCHIVE_PERIOD,
    MAX_HISTORY_QUARTERS,
    _period_index,
    _period_parts,
    latest_conservatively_available_period,
    query_income_statement_history,
)
from .realtime_data import normalize_symbol


FINANCIAL_RATIO_SCHEMA_VERSION = "stock_ai.financial_ratio_history.v1"


class FinancialRatioHistoryError(ValueError):
    pass


def _percent(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or denominator is None or float(denominator) == 0:
        return None
    return float(numerator) / float(denominator) * 100


def _previous_period(period: str) -> str:
    index = _period_index(period) - 1
    return f"{index // 4:04d}-Q{index % 4 + 1}"


def _average(current: Any, previous: Any) -> tuple[float | None, str]:
    if current is None:
        return None, "missing_current_balance"
    if previous is None:
        return float(current), "ending_balance_only"
    return (float(current) + float(previous)) / 2, "consecutive_quarter_average"


def _ratio_item(
    income: dict[str, Any],
    balance: dict[str, Any],
    previous_balance: dict[str, Any] | None,
) -> dict[str, Any]:
    period = str(income["period"])
    _, quarter = _period_parts(period)
    annualization_factor = 4 / quarter
    average_equity, equity_basis = _average(
        balance.get("total_equity"),
        (previous_balance or {}).get("total_equity"),
    )
    average_assets, asset_basis = _average(
        balance.get("total_assets"),
        (previous_balance or {}).get("total_assets"),
    )
    net_income = income.get("net_income")
    annualized_net_income = (
        float(net_income) * annualization_factor
        if net_income is not None
        else None
    )
    ratios = {
        "gross_margin_percent": _percent(
            income.get("gross_profit"),
            income.get("revenue"),
        ),
        "operating_margin_percent": _percent(
            income.get("operating_income"),
            income.get("revenue"),
        ),
        "net_margin_percent": _percent(
            net_income,
            income.get("revenue"),
        ),
        "roe_percent": _percent(annualized_net_income, average_equity),
        "roa_percent": _percent(annualized_net_income, average_assets),
        "debt_ratio_percent": _percent(
            balance.get("total_liabilities"),
            balance.get("total_assets"),
        ),
    }
    missing_metrics = [
        field for field, value in ratios.items() if value is None
    ]
    return {
        "period": period,
        "fiscal_year": income["fiscal_year"],
        "quarter": quarter,
        "symbol": income["symbol"],
        **ratios,
        "calculation_status": (
            "complete" if not missing_metrics else "partial"
        ),
        "missing_metrics": missing_metrics,
        "calculation_contract": {
            "margin_basis": "official_ytd_income_statement",
            "return_annualization_factor": annualization_factor,
            "roe_denominator_basis": equity_basis,
            "roa_denominator_basis": asset_basis,
            "previous_balance_period": (
                previous_balance.get("period") if previous_balance else None
            ),
            "formulas": {
                "gross_margin_percent": "gross_profit / revenue * 100",
                "operating_margin_percent": "operating_income / revenue * 100",
                "net_margin_percent": "net_income / revenue * 100",
                "roe_percent": (
                    "annualized_ytd_net_income / average_total_equity * 100"
                ),
                "roa_percent": (
                    "annualized_ytd_net_income / average_total_assets * 100"
                ),
                "debt_ratio_percent": (
                    "total_liabilities / total_assets * 100"
                ),
            },
        },
        "source_comparison": {
            "periods_match": income["period"] == balance["period"],
            "income_statement": {
                "source_id": income.get("source_id"),
                "source_url": income.get("source_url"),
                "statement_scope": income.get("statement_scope"),
                "inputs": {
                    field: income.get(field)
                    for field in (
                        "revenue",
                        "gross_profit",
                        "operating_income",
                        "net_income",
                    )
                },
            },
            "balance_sheet": {
                "source_id": balance.get("source_id"),
                "source_url": balance.get("source_url"),
                "statement_scope": balance.get("statement_scope"),
                "inputs": {
                    field: balance.get(field)
                    for field in (
                        "total_assets",
                        "total_liabilities",
                        "total_equity",
                    )
                },
            },
            "previous_balance_sheet": (
                {
                    "period": previous_balance.get("period"),
                    "source_id": previous_balance.get("source_id"),
                    "source_url": previous_balance.get("source_url"),
                    "inputs": {
                        "total_assets": previous_balance.get("total_assets"),
                        "total_equity": previous_balance.get("total_equity"),
                    },
                }
                if previous_balance
                else None
            ),
        },
    }


def query_financial_ratio_history(
    symbol: str,
    *,
    start_period: str = EARLIEST_ARCHIVE_PERIOD,
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = MAX_HISTORY_QUARTERS,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise FinancialRatioHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    income = query_income_statement_history(
        normalized,
        start_period=start_period,
        end_period=end,
        market_segment=market_segment,
        knowledge_at=knowledge_at,
        effective_at=effective_at,
        limit=MAX_HISTORY_QUARTERS,
        platform=service,
    )
    balance = query_balance_sheet_history(
        normalized,
        start_period=start_period,
        end_period=end,
        market_segment=market_segment,
        knowledge_at=knowledge_at,
        effective_at=effective_at,
        limit=MAX_HISTORY_QUARTERS,
        platform=service,
    )
    income_by_period = {
        str(item["period"]): item for item in income.get("items") or []
    }
    balance_by_period = {
        str(item["period"]): item for item in balance.get("items") or []
    }
    common_periods = sorted(
        set(income_by_period) & set(balance_by_period),
        key=_period_index,
        reverse=True,
    )
    page_limit = max(1, min(int(limit), MAX_HISTORY_QUARTERS))
    items = [
        _ratio_item(
            income_by_period[period],
            balance_by_period[period],
            balance_by_period.get(_previous_period(period)),
        )
        for period in common_periods[:page_limit]
    ]
    requested_count = int(
        income.get("coverage", {}).get("requested_period_count") or 0
    )
    return {
        "schema_version": FINANCIAL_RATIO_SCHEMA_VERSION,
        "symbol": normalized,
        "start_period": income["start_period"],
        "end_period": income["end_period"],
        "unit": "percent",
        "count": len(items),
        "items": items,
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "coverage": {
            "requested_period_count": requested_count,
            "ratio_period_count": len(common_periods),
            "income_statement_period_count": len(income_by_period),
            "balance_sheet_period_count": len(balance_by_period),
            "missing_income_periods": sorted(
                set(balance_by_period) - set(income_by_period),
                key=_period_index,
            ),
            "missing_balance_sheet_periods": sorted(
                set(income_by_period) - set(balance_by_period),
                key=_period_index,
            ),
            "is_complete": len(common_periods) == requested_count,
        },
        "truthfulness": {
            "same_period_only": True,
            "annualization": (
                "Q1-Q3 cumulative net income is annualized by 4/quarter; "
                "Q4 uses annual net income without scaling."
            ),
            "denominators": (
                "ROE/ROA use consecutive-quarter average balances when "
                "available; otherwise the ending balance is explicitly labeled."
            ),
            "source_comparison": (
                "Every ratio item exposes both official statement URLs and "
                "the exact official inputs used by each formula."
            ),
        },
    }
