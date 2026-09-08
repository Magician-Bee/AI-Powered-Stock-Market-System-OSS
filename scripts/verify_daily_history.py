from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import time


def _fixture(count: int):
    from stock_ai.models import PricePoint

    points = []
    current = date(2001, 1, 2)
    while len(points) < count:
        if current.weekday() < 5:
            index = len(points)
            close = 50 + index / 100
            points.append(
                PricePoint(
                    date=current.isoformat(),
                    open=close - 0.2,
                    high=close + 0.5,
                    low=close - 0.5,
                    close=close,
                    volume=1_000_000 + index,
                    turnover=(1_000_000 + index) * close,
                )
            )
        current += timedelta(days=1)
    return points


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stock003-daily-history-") as temp_dir:
        os.environ["STOCK_AI_MARKET_DATA_DB"] = str(Path(temp_dir) / "market.sqlite")

        import stock_ai.data_platform
        from fastapi.testclient import TestClient
        from stock_ai import daily_history
        from stock_ai.data_platform.service import get_market_data_platform
        from stock_ai.main import app

        get_market_data_platform.cache_clear()
        started = time.perf_counter()
        points = _fixture(6_200)
        start, end = points[0].date, points[-1].date
        months = daily_history._months_between(start, end)
        coverage = [
            {"month": month, "status": "succeeded", "row_count": 1, "error": None}
            for month in months
        ]
        daily_history.twse_history_range = lambda *_args: (points, coverage)
        print("fixture_ready", flush=True)

        first = daily_history.query_daily_history(
            "1234.TW",
            start=start,
            end=end,
            limit=5000,
            allow_fallback=False,
        )
        print(f"first_page_ready {time.perf_counter() - started:.3f}s", flush=True)
        second = daily_history.query_daily_history(
            "1234.TW",
            start=start,
            end=end,
            cursor=first["next_cursor"],
            limit=5000,
            refresh=False,
            allow_fallback=False,
        )
        print(f"second_page_ready {time.perf_counter() - started:.3f}s", flush=True)
        combined = [*first["points"], *second["points"]]
        assert len(combined) == 6_200
        assert [point.date for point in combined] == [point.date for point in points]
        assert first["total_point_count"] == 6_200
        assert first["point_count"] == 5_000
        assert second["point_count"] == 1_200
        assert first["range_complete"] is True
        assert first["turnover_complete"] is True

        entity_id = get_market_data_platform().resolve_entity(
            "1234.TW", identifier_type="display_symbol"
        )["entity"]["entity_id"]
        conflicting_fallback = points[0].model_copy(
            update={"close": 9999, "turnover": None}
        )
        daily_history._persist_points(
            symbol="1234.TW",
            entity_id=entity_id,
            source_id="yahoo_finance",
            points=[conflicting_fallback],
            is_fallback=True,
            requested_start=start,
            requested_end=end,
        )
        print(f"fallback_revision_ready {time.perf_counter() - started:.3f}s", flush=True)
        selected = daily_history.query_daily_history(
            "1234.TW",
            start=start,
            end=end,
            limit=1,
            refresh=False,
        )
        assert selected["points"][0].close == points[0].close
        assert selected["source_ids"] == ["twse_official_web"]

        revised = points[0].model_copy(update={"close": points[0].close + 0.25})
        daily_history._persist_points(
            symbol="1234.TW",
            entity_id=entity_id,
            source_id="twse_official_web",
            points=[revised],
            is_fallback=False,
            requested_start=start,
            requested_end=end,
        )
        print(f"official_revision_ready {time.perf_counter() - started:.3f}s", flush=True)
        revisions = get_market_data_platform().warehouse.revision_history(
            dataset="prices_daily",
            entity_id=entity_id,
            observation_key=points[0].date,
            source_id="twse_official_web",
        )
        assert revisions["count"] == 2
        assert revisions["status"] == "passed"

        client = TestClient(app)
        api_first = client.get(
            "/api/data/ui/v1/market/1234.TW/history",
            params={
                "start": start,
                "end": end,
                "limit": 5000,
                "refresh": "false",
            },
        )
        assert api_first.status_code == 200
        api_payload = api_first.json()
        assert api_payload["total_point_count"] == 6_200
        assert api_payload["has_more"] is True
        assert api_payload["points"][0]["turnover"] is not None
        print(f"api_ready {time.perf_counter() - started:.3f}s", flush=True)

        print(
            json.dumps(
                {
                    "schema_version": first["schema_version"],
                    "requested_range": [start, end],
                    "month_count": len(months),
                    "total_point_count": len(combined),
                    "page_counts": [first["point_count"], second["point_count"]],
                    "range_complete": first["range_complete"],
                    "turnover_complete": first["turnover_complete"],
                    "source_priority": "official_over_fallback",
                    "immutable_revision_count": revisions["count"],
                    "unified_api_status": api_first.status_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        get_market_data_platform.cache_clear()


if __name__ == "__main__":
    main()
