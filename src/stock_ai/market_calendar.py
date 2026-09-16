from __future__ import annotations

"""Single Taiwan equity-market calendar for quotes, scheduling and replay.

Weekdays are only a fallback.  The checked-in calendar is an immutable
snapshot of the official TWSE holiday schedule; callers retain its source and
snapshot hash in their result instead of silently treating a public holiday as
a normal market day.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "stock_ai.taiwan_market_calendar.v1"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "taiwan_market_calendar.json"
_SESSION_WINDOWS = (
    ("pre_open", time(8, 30), time(9, 0)),
    ("trading", time(9, 0), time(13, 25)),
    ("closing_auction", time(13, 25), time(13, 30)),
    ("post_close", time(13, 30), time(15, 0)),
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaiwanMarketCalendar:
    """Calendar snapshot with explicit closure and session classifications."""

    closed_dates: Mapping[date, str]
    special_sessions: Mapping[date, tuple[tuple[str, time, time], ...]]
    source: str
    snapshot_sha256: str
    official_source_snapshots: tuple[Mapping[str, Any], ...] = ()
    official_session_snapshots: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TaiwanMarketCalendar":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("taiwan market calendar schema is invalid")
        source = str(payload.get("official_source") or "")
        if not source.startswith("https://"):
            raise ValueError("taiwan market calendar requires an official HTTPS source")
        closed: dict[date, str] = {}
        special: dict[date, tuple[tuple[str, time, time], ...]] = {}
        years = payload.get("years")
        if not isinstance(years, Mapping):
            raise ValueError("taiwan market calendar years are required")
        source_snapshots: list[Mapping[str, Any]] = []
        for snapshot in payload.get("official_source_snapshots") or []:
            if not isinstance(snapshot, Mapping):
                raise ValueError("official calendar source snapshot is invalid")
            if not str(snapshot.get("year") or "").isdigit():
                raise ValueError("official calendar source snapshot year is invalid")
            if not str(snapshot.get("url") or "").startswith("https://"):
                raise ValueError("official calendar source snapshot requires an HTTPS URL")
            if not str(snapshot.get("retrieved_at") or "").strip():
                raise ValueError("official calendar source snapshot retrieval time is required")
            payload_sha256 = snapshot.get("payload_sha256")
            if payload_sha256 is not None and (not isinstance(payload_sha256, str) or len(payload_sha256) != 64):
                raise ValueError("official calendar source snapshot hash is invalid")
            source_snapshots.append(dict(snapshot))
        session_snapshots: list[Mapping[str, Any]] = []
        for snapshot in payload.get("official_session_snapshots") or []:
            if not isinstance(snapshot, Mapping):
                raise ValueError("official session snapshot is invalid")
            if not str(snapshot.get("snapshot_id") or "").strip():
                raise ValueError("official session snapshot ID is required")
            if not str(snapshot.get("url") or "").startswith("https://"):
                raise ValueError("official session snapshot requires an HTTPS URL")
            if not str(snapshot.get("retrieved_at") or "").strip():
                raise ValueError("official session snapshot retrieval time is required")
            payload_sha256 = snapshot.get("payload_sha256")
            if not isinstance(payload_sha256, str) or len(payload_sha256) != 64:
                raise ValueError("official session snapshot hash is invalid")
            regular_session = snapshot.get("regular_session")
            if not isinstance(regular_session, Mapping):
                raise ValueError("official session snapshot regular hours are required")
            if regular_session.get("open") != "09:00" or regular_session.get("close") != "13:30":
                raise ValueError("official session snapshot regular hours are unsupported")
            if not isinstance(snapshot.get("special_sessions"), list):
                raise ValueError("official session snapshot special sessions are required")
            session_snapshots.append(dict(snapshot))
        for year, details in years.items():
            if not str(year).isdigit() or not isinstance(details, Mapping):
                raise ValueError("taiwan market calendar year is invalid")
            for item in details.get("closed_dates") or []:
                if not isinstance(item, Mapping):
                    raise ValueError("market closure entry is invalid")
                day = date.fromisoformat(str(item.get("date") or ""))
                if day.year != int(year) or not str(item.get("name") or "").strip():
                    raise ValueError("market closure entry is incomplete")
                closed[day] = str(item["name"])
            for item in details.get("special_sessions") or []:
                if not isinstance(item, Mapping):
                    raise ValueError("market special session entry is invalid")
                day = date.fromisoformat(str(item.get("date") or ""))
                windows: list[tuple[str, time, time]] = []
                for window in item.get("windows") or []:
                    if not isinstance(window, Mapping):
                        raise ValueError("market special session window is invalid")
                    start, end = time.fromisoformat(str(window["start"])), time.fromisoformat(str(window["end"]))
                    if start >= end:
                        raise ValueError("market special session window must increase")
                    windows.append((str(window["phase"]), start, end))
                if not windows:
                    raise ValueError("market special session requires windows")
                special[day] = tuple(windows)
        return cls(
            closed_dates=closed,
            special_sessions=special,
            source=source,
            snapshot_sha256=_sha256(payload),
            official_source_snapshots=tuple(source_snapshots),
            official_session_snapshots=tuple(session_snapshots),
        )

    def day_status(self, day: date) -> dict[str, Any]:
        if day in self.closed_dates:
            return {"trading_day": False, "reason": self.closed_dates[day], "source": "official_calendar"}
        if day.weekday() >= 5:
            return {"trading_day": False, "reason": "weekend", "source": "weekday_fallback"}
        return {"trading_day": True, "reason": "regular_trading_day", "source": "official_calendar"}

    def session_at(self, moment: datetime) -> dict[str, Any]:
        local = moment.astimezone(TAIPEI_TZ)
        day = self.day_status(local.date())
        if not day["trading_day"]:
            return {**day, "session": "closed", "timezone": "Asia/Taipei"}
        windows = self.special_sessions.get(local.date(), _SESSION_WINDOWS)
        clock = local.time().replace(tzinfo=None)
        for phase, start, end in windows:
            if start <= clock < end:
                return {**day, "session": phase, "timezone": "Asia/Taipei"}
        return {**day, "session": "closed", "timezone": "Asia/Taipei"}

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while not self.day_status(candidate)["trading_day"]:
            candidate += timedelta(days=1)
        return candidate

    def receipt(self, moment: datetime) -> dict[str, Any]:
        state = self.session_at(moment)
        return {
            "schema_version": SCHEMA_VERSION,
            "venue": "TWSE",
            "source": self.source,
            "snapshot_sha256": self.snapshot_sha256,
            "official_source_snapshots": [dict(item) for item in self.official_source_snapshots],
            "official_session_snapshots": [dict(item) for item in self.official_session_snapshots],
            "as_of": moment.astimezone(TAIPEI_TZ).isoformat(),
            **{key: value for key, value in state.items() if key != "source"},
            "calendar_day_source": state["source"],
        }


@lru_cache(maxsize=1)
def default_taiwan_market_calendar() -> TaiwanMarketCalendar:
    payload = json.loads(DEFAULT_CALENDAR_PATH.read_text(encoding="utf-8"))
    return TaiwanMarketCalendar.from_payload(payload)


def calendar_from_twse_rows(rows: Iterable[Mapping[str, Any]]) -> TaiwanMarketCalendar:
    """Normalize the official `holidaySchedule` Minguo-date API response."""

    closures: list[dict[str, str]] = []
    for row in rows:
        raw = str(row.get("Date") or "")
        if len(raw) != 7 or not raw.isdigit():
            raise ValueError("TWSE holidaySchedule Date must be a seven-digit Minguo date")
        day = date(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:]))
        name = str(row.get("Name") or "").strip()
        if not name:
            raise ValueError("TWSE holidaySchedule Name is required")
        if day.weekday() < 5:
            closures.append({"date": day.isoformat(), "name": name})
    if not closures:
        raise ValueError("TWSE holidaySchedule contains no weekday closures")
    year = str(date.fromisoformat(closures[0]["date"]).year)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "venue": "TWSE",
        "timezone": "Asia/Taipei",
        "official_source": "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
        "years": {year: {"closed_dates": closures, "special_sessions": []}},
    }
    return TaiwanMarketCalendar.from_payload(payload)
