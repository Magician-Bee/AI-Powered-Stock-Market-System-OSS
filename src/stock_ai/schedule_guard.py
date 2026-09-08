from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any

from .system_contract import UPDATE_SCHEDULE
from .market_calendar import default_taiwan_market_calendar

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

PHASE_WINDOWS = {
    "pre_market": (time(8, 0), time(8, 50)),
    "intraday": (time(9, 0), time(13, 30)),
    "post_close": (time(13, 35), time(15, 30)),
    "after_hours": (time(15, 30), time(20, 0)),
}

TASK_POLICIES: dict[str, dict[str, Any]] = {
    "更新美股、費半、ADR、匯率、期貨": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "抓取重大新聞": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "檢查 MOPS 重大訊息": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "產生盤前觀察清單": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "使用授權行情或券商 API streaming": {"source_tier": 1, "requires_broker_api": False, "requires_authorized_realtime": True},
    "監控自選股、持股、候選股": {"source_tier": 1, "requires_broker_api": False, "requires_authorized_realtime": False},
    "偵測急漲急跌、爆量、突破、跌破、法人/籌碼異常": {"source_tier": 1, "requires_broker_api": False, "requires_authorized_realtime": False},
    "抓取官方收盤資料": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新日 K": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新三大法人、融資融券、成交排行": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "產生收盤報告": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新月營收、財報、重大訊息": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新新聞與產業資料": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新回測資料": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "反思今日交易與訊號品質": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
    "更新 TDCC 集保股權分散": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "檢查大戶比例、小股東人數、籌碼集中度": {"source_tier": 2, "requires_broker_api": False, "requires_authorized_realtime": False},
    "產生週報": {"source_tier": 3, "requires_broker_api": False, "requires_authorized_realtime": False},
}


def parse_taipei_datetime(value: str | None = None) -> datetime:
    if not value:
        return datetime.now(TAIPEI_TZ)
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def active_schedule_phase(at: datetime) -> str:
    local = at.astimezone(TAIPEI_TZ)
    if not default_taiwan_market_calendar().day_status(local.date())["trading_day"]:
        return "off_window"
    current = local.time()
    for phase, (start, end) in PHASE_WINDOWS.items():
        if start <= current < end:
            return phase
    if local.weekday() == 0:
        return "weekly"
    return "off_window"


def evaluate_task_policy(
    task: str,
    *,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
) -> dict[str, Any]:
    policy = TASK_POLICIES.get(task, {"source_tier": None, "requires_broker_api": False, "requires_authorized_realtime": False})
    blockers: list[str] = []
    if policy["requires_broker_api"] and not broker_api_connected:
        blockers.append("broker_api_not_connected")
    if policy["requires_authorized_realtime"] and not (authorized_realtime_feed or broker_api_connected):
        blockers.append("authorized_realtime_feed_required")
    if live_ordering_enabled and not broker_api_connected:
        blockers.append("live_ordering_without_broker_api_blocked")
    return {
        "task": task,
        "source_tier": policy["source_tier"],
        "allowed": not blockers,
        "blockers": blockers,
        "requires_broker_api": policy["requires_broker_api"],
        "requires_authorized_realtime": policy["requires_authorized_realtime"],
    }


def schedule_guard_status(
    *,
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
) -> dict[str, Any]:
    local = parse_taipei_datetime(as_of)
    active = active_schedule_phase(local)
    phases = []
    for phase in UPDATE_SCHEDULE:
        phase_name = phase["phase"]
        tasks = [
            evaluate_task_policy(
                task,
                broker_api_connected=broker_api_connected,
                authorized_realtime_feed=authorized_realtime_feed,
                live_ordering_enabled=live_ordering_enabled,
            )
            for task in phase["tasks"]
        ]
        phases.append(
            {
                "phase": phase_name,
                "window": phase["window"],
                "active": phase_name == active,
                "guardrail": phase.get("guardrail"),
                "allowed_task_count": sum(1 for item in tasks if item["allowed"]),
                "blocked_task_count": sum(1 for item in tasks if not item["allowed"]),
                "tasks": tasks,
            }
        )
    return {
        "schema_version": "stock_ai.schedule_guard.v1",
        "method": "local_schedule_window_and_safety_gate",
        "timezone": "Asia/Taipei",
        "as_of": local.isoformat(),
        "active_phase": active,
        "broker_api_connected": broker_api_connected,
        "authorized_realtime_feed": authorized_realtime_feed,
        "live_ordering_enabled": live_ordering_enabled,
        "live_ordering_allowed": broker_api_connected and live_ordering_enabled,
        "phases": phases,
        "phase_count": len(phases),
    }
