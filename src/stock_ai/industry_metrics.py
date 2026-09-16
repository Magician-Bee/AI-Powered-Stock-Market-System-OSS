from __future__ import annotations

import json
from typing import Any, Callable
from urllib.request import Request, urlopen

from open_stock_ai.agent_runtime import default_external_transport_guard

from .balance_sheet import query_balance_sheet_history
from .cash_flow_statement import query_cash_flow_history
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .income_statement import (
    latest_conservatively_available_period,
    query_income_statement_history,
)
from .realtime_data import normalize_symbol


INDUSTRY_METRICS_SCHEMA_VERSION = "stock_ai.industry_metrics.v1"
TWSE_FINANCIAL_HOLDING_INCOME_URL = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh"
)
TWSE_FINANCIAL_HOLDING_BALANCE_URL = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_fh"
)


class IndustryMetricsError(ValueError):
    pass


_PROFILE_LABELS = {
    "financial": "金融業",
    "semiconductor": "半導體業",
    "shipping": "航運業",
    "construction": "營建業",
}
_KEYWORDS = {
    "financial": ("金融", "銀行", "保險", "證券", "financ", "bank", "insurance"),
    "semiconductor": ("半導體", "semiconductor"),
    "shipping": ("航運", "海運", "shipping", "transportation"),
    "construction": ("營建", "建材", "construction", "building"),
}


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _percent(numerator: Any, denominator: Any) -> float | None:
    left, right = _number(numerator), _number(denominator)
    if left is None or right in {None, 0}:
        return None
    return left / right * 100


def _ratio(numerator: Any, denominator: Any) -> float | None:
    left, right = _number(numerator), _number(denominator)
    if left is None or right in {None, 0}:
        return None
    return left / right


def _metric(
    metric_id: str,
    label: str,
    value: float | None,
    unit: str,
    formula: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "label": label,
        "value": value,
        "unit": unit,
        "available": value is not None,
        "unavailable_reason": None if value is not None else "required_official_fields_missing",
        "formula": formula,
        "rationale": rationale,
    }


def classify_industry_profile(
    symbol: str,
    *,
    industry: str | None = None,
    platform: MarketDataPlatform | None = None,
) -> tuple[str | None, str | None, str]:
    normalized = normalize_symbol(symbol)
    raw = str(industry or "").strip()
    basis = "request"
    if not raw:
        try:
            entity = (platform or get_market_data_platform()).resolve_entity(normalized)
            raw = str((entity or {}).get("industry") or "").strip()
            basis = "security_master"
        except (LookupError, ValueError):
            raw = ""
    folded = raw.casefold()
    for profile, keywords in _KEYWORDS.items():
        if any(keyword in folded for keyword in keywords):
            return profile, raw or _PROFILE_LABELS[profile], basis
    return None, raw or None, basis if raw else "unresolved"


def _default_fetch_json(url: str) -> list[dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "StockAI/1.0 industry-metrics"})
    scope = f"source:twse_openapi:industry_metrics:{url}"

    def load() -> list[dict[str, Any]]:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official URLs
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise IndustryMetricsError("TWSE OpenAPI response must be a list")
        return [dict(item) for item in payload if isinstance(item, dict)]

    payload = default_external_transport_guard().call_sync(scope, load)
    if not isinstance(payload, list):
        raise IndustryMetricsError("TWSE OpenAPI response must be a list")
    return [dict(item) for item in payload if isinstance(item, dict)]


def _latest_item(payload: dict[str, Any]) -> dict[str, Any]:
    items = list(payload.get("items") or [])
    return dict(items[0]) if items else {}


def _ordinary_statements(
    symbol: str,
    period: str,
    *,
    platform: MarketDataPlatform,
    knowledge_at: str | None,
    effective_at: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    results: list[dict[str, Any]] = []
    functions = (
        query_income_statement_history,
        query_balance_sheet_history,
        query_cash_flow_history,
    )
    for query in functions:
        try:
            results.append(
                _latest_item(
                    query(
                        symbol,
                        start_period=period,
                        end_period=period,
                        knowledge_at=knowledge_at,
                        effective_at=effective_at,
                        limit=1,
                        platform=platform,
                    )
                )
            )
        except (LookupError, TypeError, ValueError) as exc:
            results.append({})
            failures.append(f"{query.__name__}: {exc}")
    return results[0], results[1], results[2], failures


def _ordinary_metrics(
    profile: str,
    income: dict[str, Any],
    balance: dict[str, Any],
    cash: dict[str, Any],
) -> list[dict[str, Any]]:
    revenue = income.get("revenue")
    assets = balance.get("total_assets")
    if profile == "semiconductor":
        capital_expenditure = _number(cash.get("capital_expenditure"))
        return [
            _metric("gross_margin", "毛利率", _percent(income.get("gross_profit"), revenue), "%", "毛利 / 營收", "觀察製程、產品組合與定價能力"),
            _metric("operating_margin", "營業利益率", _percent(income.get("operating_income"), revenue), "%", "營業利益 / 營收", "觀察本業獲利效率"),
            _metric("capital_intensity", "資本支出密度", _percent(abs(capital_expenditure) if capital_expenditure is not None else None, revenue), "%", "|資本支出| / 營收", "半導體擴產與先進製程投資強度"),
            _metric("inventory_intensity", "存貨資產比", _percent(balance.get("inventory"), assets), "%", "存貨 / 總資產", "觀察庫存與景氣循環風險"),
            _metric("free_cash_flow_margin", "自由現金流率", _percent(cash.get("free_cash_flow"), revenue), "%", "自由現金流 / 營收", "檢查高資本支出後的現金創造力"),
        ]
    if profile == "shipping":
        return [
            _metric("operating_margin", "營業利益率", _percent(income.get("operating_income"), revenue), "%", "營業利益 / 營收", "觀察運價與船舶成本循環"),
            _metric("net_margin", "稅後淨利率", _percent(income.get("net_income"), revenue), "%", "稅後淨利 / 營收", "觀察航運循環最終獲利"),
            _metric("debt_ratio", "負債比率", _percent(balance.get("total_liabilities"), assets), "%", "負債 / 總資產", "檢查船隊與租賃資產的財務槓桿"),
            _metric("asset_turnover", "資產週轉率", _ratio(revenue, assets), "倍", "營收 / 總資產", "觀察重資產船隊的使用效率"),
            _metric("cash_conversion", "自由現金流／淨利", _ratio(cash.get("free_cash_flow"), income.get("net_income")), "倍", "自由現金流 / 稅後淨利", "檢查帳面獲利的現金轉換"),
        ]
    return [
        _metric("gross_margin", "毛利率", _percent(income.get("gross_profit"), revenue), "%", "毛利 / 營收", "觀察個案售價與成本控制"),
        _metric("inventory_intensity", "存貨資產比", _percent(balance.get("inventory"), assets), "%", "存貨 / 總資產", "營建存貨反映土地與在建工程資金占用"),
        _metric("debt_ratio", "負債比率", _percent(balance.get("total_liabilities"), assets), "%", "負債 / 總資產", "檢查推案融資與槓桿"),
        _metric("current_ratio", "流動比率", _percent(balance.get("current_assets"), balance.get("current_liabilities")), "%", "流動資產 / 流動負債", "觀察短期償債與工程週轉"),
        _metric("operating_cash_flow_margin", "營業現金流率", _percent(cash.get("operating_cash_flow"), revenue), "%", "營業現金流 / 營收", "辨識完工交屋與現金回收時點"),
    ]


def _financial_metrics(
    symbol: str,
    fetch_json: Callable[[str], list[dict[str, Any]]],
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, str]], list[str]]:
    code = normalize_symbol(symbol).split(".", 1)[0]
    sources = [
        {"source_id": "twse_openapi", "url": TWSE_FINANCIAL_HOLDING_INCOME_URL},
        {"source_id": "twse_openapi", "url": TWSE_FINANCIAL_HOLDING_BALANCE_URL},
    ]
    try:
        income = next(
            (row for row in fetch_json(TWSE_FINANCIAL_HOLDING_INCOME_URL) if str(row.get("公司代號")) == code),
            {},
        )
        balance = next(
            (row for row in fetch_json(TWSE_FINANCIAL_HOLDING_BALANCE_URL) if str(row.get("公司代號")) == code),
            {},
        )
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        income, balance = {}, {}
        failures = [f"TWSE OpenAPI: {exc}"]
    else:
        failures = [] if income and balance else ["TWSE financial-holding rows not found"]
    year = int(str(income.get("年度") or balance.get("年度") or "0") or 0)
    quarter = str(income.get("季別") or balance.get("季別") or "")
    period = f"{year + 1911:04d}-Q{quarter}" if year and quarter else None
    assets = balance.get("資產總計")
    metrics = [
        _metric("net_interest_income", "利息淨收益", _number(income.get("利息淨收益")), "千元 TWD", "TWSE 金控損益表欄位", "金融業核心利差收入"),
        _metric("credit_provision", "呆帳及保證責任準備", _number(income.get("呆帳費用、承諾及保證責任準備提存")), "千元 TWD", "TWSE 金控損益表欄位", "觀察信用成本"),
        _metric("net_profit", "本期淨利", _number(income.get("本期稅後淨利（淨損）") or income.get("本期淨利（淨損）")), "千元 TWD", "TWSE 金控損益表欄位", "金融控股整體獲利"),
        _metric("equity_ratio", "權益資產比", _percent(balance.get("權益總計"), assets), "%", "權益總計 / 資產總計", "觀察資本緩衝，不能以製造業毛利率代替"),
        _metric("insurance_liability_intensity", "保險負債資產比", _percent(balance.get("保險合約負債及再保險合約負債"), assets), "%", "保險合約負債 / 資產總計", "辨識含壽險金控的負債結構"),
        _metric("eps", "每股盈餘", _number(income.get("基本每股盈餘（元）") or income.get("基本每股盈餘")), "TWD", "TWSE 金控損益表欄位", "股東單位獲利"),
    ]
    return period, metrics, sources, failures


def query_industry_metrics(
    symbol: str,
    *,
    period: str | None = None,
    industry: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    platform: MarketDataPlatform | None = None,
    fetch_json: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise IndustryMetricsError("symbol is required")
    service = platform or get_market_data_platform()
    profile, resolved_industry, classification_basis = classify_industry_profile(
        normalized, industry=industry, platform=service
    )
    if profile is None:
        return {
            "schema_version": INDUSTRY_METRICS_SCHEMA_VERSION,
            "symbol": normalized,
            "supported": False,
            "industry": resolved_industry,
            "profile": None,
            "classification_basis": classification_basis,
            "period": period,
            "metrics": [],
            "issues": ["unsupported_industry_profile"],
            "template_policy": "No generic template is substituted.",
        }
    requested_period = period or latest_conservatively_available_period()
    sources: list[dict[str, str]] = []
    if profile == "financial":
        actual_period, metrics, sources, failures = _financial_metrics(
            normalized, fetch_json or _default_fetch_json
        )
    else:
        income, balance, cash, failures = _ordinary_statements(
            normalized,
            requested_period,
            platform=service,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
        )
        metrics = _ordinary_metrics(profile, income, balance, cash)
        actual_period = requested_period
        for item in (income, balance, cash):
            if item.get("source_url"):
                sources.append(
                    {
                        "source_id": str(item.get("source_id") or "official"),
                        "url": str(item["source_url"]),
                    }
                )
    return {
        "schema_version": INDUSTRY_METRICS_SCHEMA_VERSION,
        "symbol": normalized,
        "supported": True,
        "industry": resolved_industry,
        "profile": profile,
        "profile_label": _PROFILE_LABELS[profile],
        "classification_basis": classification_basis,
        "period": actual_period,
        "metrics": metrics,
        "available_metric_count": sum(item["available"] for item in metrics),
        "sources": sources,
        "issues": failures,
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
        },
        "template_policy": (
            "Metrics are selected by the resolved industry profile; missing "
            "official fields remain unavailable and are never replaced by a "
            "generic company template."
        ),
    }
