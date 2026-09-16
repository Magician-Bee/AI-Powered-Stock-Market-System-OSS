from __future__ import annotations

from pathlib import Path
import sqlite3
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

import stock_ai.income_statement as income_statement_module
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.income_statement import (
    INCOME_STATEMENT_DATASET,
    INCOME_STATEMENT_HISTORY_SCHEMA_VERSION,
    INCOME_STATEMENT_SOURCE_ID,
    IncomeStatementArchivePage,
    IncomeStatementHistoryError,
    archive_periods,
    archive_url,
    backfill_income_statement_history,
    fetch_archive_period,
    iter_periods,
    parse_archive_html,
    query_income_statement_history,
)
from stock_ai.main import app


client = TestClient(app)


def _archive_html(
    *,
    period: str = "2023-Q2",
    revenue: str = "989,474,227",
) -> bytes:
    quarter = int(period[-1])
    if quarter in {2, 3}:
        heading = (
            "<tr><th>會計項目</th><th>本期單季</th><th>上期單季</th>"
            "<th>本期累計</th><th>上期累計</th></tr>"
        )

        def row(label: str, quarter_value: str, cumulative: str) -> str:
            return (
                f"<tr><td>{label}</td><td>{quarter_value}</td><td></td>"
                f"<td>1</td><td></td><td>{cumulative}</td><td></td>"
                "<td>1</td><td></td></tr>"
            )
    else:
        heading = "<tr><th>會計項目</th><th>本期</th><th>上期</th></tr>"

        def row(label: str, quarter_value: str, cumulative: str) -> str:
            return (
                f"<tr><td>{label}</td><td>{cumulative}</td><td></td>"
                "<td>1</td><td></td></tr>"
            )

    return f"""
    <html><body>
      <h2>合併綜合損益表</h2>
      <h4>本資料由台積電公司提供</h4>
      <table class="hasBorder">
        <tr><td>民國112年第{quarter}季</td></tr>
        <tr><td>單位：新台幣仟元</td></tr>
        {heading}
        {row("營業收入合計", "480,841,254", revenue)}
        {row("營業毛利（毛損）淨額", "260,199,847", "546,700,239")}
        {row("營業利益（損失）", "201,958,043", "433,196,200")}
        {row("本期淨利（淨損）", "181,717,006", "388,666,042")}
        {row("母公司業主（淨利∕損）", "181,799,021", "388,785,582")}
        <tr><td>基本每股盈餘</td><td></td><td></td><td></td><td></td></tr>
        {row("基本每股盈餘", "7.01", "14.99")}
      </table>
    </body></html>
    """.encode("utf-8")


def _parse(
    body: bytes,
    *,
    period: str,
):
    return parse_archive_html(
        body,
        period=period,
        market_segment="sii",
        symbol="2330.TW",
        source_url=archive_url(period, "sii", "2330.TW"),
        request_parameters={
            "TYPEK": "sii",
            "co_id": "2330",
            "year": str(int(period[:4]) - 1911),
            "season": f"0{period[-1]}",
        },
        acquired_at="2026-07-29T08:00:00+00:00",
    )


def test_parser_preserves_reported_and_single_quarter_values() -> None:
    item = _parse(_archive_html(), period="2023-Q2")
    assert item.symbol == "2330.TW"
    assert item.name == "台積電"
    assert item.statement_scope == "合併綜合損益表"
    assert item.statement_semantics == "year_to_date_cumulative"
    assert item.revenue == 989_474_227
    assert item.gross_profit == 546_700_239
    assert item.operating_income == 433_196_200
    assert item.net_income == 388_785_582
    assert item.net_income_basis == "attributable_to_parent"
    assert item.eps == 14.99
    assert item.current_quarter_revenue == 480_841_254
    assert item.current_quarter_gross_profit == 260_199_847
    assert item.current_quarter_operating_income == 201_958_043
    assert item.current_quarter_net_income == 181_799_021
    assert item.current_quarter_eps == 7.01
    assert item.published_at is None
    assert item.available_at == item.acquired_at
    assert item.publication_time_status == "not_provided_by_archive"


def test_q4_does_not_invent_single_quarter_or_eps() -> None:
    item = _parse(
        _archive_html(period="2023-Q4", revenue="2,161,735,841"),
        period="2023-Q4",
    )
    assert item.revenue == 2_161_735_841
    assert item.eps == 14.99
    assert item.current_quarter_revenue is None
    assert item.current_quarter_net_income is None
    assert item.current_quarter_eps is None
    assert item.current_quarter_status == "not_disclosed_in_annual_summary"


def test_period_range_is_explicit_and_covers_more_than_ten_years() -> None:
    periods = iter_periods("2013-Q1", "2023-Q4")
    assert len(periods) == 44
    assert periods[:2] == ["2013-Q1", "2013-Q2"]
    assert periods[-2:] == ["2023-Q3", "2023-Q4"]
    assert archive_url("2023-Q2", "sii", "2330.TW") == (
        "https://mopsov.twse.com.tw/mops/web/ajax_t164sb04"
        "?TYPEK=sii&co_id=2330&year=112&season=02"
    )
    with pytest.raises(IncomeStatementHistoryError, match="end_period"):
        iter_periods("2024-Q1", "2023-Q4")
    with pytest.raises(IncomeStatementHistoryError, match="YYYY-Q1"):
        iter_periods("2023-02", "2023-Q4")
    with pytest.raises(IncomeStatementHistoryError, match="starts at 2013-Q1"):
        archive_periods("2012-Q4", "2013-Q1")


def test_fetch_retries_current_mops_host_when_archive_host_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_urls: list[str] = []

    class FakeResponse:
        headers = {"Content-Type": "text/html; charset=UTF-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self) -> bytes:
            return _archive_html(period="2025-Q4")

    def fake_urlopen(request, *, timeout):
        attempted_urls.append(request.full_url)
        if len(attempted_urls) == 1:
            raise HTTPError(
                request.full_url,
                307,
                "Temporary Redirect",
                {},
                None,
            )
        return FakeResponse()

    monkeypatch.setattr(income_statement_module, "urlopen", fake_urlopen)
    page = fetch_archive_period(
        "2025-Q4",
        market_segment="sii",
        symbol="2330.TW",
    )

    assert attempted_urls == [
        "https://mopsov.twse.com.tw/mops/web/ajax_t164sb04",
        "https://mops.twse.com.tw/mops/web/ajax_t164sb04",
    ]
    assert page.item.period == "2025-Q4"
    assert page.source_url.startswith("https://mopsov.twse.com.tw/")


def test_backfill_is_incremental_queryable_and_preserves_raw_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "income-statement.sqlite"
    platform = MarketDataPlatform(database_path=database_path)

    def fake_fetch(period, *, market_segment, symbol, timeout=None):
        body = _archive_html(period=period, revenue=f"{100 + int(period[-1])}")
        request_parameters = {
            "TYPEK": market_segment,
            "co_id": "2330",
            "year": str(int(period[:4]) - 1911),
            "season": f"0{period[-1]}",
        }
        source_url = archive_url(period, market_segment, symbol)
        item = parse_archive_html(
            body,
            period=period,
            market_segment=market_segment,
            symbol=symbol,
            source_url=source_url,
            request_parameters=request_parameters,
            acquired_at="2026-07-29T08:00:00+00:00",
        )
        return IncomeStatementArchivePage(
            period=period,
            market_segment=market_segment,
            source_url=source_url,
            request_parameters=request_parameters,
            acquired_at="2026-07-29T08:00:00+00:00",
            content_type="text/html; charset=UTF-8",
            body=body,
            item=item,
        )

    monkeypatch.setattr(
        "stock_ai.income_statement.fetch_archive_period",
        fake_fetch,
    )
    first = backfill_income_statement_history(
        "2330.TW",
        start_period="2023-Q1",
        end_period="2023-Q4",
        platform=platform,
    )
    second = backfill_income_statement_history(
        "2330.TW",
        start_period="2023-Q1",
        end_period="2023-Q4",
        platform=platform,
    )
    assert first["schema_version"] == INCOME_STATEMENT_HISTORY_SCHEMA_VERSION
    assert first["sync"]["fetched_period_count"] == 4
    assert first["coverage"]["is_complete"] is True
    assert second["sync"]["fetched_period_count"] == 0
    assert second["sync"]["skipped_period_count"] == 4
    assert [item["period"] for item in first["items"]] == [
        "2023-Q4",
        "2023-Q3",
        "2023-Q2",
        "2023-Q1",
    ]

    with sqlite3.connect(database_path) as conn:
        revision_count = conn.execute(
            "select count(*) from financial_facts where dataset=? and source_id=?",
            (INCOME_STATEMENT_DATASET, INCOME_STATEMENT_SOURCE_ID),
        ).fetchone()[0]
        raw_count = conn.execute(
            "select count(*) from raw_data_objects where source_id=?",
            (INCOME_STATEMENT_SOURCE_ID,),
        ).fetchone()[0]
        checkpoint_count = conn.execute(
            """
            select count(*) from data_ingestion_checkpoints
             where source_id=? and dataset=? and status='succeeded'
               and partition_key like 'sii:%:2330.TW'
            """,
            (INCOME_STATEMENT_SOURCE_ID, INCOME_STATEMENT_DATASET),
        ).fetchone()[0]
    assert (revision_count, raw_count, checkpoint_count) == (4, 4, 4)


def test_query_reads_pre_registry_storage_identity_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = MarketDataPlatform(database_path=tmp_path / "identity.sqlite")

    def fake_fetch(period, *, market_segment, symbol, timeout=None):
        body = _archive_html(period=period)
        parameters = {
            "TYPEK": market_segment,
            "co_id": "2330",
            "year": "112",
            "season": "02",
        }
        url = archive_url(period, market_segment, symbol)
        return IncomeStatementArchivePage(
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
        fake_fetch,
    )
    backfill_income_statement_history(
        "2330.TW",
        start_period="2023-Q2",
        end_period="2023-Q2",
        platform=platform,
    )
    platform.warehouse.upsert_entity(
        EntityRecord(
            entity_id="ENT-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            entity_type="stock",
            canonical_name="台灣積體電路製造股份有限公司",
            market="taiwan",
            exchange="TWSE",
            currency="TWD",
            lifecycle_status="active",
            metadata={"display_symbol": "2330.TW"},
        ),
        identifiers=[
            {
                "source_id": "twse_openapi",
                "identifier_type": "display_symbol",
                "identifier_value": "2330.TW",
                "valid_from": "1994-09-05T00:00:00+00:00",
                "is_primary": True,
                "metadata": {},
            }
        ],
    )
    restarted = MarketDataPlatform(database_path=platform.warehouse.path)
    history = query_income_statement_history(
        "2330.TW",
        start_period="2023-Q2",
        end_period="2023-Q2",
        platform=restarted,
    )
    assert history["count"] == 1
    assert history["coverage"]["is_complete"] is True
    assert len(history["storage_entity_ids"]) == 2


def test_api_routes_and_ui_controls_are_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "schema_version": INCOME_STATEMENT_HISTORY_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "items": [],
        "coverage": {"is_complete": False},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_income_statement_history",
        lambda *args, **kwargs: expected,
    )
    monkeypatch.setattr(
        "stock_ai.main.backfill_income_statement_history",
        lambda *args, **kwargs: {**expected, "sync": {"status": "succeeded"}},
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/income-statement/history"
        "?symbol=2330.TW&start_period=2013-Q1&end_period=2023-Q4"
    )
    compatibility = client.get(
        "/api/fundamentals/income-statement/history"
        "?symbol=2330.TW&start_period=2013-Q1&end_period=2023-Q4"
    )
    sync = client.post(
        "/api/data/ui/v1/fundamentals/income-statement/history/sync",
        json={
            "symbol": "2330.TW",
            "start_period": "2013-Q1",
            "end_period": "2023-Q4",
        },
    )
    assert unified.status_code == compatibility.status_code == sync.status_code == 200
    assert unified.json() == compatibility.json() == expected
    assert sync.json()["sync"]["status"] == "succeeded"

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")
    javascript = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text(encoding="utf-8")
    for identifier in (
        "incomeStatementSymbol",
        "incomeStatementStart",
        "incomeStatementEnd",
        "loadIncomeStatementHistoryBtn",
        "syncIncomeStatementHistoryBtn",
        "incomeStatementHistoryStatus",
        "incomeStatementHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/income-statement/history/sync" in javascript
    assert "incomeStatementPair" in javascript
    assert "item.source_url" in javascript
