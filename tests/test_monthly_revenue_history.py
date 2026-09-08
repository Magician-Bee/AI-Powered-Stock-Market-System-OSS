from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from open_stock_ai.agent_runtime import ExternalTransportAdmissionError
from stock_ai.main import app
from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.phase1_data import list_institutional_flows, list_monthly_revenues
from stock_ai.monthly_revenue import (
    MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION,
    MonthlyRevenueArchivePage,
    MonthlyRevenueHistoryError,
    archive_periods,
    archive_url,
    backfill_monthly_revenue_history,
    iter_periods,
    parse_archive_html,
    query_monthly_revenue_history,
)


client = TestClient(app)


def _archive_html(*, current: str = "442,679,969") -> bytes:
    return f"""
    <html><head><meta charset="big5"></head><body>
      <table><tr><th>產業別：半導體業</th><th>單位：千元</th></tr>
        <tr><td colspan="2"><table>
          <tr><th>公司<br>代號</th><th>公司名稱</th><th>當月營收</th>
              <th>上月營收</th><th>去年當月營收</th><th>上月比較<br>增減(%)</th>
              <th>去年同月<br>增減(%)</th><th>當月累計營收</th>
              <th>去年累計營收</th><th>前期比較<br>增減(%)</th><th>備註</th></tr>
          <tr><td>2330</td><td>台積電</td><td>{current}</td><td>416,975,163</td>
              <td>263,708,978</td><td>6.16</td><td>67.87</td>
              <td>2,404,483,690</td><td>1,773,045,533</td><td>35.61</td>
              <td>因先進製程產品需求增加所致。</td></tr>
          <tr><td>2331</td><td>測試公司</td><td>100</td><td></td><td>-</td>
              <td>-</td><td>-</td><td>100</td><td>-</td><td>-</td><td>-</td></tr>
        </table></td></tr>
      </table>
    </body></html>
    """.encode("cp950")


def test_mops_archive_parser_preserves_official_history_and_nulls() -> None:
    items = parse_archive_html(
        _archive_html(),
        period="2026-06",
        market_segment="sii",
        source_url="https://mopsov.twse.com.tw/example.html",
        acquired_at="2026-07-29T08:00:00+00:00",
    )
    assert len(items) == 2
    tsmc = items[0]
    assert tsmc.symbol == "2330.TW"
    assert tsmc.industry == "半導體業"
    assert tsmc.current_revenue == 442_679_969
    assert tsmc.mom_change_percent == 6.16
    assert tsmc.yoy_change_percent == 67.87
    assert tsmc.ytd_revenue == 2_404_483_690
    assert tsmc.ytd_change_percent == 35.61
    assert tsmc.unit == "thousand_twd"
    assert tsmc.report_date is None
    assert tsmc.report_date_semantics is None
    assert tsmc.publication_time_status == "not_provided_by_archive"
    assert items[1].previous_revenue is None
    assert items[1].last_year_revenue is None
    assert items[1].yoy_change_percent is None


def test_current_openapi_export_date_is_not_claimed_as_publication_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = MarketDataPlatform(database_path=tmp_path / "current.sqlite")
    monkeypatch.setattr(
        "stock_ai.phase1_data.get_market_data_platform",
        lambda: platform,
    )
    monkeypatch.setattr(
        "stock_ai.phase1_data._fetch_twse_openapi_rows",
        lambda _path: [
            {
                "出表日期": "1150729",
                "資料年月": "11506",
                "公司代號": "2330",
                "公司名稱": "台積電",
                "產業別": "半導體業",
                "營業收入-當月營收": "442679969",
                "營業收入-上月營收": "416975163",
                "營業收入-去年當月營收": "263708978",
                "營業收入-上月比較增減(%)": "6.16",
                "營業收入-去年同月增減(%)": "67.87",
                "累計營業收入-當月累計營收": "2404483690",
                "累計營業收入-去年累計營收": "1773045533",
                "累計營業收入-前期比較增減(%)": "35.61",
                "備註": "",
            }
        ],
    )
    item = list_monthly_revenues(symbol="2330.TW", limit=1)[0]
    assert item.report_date == "2026-07-29"
    assert item.report_date_semantics == "openapi_export_date"
    assert item.published_at is None
    assert item.available_at == item.acquired_at
    assert item.publication_time_status == "not_provided_by_current_openapi"


def test_whole_market_scanner_can_use_persisted_factor_data_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stock_ai.phase1_data._read_persisted", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "stock_ai.phase1_data._fetch_twse_openapi_rows",
        lambda _path: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )
    monkeypatch.setattr(
        "stock_ai.phase1_data._get_json",
        lambda _url: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )

    assert list_monthly_revenues(limit=5_000, allow_network=False) == []
    assert list_institutional_flows(limit=5_000, allow_network=False) == []


def test_institutional_flow_uses_persisted_data_when_transport_admission_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = [object()]
    monkeypatch.setattr(
        "stock_ai.phase1_data._get_json",
        lambda _url: (_ for _ in ()).throw(
            ExternalTransportAdmissionError(
                "external transport rate limit blocked source:twse:flow: maximum_concurrency_reached"
            )
        ),
    )
    monkeypatch.setattr(
        "stock_ai.phase1_data._read_persisted",
        lambda *_args, **_kwargs: cached,
    )

    assert list_institutional_flows(symbol="2330.TW", limit=1) == cached


def test_monthly_revenue_period_range_and_archive_url_are_explicit() -> None:
    assert iter_periods("2025-11", "2026-02") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]
    assert archive_url("2026-06", "sii") == (
        "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_115_6_0.html"
    )
    with pytest.raises(MonthlyRevenueHistoryError, match="end_period"):
        iter_periods("2026-02", "2026-01")
    with pytest.raises(MonthlyRevenueHistoryError, match="YYYY-MM"):
        iter_periods("2026-13", "2026-13")
    with pytest.raises(MonthlyRevenueHistoryError, match="starts at 2010-01"):
        archive_periods("2009-12", "2010-01")


def test_backfill_is_incremental_queryable_and_preserves_raw_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "monthly-revenue.sqlite"
    platform = MarketDataPlatform(database_path=database_path)

    def fake_fetch(period, *, market_segment, symbol=None, timeout=None):
        body = _archive_html(current=str(100 + int(period[-2:])))
        source_url = archive_url(period, market_segment)
        items = parse_archive_html(
            body,
            period=period,
            market_segment=market_segment,
            source_url=source_url,
            acquired_at="2026-07-28T08:00:00+00:00",
            symbol=symbol,
        )
        return MonthlyRevenueArchivePage(
            period=period,
            market_segment=market_segment,
            source_url=source_url,
            acquired_at="2026-07-28T08:00:00+00:00",
            content_type="text/html",
            body=body,
            items=items,
        )

    monkeypatch.setattr(
        "stock_ai.monthly_revenue.fetch_archive_period",
        fake_fetch,
    )
    first = backfill_monthly_revenue_history(
        "2330.TW",
        start_period="2026-04",
        end_period="2026-06",
        platform=platform,
    )
    second = backfill_monthly_revenue_history(
        "2330.TW",
        start_period="2026-04",
        end_period="2026-06",
        platform=platform,
    )
    assert first["schema_version"] == MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION
    assert first["sync"]["fetched_period_count"] == 3
    assert first["sync"]["skipped_period_count"] == 0
    assert first["coverage"]["is_complete"] is True
    assert [item["period"] for item in first["items"]] == [
        "2026-06",
        "2026-05",
        "2026-04",
    ]
    assert second["sync"]["fetched_period_count"] == 0
    assert second["sync"]["skipped_period_count"] == 3

    stored = query_monthly_revenue_history(
        "2330.TW",
        start_period="2026-04",
        end_period="2026-06",
        platform=platform,
    )
    assert stored["count"] == 3
    assert stored["source_ids"] == ["mops_archive"]

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
    after_registry_refresh = query_monthly_revenue_history(
        "2330.TW",
        start_period="2026-04",
        end_period="2026-06",
        platform=platform,
    )
    assert after_registry_refresh["entity_id"] == (
        "ENT-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert len(after_registry_refresh["storage_entity_ids"]) == 2
    assert after_registry_refresh["coverage"]["is_complete"] is True
    assert after_registry_refresh["count"] == 3
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "select count(*) from financial_facts where dataset='revenues_monthly'"
        ).fetchone()[0] == 3
        assert conn.execute(
            """
            select count(*) from data_ingestion_checkpoints
             where source_id='mops_archive' and dataset='revenues_monthly'
               and partition_key like 'sii:%:2330.TW'
               and status='succeeded'
            """
        ).fetchone()[0] == 3
        assert conn.execute(
            """
            select count(*) from raw_data_objects
             where source_id='mops_archive'
            """
        ).fetchone()[0] == 3
        provenance = conn.execute(
            """
            select field_provenance_json from data_revisions
             where dataset='revenues_monthly'
             order by observation_key desc limit 1
            """
        ).fetchone()[0]
        assert '"/0/current_revenue"' in provenance


def test_monthly_revenue_history_api_routes_validate_and_return_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "schema_version": MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "items": [],
        "coverage": {"is_complete": False},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_monthly_revenue_history",
        lambda *args, **kwargs: expected,
    )
    monkeypatch.setattr(
        "stock_ai.main.backfill_monthly_revenue_history",
        lambda *args, **kwargs: {**expected, "sync": {"status": "succeeded"}},
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/revenue/history"
        "?symbol=2330.TW&start_period=2026-01&end_period=2026-06"
    )
    compatibility = client.get(
        "/api/fundamentals/revenue/history"
        "?symbol=2330.TW&start_period=2026-01&end_period=2026-06"
    )
    sync = client.post(
        "/api/data/ui/v1/fundamentals/revenue/history/sync",
        json={
            "symbol": "2330.TW",
            "start_period": "2026-01",
            "end_period": "2026-06",
        },
    )
    assert unified.status_code == compatibility.status_code == sync.status_code == 200
    assert unified.json() == compatibility.json() == expected
    assert sync.json()["sync"]["status"] == "succeeded"


def test_monthly_revenue_history_ui_has_real_sync_and_provenance_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")
    javascript = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text(encoding="utf-8")
    for identifier in (
        "monthlyRevenueSymbol",
        "monthlyRevenueStart",
        "monthlyRevenueEnd",
        "loadMonthlyRevenueHistoryBtn",
        "syncMonthlyRevenueHistoryBtn",
        "monthlyRevenueHistoryStatus",
        "monthlyRevenueHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/revenue/history/sync" in javascript
    assert "item.source_url" in javascript
    assert "monthlyRevenueMetric" in javascript
    assert "monthlyRevenuePercent" in javascript
