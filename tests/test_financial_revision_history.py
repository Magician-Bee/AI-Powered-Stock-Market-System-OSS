from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_ai.data_platform.contracts import TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id
from stock_ai.financial_revisions import query_financial_revision_history
from stock_ai.income_statement import query_income_statement_history
from stock_ai.main import app


SYMBOL = "9000.TW"
PERIOD = "2025-Q4"
ENTITY_ID = stable_entity_id(
    market="taiwan",
    exchange="TWSE",
    source_code="9000",
)


def _income_record(
    *,
    revenue: float,
    source_id: str,
    acquired_at: str,
) -> dict[str, object]:
    return {
        "statement_kind": "income_statement",
        "period": PERIOD,
        "fiscal_year": 2025,
        "quarter": 4,
        "symbol": SYMBOL,
        "name": "財報修訂測試公司",
        "statement_scope": "合併綜合損益表",
        "statement_semantics": "year_to_date_cumulative",
        "revenue": revenue,
        "gross_profit": 40.0,
        "operating_income": 30.0,
        "net_income": 20.0,
        "eps": 2.0,
        "current_quarter_revenue": None,
        "current_quarter_gross_profit": None,
        "current_quarter_operating_income": None,
        "current_quarter_net_income": None,
        "current_quarter_eps": None,
        "current_quarter_status": "not_disclosed_in_annual_summary",
        "unit": "thousand_twd",
        "eps_unit": "twd_per_share",
        "source": "MOPS official archive",
        "source_id": source_id,
        "source_url": f"https://mops.twse.com.tw/{source_id}/{PERIOD}",
        "source_method": "POST",
        "source_market": "sii",
        "source_request_parameters": {
            "TYPEK": "sii",
            "co_id": "9000",
            "year": "114",
            "season": "04",
        },
        "raw_field_labels": {"revenue": "營業收入合計"},
        "acquired_at": acquired_at,
        "published_at": None,
        "available_at": acquired_at,
        "publication_time_status": "not_provided_by_archive",
    }


def _write_income_revision(
    platform: MarketDataPlatform,
    *,
    revenue: float,
    source_id: str,
    acquired_at: str,
) -> str:
    revision = platform.warehouse.write_revision(
        dataset="fundamentals_quarterly",
        entity_id=ENTITY_ID,
        observation_key=PERIOD,
        source_id=source_id,
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2025-12",
            period_start="2025-01-01",
            period_end="2025-12-31",
            observed_at="2025-12-31",
            published_at=None,
            available_at=acquired_at,
            acquired_at=acquired_at,
            effective_at="2025-12-31",
        ),
        payload=_income_record(
            revenue=revenue,
            source_id=source_id,
            acquired_at=acquired_at,
        ),
        raw_payload_id=None,
    )
    return revision.revision_id


def _platform_with_restatement(tmp_path: Path) -> tuple[
    MarketDataPlatform,
    str,
    str,
    str,
]:
    platform = MarketDataPlatform(database_path=tmp_path / "financial-revisions.sqlite")
    original_id = _write_income_revision(
        platform,
        revenue=100.0,
        source_id="mops_archive",
        acquired_at="2026-05-11T01:00:00+00:00",
    )
    restated_id = _write_income_revision(
        platform,
        revenue=105.0,
        source_id="mops_archive",
        acquired_at="2026-05-21T01:00:00+00:00",
    )
    parallel_id = _write_income_revision(
        platform,
        revenue=999.0,
        source_id="mops",
        acquired_at="2026-05-15T01:00:00+00:00",
    )
    return platform, original_id, restated_id, parallel_id


def test_revision_diff_and_point_in_time_selection_do_not_look_ahead(
    tmp_path: Path,
) -> None:
    platform, original_id, restated_id, parallel_id = _platform_with_restatement(
        tmp_path
    )

    early = query_financial_revision_history(
        SYMBOL,
        statement_kind="income_statement",
        period=PERIOD,
        knowledge_at="2026-05-12T00:00:00+00:00",
        platform=platform,
    )
    late = query_financial_revision_history(
        SYMBOL,
        statement_kind="income_statement",
        period=PERIOD,
        knowledge_at="2026-05-22T00:00:00+00:00",
        platform=platform,
    )

    assert early["version_count"] == 3
    assert early["restatement_count"] == 1
    assert early["point_in_time"]["selected_revision_ids"] == [original_id]
    assert set(late["point_in_time"]["selected_revision_ids"]) == {
        restated_id,
        parallel_id,
    }

    restatement = next(
        item
        for item in late["versions"]
        if item["revision_id"] == restated_id
    )
    assert restatement["comparison_status"] == "restated"
    assert restatement["supersedes_revision_id"] == original_id
    revenue_change = next(
        change
        for change in restatement["changes"]
        if change["field"] == "revenue"
    )
    assert revenue_change == {
        "field": "revenue",
        "before": 100.0,
        "after": 105.0,
        "absolute_change": 5.0,
        "percent_change": 5.0,
    }
    parallel = next(
        item
        for item in late["versions"]
        if item["revision_id"] == parallel_id
    )
    assert parallel["comparison_status"] == "original"
    assert parallel["changes"] == []


def test_income_statement_history_uses_the_requested_known_version(
    tmp_path: Path,
) -> None:
    platform, _, _, _ = _platform_with_restatement(tmp_path)

    early = query_income_statement_history(
        SYMBOL,
        start_period=PERIOD,
        end_period=PERIOD,
        knowledge_at="2026-05-12T00:00:00+00:00",
        platform=platform,
    )
    late = query_income_statement_history(
        SYMBOL,
        start_period=PERIOD,
        end_period=PERIOD,
        knowledge_at="2026-05-22T00:00:00+00:00",
        platform=platform,
    )

    assert early["items"][0]["revenue"] == 100.0
    assert late["items"][0]["revenue"] == 105.0
    assert early["point_in_time"]["mode"] == "explicit_cutoff"


def test_revision_history_api_has_unified_and_compatibility_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    platform, original_id, _, _ = _platform_with_restatement(tmp_path)
    monkeypatch.setattr(
        "stock_ai.financial_revisions.get_market_data_platform",
        lambda: platform,
    )
    client = TestClient(app)
    params = {
        "symbol": SYMBOL,
        "statement_kind": "income_statement",
        "period": PERIOD,
        "knowledge_at": "2026-05-12T00:00:00+00:00",
    }

    unified = client.get(
        "/api/data/ui/v1/fundamentals/revisions/history",
        params=params,
    )
    compatibility = client.get(
        "/api/fundamentals/revisions/history",
        params=params,
    )

    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json()
    assert unified.json()["point_in_time"]["selected_revision_ids"] == [original_id]


def test_fundamentals_ui_exposes_revision_and_known_at_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    index_html = (
        root / "src/stock_ai/ui/static/index.html"
    ).read_text(encoding="utf-8")
    dashboard_js = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text(encoding="utf-8")

    for control_id in (
        "financialRevisionSymbol",
        "financialRevisionKind",
        "financialRevisionPeriod",
        "financialRevisionKnowledgeAt",
        "loadFinancialRevisionHistoryBtn",
        "financialRevisionHistoryStatus",
        "financialRevisionHistoryTable",
    ):
        assert f'id="{control_id}"' in index_html
    assert "財報修訂與當時版本" in index_html
    assert "/fundamentals/revisions/history?" in dashboard_js
    assert "selected_for_point_in_time" in dashboard_js
    assert "修訂差異只比較同一實體、同一來源" in dashboard_js
