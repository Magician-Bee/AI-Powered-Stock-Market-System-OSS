from __future__ import annotations

import stock_ai.guidance_tracking as subject


def test_twse_forecast_is_compared_with_reviewed_actual():
    payload = subject.query_financial_guidance(
        "2412.TW",
        fetch_json=lambda _url: [{
            "年度": "115",
            "季別": "1",
            "公司代號": "2412",
            "公司名稱": "中華電",
            "財測序號": "0",
            "涵蓋期間": "一、二、三、四",
            "截至該季經會計師查核或核閱數": "10031589.00",
            "截至該季綜合損益預測數": "8991774~9010933",
        }],
    )
    item = payload["items"][0]
    assert item["period"] == "2026-Q1"
    assert item["guidance_type"] == "company_forecast"
    assert item["comparison"]["status"] == "above_range"
    assert item["audited_or_reviewed"] is True
    assert item["source_url"] == subject.TWSE_FORECAST_ACHIEVEMENT_URL


def test_qualitative_outlook_is_not_forced_into_numeric_comparison():
    item = subject.normalize_guidance_record({
        "guidance_type": "management_outlook",
        "source_url": "https://mops.twse.com.tw/example",
        "outlook_text": "需求可望溫和回升",
        "forecast_low": None,
        "forecast_high": None,
        "actual": 100,
    })
    assert item["comparison"]["comparable"] is False
    assert item["comparison"]["status"] == "not_comparable"


def test_investor_conference_guidance_requires_source():
    try:
        subject.normalize_guidance_record({
            "guidance_type": "investor_conference_guidance",
            "source_url": "",
        })
    except subject.GuidanceTrackingError as exc:
        assert "source_url" in str(exc)
    else:
        raise AssertionError("unsourced guidance must be rejected")


def test_symbol_without_voluntary_forecast_remains_empty():
    payload = subject.query_financial_guidance(
        "2330.TW",
        fetch_json=lambda _url: [],
    )
    assert payload["items"] == []
    assert payload["empty_reason"] == "no_voluntary_quantified_forecast_found"


def test_guidance_ui_exposes_actual_comparison():
    from pathlib import Path

    root = Path(subject.__file__).resolve().parent
    html = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(encoding="utf-8")
    assert 'id="financialGuidanceSymbol"' in html
    assert "預測區間 vs 實際結果" in html
    assert "loadFinancialGuidance" in script
    assert "高於區間" in script
