from __future__ import annotations

from types import SimpleNamespace

import pytest

from stock_ai.models import Entity, MarketSummary, PricePoint
from stock_ai.shared_quality_store import SharedDecisionQualityStore
from stock_ai.screener_conditions import ScreenerConditionError, evaluate_conditions, parse_conditions
from stock_ai.services import run_screener


def test_typed_condition_parser_normalizes_aliases_and_legacy_realtime_filter():
    parsed = parse_conditions(["price >= 100", "change_pct > 1.5", "real_quote"])

    assert [item.display() for item in parsed] == [
        "close >= 100.0",
        "change_percent > 1.5",
        "realtime == true",
    ]


def test_typed_condition_parser_supports_official_facts_and_field_references():
    parsed = parse_conditions(
        [
            "revenue_yoy > 15",
            "institutional_net_5d > 0",
            "pe_percentile_5y < 30",
            "average_turnover_20d >= 50000000",
            "close > sma_60",
        ]
    )

    assert [item.display() for item in parsed] == [
        "revenue_yoy > 15.0",
        "institutional_buy_5d > 0.0",
        "pe_percentile < 30.0",
        "avg_turnover_20d >= 50000000.0",
        "close > sma_60",
    ]
    assert parsed[-1].value_is_field is True
    receipt = evaluate_conditions({"close": 110, "sma_60": 100}, [parsed[-1]])
    assert receipt[0].passed is True
    assert receipt[0].receipt()["comparison_observed"] == 100.0


@pytest.mark.parametrize(
    "expression, message",
    [
        ("flow", "must use"),
        ("unknown_metric > 1", "unsupported field"),
        ("exchange > TWSE", "not valid"),
        ("realtime == maybe", "requires true or false"),
    ],
)
def test_typed_condition_parser_rejects_unknown_or_untyped_expressions(expression, message):
    with pytest.raises(ScreenerConditionError, match=message):
        parse_conditions([expression])


def test_missing_realtime_field_fails_explicitly_instead_of_becoming_a_zero_value():
    condition = parse_conditions(["volume > 0"])
    receipt = evaluate_conditions({}, condition)

    assert receipt[0].passed is False
    assert receipt[0].reason == "field_unavailable_in_realtime_quote"


def test_screener_only_returns_symbols_passing_all_real_conditions(monkeypatch, tmp_path):
    quality_store = SharedDecisionQualityStore(tmp_path / "screener.sqlite3")
    monkeypatch.setattr("stock_ai.services._decision_quality_store", lambda: quality_store)
    summaries = {
        "AAA.TW": _summary("AAA.TW", close=110.0, volume=2_000, change_percent=2.0, realtime=True),
        "BBB.TW": _summary("BBB.TW", close=90.0, volume=2_000, change_percent=4.0, realtime=True),
        "CCC.TW": _summary("CCC.TW", close=120.0, volume=500, change_percent=5.0, realtime=False),
    }
    monkeypatch.setattr("stock_ai.services.get_market_summary", summaries.get)

    rows = run_screener(
        ["close >= 100", "volume >= 1000", "realtime == true"],
        symbols=("AAA.TW", "BBB.TW", "CCC.TW"),
    )

    assert [row.symbol for row in rows] == ["AAA.TW"]
    assert rows[0].score == 100.0
    receipt = rows[0].metrics["condition_receipt"]
    assert [item["condition"] for item in receipt] == [
        "close >= 100.0",
        "volume >= 1000.0",
        "realtime == true",
    ]
    assert all(item["passed"] for item in receipt)
    assert rows[0].data_quality_receipt is not None
    assert rows[0].data_quality_receipt.symbol == "AAA.TW"
    assert rows[0].data_quality_receipt.certification_status == "partial"
    assert len(rows[0].data_quality_receipt.receipt_sha256) == 64
    persisted = quality_store.list(surface="screener", symbol="AAA.TW")
    assert len(persisted) == 1
    assert persisted[0]["receipt"]["receipt_id"] == rows[0].data_quality_receipt.receipt_id


def test_screener_applies_persisted_official_facts_and_keeps_field_receipt(monkeypatch, tmp_path):
    quality_store = SharedDecisionQualityStore(tmp_path / "screener.sqlite3")
    monkeypatch.setattr("stock_ai.services._decision_quality_store", lambda: quality_store)
    summaries = {
        "AAA.TW": _summary("AAA.TW", close=110.0, volume=2_000, change_percent=2.0, realtime=True),
        "BBB.TW": _summary("BBB.TW", close=95.0, volume=2_000, change_percent=2.0, realtime=True),
    }
    monkeypatch.setattr("stock_ai.services.get_market_summary", summaries.get)
    monkeypatch.setattr(
        "stock_ai.services._screener_persisted_values",
        lambda symbol, *, fields: (
            {
                "revenue_yoy": 18.0 if symbol == "AAA.TW" else 9.0,
                "sma_60": 100.0,
            },
            {
                "revenue_yoy": {
                    "status": "available",
                    "source": "official monthly revenue cache",
                    "data_as_of": "2026-08",
                },
                "sma_60": {
                    "status": "available",
                    "source": ["twse_official_web"],
                    "data_as_of": "2026-08-11",
                },
            },
        ),
    )

    rows = run_screener(
        ["revenue_yoy > 15", "close > sma_60"],
        symbols=("AAA.TW", "BBB.TW"),
    )

    assert [row.symbol for row in rows] == ["AAA.TW"]
    assert rows[0].metrics["condition_field_receipt"]["revenue_yoy"]["source"] == "official monthly revenue cache"
    assert rows[0].metrics["condition_receipt"][1]["value_kind"] == "field"
    assert rows[0].metrics["condition_receipt"][1]["comparison_observed"] == 100.0


def test_persisted_metric_loader_uses_cache_only_and_requires_complete_windows(monkeypatch):
    from stock_ai import services

    revenue = SimpleNamespace(
        period="2026-08",
        yoy_change_percent=16.5,
        source="TWSE official revenue cache",
        published_at="2026-09-10",
        report_date=None,
        publication_time_status="published",
    )
    flows = [
        SimpleNamespace(
            trade_date=f"2026-08-{day:02d}",
            total_institutional_net=day * 10,
            source="TWSE official T86 cache",
        )
        for day in range(1, 6)
    ]
    points = [
        PricePoint(
            date=f"2026-06-{day:02d}",
            open=float(day),
            high=float(day),
            low=float(day),
            close=float(day),
            volume=1_000,
            turnover=1_000_000.0,
        )
        for day in range(1, 61)
    ]
    monkeypatch.setattr(services, "list_monthly_revenues", lambda **_kwargs: [revenue])
    monkeypatch.setattr(services, "list_institutional_flows", lambda **_kwargs: flows)
    history_calls = []
    monkeypatch.setattr(
        services,
        "query_daily_history",
        lambda *args, **kwargs: history_calls.append(kwargs) or {
            "points": points,
            "source_ids": ["twse_official_web"],
            "range_complete": True,
            "fallback_count": 0,
            "price_basis": "unadjusted",
        },
    )
    monkeypatch.setattr(
        services,
        "_screener_pe_percentile",
        lambda _symbol: (
            25.0,
            {"status": "complete", "source_ids": ["twse_official_web"], "data_as_of": "2026-08-29"},
        ),
    )

    values, receipt = services._screener_persisted_values(
        "AAA.TW",
        fields={"revenue_yoy", "institutional_buy_5d", "pe_percentile", "avg_turnover_20d", "sma_60"},
    )

    assert values == {
        "revenue_yoy": 16.5,
        "institutional_buy_5d": 150.0,
        "avg_turnover_20d": 1_000_000.0,
        "sma_60": 30.5,
        "pe_percentile": 25.0,
    }
    assert receipt["institutional_buy_5d"]["session_count"] == 5
    assert receipt["sma_60"]["source"] == ["twse_official_web"]
    assert history_calls[0]["refresh"] is False
    assert history_calls[0]["allow_fallback"] is False


def _summary(
    symbol: str,
    *,
    close: float,
    volume: int,
    change_percent: float,
    realtime: bool,
) -> MarketSummary:
    return MarketSummary(
        entity=Entity(
            entity_id=f"fixture:{symbol}",
            symbol=symbol,
            name=symbol.split(".")[0],
            entity_type="stock",
            market="taiwan",
            exchange="TWSE",
            currency="TWD",
        ),
        latest_price=PricePoint(
            date="2026-08-11T13:30:00+08:00",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
        ),
        change_percent=change_percent,
        trend="fixture",
        events=[],
        linked_factors=[],
        realtime=realtime,
        buy_liquidity_confirmed=realtime,
        sell_liquidity_confirmed=realtime,
        trading_state="open" if realtime else "closed",
    )
