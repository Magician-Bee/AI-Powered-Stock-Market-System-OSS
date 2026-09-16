from __future__ import annotations

import pytest

from open_stock_ai.agent_runtime.validators import (
    _final_summary_numeric_claims_check,
    _final_summary_period_measurements_check,
    _material_numeric_claims,
    _period_tokens,
)


@pytest.mark.parametrize("date", [
    "2026-09-11T02:16:20.020+08:00",
    "2026-09-10T18:16:20.020Z",
    "2026-09-11 02:16:20.020+0800",
    "2026-09-11T02:16:20+08:00",
    "2026-09-11",
    "2026/09/11",
    "2026.09.11",
    "2026年9月11日",
    "20260910 13:30:00",
])
def test_complete_dates_are_not_monthly_periods_or_financial_measurements(date):
    text = f"來源時間 {date}，市場最後成交價 2450.0。"
    assert _material_numeric_claims(text) == {"2450"}
    assert _period_tokens(text) == set()


@pytest.mark.parametrize("label", [
    "SMA 5／20／60 |",
    "SMA60:",
    "EMA(60) =",
    "WMA 20 為",
    "60日均線為",
    "5／20／60日移動平均為",
])
def test_explicit_indicator_window_is_not_a_value_but_following_value_is(label):
    assert _material_numeric_claims(f"{label} 2250.25") == {"2250.25"}


def test_window_filter_keeps_ambiguous_rsi_and_actual_measurements():
    assert _material_numeric_claims("RSI 48，價格20.02，成交量60") == {"48", "20.02", "60"}
    assert _material_numeric_claims("SMA60: 9999.99") == {"9999.99"}


@pytest.mark.parametrize("period", ["2026年7月", "2026-07", "2026/07"])
def test_month_only_financial_periods_are_still_extracted(period):
    assert _period_tokens(f"{period}營收為18,052,275千元") == {"2026-07"}


def _numeric_check(summary, result):
    return _final_summary_numeric_claims_check(
        final_summary=summary,
        evidence_required=True,
        evidence_ids={"market"},
        evidence_catalog={"market": {"result": result}},
    )


def test_timestamp_seconds_cannot_support_fabricated_price():
    evidence = {"received_at": "2026-09-11T02:16:20.020+08:00", "price": 2450.0}
    correct = _numeric_check("來源時間2026-09-11T02:16:20.020+08:00，價格2450.0", evidence)
    fabricated = _numeric_check("來源時間2026-09-11T02:16:20.020+08:00，價格20.02", evidence)
    assert correct["passed"] is True
    assert fabricated["passed"] is False
    assert fabricated["unsupported_claims"] == ["20.02"]


def test_compact_date_shaped_financial_amount_is_still_a_measurement():
    assert _material_numeric_claims("成交量20260910，價格2450") == {"20260910", "2450"}
    check = _numeric_check("成交量20260910，價格2450", {"volume": 17_798_845, "price": 2450})
    assert check["passed"] is False
    assert check["unsupported_claims"] == ["20260910"]


def test_indicator_labels_do_not_hide_fabricated_indicator_values():
    result = {"technical_features": {"sma_60": 2250.25, "rsi_14": 55.462185}}
    assert _numeric_check("SMA60: 2250.25，RSI14為55.462185", result)["passed"] is True
    assert _numeric_check("SMA60: 9999.99", result)["unsupported_claims"] == ["9999.99"]
    assert _numeric_check("RSI 48", result)["unsupported_claims"] == ["48"]


def test_daily_history_and_quote_timestamps_do_not_create_a_monthly_claim():
    result = {
        "quote": {"price": 2450.0, "received_at": "2026-09-11T02:16:20.020+08:00"},
        "history": [
            {"date": "2026-09-07", "volume": 26_898_329},
            {"date": "2026-09-08", "volume": 28_931_697},
            {"date": "2026-09-09", "volume": 17_798_845},
        ],
    }
    summary = (
        "來源時間2026-09-11T02:16:20.020+08:00，價格2450.0。\n"
        "成交量：2026-09-07為26898329、2026-09-08為28931697、2026-09-09為17798845。"
    )
    assert _numeric_check(summary, result)["passed"] is True
    assert _numeric_check(summary.replace("17798845", "99999999"), result)["passed"] is False
    check = _final_summary_period_measurements_check(
        final_summary=summary, evidence_required=True, evidence_ids={"market"},
        evidence_catalog={"market": {"result": result}},
    )
    assert check["passed"] is True
    assert check["checked_claims"] == []


def test_complete_timestamp_does_not_disable_monthly_value_binding():
    evidence = {"market": {"result": {
        "received_at": "2026-09-11T02:16:20.020+08:00",
        "items": [
            {"period": "2026-07", "revenue": 18_052_275},
            {"period": "2026-06", "revenue": 17_736_690},
        ],
    }}}
    common = {"evidence_required": True, "evidence_ids": {"market"}, "evidence_catalog": evidence}
    correct = _final_summary_period_measurements_check(
        final_summary="來源時間2026-09-11T02:16:20.020+08:00；2026年7月營收18052275千元。", **common,
    )
    cross_wired = _final_summary_period_measurements_check(
        final_summary="來源時間2026-09-11T02:16:20.020+08:00；2026年7月營收17736690千元。", **common,
    )
    assert correct["passed"] is True
    assert cross_wired["passed"] is False
    assert cross_wired["unsupported_claims"][0]["period"] == "2026-07"


def _history_count_check(summary, *, valid=True, bound=True, tool="market.research_pack"):
    return _final_summary_numeric_claims_check(
        final_summary=summary, evidence_required=True,
        evidence_ids={"market"} if bound else set(),
        evidence_catalog={"market": {
            "tool": tool, "ok": True, "validation": {"passed": valid},
            "result": {"price": 2450, "recent_history": [{"close": 2450}] * 60},
        }},
    )


@pytest.mark.parametrize("phrase", [
    "研究包回傳 60 筆", "研究包回傳60點", "研究包包含 60 個歷史點",
    "Research pack returned 60 rows",
])
def test_history_count_is_derived_only_for_an_explicit_count_phrase(phrase):
    check = _history_count_check(f"{phrase}，價格2450。")
    assert check["passed"] is True
    assert check["derived_history_count_claims"] == [{
        "count": 60, "supported": True, "source": "validated_recent_history_length",
    }]


@pytest.mark.parametrize("summary,unsupported", [
    ("研究包回傳60筆，價格60。", ["60"]),
    ("研究包回傳61筆，價格2450。", ["61"]),
    ("研究包價格60。", ["60"]),
    ("研究包回傳60筆，成交量60。", ["60"]),
])
def test_history_count_never_supports_price_volume_or_the_wrong_count(summary, unsupported):
    check = _history_count_check(summary)
    assert check["passed"] is False
    assert check["unsupported_claims"] == unsupported


@pytest.mark.parametrize("kwargs", [
    {"valid": False}, {"bound": False}, {"tool": "some.other_tool"},
])
def test_history_count_requires_this_runs_bound_validated_research_pack(kwargs):
    assert _history_count_check("研究包回傳60筆", **kwargs)["passed"] is False
