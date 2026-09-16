from __future__ import annotations

from datetime import date, datetime

from zoneinfo import ZoneInfo

from stock_ai.market_calendar import calendar_from_twse_rows, default_taiwan_market_calendar
from stock_ai.realtime_quotes import _trading_status
from stock_ai.schedule_guard import active_schedule_phase


TAIPEI = ZoneInfo("Asia/Taipei")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=TAIPEI)


def test_official_twse_calendar_snapshot_covers_holidays_and_regular_sessions():
    calendar = default_taiwan_market_calendar()

    lunar_settlement = calendar.receipt(_at("2026-02-12T09:30:00"))
    assert lunar_settlement["trading_day"] is False
    assert lunar_settlement["session"] == "closed"
    assert lunar_settlement["reason"] == "市場無交易，僅辦理結算交割作業"
    assert len(lunar_settlement["snapshot_sha256"]) == 64
    assert {item["year"] for item in lunar_settlement["official_source_snapshots"]} >= {2023, 2024, 2025, 2026}
    assert len(lunar_settlement["official_session_snapshots"]) == 2
    assert all(item["regular_session"] == {"open": "09:00", "close": "13:30"} for item in lunar_settlement["official_session_snapshots"])
    assert all(item["special_sessions"] == [] for item in lunar_settlement["official_session_snapshots"])
    assert all(len(item["payload_sha256"]) == 64 for item in lunar_settlement["official_session_snapshots"])

    assert calendar.session_at(_at("2026-01-02T08:45:00"))["session"] == "pre_open"
    assert calendar.session_at(_at("2026-01-02T09:01:00"))["session"] == "trading"
    assert calendar.session_at(_at("2026-01-02T13:26:00"))["session"] == "closing_auction"
    assert calendar.next_trading_day(date(2026, 2, 11)) == date(2026, 2, 23)


def test_historical_official_snapshots_cover_holidays_and_typhoon_closures():
    calendar = default_taiwan_market_calendar()

    for day in (date(2023, 4, 4), date(2024, 2, 8), date(2025, 1, 27)):
        assert calendar.day_status(day)["trading_day"] is False
        assert calendar.day_status(day)["source"] == "official_calendar"
    assert calendar.day_status(date(2024, 7, 25))["reason"] == "凱米颱風全日休市"
    assert calendar.day_status(date(2024, 10, 2))["reason"] == "山陀兒颱風全日休市"
    assert calendar.day_status(date(2024, 7, 26))["trading_day"] is True

    receipt = calendar.receipt(_at("2024-07-25T10:00:00"))
    typhoon_sources = [item for item in receipt["official_source_snapshots"] if item.get("event")]
    assert {item["event"] for item in typhoon_sources} == {
        "typhoon_gaemi_full_closure",
        "typhoon_krathon_full_closure",
    }
    assert all(item["url"].startswith("https://www.twse.com.tw/") for item in typhoon_sources)


def test_twse_minguo_holiday_schedule_rows_normalize_to_a_reproducible_calendar():
    calendar = calendar_from_twse_rows([
        {"Name": "市場無交易，僅辦理結算交割作業", "Date": "1150212"},
        {"Name": "農曆春節", "Date": "1150216"},
        {"Name": "農曆春節", "Date": "1150217"},
    ])

    assert calendar.day_status(date(2026, 2, 12))["trading_day"] is False
    assert calendar.day_status(date(2026, 2, 23))["trading_day"] is True
    assert calendar.receipt(_at("2026-02-12T09:00:00"))["source"].startswith("https://openapi.twse.com.tw/")


def test_quote_and_schedule_use_the_same_calendar_for_a_weekday_holiday():
    holiday = _at("2026-09-25T09:01:00")
    status, source = _trading_status(holiday, holiday)

    assert (status, source) == ("closed", "derived_session_clock")
    assert active_schedule_phase(holiday) == "off_window"
