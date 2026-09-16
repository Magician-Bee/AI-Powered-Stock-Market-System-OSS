from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from stock_ai.balance_sheet import (
    BALANCE_SHEET_DATASET,
    BALANCE_SHEET_HISTORY_SCHEMA_VERSION,
    BALANCE_SHEET_SOURCE_ID,
    BalanceSheetArchivePage,
    archive_url,
    backfill_balance_sheet_history,
    parse_archive_html,
    query_balance_sheet_history,
)
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.main import app


client = TestClient(app)


def _archive_html(period: str, *, scale: int = 1) -> bytes:
    values = {
        "現金及約當現金": 2_000_000 * scale,
        "應收帳款淨額": 300_000 * scale,
        "存貨": 250_000 * scale,
        "流動資產合計": 3_500_000 * scale,
        "資產總額": 8_000_000 * scale,
        "流動負債合計": 1_400_000 * scale,
        "負債總額": 2_500_000 * scale,
        "權益總額": 5_500_000 * scale,
    }
    rows = "".join(
        f"<tr><td>　{label}</td><td>{value:,}</td><td>1.00</td>"
        f"<td>{value - 1:,}</td><td>1.00</td></tr>"
        for label, value in values.items()
    )
    return f"""
    <html><body>
      <h2>合併資產負債表</h2>
      <h4>本資料由台積電公司提供</h4>
      <table class="hasBorder">
        <tr><th>會計項目</th><th>114年03月31日</th><th>113年12月31日</th></tr>
        <tr><th></th><th>金額</th><th>%</th><th>金額</th><th>%</th></tr>
        {rows}
      </table>
    </body></html>
    """.encode("utf-8")


def _parse(period: str, *, scale: int = 1):
    return parse_archive_html(
        _archive_html(period, scale=scale),
        period=period,
        market_segment="sii",
        symbol="2330.TW",
        source_url=archive_url(period, "sii", "2330.TW"),
        request_parameters={"TYPEK": "sii", "co_id": "2330"},
        acquired_at="2026-07-29T09:00:00+00:00",
    )


def test_parser_preserves_required_official_period_end_values() -> None:
    item = _parse("2025-Q1")
    assert item.symbol == "2330.TW"
    assert item.name == "台積電"
    assert item.statement_scope == "合併資產負債表"
    assert item.cash_and_cash_equivalents == 2_000_000
    assert item.total_assets == 8_000_000
    assert item.total_liabilities == 2_500_000
    assert item.total_equity == 5_500_000
    assert item.inventory == 250_000
    assert item.accounts_receivable == 300_000
    assert item.official_comparison_dates == ["114年03月31日", "113年12月31日"]
    assert item.published_at is None


def test_history_is_incremental_and_supports_quarter_comparison(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "balance-sheet.sqlite"
    platform = MarketDataPlatform(database_path=database_path)

    def fake_fetch(period, *, market_segment, symbol, timeout=None):
        scale = int(period[-1])
        body = _archive_html(period, scale=scale)
        url = archive_url(period, market_segment, symbol)
        parameters = {"TYPEK": market_segment, "co_id": "2330"}
        return BalanceSheetArchivePage(
            period=period,
            market_segment=market_segment,
            source_url=url,
            request_parameters=parameters,
            acquired_at="2026-07-29T09:00:00+00:00",
            content_type="text/html; charset=UTF-8",
            body=body,
            item=parse_archive_html(
                body,
                period=period,
                market_segment=market_segment,
                symbol=symbol,
                source_url=url,
                request_parameters=parameters,
                acquired_at="2026-07-29T09:00:00+00:00",
            ),
        )

    monkeypatch.setattr("stock_ai.balance_sheet.fetch_archive_period", fake_fetch)
    first = backfill_balance_sheet_history(
        "2330.TW",
        start_period="2025-Q1",
        end_period="2025-Q3",
        platform=platform,
    )
    second = backfill_balance_sheet_history(
        "2330.TW",
        start_period="2025-Q1",
        end_period="2025-Q3",
        platform=platform,
    )
    assert first["schema_version"] == BALANCE_SHEET_HISTORY_SCHEMA_VERSION
    assert first["coverage"]["is_complete"] is True
    assert first["sync"]["fetched_period_count"] == 3
    assert second["sync"]["fetched_period_count"] == 0
    assert second["sync"]["skipped_period_count"] == 3
    latest = first["items"][0]
    assert latest["period"] == "2025-Q3"
    comparison = latest["quarter_comparison"]
    assert comparison["previous_period"] == "2025-Q2"
    assert comparison["fields"]["total_assets"]["change"] == 8_000_000
    assert comparison["fields"]["total_assets"]["change_percent"] == 50

    restarted = MarketDataPlatform(database_path=database_path)
    persisted = query_balance_sheet_history(
        "2330.TW",
        start_period="2025-Q1",
        end_period="2025-Q3",
        platform=restarted,
    )
    assert persisted["count"] == 3
    with sqlite3.connect(database_path) as conn:
        revisions = conn.execute(
            """
            select count(*) from financial_facts
             where dataset=? and source_id=?
               and observation_key like 'balance-sheet:%'
            """,
            (BALANCE_SHEET_DATASET, BALANCE_SHEET_SOURCE_ID),
        ).fetchone()[0]
        checkpoints = conn.execute(
            """
            select count(*) from data_ingestion_checkpoints
             where dataset=? and source_id=? and status='succeeded'
               and partition_key like 'balance-sheet:%'
            """,
            (BALANCE_SHEET_DATASET, BALANCE_SHEET_SOURCE_ID),
        ).fetchone()[0]
    assert (revisions, checkpoints) == (3, 3)


def test_api_routes_and_ui_controls_are_real(monkeypatch) -> None:
    expected = {
        "schema_version": BALANCE_SHEET_HISTORY_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "items": [],
        "coverage": {"is_complete": False},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_balance_sheet_history",
        lambda *args, **kwargs: expected,
    )
    monkeypatch.setattr(
        "stock_ai.main.backfill_balance_sheet_history",
        lambda *args, **kwargs: {**expected, "sync": {"status": "succeeded"}},
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/balance-sheet/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q3"
    )
    compatibility = client.get(
        "/api/fundamentals/balance-sheet/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q3"
    )
    sync = client.post(
        "/api/data/ui/v1/fundamentals/balance-sheet/history/sync",
        json={
            "symbol": "2330.TW",
            "start_period": "2025-Q1",
            "end_period": "2025-Q3",
        },
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
        "balanceSheetSymbol",
        "balanceSheetStart",
        "balanceSheetEnd",
        "loadBalanceSheetHistoryBtn",
        "syncBalanceSheetHistoryBtn",
        "balanceSheetHistoryStatus",
        "balanceSheetHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/balance-sheet/history/sync" in javascript
    assert "quarter_comparison" in javascript
