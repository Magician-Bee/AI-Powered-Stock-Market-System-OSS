"""Persist observed account NAV without inventing a legacy opening baseline."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class PaperNAVLedger:
    def __init__(self, store: Any, account_id: str, timezone_name: str) -> None:
        self.account_id = account_id
        self.timezone = ZoneInfo(timezone_name)
        with store._connect() as conn:
            conn.execute("""create table if not exists paper_nav_observations (
                observation_id text primary key, account_id text not null,
                observed_at text not null, local_day text not null, equity real not null,
                inception_baseline integer not null, state_sha256 text not null,
                payload_json text not null
            )""")
            conn.execute("create index if not exists idx_paper_nav_account_time on paper_nav_observations(account_id, observed_at)")

    def observe(self, conn: sqlite3.Connection, *, equity: float, state: dict[str, Any],
                as_of: str | datetime, inception: bool = False) -> dict[str, Any]:
        timestamp = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("NAV observation requires an explicit timezone")
        if not math.isfinite(equity):
            raise ValueError("NAV must be finite")
        timestamp = timestamp.astimezone(self.timezone)
        day = timestamp.date().isoformat()
        state_hash = _hash(state)
        latest = conn.execute("select * from paper_nav_observations where account_id=? order by observed_at desc, rowid desc limit 1", (self.account_id,)).fetchone()
        if latest is not None and timestamp < datetime.fromisoformat(latest["observed_at"]):
            raise ValueError("nav_observation_time_regressed")
        if latest is None or latest["state_sha256"] != state_hash or latest["local_day"] != day or inception:
            payload = {"account_id": self.account_id, "observed_at": timestamp.isoformat(), "local_day": day,
                       "equity": equity, "state": state, "inception_baseline": inception, "timezone": str(self.timezone)}
            identifier = "PNAV-" + _hash(payload)
            conn.execute("insert or ignore into paper_nav_observations values (?,?,?,?,?,?,?,?)",
                         (identifier, self.account_id, timestamp.isoformat(), day, equity, int(inception), state_hash,
                          json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)))
        latest = conn.execute("select * from paper_nav_observations where account_id=? order by observed_at desc, rowid desc limit 1", (self.account_id,)).fetchone()
        initial = conn.execute("select * from paper_nav_observations where account_id=? and inception_baseline=1 order by observed_at limit 1", (self.account_id,)).fetchone()
        previous = conn.execute("select * from paper_nav_observations where account_id=? and local_day<? order by observed_at desc, rowid desc limit 1", (self.account_id, day)).fetchone()
        baseline = initial if initial is not None and initial["local_day"] == day else previous
        observed_peak = float(conn.execute("select max(equity) from paper_nav_observations where account_id=?", (self.account_id,)).fetchone()[0])
        peak = observed_peak if initial is not None else None
        daily_pnl = equity - float(baseline["equity"]) if baseline is not None else None
        receipt = {
            "schema_version": "open_stock_ai.paper_nav_risk_state.v1", "account_id": self.account_id,
            "observation_id": latest["observation_id"], "observed_at": latest["observed_at"],
            "timezone": str(self.timezone), "local_day": day, "equity": round(equity, 2),
            "today_pnl": round(daily_pnl, 2) if daily_pnl is not None else None,
            "peak_equity": round(peak, 2) if peak is not None else None,
            "observed_peak_equity": round(observed_peak, 2),
            "current_drawdown_pct": round(max(0.0, (peak - equity) / peak * 100), 8) if peak and peak > 0 else None,
            "day_baseline_observation_id": baseline["observation_id"] if baseline is not None else None,
            "day_baseline_observed_at": baseline["observed_at"] if baseline is not None else None,
            "day_baseline_equity": float(baseline["equity"]) if baseline is not None else None,
            "day_baseline_basis": ("account_inception" if baseline is initial and initial is not None
                                   else "latest_observed_prior_day_nav" if baseline is not None else "missing_baseline"),
            "inception_baseline_known": initial is not None,
            "status": "available" if peak is not None and daily_pnl is not None else "missing_baseline",
            "valuation_basis": "observed_paper_account_NAV_not_official_exchange_close",
        }
        return {**receipt, "receipt_sha256": _hash(receipt)}
