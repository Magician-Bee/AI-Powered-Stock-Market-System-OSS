from __future__ import annotations

from typing import Any

from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .income_statement import (
    EARLIEST_ARCHIVE_PERIOD,
    MAX_HISTORY_QUARTERS,
    _period_index,
    latest_conservatively_available_period,
    query_income_statement_history,
)
from .monthly_revenue import (
    MAX_HISTORY_MONTHS,
    latest_completed_period,
    query_monthly_revenue_history,
)
from .realtime_data import normalize_symbol


GROWTH_HISTORY_SCHEMA_VERSION = "stock_ai.growth_metric_history.v1"


class GrowthHistoryError(ValueError):
    pass


def _change_percent(current: Any, previous: Any) -> float | None:
    if current is None or previous is None or float(previous) == 0:
        return None
    return (float(current) - float(previous)) / abs(float(previous)) * 100


def _cagr_percent(current: Any, previous: Any, years: int) -> float | None:
    if (
        current is None
        or previous is None
        or float(current) <= 0
        or float(previous) <= 0
        or years <= 0
    ):
        return None
    return ((float(current) / float(previous)) ** (1 / years) - 1) * 100


def _previous_quarter(period: str) -> str:
    index = _period_index(period) - 1
    return f"{index // 4:04d}-Q{index % 4 + 1}"


def query_growth_history(
    symbol: str,
    *,
    monthly_start_period: str = "2024-01",
    monthly_end_period: str | None = None,
    quarterly_start_period: str = EARLIEST_ARCHIVE_PERIOD,
    quarterly_end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise GrowthHistoryError("symbol is required")
    monthly = query_monthly_revenue_history(
        normalized,
        start_period=monthly_start_period,
        end_period=monthly_end_period or latest_completed_period(),
        market_segment=market_segment,
        knowledge_at=knowledge_at,
        effective_at=effective_at,
        limit=MAX_HISTORY_MONTHS,
        platform=service,
    )
    quarterly = query_income_statement_history(
        normalized,
        start_period=quarterly_start_period,
        end_period=quarterly_end_period
        or latest_conservatively_available_period(),
        market_segment=market_segment,
        knowledge_at=knowledge_at,
        effective_at=effective_at,
        limit=MAX_HISTORY_QUARTERS,
        platform=service,
    )
    monthly_items = [
        {
            "period": item["period"],
            "revenue": item.get("current_revenue"),
            "mom_percent": item.get("mom_change_percent"),
            "yoy_percent": item.get("yoy_change_percent"),
            "ytd_yoy_percent": item.get("ytd_change_percent"),
            "growth_source": item.get("growth_source"),
            "source_id": item.get("source_id"),
            "source_url": item.get("source_url"),
        }
        for item in monthly.get("items") or []
    ]
    quarterly_by_period = {
        str(item["period"]): item for item in quarterly.get("items") or []
    }
    quarterly_items = []
    for period in sorted(quarterly_by_period, key=_period_index, reverse=True):
        item = quarterly_by_period[period]
        previous_period = _previous_quarter(period)
        previous = quarterly_by_period.get(previous_period)
        current_value = item.get("current_quarter_revenue")
        previous_value = (
            previous.get("current_quarter_revenue") if previous else None
        )
        is_consecutive = previous is not None
        quarterly_items.append(
            {
                "period": period,
                "revenue": current_value,
                "qoq_percent": (
                    _change_percent(current_value, previous_value)
                    if is_consecutive
                    else None
                ),
                "comparison_period": previous_period if is_consecutive else None,
                "comparison_status": (
                    "comparable"
                    if is_consecutive
                    and current_value is not None
                    and previous_value is not None
                    else "official_single_quarter_value_missing"
                    if is_consecutive
                    else "previous_quarter_not_saved"
                ),
                "source_id": item.get("source_id"),
                "source_url": item.get("source_url"),
            }
        )
    annual_by_year = {
        int(item["fiscal_year"]): item
        for item in quarterly_by_period.values()
        if int(item.get("quarter") or 0) == 4
    }
    annual_items = []
    for year in sorted(annual_by_year, reverse=True):
        item = annual_by_year[year]
        revenue = item.get("revenue")
        previous = annual_by_year.get(year - 1)
        annual_items.append(
            {
                "year": year,
                "period": item["period"],
                "revenue": revenue,
                "yoy_percent": _change_percent(
                    revenue,
                    previous.get("revenue") if previous else None,
                ),
                "cagr_3y_percent": _cagr_percent(
                    revenue,
                    (annual_by_year.get(year - 3) or {}).get("revenue"),
                    3,
                ),
                "cagr_5y_percent": _cagr_percent(
                    revenue,
                    (annual_by_year.get(year - 5) or {}).get("revenue"),
                    5,
                ),
                "cagr_10y_percent": _cagr_percent(
                    revenue,
                    (annual_by_year.get(year - 10) or {}).get("revenue"),
                    10,
                ),
                "source_id": item.get("source_id"),
                "source_url": item.get("source_url"),
            }
        )
    latest_cagr = None
    if len(annual_by_year) >= 2:
        first_year = min(annual_by_year)
        last_year = max(annual_by_year)
        years = last_year - first_year
        latest_cagr = {
            "start_year": first_year,
            "end_year": last_year,
            "years": years,
            "percent": _cagr_percent(
                annual_by_year[last_year].get("revenue"),
                annual_by_year[first_year].get("revenue"),
                years,
            ),
            "start_source_url": annual_by_year[first_year].get("source_url"),
            "end_source_url": annual_by_year[last_year].get("source_url"),
        }
    return {
        "schema_version": GROWTH_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "monthly": {
            "start_period": monthly["start_period"],
            "end_period": monthly["end_period"],
            "count": len(monthly_items),
            "items": monthly_items,
            "coverage": monthly.get("coverage"),
        },
        "quarterly": {
            "start_period": quarterly["start_period"],
            "end_period": quarterly["end_period"],
            "count": len(quarterly_items),
            "items": quarterly_items,
            "coverage": quarterly.get("coverage"),
        },
        "annual": {
            "count": len(annual_items),
            "items": annual_items,
            "available_range_cagr": latest_cagr,
        },
        "truthfulness": {
            "monthly": (
                "MoM, YoY and cumulative YoY are official MOPS-disclosed values."
            ),
            "quarterly": (
                "QoQ is calculated only from consecutive official standalone "
                "quarter revenue values. Q4 remains non-comparable because the "
                "annual summary does not disclose standalone Q4."
            ),
            "annual": "Annual growth compares official Q4 annual revenue.",
            "cagr": (
                "3/5/10-year and available-range CAGR require positive official "
                "annual endpoints; missing endpoints remain null."
            ),
        },
    }
