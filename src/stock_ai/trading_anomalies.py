from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from statistics import median
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.sqlite_store import SQLiteStore

from .daily_history import query_daily_history
from .realtime_data import normalize_symbol


ANOMALY_SCHEMA_VERSION = "stock_ai.trading_anomaly.v1"
DETECTOR_VERSION = "stock_ai.daily_trading_anomaly_detector.v1"
TRACKING_STATUSES = {"open", "acknowledged", "resolved", "reopened"}
DETECTION_POLICY = {
    "baseline_sessions": 20,
    "minimum_baseline_sessions": 5,
    "volume_spike_ratio": 2.0,
    "gap_percent": 3.0,
    "rapid_move_percent": 5.0,
    "divergence_price_percent": 2.0,
    "divergence_volume_percent": 30.0,
}


class TradingAnomalyError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _percent(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return (numerator / denominator - 1.0) * 100.0


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _event(
    *,
    symbol: str,
    point: dict[str, Any],
    event_type: str,
    direction: str,
    severity: str,
    title: str,
    summary: str,
    metrics: dict[str, Any],
    source_ids: list[str],
    policy: dict[str, Any],
    detected_at: str,
) -> dict[str, Any]:
    source_record = {
        field: point.get(field)
        for field in ("date", "open", "high", "low", "close", "volume", "turnover")
    }
    fingerprint_input = {
        "symbol": symbol,
        "trading_date": point["date"],
        "event_type": event_type,
        "detector_version": DETECTOR_VERSION,
        "source_record": source_record,
    }
    fingerprint = sha256(_canonical_json(fingerprint_input).encode()).hexdigest()
    return {
        "schema_version": ANOMALY_SCHEMA_VERSION,
        "event_id": f"TAE-{fingerprint[:32]}",
        "fingerprint": fingerprint,
        "symbol": symbol,
        "trading_date": point["date"],
        "event_type": event_type,
        "direction": direction,
        "severity": severity,
        "title": title,
        "summary": summary,
        "detector_version": DETECTOR_VERSION,
        "source_ids": sorted(set(source_ids)),
        "source_record": source_record,
        "metrics": metrics,
        "policy": policy,
        "detected_at": detected_at,
    }


def detect_trading_anomalies(
    *,
    symbol: str,
    history_points: list[dict[str, Any]],
    source_ids: list[str],
    detected_at: str | None = None,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply transparent daily rules; no values are imputed or model-generated."""

    normalized = normalize_symbol(symbol)
    rules = {**DETECTION_POLICY, **(policy or {})}
    baseline_sessions = int(rules["baseline_sessions"])
    minimum = int(rules["minimum_baseline_sessions"])
    if baseline_sessions < minimum or minimum < 2:
        raise TradingAnomalyError("baseline session policy is invalid")
    points = sorted(history_points, key=lambda item: str(item.get("date") or ""))
    usable = [
        point
        for point in points
        if point.get("date")
        and all(point.get(field) is not None for field in ("open", "close", "volume"))
        and float(point["open"]) > 0
        and float(point["close"]) > 0
        and float(point["volume"]) >= 0
    ]
    observed_at = detected_at or _now()
    output: list[dict[str, Any]] = []
    for index in range(1, len(usable)):
        prior = usable[max(0, index - baseline_sessions) : index]
        if len(prior) < minimum:
            continue
        point = usable[index]
        previous = usable[index - 1]
        open_price = float(point["open"])
        close_price = float(point["close"])
        previous_close = float(previous["close"])
        volume = float(point["volume"])
        previous_volume = float(previous["volume"])
        baseline_volumes = [float(item["volume"]) for item in prior if float(item["volume"]) > 0]
        if len(baseline_volumes) < minimum:
            continue
        median_volume = median(baseline_volumes)
        volume_ratio = volume / median_volume if median_volume > 0 else None
        gap_percent = _percent(open_price, previous_close)
        price_change_percent = _percent(close_price, previous_close)
        volume_change_percent = _percent(volume, previous_volume)
        common_metrics = {
            "previous_close": previous_close,
            "current_open": open_price,
            "current_close": close_price,
            "current_volume": int(volume),
            "baseline_median_volume": median_volume,
            "baseline_sample_count": len(baseline_volumes),
            "volume_ratio": _round(volume_ratio),
            "gap_percent": _round(gap_percent),
            "price_change_percent": _round(price_change_percent),
            "volume_change_percent": _round(volume_change_percent),
        }

        if volume_ratio is not None and volume_ratio >= float(rules["volume_spike_ratio"]):
            output.append(
                _event(
                    symbol=normalized,
                    point=point,
                    event_type="volume_spike",
                    direction="mixed",
                    severity="high" if volume_ratio >= 3 else "medium",
                    title="爆量",
                    summary=f"成交量為前 {len(baseline_volumes)} 個交易日中位數的 {volume_ratio:.2f} 倍。",
                    metrics=common_metrics,
                    source_ids=source_ids,
                    policy={"volume_ratio_gte": rules["volume_spike_ratio"]},
                    detected_at=observed_at,
                )
            )

        if gap_percent is not None and abs(gap_percent) >= float(rules["gap_percent"]):
            direction = "up" if gap_percent > 0 else "down"
            output.append(
                _event(
                    symbol=normalized,
                    point=point,
                    event_type="gap",
                    direction=direction,
                    severity="high" if abs(gap_percent) >= 6 else "medium",
                    title="向上跳空" if direction == "up" else "向下跳空",
                    summary=f"開盤價相對前收盤變動 {gap_percent:+.2f}%。",
                    metrics=common_metrics,
                    source_ids=source_ids,
                    policy={"absolute_gap_percent_gte": rules["gap_percent"]},
                    detected_at=observed_at,
                )
            )

        if (
            price_change_percent is not None
            and abs(price_change_percent) >= float(rules["rapid_move_percent"])
        ):
            direction = "up" if price_change_percent > 0 else "down"
            output.append(
                _event(
                    symbol=normalized,
                    point=point,
                    event_type="rapid_move",
                    direction=direction,
                    severity="high" if abs(price_change_percent) >= 8 else "medium",
                    title="急漲" if direction == "up" else "急跌",
                    summary=f"收盤價相對前收盤變動 {price_change_percent:+.2f}%。",
                    metrics=common_metrics,
                    source_ids=source_ids,
                    policy={"absolute_price_change_percent_gte": rules["rapid_move_percent"]},
                    detected_at=observed_at,
                )
            )

        divergent = (
            price_change_percent is not None
            and volume_change_percent is not None
            and abs(price_change_percent) >= float(rules["divergence_price_percent"])
            and abs(volume_change_percent) >= float(rules["divergence_volume_percent"])
            and price_change_percent * volume_change_percent < 0
        )
        if divergent:
            output.append(
                _event(
                    symbol=normalized,
                    point=point,
                    event_type="price_volume_divergence",
                    direction="mixed",
                    severity="medium",
                    title="價量背離",
                    summary=(
                        f"價格變動 {price_change_percent:+.2f}%，"
                        f"成交量較前一交易日變動 {volume_change_percent:+.2f}%，方向相反。"
                    ),
                    metrics=common_metrics,
                    source_ids=source_ids,
                    policy={
                        "absolute_price_change_percent_gte": rules[
                            "divergence_price_percent"
                        ],
                        "absolute_volume_change_percent_gte": rules[
                            "divergence_volume_percent"
                        ],
                        "directions_must_be_opposite": True,
                    },
                    detected_at=observed_at,
                )
            )
    return output


class TradingAnomalyStore:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def record_scan(
        self,
        *,
        symbol: str,
        events: list[dict[str, Any]],
        source_ids: list[str],
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        scan_id = str(uuid4())
        timestamp = observed_at or _now()
        created_count = 0
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            for event in events:
                cursor = conn.execute(
                    """
                    insert or ignore into trading_anomaly_events (
                        event_id, fingerprint, symbol, trading_date, event_type,
                        direction, severity, detector_version, source_ids_json,
                        source_record_json, metrics_json, policy_json, title,
                        summary, detected_at, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["fingerprint"],
                        event["symbol"],
                        event["trading_date"],
                        event["event_type"],
                        event["direction"],
                        event["severity"],
                        event["detector_version"],
                        _canonical_json(event["source_ids"]),
                        _canonical_json(event["source_record"]),
                        _canonical_json(event["metrics"]),
                        _canonical_json(event["policy"]),
                        event["title"],
                        event["summary"],
                        event["detected_at"],
                        timestamp,
                    ),
                )
                if cursor.rowcount:
                    created_count += 1
                    conn.execute(
                        """
                        insert into trading_anomaly_tracking_actions (
                            action_id, event_id, status, note, actor, acted_at
                        ) values (?, ?, 'open', ?, 'detector', ?)
                        """,
                        (str(uuid4()), event["event_id"], "首次偵測", timestamp),
                    )
                conn.execute(
                    """
                    insert into trading_anomaly_observations (
                        observation_id, scan_id, event_id, observed_at,
                        source_ids_json, metrics_json
                    ) values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        scan_id,
                        event["event_id"],
                        timestamp,
                        _canonical_json(source_ids),
                        _canonical_json(event["metrics"]),
                    ),
                )
            conn.commit()
        return {
            "scan_id": scan_id,
            "detected_count": len(events),
            "created_count": created_count,
            "existing_count": len(events) - created_count,
            "observed_at": timestamp,
        }

    def track(
        self,
        *,
        event_id: str,
        status: str,
        note: str | None = None,
        actor: str = "local_user",
    ) -> dict[str, Any]:
        if status not in TRACKING_STATUSES - {"open"}:
            raise TradingAnomalyError(
                "status must be acknowledged, resolved, or reopened"
            )
        timestamp = _now()
        action_id = str(uuid4())
        with self.store._connect() as conn:
            exists = conn.execute(
                "select 1 from trading_anomaly_events where event_id=?",
                (event_id,),
            ).fetchone()
            if not exists:
                raise TradingAnomalyError("trading anomaly event was not found")
            conn.execute(
                """
                insert into trading_anomaly_tracking_actions (
                    action_id, event_id, status, note, actor, acted_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (action_id, event_id, status, note, actor, timestamp),
            )
            conn.commit()
        return {
            "action_id": action_id,
            "event_id": event_id,
            "status": "open" if status == "reopened" else status,
            "action": status,
            "note": note,
            "actor": actor,
            "acted_at": timestamp,
        }

    def list_events(
        self,
        *,
        symbol: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        row_limit = max(1, min(int(limit), 500))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select e.*,
                       count(distinct o.observation_id) as observation_count,
                       min(o.observed_at) as first_observed_at,
                       max(o.observed_at) as last_observed_at,
                       (
                         select a.status
                           from trading_anomaly_tracking_actions a
                          where a.event_id=e.event_id
                          order by a.acted_at desc, a.rowid desc
                          limit 1
                       ) as current_status
                  from trading_anomaly_events e
                  left join trading_anomaly_observations o
                    on o.event_id=e.event_id
                 where e.symbol=?
                 group by e.event_id
                 order by e.trading_date desc, e.detected_at desc
                 limit ?
                """,
                (normalized, row_limit),
            ).fetchall()
        items = [self._serialize(row) for row in rows]
        return {
            "schema_version": "stock_ai.trading_anomaly_collection.v1",
            "symbol": normalized,
            "count": len(items),
            "items": items,
            "detector_version": DETECTOR_VERSION,
            "policy": DETECTION_POLICY,
        }

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        raw_status = row["current_status"] or "open"
        return {
            "schema_version": ANOMALY_SCHEMA_VERSION,
            "event_id": row["event_id"],
            "fingerprint": row["fingerprint"],
            "symbol": row["symbol"],
            "trading_date": row["trading_date"],
            "event_type": row["event_type"],
            "direction": row["direction"],
            "severity": row["severity"],
            "detector_version": row["detector_version"],
            "source_ids": json.loads(row["source_ids_json"]),
            "source_record": json.loads(row["source_record_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "policy": json.loads(row["policy_json"]),
            "title": row["title"],
            "summary": row["summary"],
            "detected_at": row["detected_at"],
            "tracking": {
                "status": "open" if raw_status == "reopened" else raw_status,
                "last_action": raw_status,
                "observation_count": int(row["observation_count"]),
                "first_observed_at": row["first_observed_at"],
                "last_observed_at": row["last_observed_at"],
            },
        }


def scan_symbol_anomalies(
    store: SQLiteStore,
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    end_date = date.fromisoformat(end) if end else date.today()
    start_date = (
        date.fromisoformat(start)
        if start
        else end_date - timedelta(days=180)
    )
    if start_date > end_date:
        raise TradingAnomalyError("start must not be after end")
    history = query_daily_history(
        normalized,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        limit=5000,
        refresh=refresh,
        allow_fallback=False,
        price_basis="unadjusted",
    )
    source_ids = list(history.get("source_ids") or [])
    events = detect_trading_anomalies(
        symbol=normalized,
        history_points=[point.model_dump() for point in history["points"]],
        source_ids=source_ids,
    )
    anomaly_store = TradingAnomalyStore(store)
    scan = anomaly_store.record_scan(
        symbol=normalized,
        events=events,
        source_ids=source_ids,
    )
    return {
        "schema_version": "stock_ai.trading_anomaly_scan.v1",
        "symbol": normalized,
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "history_point_count": len(history["points"]),
        "history_range_complete": bool(history.get("range_complete")),
        "history_source_ids": source_ids,
        "fallback_count": int(history.get("fallback_count") or 0),
        "detector_version": DETECTOR_VERSION,
        "policy": DETECTION_POLICY,
        **scan,
        "events": anomaly_store.list_events(symbol=normalized)["items"],
    }
