from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4
import json
import math
import re
import unicodedata

import yaml

from .contracts import normalize_timestamp, utc_now
from .warehouse import MarketDataWarehouse, canonical_json, content_hash


DEFAULT_RECONCILIATION_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "data_reconciliation_rules.yaml"
)


class ReconciliationEngine:
    """Compare latest point-in-time observations while retaining every source."""

    def __init__(
        self,
        warehouse: MarketDataWarehouse,
        *,
        rules_path: str | Path = DEFAULT_RECONCILIATION_RULES_PATH,
    ) -> None:
        self.warehouse = warehouse
        self.rules_path = Path(rules_path).expanduser().resolve()
        payload = yaml.safe_load(self.rules_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "stock_ai.data_reconciliation_rules.v1"
        ):
            raise ValueError(
                "Invalid reconciliation rules; expected "
                "stock_ai.data_reconciliation_rules.v1"
            )
        self.rules_version = str(payload["schema_version"])
        self.defaults = dict(payload.get("defaults") or {})
        self.dataset_rules = {
            str(key): dict(value or {})
            for key, value in dict(payload.get("datasets") or {}).items()
        }

    def run(
        self,
        *,
        dataset: str,
        entity_id: str | None = None,
        observation_key: str | None = None,
        knowledge_at: str | None = None,
        field_rules: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        dataset_name = str(dataset or "").strip()
        if not dataset_name:
            raise ValueError("dataset is required")
        configured = self.dataset_rules.get(dataset_name)
        if configured is None and field_rules is None:
            raise ValueError(f"No reconciliation rules configured for dataset: {dataset_name}")
        rules = field_rules or dict(configured.get("fields") or {})
        if not rules:
            raise ValueError(f"No reconciliation fields configured for dataset: {dataset_name}")
        cutoff = normalize_timestamp(knowledge_at or utc_now(), required=True)
        assert cutoff is not None
        started_at = utc_now()
        run_id = f"DRR-{uuid4().hex}"
        rows = self._inputs(
            dataset=dataset_name,
            entity_id=entity_id,
            observation_key=observation_key,
            knowledge_at=cutoff,
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row["entity_id"]), str(row["observation_key"]))].append(row)

        conflicts: list[dict[str, Any]] = []
        resolved_ids: set[str] = set()
        comparison_count = 0
        comparable_observations = 0
        for (current_entity, current_observation), observations in grouped.items():
            if len(observations) < 2:
                continue
            comparable_observations += 1
            for left, right in combinations(
                sorted(observations, key=lambda item: str(item["source_id"])),
                2,
            ):
                for field_name, raw_rule in rules.items():
                    if (
                        field_rules is None
                        and field_name not in left["payload"]
                        and field_name not in right["payload"]
                    ):
                        continue
                    rule = {**self.defaults, **dict(raw_rule or {})}
                    comparison_count += 1
                    conflict = self._compare(
                        dataset=dataset_name,
                        entity_id=current_entity,
                        observation_key=current_observation,
                        field_name=str(field_name),
                        rule=rule,
                        left=left,
                        right=right,
                        run_id=run_id,
                    )
                    if conflict is None:
                        resolved_ids.add(
                            self._conflict_id(
                                dataset_name,
                                current_entity,
                                current_observation,
                                str(field_name),
                                str(left["source_id"]),
                                str(right["source_id"]),
                            )
                        )
                    else:
                        conflicts.append(conflict)

        completed_at = utc_now()
        status = (
            "conflict"
            if conflicts
            else "consistent"
            if comparison_count
            else "insufficient_sources"
        )
        source_count = len({str(row["source_id"]) for row in rows})
        summary = {
            "schema_version": "stock_ai.reconciliation_report.v2",
            "run_id": run_id,
            "rules_version": self.rules_version,
            "dataset": dataset_name,
            "domain": str((configured or {}).get("domain") or "custom"),
            "entity_id": entity_id,
            "observation_key": observation_key,
            "knowledge_at": cutoff,
            "status": status,
            "source_count": source_count,
            "observation_count": len(grouped),
            "comparable_observation_count": comparable_observations,
            "comparison_count": comparison_count,
            "conflict_count": len(conflicts),
            "resolved_count": 0,
            "conflicts": conflicts,
            "source_data_preserved": True,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        self._persist_run(summary, resolved_ids)
        return summary

    def run_legacy(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        field_names: Iterable[str],
        tolerance: float,
        as_of: str | None,
    ) -> dict[str, Any]:
        return self.run(
            dataset=dataset,
            entity_id=entity_id,
            observation_key=observation_key,
            knowledge_at=as_of,
            field_rules={
                str(field): {
                    "method": "numeric",
                    "absolute_tolerance": float(tolerance),
                    "relative_tolerance": 0.0,
                }
                for field in field_names
            },
        )

    def status(self) -> dict[str, Any]:
        with self.warehouse._connect() as conn:
            run_count = int(
                conn.execute("select count(*) from data_reconciliation_runs").fetchone()[0]
            )
            open_count = int(
                conn.execute(
                    "select count(*) from data_reconciliation_conflicts where status='open'"
                ).fetchone()[0]
            )
            resolved_count = int(
                conn.execute(
                    "select count(*) from data_reconciliation_conflicts where status='resolved'"
                ).fetchone()[0]
            )
            latest = conn.execute(
                """
                select * from data_reconciliation_runs
                 order by completed_at desc, run_id desc limit 1
                """
            ).fetchone()
        return {
            "schema_version": "stock_ai.reconciliation_status.v1",
            "rules_version": self.rules_version,
            "configured_datasets": sorted(self.dataset_rules),
            "run_count": run_count,
            "open_conflict_count": open_count,
            "resolved_conflict_count": resolved_count,
            "latest_run": self._run_row(latest) if latest is not None else None,
            "source_data_preserved": True,
        }

    def runs(self, *, limit: int = 100) -> dict[str, Any]:
        with self.warehouse._connect() as conn:
            rows = conn.execute(
                """
                select * from data_reconciliation_runs
                 order by completed_at desc, run_id desc limit ?
                """,
                (int(limit),),
            ).fetchall()
        items = [self._run_row(row) for row in rows]
        return {
            "schema_version": "stock_ai.reconciliation_runs.v1",
            "count": len(items),
            "items": items,
        }

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self.warehouse._connect() as conn:
            row = conn.execute(
                "select * from data_reconciliation_runs where run_id=?",
                (run_id,),
            ).fetchone()
        return self._run_row(row) if row is not None else None

    def conflicts(
        self,
        *,
        dataset: str | None = None,
        status: str | None = "open",
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if dataset:
            clauses.append("dataset=?")
            parameters.append(dataset)
        if status:
            clauses.append("status=?")
            parameters.append(status)
        parameters.append(int(limit))
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self.warehouse._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_reconciliation_conflicts
                 {where}
                 order by detected_at desc, conflict_id desc limit ?
                """,
                parameters,
            ).fetchall()
        items = [self._conflict_row(row) for row in rows]
        return {
            "schema_version": "stock_ai.reconciliation_conflicts.v1",
            "count": len(items),
            "items": items,
        }

    def _inputs(
        self,
        *,
        dataset: str,
        entity_id: str | None,
        observation_key: str | None,
        knowledge_at: str,
    ) -> list[dict[str, Any]]:
        clauses = [
            "dataset=?",
            "(published_at is null or published_at <= ?)",
            "available_at <= ?",
            "acquired_at <= ?",
        ]
        parameters: list[Any] = [dataset, knowledge_at, knowledge_at, knowledge_at]
        if entity_id:
            clauses.append("entity_id=?")
            parameters.append(entity_id)
        if observation_key:
            clauses.append("observation_key=?")
            parameters.append(observation_key)
        with self.warehouse._connect() as conn:
            rows = conn.execute(
                f"""
                with ranked as (
                    select revision_id, dataset, entity_id, observation_key,
                           source_id, revision, payload_json, available_at,
                           acquired_at,
                           row_number() over (
                               partition by dataset, entity_id, observation_key, source_id
                               order by revision desc, acquired_at desc, revision_id desc
                           ) as row_rank
                      from data_revisions
                     where {' and '.join(clauses)}
                )
                select * from ranked where row_rank=1
                 order by entity_id, observation_key, source_id
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def _compare(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        field_name: str,
        rule: dict[str, Any],
        left: dict[str, Any],
        right: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any] | None:
        left_value = left["payload"].get(field_name)
        right_value = right["payload"].get(field_name)
        equivalent, absolute_difference, relative_difference = self._equivalent(
            left_value,
            right_value,
            rule,
        )
        if equivalent:
            return None
        left_source = str(left["source_id"])
        right_source = str(right["source_id"])
        conflict_id = self._conflict_id(
            dataset,
            entity_id,
            observation_key,
            field_name,
            left_source,
            right_source,
        )
        tolerance = (
            float(rule.get("absolute_tolerance") or 0)
            if str(rule.get("method") or "exact") == "numeric"
            else float(rule.get("seconds_tolerance") or 0)
            if str(rule.get("method")) == "timestamp"
            else None
        )
        return {
            "conflict_id": conflict_id,
            "run_id": run_id,
            "dataset": dataset,
            "entity_id": entity_id,
            "observation_key": observation_key,
            "field_name": field_name,
            "rule_id": str(rule.get("rule_id") or f"{dataset}.{field_name}"),
            "comparison_method": str(rule.get("method") or "exact"),
            "left_revision_id": str(left["revision_id"]),
            "right_revision_id": str(right["revision_id"]),
            "left_source_id": left_source,
            "right_source_id": right_source,
            "left_value": left_value,
            "right_value": right_value,
            "tolerance": tolerance,
            "absolute_difference": absolute_difference,
            "relative_difference": relative_difference,
            "status": "open",
            "details": {
                "absolute_tolerance": rule.get("absolute_tolerance"),
                "relative_tolerance": rule.get("relative_tolerance"),
                "seconds_tolerance": rule.get("seconds_tolerance"),
                "missing_is_conflict": bool(rule.get("missing_is_conflict", True)),
            },
        }

    def _persist_run(
        self,
        summary: dict[str, Any],
        resolved_ids: set[str],
    ) -> None:
        now = str(summary["completed_at"])
        with self.warehouse._write_lock:
            with self.warehouse._connect() as conn:
                for conflict in summary["conflicts"]:
                    conn.execute(
                        """
                        insert into data_reconciliation_conflicts (
                            conflict_id, dataset, entity_id, observation_key,
                            field_name, left_revision_id, right_revision_id,
                            left_value_json, right_value_json, tolerance, status,
                            detected_at, resolved_at, resolution_json, run_id,
                            rule_id, comparison_method, left_source_id,
                            right_source_id, absolute_difference,
                            relative_difference, details_json
                        ) values (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, null, null,
                            ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        on conflict(conflict_id) do update set
                            left_revision_id=excluded.left_revision_id,
                            right_revision_id=excluded.right_revision_id,
                            left_value_json=excluded.left_value_json,
                            right_value_json=excluded.right_value_json,
                            tolerance=excluded.tolerance,
                            status='open',
                            resolved_at=null,
                            resolution_json=null,
                            run_id=excluded.run_id,
                            rule_id=excluded.rule_id,
                            comparison_method=excluded.comparison_method,
                            left_source_id=excluded.left_source_id,
                            right_source_id=excluded.right_source_id,
                            absolute_difference=excluded.absolute_difference,
                            relative_difference=excluded.relative_difference,
                            details_json=excluded.details_json
                        """,
                        (
                            conflict["conflict_id"],
                            conflict["dataset"],
                            conflict["entity_id"],
                            conflict["observation_key"],
                            conflict["field_name"],
                            conflict["left_revision_id"],
                            conflict["right_revision_id"],
                            canonical_json(conflict["left_value"]),
                            canonical_json(conflict["right_value"]),
                            conflict["tolerance"],
                            now,
                            conflict["run_id"],
                            conflict["rule_id"],
                            conflict["comparison_method"],
                            conflict["left_source_id"],
                            conflict["right_source_id"],
                            conflict["absolute_difference"],
                            conflict["relative_difference"],
                            canonical_json(conflict["details"]),
                        ),
                    )
                resolved_count = 0
                if resolved_ids:
                    placeholders = ",".join("?" for _ in resolved_ids)
                    resolved_count = int(conn.execute(
                        f"""
                        update data_reconciliation_conflicts
                           set status='resolved', resolved_at=?,
                               resolution_json=?
                         where status='open' and conflict_id in ({placeholders})
                        """,
                        (
                            now,
                            canonical_json(
                                {
                                    "resolution": "latest_cross_source_values_converged",
                                    "run_id": summary["run_id"],
                                }
                            ),
                            *sorted(resolved_ids),
                        ),
                    ).rowcount)
                summary["resolved_count"] = resolved_count
                conn.execute(
                    """
                    insert into data_reconciliation_runs (
                        run_id, dataset, entity_id, observation_key, status,
                        rules_version, knowledge_at, source_count,
                        observation_count, comparison_count, conflict_count,
                        resolved_count, summary_json, started_at, completed_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary["run_id"],
                        summary["dataset"],
                        summary["entity_id"],
                        summary["observation_key"],
                        summary["status"],
                        summary["rules_version"],
                        summary["knowledge_at"],
                        summary["source_count"],
                        summary["observation_count"],
                        summary["comparison_count"],
                        summary["conflict_count"],
                        summary["resolved_count"],
                        canonical_json(summary),
                        summary["started_at"],
                        summary["completed_at"],
                    ),
                )
                conn.commit()

    @staticmethod
    def _equivalent(
        left: Any,
        right: Any,
        rule: dict[str, Any],
    ) -> tuple[bool, float | None, float | None]:
        if left is None or right is None:
            if left is None and right is None:
                return True, None, None
            return not bool(rule.get("missing_is_conflict", True)), None, None
        method = str(rule.get("method") or "exact")
        if method == "numeric":
            try:
                left_number = float(left)
                right_number = float(right)
            except (TypeError, ValueError):
                return False, None, None
            if not math.isfinite(left_number) or not math.isfinite(right_number):
                return False, None, None
            difference = abs(left_number - right_number)
            scale = max(abs(left_number), abs(right_number))
            relative = difference / scale if scale else 0.0
            allowed = max(
                float(rule.get("absolute_tolerance") or 0),
                float(rule.get("relative_tolerance") or 0) * scale,
            )
            return difference <= allowed, difference, relative
        if method == "normalized_text":
            return (
                ReconciliationEngine._normalize_text(left)
                == ReconciliationEngine._normalize_text(right),
                None,
                None,
            )
        if method == "timestamp":
            try:
                left_time = datetime.fromisoformat(
                    str(normalize_timestamp(left, required=True))
                )
                right_time = datetime.fromisoformat(
                    str(normalize_timestamp(right, required=True))
                )
            except (TypeError, ValueError):
                return False, None, None
            difference = abs((left_time - right_time).total_seconds())
            return (
                difference <= float(rule.get("seconds_tolerance") or 0),
                difference,
                None,
            )
        return left == right, None, None

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)

    @staticmethod
    def _conflict_id(
        dataset: str,
        entity_id: str,
        observation_key: str,
        field_name: str,
        left_source: str,
        right_source: str,
    ) -> str:
        sources = sorted((left_source, right_source))
        return "DCF-" + content_hash(
            {
                "dataset": dataset,
                "entity_id": entity_id,
                "observation_key": observation_key,
                "field_name": field_name,
                "sources": sources,
            }
        )[:32]

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        payload = dict(row)
        summary = json.loads(payload.pop("summary_json"))
        return {**payload, "summary": summary}

    @staticmethod
    def _conflict_row(row: Any) -> dict[str, Any]:
        payload = dict(row)
        payload["left_value"] = json.loads(payload.pop("left_value_json"))
        payload["right_value"] = json.loads(payload.pop("right_value_json"))
        payload["details"] = json.loads(payload.pop("details_json") or "{}")
        resolution_json = payload.pop("resolution_json")
        payload["resolution"] = json.loads(resolution_json) if resolution_json else None
        return payload
