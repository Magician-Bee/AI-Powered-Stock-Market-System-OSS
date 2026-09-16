"""Persistence, optimistic updates and cross-process leases for trading plans."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from open_stock_ai.storage.sqlite_store import SQLiteStore
from .trading_plan import TradingPlan, content_hash, utc_time


TERMINAL_PLAN_STATES = frozenset({"closed", "cancelled", "expired", "invalidated", "rejected"})


class TradingPlanStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        with store._connect() as conn:
            conn.executescript("""
                create table if not exists autonomous_trading_plans (
                    plan_id text primary key, account_id text not null,
                    symbol text not null, idempotency_key text not null,
                    definition_hash text not null, definition_json text not null,
                    state_json text not null, status text not null,
                    revision integer not null default 1,
                    created_at text not null, updated_at text not null,
                    lease_owner text, lease_until text,
                    unique(account_id, idempotency_key)
                );
                create index if not exists autonomous_plans_account_status
                    on autonomous_trading_plans(account_id, status);
                create table if not exists autonomous_trading_plan_events (
                    sequence integer primary key autoincrement,
                    plan_id text not null, created_at text not null,
                    event_type text not null, payload_json text not null
                );
                create table if not exists autonomous_account_leases (
                    account_id text primary key, lease_owner text not null,
                    lease_until text not null
                );
                create table if not exists autonomous_plan_exit_requests (
                    request_id text primary key, plan_id text not null, strategy_version text not null,
                    observed_bar_at text not null, evidence_id text not null, reason text not null,
                    created_at text not null, unique(plan_id, observed_bar_at)
                );
                create table if not exists autonomous_position_reviews (
                    plan_id text primary key, observed_bar_at text not null,
                    strategy_version text not null, evidence_id text not null, action text not null
                );
            """)

    def create(self, *, account_id: str, plan: TradingPlan, idempotency_key: str,
               now: datetime | None = None) -> dict[str, Any]:
        if not account_id.strip() or not idempotency_key.strip():
            raise ValueError("account_and_idempotency_key_required")
        instant = utc_time(now or datetime.now(timezone.utc)).isoformat()
        definition = plan.to_dict()
        digest = content_hash(definition)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                "select * from autonomous_trading_plans where account_id=? and idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["definition_hash"] != digest:
                    raise ValueError("trading_plan_idempotency_conflict")
                return self._decode(existing)
            placeholders = ",".join("?" for _ in TERMINAL_PLAN_STATES)
            active = conn.execute(
                f"select plan_id from autonomous_trading_plans where account_id=? and symbol=? and status not in ({placeholders})",
                (account_id, plan.symbol, *sorted(TERMINAL_PLAN_STATES)),
            ).fetchone()
            if active:
                raise ValueError("active_plan_for_symbol_requires_revision_or_cancellation")
            plan_id = "TP-" + uuid4().hex
            state = {"status": "waiting_entry", "entry_order_id": None, "exit_order_id": None,
                     "entered_at": None, "filled_quantity": 0.0, "remaining_quantity": 0.0}
            conn.execute(
                "insert into autonomous_trading_plans (plan_id,account_id,symbol,idempotency_key,definition_hash,definition_json,state_json,status,created_at,updated_at) values (?,?,?,?,?,?,?,?,?,?)",
                (plan_id, account_id, plan.symbol, idempotency_key, digest,
                 json.dumps(definition, ensure_ascii=False), json.dumps(state), state["status"], instant, instant),
            )
            self._event(conn, plan_id, instant, "plan.created", {"definition_hash": digest})
            conn.commit()
        return self.get(plan_id)

    def get(self, plan_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from autonomous_trading_plans where plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError("trading_plan_not_found")
        return self._decode(row)

    def list(self, *, account_id: str, active_only: bool = False) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("select * from autonomous_trading_plans where account_id=? order by created_at,plan_id", (account_id,)).fetchall()
        return [self._decode(row) for row in rows if not active_only or row["status"] not in TERMINAL_PLAN_STATES]

    def get_by_idempotency(self, *, account_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from autonomous_trading_plans where account_id=? and idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
        return self._decode(row) if row else None

    def record_position_review(self, *, plan_id: str, strategy_version: str, evidence_id: str,
                               observed_bar_at: str, action: str, reason: str | None, now: datetime) -> dict[str, Any]:
        """Host-only result of the frozen candle strategy; never a tool payload."""
        observed = utc_time(observed_bar_at)
        if observed > utc_time(now) or not evidence_id or action not in {"hold", "sell"}:
            raise ValueError("invalid_host_position_review")
        if action == "sell" and reason not in {"close_below_slow_mean", "holding_period_expired"}:
            raise ValueError("unsupported_host_exit_reason")
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            row = conn.execute("select * from autonomous_trading_plans where plan_id=?", (plan_id,)).fetchone()
            plan = self._decode(row) if row else None
            if plan is None or plan["definition"]["strategy_version"] != strategy_version:
                raise ValueError("position_review_strategy_version_mismatch")
            if plan["status"] in TERMINAL_PLAN_STATES:
                return {"status": "plan_terminal"}
            prior = conn.execute("select observed_bar_at from autonomous_position_reviews where plan_id=?", (plan_id,)).fetchone()
            if prior and utc_time(prior[0]) >= observed:
                return {"status": "already_reviewed"}
            conn.execute("insert or replace into autonomous_position_reviews values (?,?,?,?,?)",
                         (plan_id, observed.isoformat(), strategy_version, evidence_id, action))
            result = {"status": "reviewed", "action": action, "reason": reason, "evidence_id": evidence_id,
                      "observed_bar_at": observed.isoformat(), "strategy_version": strategy_version}
            if action == "sell":
                identifier = "TX-" + content_hash({"plan_id": plan_id, **result})
                conn.execute("insert into autonomous_plan_exit_requests values (?,?,?,?,?,?,?)",
                             (identifier, plan_id, strategy_version, observed.isoformat(), evidence_id, reason, utc_time(now).isoformat()))
                result["request_id"] = identifier
            self._event(conn, plan_id, utc_time(now).isoformat(), "plan.host_position_review", result)
            conn.commit()
        return result

    def position_review(self, plan_id: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from autonomous_position_reviews where plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None

    def request_agent_exit(self, *, account_id: str, plan_id: str, evidence_id: str, now: datetime) -> dict[str, Any]:
        """Persist a Host-authenticated cancellation/reduction; never add exposure."""
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            row = conn.execute("select * from autonomous_trading_plans where plan_id=? and account_id=?", (plan_id, account_id)).fetchone()
            if row is None:
                raise ValueError("plan_broker_account_mismatch")
            evidence = conn.execute("select 1 from autonomous_evidence where account_id=? and evidence_id=? and kind='agent_exit_decision'", (account_id, evidence_id)).fetchone()
            if evidence is None:
                raise ValueError("retained_agent_exit_decision_required")
            plan = self._decode(row)
            request_id = "TX-" + content_hash({"plan_id": plan_id, "evidence_id": evidence_id})
            prior = conn.execute("select * from autonomous_plan_exit_requests where request_id=?", (request_id,)).fetchone()
            if prior:
                return dict(prior)
            instant = utc_time(now).isoformat()
            result = {"request_id": request_id, "plan_id": plan_id, "strategy_version": plan["definition"]["strategy_version"],
                      "observed_bar_at": instant, "evidence_id": evidence_id, "reason": "agent_reassessment", "created_at": instant}
            # Another Host review in the same instant is already an exit; a
            # duplicate cannot create a second order or increase its quantity.
            conn.execute("insert or ignore into autonomous_plan_exit_requests values (?,?,?,?,?,?,?)", tuple(result.values()))
            saved = conn.execute("select * from autonomous_plan_exit_requests where plan_id=? and observed_bar_at=?", (plan_id, instant)).fetchone()
            self._event(conn, plan_id, instant, "plan.agent_exit_requested", dict(saved))
            conn.commit()
        return dict(saved)

    def exit_request(self, plan_id: str, *, strategy_version: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from autonomous_plan_exit_requests where plan_id=? and strategy_version=? order by observed_bar_at desc limit 1",
                               (plan_id, strategy_version)).fetchone()
        return dict(row) if row else None

    def save_state(self, plan_id: str, *, state: dict[str, Any], revision: int,
                   event_type: str, now: datetime, lease_owner: str) -> dict[str, Any]:
        instant = utc_time(now).isoformat()
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute(
                "update autonomous_trading_plans set state_json=?,status=?,revision=revision+1,updated_at=? where plan_id=? and revision=? and lease_owner=? and lease_until>?",
                (json.dumps(state, ensure_ascii=False, allow_nan=False), state["status"], instant, plan_id, revision, lease_owner, instant),
            ).rowcount
            if changed != 1:
                raise RuntimeError("trading_plan_concurrent_update")
            self._event(conn, plan_id, instant, event_type, state)
            conn.commit()
        return self.get(plan_id)

    def unresolved_dispatches(self, *, account_id: str, excluding_plan_id: str) -> list[str]:
        """Unknown acceptance is account exposure until a broker resolves it."""
        with self.store._connect() as conn:
            rows = conn.execute(
                """select plan_id from autonomous_trading_plans
                     where account_id=? and plan_id<>?
                       and status in ('entry_dispatching', 'exit_dispatching', 'reconciliation_required')
                     order by plan_id""", (account_id, excluding_plan_id),
            ).fetchall()
        return [str(row[0]) for row in rows]

    @contextmanager
    def lease(self, plan_id: str, *, now: datetime, seconds: int = 120) -> Iterator[str | None]:
        if seconds < 1:
            raise ValueError("positive_lease_duration_required")
        instant = utc_time(now)
        owner = uuid4().hex
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute(
                "update autonomous_trading_plans set lease_owner=?,lease_until=? where plan_id=? and (lease_until is null or lease_until<=?)",
                (owner, (instant + timedelta(seconds=seconds)).isoformat(), plan_id, instant.isoformat()),
            ).rowcount
            conn.commit()
        try:
            yield owner if changed else None
        finally:
            if changed:
                with self.store._connect() as conn:
                    conn.execute("update autonomous_trading_plans set lease_owner=null,lease_until=null where plan_id=? and lease_owner=?", (plan_id, owner))
                    conn.commit()

    def events(self, plan_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("select * from autonomous_trading_plan_events where plan_id=? order by sequence", (plan_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    @contextmanager
    def account_lease(self, account_id: str, *, now: datetime, seconds: int = 120) -> Iterator[str | None]:
        """Serialize risk snapshots and submissions across every plan in an account."""
        if seconds < 1:
            raise ValueError("positive_lease_duration_required")
        instant, owner = utc_time(now), uuid4().hex
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            changed = conn.execute(
                "insert into autonomous_account_leases values (?,?,?) on conflict(account_id) do update set lease_owner=excluded.lease_owner,lease_until=excluded.lease_until where autonomous_account_leases.lease_until<=?",
                (account_id, owner, (instant + timedelta(seconds=seconds)).isoformat(), instant.isoformat()),
            ).rowcount
            conn.commit()
        try:
            yield owner if changed else None
        finally:
            if changed:
                with self.store._connect() as conn:
                    conn.execute("delete from autonomous_account_leases where account_id=? and lease_owner=?", (account_id, owner))
                    conn.commit()

    @staticmethod
    def _event(conn, plan_id: str, instant: str, event_type: str, payload: dict) -> None:
        conn.execute("insert into autonomous_trading_plan_events (plan_id,created_at,event_type,payload_json) values (?,?,?,?)", (plan_id, instant, event_type, json.dumps(payload, ensure_ascii=False)))

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        result = dict(row)
        result["definition"] = json.loads(result.pop("definition_json"))
        result["state"] = json.loads(result.pop("state_json"))
        result.pop("lease_owner", None)
        result.pop("lease_until", None)
        return result
