"""Official venue discovery retains valid results during a peer outage."""
from __future__ import annotations

import stock_ai.main  # Initializes the data-platform package before direct module import.
from stock_ai import taiwan_official as official


def _offline():
    raise OSError("official venue temporarily unavailable")


def test_twse_match_survives_tpex_transport_failure(monkeypatch):
    monkeypatch.setattr(official, "twse_quotes", lambda: [
        {"Code": "2330", "Name": "台積電"},
        {"Code": "0050", "Name": "元大台灣50"},
    ])
    monkeypatch.setattr(official, "tpex_quotes", _offline)
    monkeypatch.setattr(official, "twse_companies", _offline)
    monkeypatch.setattr(official, "tpex_companies", _offline)

    assert [(item.symbol, item.exchange) for item in official.search_taiwan_official("台積電")] == [
        ("2330.TW", "TWSE")
    ]
    assert [(item.symbol, item.exchange) for item in official.search_taiwan_official("0050")] == [
        ("0050.TW", "TWSE")
    ]


def test_tpex_match_survives_twse_transport_failure(monkeypatch):
    monkeypatch.setattr(official, "twse_quotes", _offline)
    monkeypatch.setattr(official, "tpex_quotes", lambda: [
        {"SecuritiesCompanyCode": "6488", "CompanyName": "環球晶"}
    ])
    monkeypatch.setattr(official, "twse_companies", _offline)
    monkeypatch.setattr(official, "tpex_companies", _offline)

    items = official.search_taiwan_official("6488")
    assert [(item.symbol, item.exchange) for item in items] == [("6488.TWO", "TPEx")]


def test_failed_quote_sources_can_fall_back_to_official_company_directory(monkeypatch):
    monkeypatch.setattr(official, "twse_quotes", _offline)
    monkeypatch.setattr(official, "tpex_quotes", _offline)
    monkeypatch.setattr(official, "twse_companies", lambda: [
        {"公司代號": "2330", "公司簡稱": "台積電", "公司名稱": "台灣積體電路製造股份有限公司"}
    ])
    monkeypatch.setattr(official, "tpex_companies", _offline)

    items = official.search_taiwan_official("台灣積體電路")
    assert [(item.symbol, item.name) for item in items] == [("2330.TW", "台積電")]
