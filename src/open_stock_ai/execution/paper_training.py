from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.learning.memory_retriever import MemoryRetriever
from open_stock_ai.learning.policy_proposal import PolicyProposalManager


@dataclass
class PaperTrainingLab:
    """Persistent paper-trading experiment and reflection framework.

    The lab never invents symbols or prices. Price resolution happens in the
    Stock AI market-data layer and only verified marks are accepted here.
    Research and RiskEngine results may be recorded as advisory evidence, but
    they do not block an experiment. Accounting invariants still apply: an
    account cannot spend cash it does not have or sell shares it does not own.
    """

    store: SQLiteStore
    oms: PaperOMS

    def reset_account(self, initial_cash: float) -> dict[str, Any]:
        amount = float(initial_cash)
        if amount <= 0:
            raise ValueError("initial_cash_must_be_positive")
        account_id = self.oms.account_id
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                "delete from strategy_versions where proposal_id in (select proposal_id from policy_proposals where account_id = ?)",
                (account_id,),
            )
            conn.execute("delete from policy_proposals where account_id = ?", (account_id,))
            conn.execute("delete from agent_reflections where account_id = ?", (account_id,))
            conn.execute("delete from agent_learning_events where account_id = ?", (account_id,))
            conn.execute("delete from agent_learning_episodes where account_id = ?", (account_id,))
            conn.execute("delete from paper_price_marks where account_id = ?", (account_id,))
            conn.execute("delete from paper_settlement_events where account_id = ?", (account_id,))
            conn.execute("delete from paper_settlements where account_id = ?", (account_id,))
            conn.execute("delete from paper_fills where account_id = ?", (account_id,))
            conn.execute("delete from paper_orders where account_id = ?", (account_id,))
            conn.execute("delete from paper_positions where account_id = ?", (account_id,))
            conn.execute("delete from cash_ledger where account_id = ?", (account_id,))
            conn.execute("delete from paper_accounts where account_id = ?", (account_id,))
            conn.commit()
        self.oms.initial_cash = amount
        self.oms.ensure_account()
        episode = self.start_episode(
            objective="Fresh paper-training account",
            metadata={"reset": True, "initial_cash": amount},
        )
        return {
            "schema_version": "open_stock_ai.paper_training_reset.v1",
            "account": self.account_summary(),
            "episode": episode,
        }

    def account_summary(self) -> dict[str, Any]:
        summary = self.oms.portfolio_summary()
        latest_marks = self.latest_marks()
        for position in summary.get("positions") or []:
            mark = latest_marks.get(str(position.get("symbol")))
            if mark:
                position["valuation_basis"] = "latest_verified_market_mark"
                position["price_source"] = mark.get("price_source")
                position["price_timestamp"] = mark.get("source_timestamp")
                position["price_received_at"] = mark.get("received_at")
                position["is_realtime"] = mark.get("is_realtime") is True
                position["is_fallback"] = mark.get("is_fallback") is True
            else:
                position["valuation_basis"] = "latest_real_paper_fill_price"
                position["price_source"] = "paper_fill"
        summary["schema_version"] = "open_stock_ai.paper_training_account.v1"
        summary["valuation_basis"] = "cash_plus_latest_verified_market_marks_or_real_fills"
        summary["latest_marks"] = latest_marks
        summary["learning"] = self.learning_summary(limit=20)
        summary["training_policy"] = {
            "research_gate_enforced": False,
            "risk_gate_enforced": False,
            "risk_is_advisory": True,
            "real_symbol_required": True,
            "verified_market_price_required": True,
            "cash_and_position_invariants_enforced": True,
            "live_broker_submission": False,
        }
        return summary

    def apply_market_mark(
        self,
        *,
        symbol: str,
        market: str,
        price: float,
        price_source: str,
        source_timestamp: str,
        is_realtime: bool,
        is_fallback: bool,
        metadata: dict[str, Any] | None = None,
        episode_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol or "").strip()
        value = float(price)
        if not normalized_symbol:
            raise ValueError("missing_symbol")
        if value <= 0:
            raise ValueError("verified_price_must_be_positive")
        if not str(price_source or "").strip():
            raise ValueError("missing_price_source")
        received_at = self._now()
        payload = metadata or {}
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into paper_price_marks (
                    account_id, symbol, market, price, price_source,
                    source_timestamp, received_at, is_realtime, is_fallback,
                    metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.oms.account_id,
                    normalized_symbol,
                    market,
                    value,
                    price_source,
                    source_timestamp,
                    received_at,
                    1 if is_realtime else 0,
                    1 if is_fallback else 0,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            conn.execute(
                """
                update paper_positions
                set last_price = ?, updated_at = ?
                where account_id = ? and symbol = ? and quantity <> 0
                """,
                (value, received_at, self.oms.account_id, normalized_symbol),
            )
            conn.commit()
        if episode_id:
            self.record_event(
                episode_id=episode_id,
                event_type="market_mark",
                symbol=normalized_symbol,
                price=value,
                payload={
                    "market": market,
                    "price_source": price_source,
                    "source_timestamp": source_timestamp,
                    "is_realtime": is_realtime,
                    "is_fallback": is_fallback,
                    **payload,
                },
            )
        return {
            "schema_version": "open_stock_ai.paper_market_mark.v1",
            "symbol": normalized_symbol,
            "market": market,
            "price": value,
            "price_source": price_source,
            "source_timestamp": source_timestamp,
            "received_at": received_at,
            "is_realtime": is_realtime,
            "is_fallback": is_fallback,
            "account": self.account_summary(),
        }

    def latest_marks(self) -> dict[str, dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select m.*
                from paper_price_marks m
                join (
                    select symbol, max(id) as max_id
                    from paper_price_marks
                    where account_id = ?
                    group by symbol
                ) latest on latest.max_id = m.id
                order by m.symbol
                """,
                (self.oms.account_id,),
            ).fetchall()
        return {
            str(row["symbol"]): {
                "symbol": row["symbol"],
                "market": row["market"],
                "price": row["price"],
                "price_source": row["price_source"],
                "source_timestamp": row["source_timestamp"],
                "received_at": row["received_at"],
                "is_realtime": bool(row["is_realtime"]),
                "is_fallback": bool(row["is_fallback"]),
            }
            for row in rows
        }

    def start_episode(
        self,
        *,
        objective: str,
        actor: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        episode_id = f"EP-{uuid4().hex}"
        starting_equity = float(self.oms.portfolio_summary().get("total_equity") or 0.0)
        with self.store._connect() as conn:
            conn.execute(
                """
                insert into agent_learning_episodes (
                    episode_id, account_id, created_at, updated_at, status,
                    actor, objective, starting_equity, ending_equity,
                    total_reward, metadata_json
                ) values (?, ?, ?, ?, 'active', ?, ?, ?, null, null, ?)
                """,
                (
                    episode_id,
                    self.oms.account_id,
                    now,
                    now,
                    actor,
                    objective,
                    starting_equity,
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        return {
            "schema_version": "open_stock_ai.agent_learning_episode.v1",
            "episode_id": episode_id,
            "status": "active",
            "actor": actor,
            "objective": objective,
            "starting_equity": starting_equity,
            "created_at": now,
        }

    def record_event(
        self,
        *,
        episode_id: str,
        event_type: str,
        symbol: str | None = None,
        action: str | None = None,
        price: float | None = None,
        quantity: float | None = None,
        rationale: str | None = None,
        reward: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        equity = float(self.oms.portfolio_summary().get("total_equity") or 0.0)
        with self.store._connect() as conn:
            episode = conn.execute(
                "select episode_id from agent_learning_episodes where episode_id = ? and account_id = ?",
                (episode_id, self.oms.account_id),
            ).fetchone()
            if episode is None:
                raise ValueError("learning_episode_not_found")
            cursor = conn.execute(
                """
                insert into agent_learning_events (
                    episode_id, account_id, created_at, event_type, symbol,
                    action, price, quantity, equity, reward, rationale,
                    payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    self.oms.account_id,
                    now,
                    event_type,
                    symbol,
                    action,
                    price,
                    quantity,
                    equity,
                    reward,
                    rationale,
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                ),
            )
            conn.execute(
                "update agent_learning_episodes set updated_at = ? where episode_id = ?",
                (now, episode_id),
            )
            conn.commit()
        return {
            "schema_version": "open_stock_ai.agent_learning_event.v1",
            "id": cursor.lastrowid,
            "episode_id": episode_id,
            "event_type": event_type,
            "equity": equity,
            "created_at": now,
        }

    def evaluate_episode(self, episode_id: str, *, close: bool = False) -> dict[str, Any]:
        now = self._now()
        ending_equity = float(self.oms.portfolio_summary().get("total_equity") or 0.0)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_learning_episodes where episode_id = ? and account_id = ?",
                (episode_id, self.oms.account_id),
            ).fetchone()
            if row is None:
                raise ValueError("learning_episode_not_found")
            starting_equity = float(row["starting_equity"] or 0.0)
            reward = ending_equity - starting_equity
            status = "closed" if close else str(row["status"] or "active")
            conn.execute(
                """
                update agent_learning_episodes
                set updated_at = ?, status = ?, ending_equity = ?, total_reward = ?
                where episode_id = ?
                """,
                (now, status, ending_equity, reward, episode_id),
            )
            conn.commit()
        self.record_event(
            episode_id=episode_id,
            event_type="episode_evaluation",
            reward=reward,
            payload={"starting_equity": starting_equity, "ending_equity": ending_equity, "closed": close},
        )
        return {
            "schema_version": "open_stock_ai.agent_learning_evaluation.v1",
            "episode_id": episode_id,
            "status": status,
            "starting_equity": starting_equity,
            "ending_equity": ending_equity,
            "reward": reward,
            "return_pct": round(reward / starting_equity * 100.0, 6) if starting_equity else 0.0,
            "evaluated_at": now,
        }

    def save_reflection(
        self,
        *,
        episode_id: str,
        summary: str,
        lessons: list[str],
        next_rules: list[str],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self.store._connect() as conn:
            episode = conn.execute(
                "select episode_id from agent_learning_episodes where episode_id = ? and account_id = ?",
                (episode_id, self.oms.account_id),
            ).fetchone()
            if episode is None:
                raise ValueError("learning_episode_not_found")
            cursor = conn.execute(
                """
                insert into agent_reflections (
                    episode_id, account_id, created_at, summary,
                    lessons_json, next_rules_json, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    self.oms.account_id,
                    now,
                    summary,
                    json.dumps(lessons, ensure_ascii=False),
                    json.dumps(next_rules, ensure_ascii=False),
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        self.record_event(
            episode_id=episode_id,
            event_type="reflection",
            rationale=summary,
            payload={"lessons": lessons, "next_rules": next_rules},
        )
        proposal = None
        if next_rules:
            proposal = PolicyProposalManager(self.store).create(
                account_id=self.oms.account_id,
                reflection_id=int(cursor.lastrowid),
                title=f"Reflection proposal for {episode_id}",
                rules=[str(item) for item in next_rules],
                evidence={"episode_id": episode_id, "reflection_summary": summary},
            )
        return {
            "schema_version": "open_stock_ai.agent_reflection.v1",
            "id": cursor.lastrowid,
            "episode_id": episode_id,
            "summary": summary,
            "lessons": lessons,
            "next_rules": next_rules,
            "policy_proposal": proposal,
            "created_at": now,
        }

    def learning_summary(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            episodes = conn.execute(
                """
                select * from agent_learning_episodes
                where account_id = ?
                order by created_at desc
                limit ?
                """,
                (self.oms.account_id, limit),
            ).fetchall()
            events = conn.execute(
                """
                select * from agent_learning_events
                where account_id = ?
                order by id desc
                limit ?
                """,
                (self.oms.account_id, limit * 5),
            ).fetchall()
            reflections = conn.execute(
                """
                select * from agent_reflections
                where account_id = ?
                order by id desc
                limit ?
                """,
                (self.oms.account_id, limit),
            ).fetchall()
        episode_items = [dict(row) for row in episodes]
        rewards = [float(row["total_reward"]) for row in episodes if row["total_reward"] is not None]
        memory = MemoryRetriever(self.store).retrieve(
            account_id=self.oms.account_id,
            run_state={},
            limit=limit,
        )
        return {
            "schema_version": "open_stock_ai.agent_learning_summary.v1",
            "episode_count": len(episode_items),
            "evaluated_episode_count": len(rewards),
            "positive_episode_count": sum(1 for reward in rewards if reward > 0),
            "negative_episode_count": sum(1 for reward in rewards if reward < 0),
            "total_reward": round(sum(rewards), 6),
            "average_reward": round(sum(rewards) / len(rewards), 6) if rewards else 0.0,
            "episodes": episode_items,
            "events": [dict(row) for row in events],
            "reflections": [
                {
                    **dict(row),
                    "lessons": self._json_list(row["lessons_json"]),
                    "next_rules": self._json_list(row["next_rules_json"]),
                }
                for row in reflections
            ],
            "memory_layers": memory,
            "policy_proposals": memory["policy_proposal_memory"],
            "learning_boundary": (
                "Reflections are evidence and proposals. They do not rewrite production strategy code or enable live trading."
            ),
        }

    def _json_list(self, value: str | None) -> list[str]:
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
