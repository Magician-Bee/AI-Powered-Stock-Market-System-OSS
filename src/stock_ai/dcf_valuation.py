from __future__ import annotations

from typing import Any


DCF_SCHEMA_VERSION = "stock_ai.dcf_valuation.v1"


class DCFValuationError(ValueError):
    pass


def _assumptions(
    *,
    base_revenue: float,
    gross_margin_percent: float,
    fcf_conversion_percent: float,
    revenue_growth_percent: float,
    discount_rate_percent: float,
    terminal_growth_percent: float,
    net_debt: float,
    shares_outstanding: float,
    forecast_years: int,
) -> dict[str, float | int]:
    values = {
        "base_revenue_thousand_twd": float(base_revenue),
        "gross_margin_percent": float(gross_margin_percent),
        "fcf_conversion_of_gross_profit_percent": float(fcf_conversion_percent),
        "revenue_growth_percent": float(revenue_growth_percent),
        "discount_rate_percent": float(discount_rate_percent),
        "terminal_growth_percent": float(terminal_growth_percent),
        "net_debt_thousand_twd": float(net_debt),
        "shares_outstanding_thousand": float(shares_outstanding),
        "forecast_years": int(forecast_years),
    }
    if values["base_revenue_thousand_twd"] <= 0:
        raise DCFValuationError("base revenue must be positive")
    if not 0 <= values["gross_margin_percent"] <= 100:
        raise DCFValuationError("gross margin must be between 0 and 100")
    if not 0 <= values["fcf_conversion_of_gross_profit_percent"] <= 100:
        raise DCFValuationError("FCF conversion must be between 0 and 100")
    if values["revenue_growth_percent"] <= -100:
        raise DCFValuationError("revenue growth must be greater than -100")
    if values["discount_rate_percent"] <= 0:
        raise DCFValuationError("discount rate must be positive")
    if values["discount_rate_percent"] <= values["terminal_growth_percent"]:
        raise DCFValuationError("discount rate must exceed terminal growth")
    if values["shares_outstanding_thousand"] <= 0:
        raise DCFValuationError("shares outstanding must be positive")
    if not 1 <= values["forecast_years"] <= 10:
        raise DCFValuationError("forecast years must be between 1 and 10")
    return values


def calculate_dcf(assumptions: dict[str, Any]) -> dict[str, Any]:
    revenue = float(assumptions["base_revenue_thousand_twd"])
    margin = float(assumptions["gross_margin_percent"]) / 100
    conversion = (
        float(assumptions["fcf_conversion_of_gross_profit_percent"]) / 100
    )
    growth = float(assumptions["revenue_growth_percent"]) / 100
    discount = float(assumptions["discount_rate_percent"]) / 100
    terminal_growth = float(assumptions["terminal_growth_percent"]) / 100
    years = int(assumptions["forecast_years"])
    if discount <= terminal_growth:
        raise DCFValuationError("discount rate must exceed terminal growth")
    if not 0 <= margin <= 1 or not 0 <= conversion <= 1:
        raise DCFValuationError("margin and FCF conversion must be between 0 and 100")
    if growth <= -1:
        raise DCFValuationError("revenue growth must be greater than -100")
    forecast: list[dict[str, float | int]] = []
    pv_forecast = 0.0
    for year in range(1, years + 1):
        revenue *= 1 + growth
        gross_profit = revenue * margin
        free_cash_flow = gross_profit * conversion
        discount_factor = (1 + discount) ** year
        present_value = free_cash_flow / discount_factor
        pv_forecast += present_value
        forecast.append(
            {
                "year": year,
                "revenue_thousand_twd": round(revenue, 2),
                "gross_profit_thousand_twd": round(gross_profit, 2),
                "free_cash_flow_thousand_twd": round(free_cash_flow, 2),
                "discount_factor": round(discount_factor, 6),
                "present_value_thousand_twd": round(present_value, 2),
            }
        )
    final_fcf = float(forecast[-1]["free_cash_flow_thousand_twd"])
    terminal_value = final_fcf * (1 + terminal_growth) / (
        discount - terminal_growth
    )
    pv_terminal = terminal_value / ((1 + discount) ** years)
    enterprise_value = pv_forecast + pv_terminal
    equity_value = enterprise_value - float(
        assumptions["net_debt_thousand_twd"]
    )
    per_share = equity_value / float(
        assumptions["shares_outstanding_thousand"]
    )
    return {
        "assumptions": dict(assumptions),
        "forecast": forecast,
        "present_value_forecast_fcf_thousand_twd": round(pv_forecast, 2),
        "terminal_value_thousand_twd": round(terminal_value, 2),
        "present_value_terminal_thousand_twd": round(pv_terminal, 2),
        "enterprise_value_thousand_twd": round(enterprise_value, 2),
        "equity_value_thousand_twd": round(equity_value, 2),
        "implied_value_per_share_twd": round(per_share, 4),
        "status": "positive_equity_value" if equity_value > 0 else "non_positive_equity_value",
    }


def _adjust(base: dict[str, Any], **deltas: float) -> dict[str, Any]:
    output = dict(base)
    for field, delta in deltas.items():
        output[field] = float(output[field]) + float(delta)
    if output["discount_rate_percent"] <= output["terminal_growth_percent"]:
        output["terminal_growth_percent"] = (
            float(output["discount_rate_percent"]) - 0.5
        )
    return output


def _matrix(
    base: dict[str, Any],
    *,
    row_field: str,
    row_values: list[float],
    column_field: str,
    column_values: list[float],
) -> dict[str, Any]:
    cells: list[list[float | None]] = []
    for row_value in row_values:
        row: list[float | None] = []
        for column_value in column_values:
            item = dict(base)
            item[row_field] = row_value
            item[column_field] = column_value
            try:
                row.append(calculate_dcf(item)["implied_value_per_share_twd"])
            except (DCFValuationError, ZeroDivisionError):
                row.append(None)
        cells.append(row)
    return {
        "row_field": row_field,
        "row_values": row_values,
        "column_field": column_field,
        "column_values": column_values,
        "cells_implied_value_per_share_twd": cells,
    }


def build_dcf_valuation(
    *,
    base_revenue: float,
    gross_margin_percent: float,
    fcf_conversion_percent: float,
    revenue_growth_percent: float,
    discount_rate_percent: float,
    terminal_growth_percent: float,
    net_debt: float,
    shares_outstanding: float,
    forecast_years: int = 5,
) -> dict[str, Any]:
    neutral = _assumptions(
        base_revenue=base_revenue,
        gross_margin_percent=gross_margin_percent,
        fcf_conversion_percent=fcf_conversion_percent,
        revenue_growth_percent=revenue_growth_percent,
        discount_rate_percent=discount_rate_percent,
        terminal_growth_percent=terminal_growth_percent,
        net_debt=net_debt,
        shares_outstanding=shares_outstanding,
        forecast_years=forecast_years,
    )
    scenario_assumptions = {
        "pessimistic": _adjust(
            neutral,
            revenue_growth_percent=-2,
            gross_margin_percent=-3,
            discount_rate_percent=2,
            terminal_growth_percent=-1,
        ),
        "neutral": neutral,
        "optimistic": _adjust(
            neutral,
            revenue_growth_percent=2,
            gross_margin_percent=3,
            discount_rate_percent=-2,
            terminal_growth_percent=1,
        ),
    }
    scenarios = [
        {"scenario": name, **calculate_dcf(values)}
        for name, values in scenario_assumptions.items()
    ]
    scenario_values = [
        float(item["implied_value_per_share_twd"]) for item in scenarios
    ]
    growth_values = [
        round(float(neutral["revenue_growth_percent"]) + delta, 2)
        for delta in (-2, -1, 0, 1, 2)
    ]
    discount_values = [
        round(float(neutral["discount_rate_percent"]) + delta, 2)
        for delta in (-2, -1, 0, 1, 2)
    ]
    margin_values = [
        round(float(neutral["gross_margin_percent"]) + delta, 2)
        for delta in (-4, -2, 0, 2, 4)
    ]
    terminal_values = [
        round(float(neutral["terminal_growth_percent"]) + delta, 2)
        for delta in (-1, -0.5, 0, 0.5, 1)
    ]
    return {
        "schema_version": DCF_SCHEMA_VERSION,
        "currency": "TWD",
        "valuation_output_policy": "scenario_range_not_single_target_price",
        "scenarios": scenarios,
        "scenario_range_per_share_twd": {
            "minimum": round(min(scenario_values), 4),
            "maximum": round(max(scenario_values), 4),
        },
        "sensitivity": [
            _matrix(
                neutral,
                row_field="discount_rate_percent",
                row_values=discount_values,
                column_field="revenue_growth_percent",
                column_values=growth_values,
            ),
            _matrix(
                neutral,
                row_field="gross_margin_percent",
                row_values=margin_values,
                column_field="terminal_growth_percent",
                column_values=terminal_values,
            ),
        ],
        "formula_contract": {
            "free_cash_flow": "revenue * gross_margin * FCF conversion of gross profit",
            "forecast_discount": "FCF_t / (1 + discount_rate)^t",
            "terminal_value": "FCF_n * (1 + terminal_growth) / (discount_rate - terminal_growth)",
            "equity_value": "enterprise_value - net_debt",
            "per_share": "equity_value_thousand_twd / shares_outstanding_thousand",
        },
        "truthfulness": {
            "all_assumptions_visible_per_scenario": True,
            "company_guidance_or_analyst_estimates_included": False,
            "model_inputs_are_user_assumptions": True,
            "no_single_target_price": True,
        },
    }
