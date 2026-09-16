from stock_ai.basic_valuation import (
    BASIC_VALUATION_SCHEMA_VERSION,
    build_basic_valuation,
)
import stock_ai.basic_valuation as subject


def _payload(*, debt=20_000_000):
    return build_basic_valuation(
        symbol="2330.TW",
        valuation_date="2026-07-29",
        price=100,
        issued_common_shares=1_000_000_000,
        income={
            "period": "2025-Q4",
            "quarter": 4,
            "revenue": 50_000_000,
            "net_income": 10_000_000,
            "operating_income": 12_000_000,
            "source_id": "mops_archive",
        },
        balance={
            "period": "2025-Q4",
            "total_equity": 40_000_000,
            "cash_and_cash_equivalents": 10_000_000,
            "interest_bearing_debt": debt,
            "total_liabilities": 99_000_000,
            "source_id": "mops_archive",
        },
        cash_flow={
            "period": "2025-Q4",
            "free_cash_flow": 5_000_000,
            "depreciation_and_amortization": 3_000_000,
            "source_id": "mops_archive",
        },
        official_daily={
            "date": "2026-07-29",
            "pe": 11,
            "pb": 2.6,
            "dividend_yield_percent": 3,
            "source_id": "twse_openapi",
        },
        price_source_ids=["twse_official_web"],
        share_revision={
            "revision_id": "shares-1",
            "issued_common_shares": 1_000_000_000,
        },
    )


def test_basic_valuation_uses_one_explicit_input_set_for_six_metrics():
    result = _payload()
    assert result["schema_version"] == BASIC_VALUATION_SCHEMA_VERSION
    assert result["status"] == "complete"
    values = {item["code"]: item["value"] for item in result["metrics"]}
    assert values == {
        "pe": 10.0,
        "pb": 2.5,
        "ps": 2.0,
        "dividend_yield": 3.0,
        "ev_to_ebitda": 7.3333,
        "fcf_yield": 5.0,
    }
    assert result["inputs"]["market_cap_thousand_twd"] == 100_000_000
    assert result["inputs"]["enterprise_value_thousand_twd"] == 110_000_000
    assert result["cross_check"]["official_pe"] == 11


def test_ev_ebitda_does_not_substitute_total_liabilities_for_missing_debt():
    result = _payload(debt=None)
    metric = next(
        item for item in result["metrics"] if item["code"] == "ev_to_ebitda"
    )
    assert metric["status"] == "unavailable"
    assert metric["value"] is None
    assert result["inputs"]["interest_bearing_debt_thousand_twd"] is None
    assert result["truthfulness"]["total_liabilities_not_used_as_debt"] is True


def test_official_daily_valuation_reuses_one_exchange_report_for_peer_symbols(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"Code": "2330", "Date": "1150731", "PEratio": "20", "PBratio": "5", "DividendYield": "1.5"},
                {"Code": "2303", "Date": "1150731", "PEratio": "15", "PBratio": "2", "DividendYield": "2.0"},
            ]

    def fake_get(url, **_kwargs):
        calls.append(url)
        return Response()

    subject._OFFICIAL_DAILY_VALUATION_ROWS.clear()
    monkeypatch.setattr(subject.httpx, "get", fake_get)
    first = subject.fetch_official_daily_valuation("2330.TW")
    second = subject.fetch_official_daily_valuation("2303.TW")

    assert len(calls) == 1
    assert first["pe"] == 20.0
    assert second["pb"] == 2.0
