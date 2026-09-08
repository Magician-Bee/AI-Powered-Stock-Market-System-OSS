from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from stock_ai.cash_flow_statement import (
    CASH_FLOW_DATASET,
    CASH_FLOW_HISTORY_SCHEMA_VERSION,
    CASH_FLOW_SOURCE_ID,
    CashFlowArchivePage,
    archive_url,
    backfill_cash_flow_history,
    parse_archive_html,
    query_cash_flow_history,
)
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.income_statement import (
    IncomeStatementArchivePage,
    archive_url as income_archive_url,
    backfill_income_statement_history,
    parse_archive_html as parse_income_html,
)
from stock_ai.main import app


client = TestClient(app)


def _cash_html(scale: int = 1) -> bytes:
    return f"""
    <html><body>
      <h2>合併現金流量表</h2>
      <h4>本資料由台積電公司提供</h4>
      <table class="hasBorder">
        <tr><th>會計項目</th><th>114年度</th><th>113年度</th></tr>
        <tr><td>營業活動之淨現金流入（流出）</td><td>{1_000 * scale}</td><td>1</td></tr>
        <tr><td>取得不動產、廠房及設備</td><td>{-300 * scale}</td><td>-1</td></tr>
        <tr><td>投資活動之淨現金流入（流出）</td><td>{-400 * scale}</td><td>-1</td></tr>
        <tr><td>籌資活動之淨現金流入（流出）</td><td>{-200 * scale}</td><td>-1</td></tr>
      </table>
    </body></html>
    """.encode()


def _income_html() -> bytes:
    return """
    <html><body>
      <h2>合併綜合損益表</h2>
      <h4>本資料由台積電公司提供</h4>
      <table class="hasBorder">
        <tr><th>會計項目</th><th>本期</th><th>上期</th></tr>
        <tr><td>營業收入合計</td><td>2000</td><td>1</td></tr>
        <tr><td>營業毛利（毛損）淨額</td><td>1200</td><td>1</td></tr>
        <tr><td>營業利益（損失）</td><td>900</td><td>1</td></tr>
        <tr><td>母公司業主（淨利∕損）</td><td>800</td><td>1</td></tr>
        <tr><td>基本每股盈餘</td><td>10</td><td>1</td></tr>
      </table>
    </body></html>
    """.encode()


def _parse_cash(period: str, *, scale: int = 1):
    return parse_archive_html(
        _cash_html(scale),
        period=period,
        market_segment="sii",
        symbol="2330.TW",
        source_url=archive_url(period, "sii", "2330.TW"),
        request_parameters={"TYPEK": "sii", "co_id": "2330"},
        acquired_at="2026-07-29T08:00:00+00:00",
    )


def test_parser_preserves_official_cash_flows_and_derives_fcf() -> None:
    item = _parse_cash("2025-Q4")
    assert item.operating_cash_flow == 1_000
    assert item.investing_cash_flow == -400
    assert item.financing_cash_flow == -200
    assert item.capital_expenditure == -300
    assert item.free_cash_flow == 700
    assert item.free_cash_flow_formula == (
        "operating_cash_flow - abs(capital_expenditure)"
    )
    assert item.published_at is None


def test_incremental_history_and_profit_quality_use_same_period_income(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "cash-flow.sqlite"
    platform = MarketDataPlatform(database_path=database_path)

    def fake_income_fetch(period, *, market_segment, symbol, timeout=None):
        body = _income_html()
        url = income_archive_url(period, market_segment, symbol)
        parameters = {"TYPEK": market_segment, "co_id": "2330"}
        return IncomeStatementArchivePage(
            period=period,
            market_segment=market_segment,
            source_url=url,
            request_parameters=parameters,
            acquired_at="2026-07-29T08:00:00+00:00",
            content_type="text/html",
            body=body,
            item=parse_income_html(
                body,
                period=period,
                market_segment=market_segment,
                symbol=symbol,
                source_url=url,
                request_parameters=parameters,
                    acquired_at="2026-07-29T08:00:00+00:00",
            ),
        )

    def fake_cash_fetch(period, *, market_segment, symbol, timeout=None):
        scale = int(period[-1])
        body = _cash_html(scale)
        url = archive_url(period, market_segment, symbol)
        parameters = {"TYPEK": market_segment, "co_id": "2330"}
        return CashFlowArchivePage(
            period=period,
            market_segment=market_segment,
            source_url=url,
            request_parameters=parameters,
            acquired_at="2026-07-29T08:00:00+00:00",
            content_type="text/html",
            body=body,
            item=parse_archive_html(
                body,
                period=period,
                market_segment=market_segment,
                symbol=symbol,
                source_url=url,
                request_parameters=parameters,
                acquired_at="2026-07-29T08:00:00+00:00",
            ),
        )

    monkeypatch.setattr(
        "stock_ai.income_statement.fetch_archive_period",
        fake_income_fetch,
    )
    monkeypatch.setattr(
        "stock_ai.cash_flow_statement.fetch_archive_period",
        fake_cash_fetch,
    )
    backfill_income_statement_history(
        "2330.TW",
        start_period="2025-Q4",
        end_period="2025-Q4",
        platform=platform,
    )
    first = backfill_cash_flow_history(
        "2330.TW",
        start_period="2025-Q3",
        end_period="2025-Q4",
        platform=platform,
    )
    second = backfill_cash_flow_history(
        "2330.TW",
        start_period="2025-Q3",
        end_period="2025-Q4",
        platform=platform,
    )
    assert first["coverage"]["is_complete"] is True, first
    assert first["sync"]["fetched_period_count"] == 2
    assert second["sync"]["fetched_period_count"] == 0
    latest = first["items"][0]
    assert latest["period"] == "2025-Q4"
    assert latest["profit_quality"]["status"] == "strong_cash_conversion"
    assert latest["profit_quality"]["operating_cash_flow_to_net_income"] == 5

    restarted = MarketDataPlatform(database_path=database_path)
    persisted = query_cash_flow_history(
        "2330.TW",
        start_period="2025-Q3",
        end_period="2025-Q4",
        platform=restarted,
    )
    assert persisted["count"] == 2
    with sqlite3.connect(database_path) as conn:
        facts = conn.execute(
            """
            select count(*) from financial_facts
             where dataset=? and source_id=?
               and observation_key like 'cash-flow:%'
            """,
            (CASH_FLOW_DATASET, CASH_FLOW_SOURCE_ID),
        ).fetchone()[0]
    assert facts == 2


def test_api_and_ui_contract(monkeypatch) -> None:
    expected = {
        "schema_version": CASH_FLOW_HISTORY_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "items": [],
        "coverage": {"is_complete": False},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_cash_flow_history",
        lambda *args, **kwargs: expected,
    )
    monkeypatch.setattr(
        "stock_ai.main.backfill_cash_flow_history",
        lambda *args, **kwargs: {**expected, "sync": {"status": "succeeded"}},
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/cash-flow/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q4"
    )
    compatibility = client.get(
        "/api/fundamentals/cash-flow/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q4"
    )
    sync = client.post(
        "/api/data/ui/v1/fundamentals/cash-flow/history/sync",
        json={"symbol": "2330.TW", "start_period": "2025-Q1", "end_period": "2025-Q4"},
    )
    assert unified.status_code == compatibility.status_code == sync.status_code == 200
    assert unified.json() == compatibility.json() == expected
    assert sync.json()["sync"]["status"] == "succeeded"

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/stock_ai/ui/static/index.html").read_text()
    javascript = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text()
    for identifier in (
        "cashFlowSymbol",
        "cashFlowStart",
        "cashFlowEnd",
        "loadCashFlowHistoryBtn",
        "syncCashFlowHistoryBtn",
        "cashFlowHistoryStatus",
        "cashFlowHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/cash-flow/history/sync" in javascript
    assert "profit_quality" in javascript
