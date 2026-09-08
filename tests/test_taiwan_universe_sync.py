from __future__ import annotations

from stock_ai.models import PricePoint


def _point(date: str, close: float = 100.0) -> PricePoint:
    return PricePoint(date=date, open=close, high=close, low=close, close=close, volume=1000)


def test_tpex_monthly_history_is_parsed_from_official_trading_stock(monkeypatch):
    from stock_ai import taiwan_official

    taiwan_official.clear_official_caches()
    monkeypatch.setattr(
        taiwan_official,
        "_get_json",
        lambda _url: {
            "stat": "OK",
            "tables": [
                {
                    "data": [
                        ["115/07/01", "3,850", "4,250,202", "1,105.00", "1,110.00", "1,090.00", "1,105.00"]
                    ]
                }
            ],
        },
    )

    rows = taiwan_official.tpex_history("6488", "2026-07-21", months=1)

    assert len(rows) == 1
    assert rows[0].date == "2026-07-01"
    assert rows[0].high == 1110.0
    assert rows[0].volume == 3_850_000
    assert rows[0].turnover == 4_250_202_000
    taiwan_official.clear_official_caches()


def test_empty_twse_history_is_not_permanently_cached(monkeypatch):
    from stock_ai import taiwan_official

    taiwan_official.clear_official_caches()
    monkeypatch.setattr(taiwan_official, "_get_json", lambda _url: (_ for _ in ()).throw(TimeoutError()))
    assert taiwan_official.twse_history("1303", "2026-07-21", months=1) == []

    monkeypatch.setattr(
        taiwan_official,
        "_get_json",
        lambda _url: {
            "stat": "OK",
            "data": [["115/07/01", "1,000", "100,000", "100", "101", "99", "100.5"]],
        },
    )
    rows = taiwan_official.twse_history("1303", "2026-07-21", months=1)

    assert len(rows) == 1
    assert rows[0].close == 100.5
    taiwan_official.clear_official_caches()


def test_security_master_unions_quotes_companies_and_both_exchanges(monkeypatch):
    from stock_ai import phase1_data

    monkeypatch.setattr(phase1_data, "_cached_margin_rows", lambda _bucket: [])
    monkeypatch.setattr(phase1_data, "twse_companies", lambda: [{"公司代號": "2330", "公司簡稱": "台積電"}])
    monkeypatch.setattr(
        phase1_data,
        "twse_quotes",
        lambda: [{"Code": "2330", "Name": "台積電"}, {"Code": "0050", "Name": "元大台灣50"}],
    )
    monkeypatch.setattr(
        phase1_data,
        "tpex_companies",
        lambda: [{"SecuritiesCompanyCode": "6488", "CompanyAbbreviation": "環球晶"}, {"SecuritiesCompanyCode": "7999", "CompanyAbbreviation": "新掛牌"}],
    )
    monkeypatch.setattr(
        phase1_data,
        "tpex_quotes",
        lambda: [{"SecuritiesCompanyCode": "6488", "CompanyName": "環球晶"}],
    )

    rows = phase1_data.list_securities_master(limit=100)
    by_symbol = {row.symbol: row for row in rows}

    assert set(by_symbol) == {"0050.TW", "2330.TW", "6488.TWO", "7999.TWO"}
    assert by_symbol["0050.TW"].is_etf is True
    assert by_symbol["7999.TWO"].trading_status == "listed_pending_quote"


def test_lifecycle_security_master_read_uses_stored_rows_without_remote_sync(monkeypatch):
    from stock_ai import phase1_data

    class StoredPlatform:
        def securities(self, *, query: str, market: str, limit: int):
            assert query == "2330"
            assert market == "taiwan"
            assert limit == 10
            return [
                {
                    "symbol": "2330.TW",
                    "name": "台積電",
                    "market": "taiwan",
                    "exchange": "TWSE",
                    "listing_type": "listed",
                    "industry": "半導體",
                    "trade_unit": 1000,
                    "day_trade_eligible": None,
                    "margin_eligible": None,
                    "short_eligible": None,
                    "is_etf": False,
                    "is_warrant": False,
                    "list_date": "1994-09-05",
                    "trading_status": "active",
                    "source": "Unified Market Warehouse security_master",
                }
            ]

    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: StoredPlatform())
    monkeypatch.setattr(
        phase1_data,
        "twse_companies",
        lambda: (_ for _ in ()).throw(AssertionError("remote fetch must not run")),
    )

    rows = phase1_data.list_securities_master(
        q="2330",
        market="taiwan",
        limit=10,
        include_lifecycle=True,
    )

    assert [item.symbol for item in rows] == ["2330.TW"]


def test_security_master_status_uses_cached_warehouse_without_loader(monkeypatch):
    from stock_ai import phase1_data

    class StoredWarehouse:
        path = "/tmp/market-data.sqlite"

        def lifecycle_summary(self):
            return {
                "by_entity_type": {"stock": 2},
                "by_status": {"active": 2},
                "by_exchange": {"TWSE": 1, "TPEx": 1},
            }

        def status(self):
            raise AssertionError("deep warehouse diagnostics must not run")

    class StoredPlatform:
        warehouse = StoredWarehouse()

    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: StoredPlatform())
    monkeypatch.setattr(
        phase1_data.OfficialSecurityMasterLoader,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("refresh loader must not run")
        ),
    )
    monkeypatch.setattr(
        phase1_data,
        "twse_companies",
        lambda: (_ for _ in ()).throw(AssertionError("remote fetch must not run")),
    )

    status = phase1_data.securities_master_status()

    assert status["count"] == 2
    assert status["by_exchange"] == {"TWSE": 1, "TPEx": 1}
    assert status["data_platform"]["status"] == "cached"


def test_history_payload_uses_longer_fallback_and_reports_indicator_availability(monkeypatch):
    from stock_ai import services

    monkeypatch.setattr(services, "official_history", lambda _symbol: [_point("2026-07-21")])
    monkeypatch.setattr(
        services,
        "fetch_yahoo_history",
        lambda _symbol: {"points": [_point(f"2026-06-{day:02d}", 100 + day) for day in range(1, 31)]},
    )

    payload = services.get_price_history_payload("1303.TW")

    assert payload["point_count"] == 30
    assert payload["source"] == "Yahoo Finance historical fallback"
    assert payload["available_indicators"]["MA20"] is True
    assert payload["available_indicators"]["MA60"] is False
    assert payload["quality"] == "limited"
