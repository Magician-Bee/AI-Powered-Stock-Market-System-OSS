from __future__ import annotations

from datetime import date, timedelta
import sqlite3

import pytest

from stock_ai.data_platform.service import get_market_data_platform
from stock_ai.models import PricePoint


@pytest.fixture()
def isolated_adjustment_platform(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "STOCK_AI_MARKET_DATA_DB",
        str(tmp_path / "adjustments.sqlite"),
    )
    get_market_data_platform.cache_clear()
    yield
    get_market_data_platform.cache_clear()


def _raw_points() -> list[PricePoint]:
    closes = [100.0, 110.0, 88.0, 90.0]
    first = date(2024, 1, 1)
    return [
        PricePoint(
            date=(first + timedelta(days=index)).isoformat(),
            open=close - 1,
            high=close + 2,
            low=close - 2,
            close=close,
            volume=1_000 + index,
            turnover=(1_000 + index) * close,
        )
        for index, close in enumerate(closes)
    ]


def _raw_coverage() -> list[dict]:
    return [
        {
            "month": "202401",
            "status": "succeeded",
            "row_count": 4,
            "error": None,
        }
    ]


def _fetched_adjustment(*, status: str = "succeeded") -> dict:
    event = {
        "event_date": "2024-01-03",
        "symbol": "1234.TW",
        "name": "測試公司",
        "event_type": "息",
        "previous_close": 110.0,
        "reference_price": 88.0,
        "theoretical_reference_price": 88.0,
        "adjustment_value": 22.0,
        "event_ratio": 0.8,
        "source_id": "twse_official_web",
        "dataset_id": "twse_exright_results",
        "factor_method": "official_reference_price_ratio",
        "source_record": ["113年01月03日", "1234"],
    }
    return {
        "symbol": "1234.TW",
        "source_id": "twse_official_web",
        "dataset_id": "twse_exright_results",
        "supported_start": "2003-05-05",
        "coverage": [
            {
                "year": 2024,
                "status": status,
                "row_count": 1 if status == "succeeded" else 0,
                "upstream_row_count": 1 if status == "succeeded" else 0,
                "error": None if status == "succeeded" else "TimeoutError",
            }
        ],
        "events": [event] if status == "succeeded" else [],
        "source_payloads": [
            {
                "request": {"method": "GET", "url": "https://example.test"},
                "response": {"stat": "OK", "data": [event["source_record"]]},
            }
        ]
        if status == "succeeded"
        else [],
    }


def _prepare(monkeypatch, *, adjustment_status: str = "succeeded") -> None:
    from stock_ai import daily_history, price_adjustments

    monkeypatch.setattr(
        daily_history,
        "twse_history_range",
        lambda *_args: (_raw_points(), _raw_coverage()),
    )
    monkeypatch.setattr(
        price_adjustments,
        "fetch_official_adjustment_events",
        lambda *_args, **_kwargs: _fetched_adjustment(
            status=adjustment_status
        ),
    )


def test_front_and_back_adjusted_prices_are_persisted_with_factors(
    isolated_adjustment_platform,
    monkeypatch,
):
    from stock_ai.daily_history import query_daily_history

    _prepare(monkeypatch)
    front = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="forward_adjusted",
        allow_fallback=False,
    )
    assert [point.close for point in front["points"]] == [
        80.0,
        88.0,
        88.0,
        90.0,
    ]
    assert [point.adjustment_factor for point in front["points"]] == [
        0.8,
        0.8,
        1.0,
        1.0,
    ]
    assert front["adjustment_complete"] is True
    assert front["adjustment_event_count"] == 1
    assert front["factor_source_id"] == "twse_official_web"
    assert front["volume_basis"] == "raw_shares"
    assert front["points"][0].raw_close == 100.0
    assert front["points"][0].backward_adjusted_close == 100.0
    assert front["points"][-1].backward_adjusted_close == 112.5

    back = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="backward_adjusted",
        refresh=False,
        refresh_adjustments=False,
        allow_fallback=False,
    )
    assert [point.close for point in back["points"]] == [
        100.0,
        110.0,
        110.0,
        112.5,
    ]
    assert [point.adjustment_factor for point in back["points"]] == [
        1.0,
        1.0,
        1.25,
        1.25,
    ]
    assert back["factor_set_id"] == front["factor_set_id"]
    for forward_point, backward_point in zip(front["points"], back["points"]):
        assert forward_point.raw_close == pytest.approx(
            forward_point.close / forward_point.adjustment_factor
        )
        assert backward_point.raw_close == pytest.approx(
            backward_point.close / backward_point.adjustment_factor
        )
        assert forward_point.raw_close == pytest.approx(backward_point.raw_close)

    platform = get_market_data_platform()
    entity_id = platform.resolve_entity(
        "1234.TW", identifier_type="display_symbol"
    )["entity"]["entity_id"]
    revisions = platform.query(
        dataset="prices_adjusted_daily",
        entity_id=entity_id,
        limit=20,
    )
    assert len(revisions) == 4
    assert all(
        {
            "unadjusted",
            "forward_adjusted",
            "backward_adjusted",
            "forward_factor",
            "backward_factor",
        }.issubset(revision.payload)
        for revision in revisions
    )


def test_adjusted_projection_is_immutable_and_idempotent(
    isolated_adjustment_platform,
    monkeypatch,
):
    from stock_ai.daily_history import query_daily_history

    _prepare(monkeypatch)
    first = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="forward_adjusted",
        allow_fallback=False,
    )
    second = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="forward_adjusted",
        refresh=False,
        refresh_adjustments=False,
        allow_fallback=False,
    )
    assert first["factor_set_id"] == second["factor_set_id"]

    platform = get_market_data_platform()
    entity_id = platform.resolve_entity(
        "1234.TW", identifier_type="display_symbol"
    )["entity"]["entity_id"]
    revisions = platform.query(
        dataset="prices_adjusted_daily",
        entity_id=entity_id,
        limit=20,
    )
    assert len(revisions) == 4
    with platform.warehouse._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                update market_prices set quality_status='warning'
                 where dataset='prices_adjusted_daily'
                """
            )


def test_adjusted_pagination_and_backtest_basis_are_explicit(
    isolated_adjustment_platform,
    monkeypatch,
):
    from stock_ai.daily_history import query_daily_history
    from stock_ai.price_adjustments import backtest_price_series

    _prepare(monkeypatch)
    first = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="forward_adjusted",
        limit=2,
        allow_fallback=False,
    )
    second = query_daily_history(
        "1234.TW",
        start="2024-01-01",
        end="2024-01-04",
        price_basis="forward_adjusted",
        cursor=first["next_cursor"],
        limit=2,
        refresh=False,
        refresh_adjustments=False,
        allow_fallback=False,
    )
    assert [point.date for point in [*first["points"], *second["points"]]] == [
        point.date for point in _raw_points()
    ]
    assert first["total_point_count"] == second["total_point_count"] == 4

    entity_id = get_market_data_platform().resolve_entity(
        "1234.TW", identifier_type="display_symbol"
    )["entity"]["entity_id"]
    dataset = backtest_price_series(
        symbol="1234.TW",
        entity_id=entity_id,
        start="2024-01-01",
        end="2024-01-04",
        price_basis="backward_adjusted",
        refresh=False,
    )
    assert dataset["price_basis"] == "backward_adjusted"
    assert dataset["adjustment_complete"] is True
    assert dataset["factor_set_id"] == first["factor_set_id"]
    assert dataset["total_point_count"] == 4
    assert dataset["points"][-1].close == 112.5


def test_incomplete_official_factor_coverage_fails_closed(
    isolated_adjustment_platform,
    monkeypatch,
):
    from stock_ai.daily_history import DailyHistoryQueryError, query_daily_history

    _prepare(monkeypatch, adjustment_status="failed")
    with pytest.raises(
        DailyHistoryQueryError,
        match="official adjustment factor coverage is incomplete",
    ):
        query_daily_history(
            "1234.TW",
            start="2024-01-01",
            end="2024-01-04",
            price_basis="forward_adjusted",
            allow_fallback=False,
        )


def test_twse_and_tpex_official_adjustment_parsers_use_reference_ratios():
    from stock_ai.price_adjustments import (
        parse_tpex_adjustment_payload,
        parse_twse_adjustment_payload,
    )

    twse = parse_twse_adjustment_payload(
        {
            "stat": "OK",
            "data": [
                [
                    "114年07月01日",
                    "1234",
                    "測試",
                    "100.00",
                    "94.99",
                    "5.000000",
                    "息",
                    "104.00",
                    "85.00",
                    "95.00",
                    "94.99",
                ]
            ],
        },
        code="1234",
    )
    assert twse[0]["event_date"] == "2025-07-01"
    assert twse[0]["theoretical_reference_price"] == 95.0
    assert twse[0]["event_ratio"] == 0.95

    tpex = parse_tpex_adjustment_payload(
        {
            "stat": "ok",
            "tables": [
                {
                    "data": [
                        [
                            "114/07/01",
                            "5678",
                            "測試",
                            "200.00",
                            "180.00",
                            "15.000000",
                            "5.000000",
                            "20.000000",
                            "除權息",
                        ]
                    ]
                }
            ],
        },
        code="5678",
    )
    assert tpex[0]["event_date"] == "2025-07-01"
    assert tpex[0]["theoretical_reference_price"] == 180.0
    assert tpex[0]["event_ratio"] == 0.9


def test_adjusted_api_and_ui_expose_all_three_price_bases(
    isolated_adjustment_platform,
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from stock_ai.main import app

    _prepare(monkeypatch)
    with TestClient(app) as client:
        response = client.get(
            "/api/data/ui/v1/market/1234.TW/history",
            params={
                "start": "2024-01-01",
                "end": "2024-01-04",
                "price_basis": "forward_adjusted",
            },
        )
        invalid = client.get(
            "/api/data/ui/v1/market/1234.TW/history",
            params={
                "start": "2024-01-01",
                "end": "2024-01-04",
                "price_basis": "invented",
            },
        )
        html = client.get("/").text

    assert response.status_code == 200
    payload = response.json()
    assert payload["price_basis"] == "forward_adjusted"
    assert payload["adjustment_complete"] is True
    assert payload["points"][0]["raw_close"] == 100.0
    assert payload["points"][0]["close"] == 80.0
    assert invalid.status_code == 422
    assert 'id="dailyHistoryPriceBasis"' in html
    assert '<option value="unadjusted">原始未復權</option>' in html
    assert '<option value="forward_adjusted">前復權</option>' in html
    assert '<option value="backward_adjusted">後復權</option>' in html
