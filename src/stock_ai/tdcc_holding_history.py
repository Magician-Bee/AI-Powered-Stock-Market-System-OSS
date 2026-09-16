from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from .data_platform.source_registry import source_endpoint
from .data_platform.availability import get_data_availability_registry
from .official_derivatives import (
    DEFAULT_DB_PATH,
    DEFAULT_TDCC_SOURCE_TEXT,
    import_tdcc_holding_distribution,
)


TDCC_HOLDING_HISTORY_SCHEMA_VERSION = "stock_ai.tdcc_holding_history.v2"
TDCC_HOLDING_DISTRIBUTION_OPENAPI = source_endpoint(
    "tdcc_holding_distribution_openapi"
)

GRADE_RANGES = {
    1: "1–999 股",
    2: "1,000–5,000 股",
    3: "5,001–10,000 股",
    4: "10,001–15,000 股",
    5: "15,001–20,000 股",
    6: "20,001–30,000 股",
    7: "30,001–40,000 股",
    8: "40,001–50,000 股",
    9: "50,001–100,000 股",
    10: "100,001–200,000 股",
    11: "200,001–400,000 股",
    12: "400,001–600,000 股",
    13: "600,001–800,000 股",
    14: "800,001–1,000,000 股",
    15: "1,000,001 股以上",
    16: "差異數調整",
    17: "合計",
}


def _db_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv("STOCK_AI_OFFICIAL_DERIVATIVES_DB_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_DB_PATH


def _normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not value:
        raise ValueError("請輸入台股股票代號。")
    if re.fullmatch(r"\d{4,6}", value):
        return f"{value}.TW"
    if re.fullmatch(r"\d{4,6}\.(?:TW|TWO)", value):
        return value
    raise ValueError("TDCC 持股分布只支援數字代號或明確的 .TW／.TWO 台股代號。")


def _number(value: Any, *, integer: bool = False) -> int | float:
    text = str(value or "0").strip().replace(",", "").replace("%", "")
    try:
        number = float(text or 0)
    except ValueError:
        number = 0.0
    return int(round(number)) if integer else number


def _report_date(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        raise ValueError(f"TDCC OpenAPI 回傳無法辨識的資料日期：{value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _field(row: dict[str, Any], name: str) -> Any:
    for key, value in row.items():
        if str(key).replace("\ufeff", "").strip() == name:
            return value
    return None


def parse_tdcc_openapi_distribution(
    payload: list[dict[str, Any]],
    symbol: str,
) -> dict[str, Any]:
    """Normalize one security's 17 official TDCC holding-grade rows."""

    normalized = _normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    matched = [
        row
        for row in payload
        if isinstance(row, dict)
        and str(_field(row, "證券代號") or "").strip() == code
    ]
    if not matched:
        raise ValueError(f"TDCC 最新公開資料沒有 {code} 的持股分級資料。")

    distribution: list[dict[str, Any]] = []
    report_dates: set[str] = set()
    for row in matched:
        grade = int(_number(_field(row, "持股分級"), integer=True))
        if grade not in GRADE_RANGES:
            continue
        report_dates.add(_report_date(_field(row, "資料日期")))
        distribution.append(
            {
                "grade": grade,
                "range": GRADE_RANGES[grade],
                "holders": int(_number(_field(row, "人數"), integer=True)),
                "shares": int(_number(_field(row, "股數"), integer=True)),
                "ratio_percent": round(
                    float(_number(_field(row, "占集保庫存數比例%"))),
                    4,
                ),
            }
        )
    distribution.sort(key=lambda item: item["grade"])
    if len(report_dates) != 1:
        raise ValueError("TDCC 同一證券的分級列包含不一致的資料日期。")
    by_grade = {item["grade"]: item for item in distribution}
    if 17 not in by_grade:
        raise ValueError("TDCC 持股分級資料缺少第 17 級合計列。")

    canonical_rows = json.dumps(
        sorted(
            matched,
            key=lambda row: int(_number(_field(row, "持股分級"), integer=True)),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    small_grades = [by_grade[grade] for grade in (1, 2, 3) if grade in by_grade]
    holder_400_grades = [
        by_grade[grade] for grade in (12, 13, 14, 15) if grade in by_grade
    ]
    major_grade = by_grade.get(15)
    total = by_grade[17]
    small_count = sum(item["holders"] for item in small_grades)
    small_ratio = round(sum(item["ratio_percent"] for item in small_grades), 4)
    holder_400_ratio = round(
        sum(item["ratio_percent"] for item in holder_400_grades),
        4,
    )
    major_ratio = major_grade["ratio_percent"] if major_grade else None
    return {
        "report_date": next(iter(report_dates)),
        "symbol": normalized,
        "total_holders": total["holders"],
        "total_shares": total["shares"],
        "major_holder_1000_lot_ratio": major_ratio,
        "holder_400_lot_ratio": holder_400_ratio,
        "small_shareholder_count": small_count,
        "small_shareholder_ratio": small_ratio,
        "concentration_score": holder_400_ratio,
        "distribution": distribution,
        "source": DEFAULT_TDCC_SOURCE_TEXT,
        "source_id": "tdcc_openapi",
        "source_url": TDCC_HOLDING_DISTRIBUTION_OPENAPI,
        "raw_hash": sha256(canonical_rows.encode("utf-8")).hexdigest(),
        "acquired_at": datetime.now(timezone.utc).isoformat(),
        # TDCC's OpenAPI endpoint exposes the current published weekly
        # snapshot, but it does not attest when a historical snapshot became
        # public.  Preserve the distinction instead of granting PIT status
        # merely because this installation later saved a row.
        "published_at": None,
    }


def _canonical_revision_hash(payload: dict[str, Any]) -> str:
    """Hash the immutable observed payload, excluding local capture metadata."""

    stable = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "acquired_at",
            "revision_id",
            "revision_sha256",
            "availability_contract_snapshot",
        }
    }
    return sha256(
        json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _freeze_availability_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Bind the exact availability decision to a TDCC revision at ingestion."""

    source_id = str(payload.get("source_id") or "tdcc_openapi")
    registry = get_data_availability_registry()
    snapshot = registry.resolve(
        source_id=source_id,
        dataset="tdcc_holding_distribution",
    ).snapshot(
        observed_at=str(payload.get("report_date") or "") or None,
        published_at=str(payload.get("published_at") or "") or None,
        acquired_at=str(payload.get("acquired_at") or "") or None,
    )
    return {**payload, "source_id": source_id, "availability_contract_snapshot": snapshot}


class TDCCHoldingHistoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = _db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            conn.execute(
                """
                create table if not exists tdcc_holding_history (
                    symbol text not null,
                    report_date text not null,
                    payload_json text not null,
                    raw_hash text not null,
                    acquired_at text not null,
                    primary key(symbol, report_date)
                )
                """
            )
            conn.execute(
                """
                create table if not exists tdcc_holding_history_revisions (
                    symbol text not null,
                    report_date text not null,
                    revision_sha256 text not null,
                    raw_hash text not null,
                    payload_json text not null,
                    acquired_at text not null,
                    primary key(symbol, report_date, revision_sha256)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_tdcc_holding_history_revisions_latest
                on tdcc_holding_history_revisions(symbol, report_date, acquired_at desc)
                """
            )
            conn.commit()

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a TDCC revision; never rewrite an observed weekly payload."""

        frozen = _freeze_availability_contract(dict(payload))
        revision_sha256 = _canonical_revision_hash(frozen)
        revision_id = f"TDCC-{frozen['symbol']}-{frozen['report_date']}-{revision_sha256[:16]}"
        stored = {
            **frozen,
            "revision_id": revision_id,
            "revision_sha256": revision_sha256,
        }
        payload_json = json.dumps(stored, ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            cursor = conn.execute(
                """
                insert or ignore into tdcc_holding_history_revisions
                    (symbol, report_date, revision_sha256, raw_hash, payload_json, acquired_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["symbol"],
                    stored["report_date"],
                    revision_sha256,
                    stored["raw_hash"],
                    payload_json,
                    stored["acquired_at"],
                ),
            )
            conn.commit()
        return {
            "revision_id": revision_id,
            "revision_sha256": revision_sha256,
            "created": cursor.rowcount == 1,
            "availability_contract_snapshot": stored["availability_contract_snapshot"],
        }

    def query(self, symbol: str, *, weeks: int = 52) -> list[dict[str, Any]]:
        normalized = _normalize_symbol(symbol)
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            rows = conn.execute(
                """
                select payload_json
                from tdcc_holding_history_revisions
                where symbol=?
                order by report_date asc, acquired_at asc, revision_sha256 asc
                """,
                (normalized,),
            ).fetchall()
            # Legacy rows are retained as a read-only projection, but lack a
            # frozen source contract. Treat them as unverified rather than
            # silently certifying old UPSERT data.
            legacy_rows = conn.execute(
                """
                select payload_json, raw_hash, acquired_at
                from tdcc_holding_history
                where symbol=?
                order by report_date asc, acquired_at asc
                """,
                (normalized,),
            ).fetchall()
        latest_by_date: dict[str, dict[str, Any]] = {}
        for row in legacy_rows:
            legacy = json.loads(row[0])
            legacy.setdefault("raw_hash", row[1])
            legacy.setdefault("acquired_at", row[2])
            legacy["source_id"] = "legacy_tdcc_history"
            legacy["availability_contract_snapshot"] = {
                "schema_version": "stock_ai.data_availability_contract_snapshot.v1",
                "contract": {"basis": "unverified", "historical_pit_eligible": False},
                "decision": {
                    "basis": "unverified",
                    "historical_pit_eligible": False,
                    "reason": "legacy_tdcc_revision_missing_frozen_contract",
                    "available_at": legacy.get("acquired_at"),
                },
            }
            latest_by_date[str(legacy["report_date"])] = legacy
        for row in rows:
            item = json.loads(row[0])
            latest_by_date[str(item["report_date"])] = item
        selected = [latest_by_date[key] for key in sorted(latest_by_date)]
        return selected[-max(1, min(int(weeks), 104)):]

    def revision_count(self, symbol: str) -> int:
        normalized = _normalize_symbol(symbol)
        with sqlite3.connect(self.path, factory=ManagedSQLiteConnection) as conn:
            row = conn.execute(
                "select count(*) from tdcc_holding_history_revisions where symbol=?",
                (normalized,),
            ).fetchone()
        return int(row[0] if row else 0)


def fetch_tdcc_holding_distribution(
    symbol: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    response = default_external_transport_guard().call_sync(
        "source:tdcc_openapi:holding_distribution",
        lambda: httpx.get(
            TDCC_HOLDING_DISTRIBUTION_OPENAPI,
            headers={
                "Accept": "application/json",
                "User-Agent": "Open-Stock-AI/1.0",
            },
            timeout=max(1.0, min(float(timeout_seconds), 60.0)),
            follow_redirects=True,
        ),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("TDCC OpenAPI 回傳格式不是預期的列陣列。")
    return parse_tdcc_openapi_distribution(payload, symbol)


def _change(current: Any, previous: Any) -> int | float | None:
    if current is None or previous is None:
        return None
    value = float(current) - float(previous)
    return int(value) if value.is_integer() else round(value, 4)


def _decorate(items: list[dict[str, Any]]) -> dict[str, Any]:
    tracked = (
        "total_holders",
        "total_shares",
        "major_holder_1000_lot_ratio",
        "holder_400_lot_ratio",
        "small_shareholder_count",
        "small_shareholder_ratio",
    )
    for index, item in enumerate(items):
        previous = items[index - 1] if index else None
        item["week_over_week"] = {
            field: _change(item.get(field), previous.get(field) if previous else None)
            for field in tracked
        }

    direction = "unavailable"
    streak = 0
    latest_delta = (
        items[-1].get("week_over_week", {}).get("major_holder_1000_lot_ratio")
        if items
        else None
    )
    if latest_delta is not None:
        direction = (
            "increase" if latest_delta > 0 else "decrease" if latest_delta < 0 else "flat"
        )
        for item in reversed(items[1:]):
            delta = item.get("week_over_week", {}).get(
                "major_holder_1000_lot_ratio"
            )
            current_direction = (
                "increase" if delta and delta > 0
                else "decrease" if delta and delta < 0
                else "flat" if delta == 0
                else "unavailable"
            )
            if current_direction != direction:
                break
            streak += 1
    latest = items[-1] if items else None
    return {
        "latest": latest,
        "week_over_week": latest.get("week_over_week") if latest else None,
        "major_holder_1000_lot_trend": {
            "direction": direction,
            "streak_weeks": streak,
        },
    }


def query_tdcc_holding_history(
    symbol: str,
    *,
    weeks: int = 52,
    refresh: bool = False,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized = _normalize_symbol(symbol)
    week_count = max(1, min(int(weeks), 104))
    store = TDCCHoldingHistoryStore(db_path)
    sync: dict[str, Any] | None = None
    if refresh:
        try:
            current = fetch_tdcc_holding_distribution(normalized)
            revision = store.save(current)
            import_tdcc_holding_distribution(
                {
                    "items": [
                        {
                            key: current.get(key)
                            for key in (
                                "report_date",
                                "symbol",
                                "total_holders",
                                "total_shares",
                                "major_holder_1000_lot_ratio",
                                "holder_400_lot_ratio",
                                "small_shareholder_count",
                                "concentration_score",
                                "source",
                            )
                        }
                    ]
                },
                db_path=store.path,
            )
            sync = {
                "status": "refreshed",
                "report_date": current["report_date"],
                "official_grade_count": len(current["distribution"]),
                "raw_hash": current["raw_hash"],
                "revision_id": revision["revision_id"],
                "revision_created": revision["created"],
                "historical_pit_eligible": revision[
                    "availability_contract_snapshot"
                ]["decision"]["historical_pit_eligible"],
            }
        except Exception as exc:
            sync = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc)[:240],
            }

    items = store.query(normalized, weeks=week_count)
    summary = _decorate(items)
    complete_items = [
        item
        for item in items
        if all(
            item.get(field) is not None
            for field in (
                "total_holders",
                "total_shares",
                "major_holder_1000_lot_ratio",
                "holder_400_lot_ratio",
                "small_shareholder_count",
                "small_shareholder_ratio",
            )
        )
    ]
    availability_decisions = [
        (item.get("availability_contract_snapshot") or {}).get("decision") or {}
        for item in items
    ]
    pit_certified_items = [
        item
        for item, decision in zip(items, availability_decisions)
        if bool(decision.get("historical_pit_eligible"))
        and bool(decision.get("production_contract_covered"))
        and bool(decision.get("available_at"))
    ]
    historical_pit_eligible = bool(items) and len(pit_certified_items) == len(items)
    status = (
        "stale_cache"
        if items and sync and sync["status"] == "failed"
        else "pit_complete"
        if items and len(complete_items) == len(items) and historical_pit_eligible
        else "operational_latest_only"
        if items and len(complete_items) == len(items)
        else "partial"
        if items
        else "no_data"
    )
    return {
        "schema_version": TDCC_HOLDING_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "status": status,
        "items": items,
        "summary": summary,
        "coverage": {
            "requested_weeks": week_count,
            "count": len(items),
            "complete_count": len(complete_items),
            "pit_certified_count": len(pit_certified_items),
            "revision_count": store.revision_count(normalized),
            "first_report_date": items[0]["report_date"] if items else None,
            "last_report_date": items[-1]["report_date"] if items else None,
        },
        "sync": sync,
        "source": {
            "name": "臺灣集中保管結算所 OpenAPI",
            "url": TDCC_HOLDING_DISTRIBUTION_OPENAPI,
            "official": True,
            "cadence": "weekly_after_last_business_day",
        },
        "truthfulness": {
            "small_shareholder_definition": (
                "本畫面定義為持有 1–10,000 股（第 1–3 級）；"
                "這是公開的顯示分群，不代表 TDCC 的投資人分類。"
            ),
            "holder_400_lot_definition": "第 12–15 級（400,001 股以上）比例合計。",
            "major_holder_1000_lot_definition": "第 15 級（1,000,001 股以上）比例。",
            "concentration_score_method": (
                "直接採 400,001 股以上比例，為機械式顯示指標，"
                "不是模型分數或投資訊號。"
            ),
            "grade_16_excluded_from_groups": True,
            "grade_17_is_total": True,
            "openapi_latest_snapshot_only": True,
            "historical_pit_eligible": historical_pit_eligible,
            "pit_blockers": (
                []
                if historical_pit_eligible
                else [
                    "tdcc_original_publication_timestamp_or_reviewed_history_coverage_missing"
                ]
            ),
            "history_policy": (
                "每次刷新保存官方當週快照；本地歷史會逐週累積。"
                "官方網頁歷史保存一年，但 OpenAPI 本端點只提供最新快照，"
                "因此不宣稱已自動回補完整一年。"
            ),
            "missing_weeks_not_zero_filled": True,
            "raw_rows_hash_preserved": (
                all(bool(item.get("raw_hash")) for item in items)
                if items
                else False
            ),
            "revision_history_append_only": True,
            "not_realtime_trading_data": True,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
