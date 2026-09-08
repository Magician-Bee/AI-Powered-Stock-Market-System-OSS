from __future__ import annotations

from typing import Any

from .balance_sheet import query_balance_sheet_history
from .cash_flow_statement import query_cash_flow_history
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .income_statement import (
    EARLIEST_ARCHIVE_PERIOD,
    latest_conservatively_available_period,
    query_income_statement_history,
)
from .realtime_data import normalize_symbol


FINANCIAL_ANOMALY_SCHEMA_VERSION = "stock_ai.financial_anomaly_flags.v1"


def _ratio(a: Any, b: Any) -> float | None:
    if a is None or b is None or float(b) == 0:
        return None
    return float(a) / float(b)


def _growth(current: Any, previous: Any) -> float | None:
    if current is None or previous is None or float(previous) == 0:
        return None
    return (float(current) - float(previous)) / abs(float(previous)) * 100


def detect_financial_anomalies(
    *,
    income: dict[str, Any],
    previous_income: dict[str, Any],
    balance: dict[str, Any],
    previous_balance: dict[str, Any],
    cash_flow: dict[str, Any],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []

    def add(code: str, label: str, triggered: bool | None, observed: Any, threshold: str, evidence: dict[str, Any]) -> None:
        flags.append({
            "code": code,
            "label": label,
            "status": "triggered" if triggered is True else "clear" if triggered is False else "unavailable",
            "triggered": triggered,
            "observed": observed,
            "threshold": threshold,
            "evidence": evidence,
        })

    revenue_growth = _growth(income.get("revenue"), previous_income.get("revenue"))
    ar_growth = _growth(balance.get("accounts_receivable"), previous_balance.get("accounts_receivable"))
    ar_intensity = _ratio(balance.get("accounts_receivable"), balance.get("total_assets"))
    previous_ar_intensity = _ratio(previous_balance.get("accounts_receivable"), previous_balance.get("total_assets"))
    ar_spread = (
        (ar_intensity - previous_ar_intensity) * 100
        if ar_intensity is not None and previous_ar_intensity is not None else None
    )
    ar_trigger = (
        ar_growth > revenue_growth + 20 and ar_spread > 1
        if ar_growth is not None and revenue_growth is not None and ar_spread is not None else None
    )
    add("receivables", "應收帳款異常", ar_trigger, {"ar_growth_percent": ar_growth, "revenue_growth_percent": revenue_growth, "asset_share_change_pp": ar_spread}, "應收成長高於營收逾 20pp，且資產占比增加逾 1pp", {"current": balance, "previous": previous_balance})

    inv_now = _ratio(balance.get("inventory"), balance.get("total_assets"))
    inv_prev = _ratio(previous_balance.get("inventory"), previous_balance.get("total_assets"))
    inv_change = (inv_now - inv_prev) * 100 if inv_now is not None and inv_prev is not None else None
    add("inventory", "存貨異常", inv_change > 3 if inv_change is not None else None, {"asset_share_change_pp": inv_change}, "存貨占總資產季增逾 3pp", {"current": balance, "previous": previous_balance})

    conversion = _ratio(cash_flow.get("operating_cash_flow"), income.get("net_income"))
    cash_trigger = conversion < 0.5 if conversion is not None and float(income.get("net_income") or 0) > 0 else None
    add("cash_conversion", "現金流異常", cash_trigger, {"ocf_to_net_income": conversion}, "正淨利時營業現金流／淨利低於 0.5", {"cash_flow": cash_flow, "income": income})

    margin = _ratio(income.get("gross_profit"), income.get("revenue"))
    previous_margin = _ratio(previous_income.get("gross_profit"), previous_income.get("revenue"))
    margin_change = (margin - previous_margin) * 100 if margin is not None and previous_margin is not None else None
    add("gross_margin", "毛利率異常", abs(margin_change) > 5 if margin_change is not None else None, {"change_pp": margin_change}, "毛利率季變動絕對值逾 5pp", {"current": income, "previous": previous_income})

    one_time = income.get("one_time_gain_loss")
    add("one_time_gain_loss", "一次性損益異常", None if one_time is None else abs(float(one_time)) > abs(float(income.get("net_income") or 0)) * 0.3, {"one_time_gain_loss": one_time}, "一次性損益絕對值逾淨利 30%", {"income": income, "missing_policy": "官方標準損益欄未揭露時不可判斷"})
    return flags


def query_financial_anomalies(
    symbol: str,
    *,
    end_period: str | None = None,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise ValueError("symbol is required")
    service = platform or get_market_data_platform()
    end = end_period or latest_conservatively_available_period()
    income = query_income_statement_history(normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end, limit=8, platform=service)
    balance = query_balance_sheet_history(normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end, limit=8, platform=service)
    cash = query_cash_flow_history(normalized, start_period=EARLIEST_ARCHIVE_PERIOD, end_period=end, limit=8, platform=service)
    incomes = {x["period"]: x for x in income.get("items") or []}
    balances = {x["period"]: x for x in balance.get("items") or []}
    cashes = {x["period"]: x for x in cash.get("items") or []}
    periods = sorted(set(incomes) & set(balances) & set(cashes), reverse=True)
    if len(periods) < 2:
        return {"schema_version": FINANCIAL_ANOMALY_SCHEMA_VERSION, "symbol": normalized, "period": periods[0] if periods else None, "count": 0, "flags": [], "status": "insufficient_consecutive_periods"}
    current, previous = periods[0], periods[1]
    flags = detect_financial_anomalies(income=incomes[current], previous_income=incomes[previous], balance=balances[current], previous_balance=balances[previous], cash_flow=cashes[current])
    return {"schema_version": FINANCIAL_ANOMALY_SCHEMA_VERSION, "symbol": normalized, "period": current, "comparison_period": previous, "count": len(flags), "triggered_count": sum(x["triggered"] is True for x in flags), "flags": flags, "status": "complete"}
