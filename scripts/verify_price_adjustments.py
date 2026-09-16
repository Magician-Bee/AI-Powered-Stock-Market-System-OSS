from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time


def _fixture(count: int):
    from stock_ai.models import PricePoint

    points = []
    current = date(2005, 1, 3)
    while len(points) < count:
        if current.weekday() < 5:
            index = len(points)
            close = Decimal("40") + Decimal(index) / Decimal("100")
            points.append(
                PricePoint(
                    date=current.isoformat(),
                    open=float(close - Decimal("0.2")),
                    high=float(close + Decimal("0.5")),
                    low=float(close - Decimal("0.5")),
                    close=float(close),
                    volume=2_000_000 + index,
                    turnover=float((2_000_000 + index) * close),
                )
            )
        current += timedelta(days=1)
    return points


def _official_fixture(points) -> dict:
    event_specs = (
        (points[2_000].date, Decimal("0.9"), "除息"),
        (points[4_500].date, Decimal("0.8"), "除權息"),
    )
    events = []
    for event_date, ratio, event_type in event_specs:
        previous_close = Decimal("100")
        adjustment_value = previous_close * (Decimal("1") - ratio)
        events.append(
            {
                "event_date": event_date,
                "symbol": "1234.TW",
                "name": "調整因子驗證標的",
                "event_type": event_type,
                "previous_close": float(previous_close),
                "reference_price": float(previous_close - adjustment_value),
                "theoretical_reference_price": float(
                    previous_close - adjustment_value
                ),
                "adjustment_value": float(adjustment_value),
                "event_ratio": float(ratio),
                "source_id": "twse_official_web",
                "dataset_id": "twse_exright_results",
                "factor_method": "official_reference_price_ratio",
                "source_record": {
                    "event_date": event_date,
                    "event_type": event_type,
                    "fixture": True,
                },
            }
        )
    years = range(
        date.fromisoformat(points[0].date).year,
        date.fromisoformat(points[-1].date).year + 1,
    )
    return {
        "symbol": "1234.TW",
        "source_id": "twse_official_web",
        "dataset_id": "twse_exright_results",
        "supported_start": "2003-05-05",
        "coverage": [
            {
                "year": year,
                "status": "succeeded",
                "row_count": sum(
                    date.fromisoformat(item["event_date"]).year == year
                    for item in events
                ),
                "upstream_row_count": sum(
                    date.fromisoformat(item["event_date"]).year == year
                    for item in events
                ),
                "error": None,
            }
            for year in years
        ],
        "events": events,
        "source_payloads": [
            {
                "request": {"fixture": True},
                "response": {"events": events},
            }
        ],
    }


def _query_pages(daily_history, *, basis: str, start: str, end: str):
    first = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        limit=5_000,
        refresh=False,
        allow_fallback=False,
        price_basis=basis,
        refresh_adjustments=False,
    )
    second = daily_history.query_daily_history(
        "1234.TW",
        start=start,
        end=end,
        cursor=first["next_cursor"],
        limit=5_000,
        refresh=False,
        allow_fallback=False,
        price_basis=basis,
        refresh_adjustments=False,
    )
    points = [*first["points"], *second["points"]]
    assert len(points) == 6_200
    assert first["point_count"] == 5_000
    assert second["point_count"] == 1_200
    assert first["adjustment_complete"] is True
    assert first["quality"] == "complete"
    assert first["factor_set_id"] == second["factor_set_id"]
    return first, second, points


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stock004-price-adjustments-") as temp_dir:
        os.environ["STOCK_AI_MARKET_DATA_DB"] = str(
            Path(temp_dir) / "market.sqlite"
        )

        from stock_ai import daily_history, price_adjustments
        from stock_ai.data_platform.service import get_market_data_platform

        get_market_data_platform.cache_clear()
        started = time.perf_counter()
        raw_points = _fixture(6_200)
        start, end = raw_points[0].date, raw_points[-1].date
        months = daily_history._months_between(start, end)
        raw_coverage = [
            {
                "month": month,
                "status": "succeeded",
                "row_count": 1,
                "error": None,
            }
            for month in months
        ]
        fetched = _official_fixture(raw_points)
        daily_history.twse_history_range = lambda *_args: (
            raw_points,
            raw_coverage,
        )
        price_adjustments.fetch_official_adjustment_events = (
            lambda *_args, **_kwargs: fetched
        )

        initial = daily_history.query_daily_history(
            "1234.TW",
            start=start,
            end=end,
            limit=5_000,
            allow_fallback=False,
            price_basis="forward_adjusted",
            refresh_adjustments=True,
        )
        assert initial["total_point_count"] == 6_200
        assert initial["adjustment_complete"] is True
        assert initial["factor_set_id"]
        print(
            f"initial_projection_ready {time.perf_counter() - started:.3f}s",
            flush=True,
        )

        forward_first, forward_second, forward = _query_pages(
            daily_history,
            basis="forward_adjusted",
            start=start,
            end=end,
        )
        backward_first, backward_second, backward = _query_pages(
            daily_history,
            basis="backward_adjusted",
            start=start,
            end=end,
        )
        expected_dates = [point.date for point in raw_points]
        assert [point.date for point in forward] == expected_dates
        assert [point.date for point in backward] == expected_dates
        assert forward_first["factor_set_id"] == backward_first["factor_set_id"]
        assert forward[-1].adjustment_factor == 1.0
        assert forward[-1].close == raw_points[-1].close
        assert backward[0].adjustment_factor == 1.0
        assert backward[0].close == raw_points[0].close
        assert forward[0].adjustment_factor == 0.72
        assert round(backward[-1].adjustment_factor, 12) == round(1 / 0.72, 12)

        platform = get_market_data_platform()
        entity_id = platform.resolve_entity(
            "1234.TW", identifier_type="display_symbol"
        )["entity"]["entity_id"]
        persisted = platform.warehouse.adjusted_price_history(
            entity_id=entity_id,
            source_id="twse_official_web",
            factor_set_id=forward_first["factor_set_id"],
            start_date=start,
            end_date=end,
            limit=1,
        )["items"][0]["record"]
        assert persisted["unadjusted"]["close"] == raw_points[0].close
        assert persisted["forward_adjusted"]["close"] == forward[0].close
        assert persisted["backward_adjusted"]["close"] == backward[0].close
        assert persisted["raw_price_revision_id"]
        assert persisted["volume_basis"] == "raw_shares"
        assert persisted["turnover_basis"] == "raw_twd"

        backtests = {}
        for index, basis in enumerate(
            ("forward_adjusted", "unadjusted", "backward_adjusted")
        ):
            backtest_end = end if basis == "forward_adjusted" else raw_points[99].date
            result = price_adjustments.backtest_price_series(
                symbol="1234.TW",
                entity_id=entity_id,
                start=start,
                end=backtest_end,
                price_basis=basis,
                refresh=index < 2,
            )
            assert result["price_basis"] == basis
            assert result["adjustment_complete"] is True
            assert len(result["points"]) == (
                6_200 if basis == "forward_adjusted" else 100
            )
            assert result["total_point_count"] == len(result["points"])
            backtests[basis] = result["factor_set_id"]

        event_history = platform.warehouse.revision_history(
            dataset="price_adjustment_events",
            entity_id=entity_id,
            observation_key=f"{raw_points[2_000].date}:除息",
            source_id="twse_official_web",
        )
        assert event_history["count"] == 1
        assert event_history["status"] == "passed"

        elapsed = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "schema_version": forward_first[
                        "adjustment_schema_version"
                    ],
                    "raw_point_count": len(raw_points),
                    "page_counts": [
                        forward_first["point_count"],
                        forward_second["point_count"],
                    ],
                    "adjustment_event_count": forward_first[
                        "adjustment_event_count"
                    ],
                    "adjustment_complete": forward_first[
                        "adjustment_complete"
                    ],
                    "factor_set_id": forward_first["factor_set_id"],
                    "forward_anchor": {
                        "date": forward[-1].date,
                        "factor": forward[-1].adjustment_factor,
                    },
                    "backward_anchor": {
                        "date": backward[0].date,
                        "factor": backward[0].adjustment_factor,
                    },
                    "backtest_price_bases": sorted(backtests),
                    "immutable_event_revision_count": event_history["count"],
                    "elapsed_seconds": round(elapsed, 3),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        get_market_data_platform.cache_clear()


if __name__ == "__main__":
    main()
