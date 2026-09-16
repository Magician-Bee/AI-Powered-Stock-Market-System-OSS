from __future__ import annotations

from datetime import date, timedelta

import pytest

from stock_ai.data_platform.service import get_market_data_platform
from stock_ai.models import PricePoint


@pytest.fixture()
def isolated_history_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AI_MARKET_DATA_DB", str(tmp_path / "history.sqlite"))
    get_market_data_platform.cache_clear()
    yield
    get_market_data_platform.cache_clear()


def _points(count: int, *, close_offset: float = 0.0) -> list[PricePoint]:
    first = date(2024, 1, 1)
    return [
        PricePoint(
            date=(first + timedelta(days=index)).isoformat(),
            open=100 + index + close_offset,
            high=102 + index + close_offset,
            low=99 + index + close_offset,
            close=101 + index + close_offset,
            volume=1000 + index,
            turnover=(1000 + index) * (101 + index + close_offset),
        )
        for index in range(count)
    ]


def _coverage(start: str, end: str) -> list[dict]:
    from stock_ai.daily_history import _months_between

    return [
        {"month": month, "status": "succeeded", "row_count": 1, "error": None}
        for month in _months_between(start, end)
    ]


def test_complete_history_is_not_truncated_and_pages_without_gaps(
    isolated_history_platform,
    monkeypatch,
):
    from stock_ai import daily_history

    records = _points(400)
    start, end = records[0].date, records[-1].date
    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda _code, _start, _end: (records, _coverage(start, end)),
    )

    first = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        limit=175,
        allow_fallback=False,
    )
    assert first["total_point_count"] == 400
    assert first["point_count"] == 175
    assert first["has_more"] is True
    assert first["next_cursor"] == records[174].date
    assert first["range_complete"] is True
    assert first["turnover_complete"] is True
    assert first["price_basis"] == "unadjusted"

    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: (_ for _ in ()).throw(AssertionError("refresh must be disabled")),
    )
    second = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        cursor=first["next_cursor"],
        limit=5000,
        refresh=False,
        allow_fallback=False,
    )
    combined = [*first["points"], *second["points"]]
    assert len(combined) == 400
    assert [point.date for point in combined] == [point.date for point in records]
    assert second["has_more"] is False


def test_official_history_wins_without_deleting_fallback_revision(
    isolated_history_platform,
    monkeypatch,
):
    from stock_ai import daily_history

    official = _points(2)
    start, end = official[0].date, official[-1].date
    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: (official, _coverage(start, end)),
    )
    initial = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        allow_fallback=False,
    )
    entity_id = get_market_data_platform().resolve_entity(
        "1234.TW", identifier_type="display_symbol"
    )["entity"]["entity_id"]
    fallback = [
        point.model_copy(update={"close": point.close + 500, "turnover": None})
        for point in official
    ]
    daily_history._persist_points(
        symbol="1234.TW",
        entity_id=entity_id,
        source_id="yahoo_finance",
        points=fallback,
        is_fallback=True,
        requested_start=start,
        requested_end=end,
    )

    result = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        refresh=False,
    )
    assert [point.close for point in result["points"]] == [
        point.close for point in official
    ]
    assert result["source_ids"] == ["twse_official_web"]
    assert result["fallback_count"] == 0
    rows = get_market_data_platform().query(
        dataset="prices_daily",
        entity_id=entity_id,
        limit=20,
    )
    assert {row.source_id for row in rows} == {
        "twse_official_web",
        "yahoo_finance",
    }
    assert initial["range_complete"] is True


def test_official_restatement_creates_revision_and_current_query_uses_it(
    isolated_history_platform,
    monkeypatch,
):
    from stock_ai import daily_history

    first_point = _points(1)[0]
    revised_point = first_point.model_copy(update={"close": first_point.close + 1})
    start = end = first_point.date
    responses = iter(
        [
            ([first_point], _coverage(start, end)),
            ([revised_point], _coverage(start, end)),
        ]
    )
    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: next(responses),
    )
    daily_history.query_daily_history(
        "1234.TW", start=start, end=end, allow_fallback=False
    )
    current = daily_history.query_daily_history(
        "1234.TW", start=start, end=end, allow_fallback=False
    )

    entity_id = get_market_data_platform().resolve_entity(
        "1234.TW", identifier_type="display_symbol"
    )["entity"]["entity_id"]
    revisions = get_market_data_platform().warehouse.revision_history(
        dataset="prices_daily",
        entity_id=entity_id,
        observation_key=start,
        source_id="twse_official_web",
    )
    assert current["points"][0].close == revised_point.close
    assert revisions["count"] == 2
    assert revisions["status"] == "passed"


def test_failed_official_month_is_disclosed_and_fallback_only_fills_real_rows(
    isolated_history_platform,
    monkeypatch,
):
    from stock_ai import daily_history

    official = _points(1)
    fallback = [
        official[0].model_copy(update={"close": 999, "turnover": None}),
        PricePoint(
            date="2024-02-01",
            open=110,
            high=112,
            low=109,
            close=111,
            volume=500,
            turnover=None,
        ),
    ]
    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: (
            official,
            [
                {"month": "202401", "status": "succeeded", "row_count": 1, "error": None},
                {"month": "202402", "status": "failed", "row_count": 0, "error": "TimeoutError"},
            ],
        ),
    )
    monkeypatch.setattr(
        daily_history,
        "_fetch_yahoo_range",
        lambda *_args: fallback,
    )

    result = daily_history.query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-02-29",
    )
    assert result["range_complete"] is False
    assert result["quality"] == "partial"
    assert result["fallback_count"] == 1
    assert result["turnover_complete"] is False
    assert [point.close for point in result["points"]] == [official[0].close, 111]
    assert result["errors"][0]["month"] == "202402"


@pytest.mark.parametrize(
    ("start", "end", "cursor", "message"),
    [
        ("2024-02-01", "2024-01-01", None, "start must not be after end"),
        ("not-a-date", "2024-01-01", None, "start must be an ISO date"),
        ("2024-01-01", "2024-01-31", "2023-12-31", "cursor must be inside"),
    ],
)
def test_invalid_complete_history_ranges_fail_closed(
    isolated_history_platform,
    start,
    end,
    cursor,
    message,
):
    from stock_ai.daily_history import DailyHistoryQueryError, query_daily_history

    with pytest.raises(DailyHistoryQueryError, match=message):
        query_daily_history(
            "1234.TW",
            start=start,
            end=end,
            cursor=cursor,
            refresh=False,
        )


def test_unified_api_and_ui_expose_complete_daily_history_controls(
    isolated_history_platform,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from stock_ai import daily_history
    from stock_ai.main import app

    records = _points(3)
    start, end = records[0].date, records[-1].date
    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: (records, _coverage(start, end)),
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/data/ui/v1/market/1234.TW/history",
            params={"start": start, "end": end, "refresh": "true"},
        )
        invalid = client.get(
            "/api/data/ui/v1/market/1234.TW/history",
            params={"start": end, "end": start, "refresh": "false"},
        )
        html = client.get("/").text

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "stock_ai.daily_history.v1"
    assert payload["range_complete"] is True
    assert payload["total_point_count"] == 3
    assert payload["points"][0]["turnover"] == records[0].turnover
    assert payload["source_ids"] == ["twse_official_web"]
    assert invalid.status_code == 422
    assert 'id="dailyHistoryStart"' in html
    assert 'id="dailyHistoryEnd"' in html
    # Historical K-line requests follow the shared selected-symbol context;
    # keeping a second symbol input here would let the chart and Agent diverge.
    assert 'id="dailyHistorySymbol"' not in html
    assert 'id="symbolSelect"' in html
    assert 'id="loadDailyHistory"' in html
