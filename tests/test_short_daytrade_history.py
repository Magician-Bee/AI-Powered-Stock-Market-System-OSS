from datetime import date
from pathlib import Path

from open_stock_ai.storage.sqlite_store import SQLiteStore

import stock_ai.short_daytrade_history as short_daytrade_module
from stock_ai.short_daytrade_history import (
    ShortDaytradeStore,
    _fetch_date,
    parse_tpex_daytrade,
    parse_tpex_short_balance,
    parse_twse_daytrade,
    parse_twse_short_balance,
    query_short_daytrade_history,
)


SHORT_FIELDS = [
    "股票代號",
    "股票名稱",
    "融資買進",
    "融資賣出",
    "融資現金償還",
    "融資前日餘額",
    "融資今日餘額",
    "融資限額",
    "借券賣出前日餘額",
    "借券賣出",
    "借券賣出還券",
    "借券賣出調整",
    "借券賣出當日餘額",
    "次一營業日可借券賣出限額",
]
DAYTRADE_FIELDS = [
    "證券代號",
    "證券名稱",
    "暫停現股賣出後現款買進當沖註記",
    "當日沖銷交易成交股數",
    "當日沖銷交易買進成交金額",
    "當日沖銷交易賣出成交金額",
]


def _short_row(code: str) -> list[str]:
    return [
        code,
        "測試公司",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "10,000",
        "2,000",
        "500",
        "100",
        "11,600",
        "50,000",
    ]


def _daytrade_row(code: str) -> list[str]:
    return [code, "測試公司", "", "300,000", "15,000,000", "15,300,000"]


def test_twse_parsers_keep_borrowed_short_separate_and_calculate_daytrade_ratio():
    short = parse_twse_short_balance(
        {"date": "20260729", "fields": SHORT_FIELDS, "data": [_short_row("2330")]},
        "2330.TW",
    )
    daytrade = parse_twse_daytrade(
        {
            "date": "20260729",
            "tables": [{"fields": DAYTRADE_FIELDS, "data": [_daytrade_row("2330")]}],
        },
        {
            "tables": [
                {
                    "fields": ["證券代號", "證券名稱", "成交股數"],
                    "data": [["2330", "測試公司", "1,000,000"]],
                }
            ]
        },
        "2330.TW",
    )
    assert short is not None
    assert short["borrowed_sell"] == 2_000
    assert short["borrowed_return"] == 500
    assert short["borrowed_sell_balance"] == 11_600
    assert daytrade is not None
    assert daytrade["daytrade_volume"] == 300_000
    assert daytrade["daytrade_ratio_percent"] == 30


def test_tpex_parsers_preserve_two_suffix_and_use_official_daily_volume():
    short = parse_tpex_short_balance(
        {
            "date": "2026/07/29",
            "tables": [{"fields": SHORT_FIELDS, "data": [_short_row("6488")]}],
        },
        "6488.TWO",
    )
    daytrade = parse_tpex_daytrade(
        {
            "date": "1150729",
            "tables": [{"fields": DAYTRADE_FIELDS, "data": [_daytrade_row("6488")]}],
        },
        "6488.TWO",
        total_volume=600_000,
    )
    assert short is not None
    assert short["symbol"] == "6488.TWO"
    assert short["trade_date"] == "2026-07-29"
    assert daytrade is not None
    assert daytrade["symbol"] == "6488.TWO"
    assert daytrade["trade_date"] == "2026-07-29"
    assert daytrade["daytrade_ratio_percent"] == 50


def test_tpex_history_request_uses_official_slash_date_format(monkeypatch):
    calls = []

    class Response:
        def __init__(self, url, payload):
            self.url = url
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, *, params):
            calls.append((url, params))
            if "margin/sbl" in url:
                payload = {
                    "date": "20260724",
                    "tables": [
                        {"fields": SHORT_FIELDS, "data": [_short_row("6488")]}
                    ],
                }
            else:
                payload = {
                    "date": "20260724",
                    "tables": [
                        {
                            "fields": DAYTRADE_FIELDS,
                            "data": [_daytrade_row("6488")],
                        }
                    ],
                }
            return Response(f"{url}?date={params['date']}", payload)

    monkeypatch.setattr(short_daytrade_module.httpx, "Client", Client)
    item = _fetch_date(
        "6488.TWO",
        date(2026, 7, 24),
        tpex_total_volume=600_000,
    )
    assert item is not None
    assert item["trade_date"] == "2026-07-24"
    assert all(params["date"] == "2026/07/24" for _url, params in calls)


def test_store_preserves_hash_and_query_builds_neutral_summaries(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "short-daytrade.sqlite")
    history = ShortDaytradeStore(store)
    for trade_date, balance, ratio in (
        ("2026-07-28", 10_000, 20.0),
        ("2026-07-29", 11_600, 45.0),
    ):
        history.save(
            {
                "trade_date": trade_date,
                "symbol": "2330.TW",
                "name": "測試公司",
                "exchange": "TWSE",
                "borrowed": {
                    "borrowed_sell_previous_balance": 10_000,
                    "borrowed_sell": 2_000,
                    "borrowed_return": 500,
                    "borrowed_sell_balance": balance,
                },
                "daytrade": {
                    "daytrade_volume": int(ratio * 10_000),
                    "total_traded_volume": 1_000_000,
                    "daytrade_ratio_percent": ratio,
                },
                "source_urls": ["https://www.twse.com.tw/"],
            }
        )
    payload = query_short_daytrade_history(
        store,
        "2330.TW",
        days=14,
        refresh=False,
    )
    assert payload["status"] == "complete"
    assert payload["coverage"]["count"] == 2
    assert payload["summary"]["borrowed"]["latest_balance"] == 11_600
    assert payload["summary"]["daytrade"]["latest_ratio_percent"] == 45
    assert payload["summary"]["daytrade"]["ratio_change_percentage_points"] == 25
    assert payload["summary"]["daytrade"]["heat"]["level"] == "elevated"
    assert payload["truthfulness"]["borrowed_sell_is_separate_from_margin_short"] is True
    assert payload["truthfulness"]["raw_payload_hash_preserved"] is True
    assert all(item["raw_hash"] for item in payload["items"])


def test_borrowed_short_projection_keeps_ingestion_evidence_and_not_an_executable_locate(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "short-daytrade.sqlite")
    history = ShortDaytradeStore(store)
    history.save(
        {
            "trade_date": "2026-07-29",
            "symbol": "2330.TW",
            "name": "測試公司",
            "exchange": "TWSE",
            "borrowed": {"borrowed_sell": 2_000, "borrowed_sell_balance": 11_600},
            "daytrade": None,
            "source_urls": ["https://www.twse.com.tw/"],
        }
    )

    projected = history.borrowed_short_items("2330.TW")

    assert projected[0]["source_id"] == "twse_official_web"
    assert projected[0]["acquired_at"]
    assert projected[0]["raw_hash"]
    assert projected[0]["record_kind"] == "published_borrow_activity"


def test_ui_exposes_blank_symbol_official_controls_and_truthful_labels():
    root = Path(__file__).resolve().parents[1] / "src/stock_ai"
    markup = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(encoding="utf-8")
    assert 'id="shortDaytradeSymbol"' in markup
    assert 'id="loadShortDaytradeBtn"' in markup
    assert 'id="shortDaytradeSymbol" placeholder=' in markup
    assert 'id="shortDaytradeSymbol" value=' not in markup
    assert "借券與融券分開呈現" in markup
    assert "T+2" in markup
    assert 'id="shortDaytradeTable" class="table compact" style="overflow-x:auto"' in markup
    assert "renderShortDaytradeHistory" in script
    assert "min-width:1100px" in script
    assert "機械區間，非投資訊號" in script
