from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .migration_safety import (
    complete_startup_migration,
    prepare_startup_migration_backup,
    rollback_startup_migration_backup,
)
from .migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


_SIGNAL_LEDGER_RETENTION = 500
# The desktop UI loads several ledger projections concurrently.  Schema
# migration is a write operation even when the schema is already current, so
# it must be serialized within this process to avoid SQLite lock failures.
_SCHEMA_INITIALIZATION_LOCK = RLock()


@dataclass
class SQLiteStore:
    db_path: str | Path = "output/open_stock_ai.sqlite"

    def __post_init__(self) -> None:
        self.path = Path(self.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def configured(self) -> bool:
        return True

    def save_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = payload.get("request") or {}
        signal = payload.get("signal") or {}
        risk = payload.get("risk") or {}
        execution = payload.get("execution") or {}
        created_at = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into signals (
                    created_at, symbol, market, horizon, action, confidence,
                    risk_approved, executed, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    request.get("symbol"),
                    request.get("market"),
                    request.get("horizon"),
                    signal.get("action"),
                    signal.get("confidence"),
                    1 if risk.get("approved") else 0,
                    1 if execution.get("executed") else 0,
                    json.dumps(self._signal_ledger_payload(payload), ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        return {"saved": True, "id": cursor.lastrowid, "created_at": created_at, "db_path": str(self.path)}

    def recent_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, created_at, symbol, market, horizon, action,
                       confidence, risk_approved, executed, payload_json
                from signals
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            signal = payload.get("signal") or {}
            validation = signal.get("ai_trader_validation") or {}
            items.append(
                {
                    "id": row["id"],
                    "schema_version": "open_stock_ai.signal_ledger_row.v1",
                    "created_at": row["created_at"],
                    "symbol": row["symbol"],
                    "market": row["market"],
                    "horizon": row["horizon"],
                    "action": row["action"],
                    "confidence": row["confidence"],
                    "risk_approved": bool(row["risk_approved"]),
                    "executed": bool(row["executed"]),
                    "ai_trader_valid": validation.get("valid"),
                    "ai_trader_schema": validation.get("schema_title"),
                    "payload": payload,
                }
            )
        return items

    def save_report(self, report: str) -> dict[str, Any]:
        created_at = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                "insert into reports (created_at, report_text) values (?, ?)",
                (created_at, report),
            )
            conn.commit()
        return {"saved": True, "id": cursor.lastrowid, "created_at": created_at, "db_path": str(self.path)}

    def save_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        created_at = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                "insert into trades (created_at, symbol, action, payload_json) values (?, ?, ?, ?)",
                (
                    created_at,
                    trade.get("symbol"),
                    trade.get("action"),
                    json.dumps(trade, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        return {"saved": True, "id": cursor.lastrowid, "created_at": created_at, "db_path": str(self.path)}

    def save_decision_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = payload.get("request") or {}
        signal = payload.get("signal") or {}
        decision_schema = signal.get("decision_schema") or {}
        risk = payload.get("risk") or {}
        execution = payload.get("execution") or {}
        created_at = self._now()
        lesson = self._decision_lesson(signal=signal, risk=risk, execution=execution)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into decision_logs (
                    created_at, symbol, market, horizon, rating, trader_action,
                    signal_action, confidence, entry_price, target_price,
                    stop_loss, position_size_pct, risk_approved, executed,
                    lesson, payload_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    request.get("symbol"),
                    request.get("market"),
                    request.get("horizon"),
                    decision_schema.get("rating"),
                    decision_schema.get("action"),
                    signal.get("action"),
                    signal.get("confidence"),
                    signal.get("entry_price"),
                    signal.get("target_price"),
                    signal.get("stop_loss"),
                    signal.get("position_size_pct"),
                    1 if risk.get("approved") else 0,
                    1 if execution.get("executed") else 0,
                    lesson,
                    "{}",
                ),
            )
            conn.commit()
        return {
            "saved": True,
            "schema_version": "open_stock_ai.decision_log_row.v1",
            "id": cursor.lastrowid,
            "created_at": created_at,
            "lesson": lesson,
            "db_path": str(self.path),
        }

    def recent_decision_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, created_at, symbol, market, horizon, rating,
                       trader_action, signal_action, confidence, entry_price,
                       target_price, stop_loss, position_size_pct,
                       risk_approved, executed, lesson
                from decision_logs
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "schema_version": "open_stock_ai.decision_log_row.v1",
                "risk_approved": bool(row["risk_approved"]),
                "executed": bool(row["executed"]),
            }
            for row in rows
        ]

    def decision_review(self, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        symbol = symbol.strip() if symbol else ""
        where = "where symbol = ?" if symbol else ""
        params: tuple[Any, ...] = (symbol, limit) if symbol else (limit,)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                select id, created_at, symbol, market, horizon, rating,
                       trader_action, signal_action, confidence,
                       position_size_pct, risk_approved, executed, lesson
                from decision_logs
                {where}
                order by id desc
                limit ?
                """,
                params,
            ).fetchall()
        items = [
            {
                **dict(row),
                "schema_version": "open_stock_ai.decision_log_row.v1",
                "risk_approved": bool(row["risk_approved"]),
                "executed": bool(row["executed"]),
            }
            for row in rows
        ]
        total = len(items)
        approved = sum(1 for item in items if item["risk_approved"])
        executed = sum(1 for item in items if item["executed"])
        confidences = [
            float(item["confidence"])
            for item in items
            if item.get("confidence") is not None
        ]
        return {
            "method": "tradingagents_style_decision_replay",
            "schema_version": "open_stock_ai.decision_review.v1",
            "symbol": symbol or None,
            "limit": limit,
            "total": total,
            "approved": approved,
            "executed": executed,
            "approval_rate": round(approved / total, 4) if total else 0.0,
            "execution_rate": round(executed / total, 4) if total else 0.0,
            "average_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "ratings": self._counts(items, "rating"),
            "trader_actions": self._counts(items, "trader_action"),
            "signal_actions": self._counts(items, "signal_action"),
            "latest": items[0] if items else None,
            "lessons": [item["lesson"] for item in items[:10] if item.get("lesson")],
            "items": items,
        }

    def recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, created_at, symbol, action, payload_json
                from trades
                order by id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            items.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "symbol": row["symbol"],
                    "action": row["action"],
                    "schema_version": payload.get("schema_version"),
                    "order_id": payload.get("order_id"),
                    "mode": payload.get("mode"),
                    "risk_approved": payload.get("risk_approved"),
                    "risk_schema_version": payload.get("risk_schema_version"),
                    "risk_gate_count": len(payload.get("risk_gate_checks") or []),
                    "confidence": payload.get("confidence"),
                    "position_size_pct": payload.get("position_size_pct"),
                    "ai_trader_valid": (payload.get("ai_trader_validation") or {}).get("valid"),
                    "ai_trader_schema": (payload.get("ai_trader_validation") or {}).get("schema_title"),
                    "payload": payload,
                }
            )
        return items

    def paper_portfolio_exposure(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, created_at, symbol, action, payload_json
                from trades
                order by id asc
                """
            ).fetchall()
        symbols: dict[str, dict[str, Any]] = {}
        order_count = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("schema_version") != "open_stock_ai.paper_order.v1":
                continue
            if payload.get("mode") != "paper" or payload.get("risk_approved") is not True:
                continue
            symbol = str(payload.get("symbol") or row["symbol"] or "").strip()
            if not symbol:
                continue
            action = str(payload.get("action") or row["action"] or "").lower()
            size = self._safe_float(payload.get("position_size_pct"))
            if size <= 0:
                continue
            direction = -1.0 if action in {"sell", "reduce"} else 1.0
            current = symbols.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": payload.get("market"),
                    "position_size_pct": 0.0,
                    "order_count": 0,
                    "latest_order_id": None,
                    "latest_created_at": None,
                },
            )
            current["position_size_pct"] = max(0.0, round(current["position_size_pct"] + direction * size, 4))
            current["order_count"] += 1
            current["latest_order_id"] = payload.get("order_id")
            current["latest_created_at"] = row["created_at"]
            order_count += 1
        total = round(sum(item["position_size_pct"] for item in symbols.values()), 4)
        return {
            "method": "open_stock_ai_paper_portfolio_exposure",
            "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
            "total_position_size_pct": total,
            "symbol_count": len(symbols),
            "order_count": order_count,
            "symbols": dict(sorted(symbols.items())),
            "db_path": str(self.path),
        }

    def count(self, table: str) -> int:
        if table not in {"signals", "reports", "trades", "decision_logs"}:
            raise ValueError(f"Unsupported table: {table}")
        with self._connect() as conn:
            row = conn.execute(f"select count(*) from {table}").fetchone()
        return int(row[0])

    def save_research_model_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert one content-addressed model version without overwriting it."""

        required = (
            "model_version_id",
            "framework",
            "artifact_sha256",
            "dataset_manifest_hash",
            "dataset_sha256",
            "configuration_sha256",
            "runtime_receipt_sha256",
            "created_at",
        )
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError(f"research_model_version_missing:{','.join(missing)}")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        values = tuple(payload[key] for key in required)
        with self._connect() as conn:
            existing = conn.execute(
                "select payload_json from research_model_versions where model_version_id=?",
                (payload["model_version_id"],),
            ).fetchone()
            if existing is not None:
                prior = json.loads(existing[0])
                identity_keys = (
                    "schema_version", "model_version_id", "framework", "artifact_sha256",
                    "dataset_manifest_hash", "dataset_sha256", "configuration_sha256",
                )
                if any(prior.get(key) != payload.get(key) for key in identity_keys):
                    raise ValueError("research_model_version_immutable_conflict")
                return {"saved": False, "already_exists": True, "model_version_id": payload["model_version_id"]}
            conn.execute(
                """
                insert into research_model_versions (
                    model_version_id, framework, artifact_sha256,
                    dataset_manifest_hash, dataset_sha256, configuration_sha256,
                    runtime_receipt_sha256, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, encoded),
            )
            conn.commit()
        return {"saved": True, "already_exists": False, "model_version_id": payload["model_version_id"]}

    def save_strategy_artifact(
        self,
        payload: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Persist one content-addressed baseline/approved strategy artifact."""

        required = (
            "artifact_id", "strategy_id", "artifact_kind", "source_sha256",
            "configuration_sha256", "approval_status", "created_at",
        )
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError(f"strategy_artifact_missing:{','.join(missing)}")
        if payload["artifact_kind"] not in {"baseline_rule", "approved_rule", "model"}:
            raise ValueError("strategy_artifact_kind_invalid")
        if connection is not None:
            return self._save_strategy_artifact(connection, payload, required)
        with self._connect() as conn:
            result = self._save_strategy_artifact(conn, payload, required)
            conn.commit()
        return result

    @staticmethod
    def _save_strategy_artifact(
        conn: sqlite3.Connection,
        payload: dict[str, Any],
        required: tuple[str, ...],
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        existing = conn.execute(
            "select payload_json from strategy_artifacts where artifact_id=?", (payload["artifact_id"],)
        ).fetchone()
        if existing is not None:
            prior = json.loads(existing[0])
            identity_keys = (
                "schema_version", "artifact_id", "strategy_id", "artifact_kind",
                "source_sha256", "configuration_sha256", "approval_status", "rules",
                "production_eligible", "execution_evidence_eligible", "promotion_boundary",
            )
            if any(prior.get(key) != payload.get(key) for key in identity_keys):
                raise ValueError("strategy_artifact_immutable_conflict")
            return {"saved": False, "already_exists": True, "artifact_id": payload["artifact_id"]}
        conn.execute(
            """
            insert into strategy_artifacts (
                artifact_id, strategy_id, artifact_kind, source_sha256,
                configuration_sha256, approval_status, created_at, payload_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*[payload[key] for key in required], encoded),
        )
        return {"saved": True, "already_exists": False, "artifact_id": payload["artifact_id"]}

    def strategy_artifacts(self, strategy_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            if strategy_id:
                rows = conn.execute(
                    "select payload_json from strategy_artifacts where strategy_id=? order by created_at desc limit ?",
                    (strategy_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select payload_json from strategy_artifacts order by created_at desc limit ?", (limit,)
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_research_experiment_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility facade routed to the dedicated ExperimentStore."""

        from open_stock_ai.research.experiment_store import ExperimentStore

        return ExperimentStore(self.path).save(payload)

    def research_model_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                "select payload_json from research_model_versions order by created_at desc limit ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def research_model_version(self, model_version_id: str) -> dict[str, Any] | None:
        """Return one immutable model version by its content-addressed ID."""

        with self._connect() as conn:
            row = conn.execute(
                "select payload_json from research_model_versions where model_version_id=?",
                (str(model_version_id),),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def research_experiment_receipts(self, limit: int = 50) -> list[dict[str, Any]]:
        from open_stock_ai.research.experiment_store import ExperimentStore

        return ExperimentStore(self.path).list(limit)

    def save_model_promotion_receipt(
        self,
        payload: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append a human-approved champion/challenger deployment receipt."""

        required = (
            "promotion_id", "champion_model_version_id", "challenger_model_version_ids",
            "deployment_mode", "human_promotion_receipt", "created_at",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"model_promotion_missing:{','.join(missing)}")
        challengers = payload["challenger_model_version_ids"]
        if not isinstance(challengers, list) or not challengers:
            raise ValueError("model_promotion_challengers_required")
        if payload["champion_model_version_id"] in challengers:
            raise ValueError("model_promotion_champion_cannot_be_challenger")
        if payload["deployment_mode"] not in {"shadow", "production"}:
            raise ValueError("model_promotion_mode_invalid")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        def write(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = conn.execute(
                "select payload_json from model_promotion_receipts where promotion_id=?",
                (payload["promotion_id"],),
            ).fetchone()
            if existing is not None:
                if json.loads(existing[0]) != json.loads(encoded):
                    raise ValueError("model_promotion_immutable_conflict")
                return {"saved": False, "already_exists": True, "promotion_id": payload["promotion_id"]}
            conn.execute(
                """
                insert into model_promotion_receipts (
                    promotion_id, champion_model_version_id, challenger_model_version_ids_json,
                    deployment_mode, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["promotion_id"], payload["champion_model_version_id"],
                    json.dumps(challengers, ensure_ascii=False, separators=(",", ":")),
                    payload["deployment_mode"], payload["created_at"], encoded,
                ),
            )
            return {"saved": True, "already_exists": False, "promotion_id": payload["promotion_id"]}
        if connection is not None:
            return write(connection)
        with self._connect() as conn:
            result = write(conn)
            conn.commit()
        return result

    def model_promotion_receipts(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                "select payload_json from model_promotion_receipts order by created_at desc limit ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn

    def _init_schema(self) -> None:
        with _SCHEMA_INITIALIZATION_LOCK:
            migration_receipt = prepare_startup_migration_backup(self.path)
            try:
                with self._connect() as conn:
                    apply_migrations(conn)
                    conn.execute(
                        "update decision_logs set payload_json='{}' where payload_json<>'{}'"
                    )
                    conn.execute(
                        """
                        delete from signals
                         where id not in (
                            select id from signals order by id desc limit ?
                         )
                        """,
                        (_SIGNAL_LEDGER_RETENTION,),
                    )
                    rows = conn.execute("select id, payload_json from signals").fetchall()
                    compacted: list[tuple[str, int]] = []
                    for row_id, encoded in rows:
                        try:
                            payload = json.loads(encoded)
                        except (TypeError, json.JSONDecodeError):
                            payload = {}
                        compacted.append(
                            (
                                json.dumps(
                                    self._signal_ledger_payload(payload),
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                int(row_id),
                            )
                        )
                    if compacted:
                        conn.executemany(
                            "update signals set payload_json=? where id=?",
                            compacted,
                        )
                    conn.commit()
            except Exception:
                if migration_receipt is not None:
                    rollback_startup_migration_backup(self.path, migration_receipt)
                raise
            if migration_receipt is not None:
                complete_startup_migration(migration_receipt)

    def _signal_ledger_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep the bounded replay contract while excluding unknown bulk fields."""

        return {
            key: payload[key]
            for key in (
                "schema_version",
                "request",
                "market_snapshot",
                "intelligence",
                "research",
                "signal",
                "risk",
                "execution",
            )
            if key in payload
        }

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _decision_lesson(
        self,
        *,
        signal: dict[str, Any],
        risk: dict[str, Any],
        execution: dict[str, Any],
    ) -> str:
        decision_schema = signal.get("decision_schema") or {}
        rating = decision_schema.get("rating") or "Hold"
        trader_action = decision_schema.get("action") or "Hold"
        if not risk.get("approved"):
            return f"{rating}/{trader_action} held by risk gate: {risk.get('reason') or 'unspecified'}"
        if execution.get("executed"):
            return f"{rating}/{trader_action} converted to paper order for review."
        return f"{rating}/{trader_action} approved but not executed: {execution.get('reason') or 'unspecified'}"

    def _counts(self, items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = item.get(key) or "unknown"
            counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
