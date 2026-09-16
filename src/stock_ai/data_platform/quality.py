from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo
import re

import yaml

from .contracts import normalize_timestamp, utc_now
from .warehouse import MarketDataWarehouse, content_hash


DEFAULT_QUALITY_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "data_quality_rules.yaml"
)


class DataQualityService:
    """Generate immutable-evidence daily reports over point-in-time revisions."""

    def __init__(
        self,
        warehouse: MarketDataWarehouse,
        *,
        rules_path: str | Path = DEFAULT_QUALITY_RULES_PATH,
    ) -> None:
        self.warehouse = warehouse
        self.rules_path = Path(rules_path).expanduser().resolve()
        payload = yaml.safe_load(self.rules_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "stock_ai.data_quality_rules.v1"
        ):
            raise ValueError("Invalid data quality rules; expected stock_ai.data_quality_rules.v1")
        self.rules_version = str(payload["schema_version"])
        self.defaults = dict(payload.get("defaults") or {})
        self.dataset_rules = {
            str(key): dict(value or {})
            for key, value in dict(payload.get("datasets") or {}).items()
        }

    def run_daily_report(
        self,
        dataset: str,
        *,
        partition_key: str = "all",
        report_date: str | None = None,
        knowledge_at: str | None = None,
    ) -> dict[str, Any]:
        dataset_name = str(dataset or "").strip()
        if not dataset_name:
            raise ValueError("dataset is required")
        day, cutoff = self._report_window(report_date, knowledge_at)
        scan = self.warehouse.quality_scan_inputs(
            dataset=dataset_name,
            knowledge_at=cutoff,
        )
        rules = self.dataset_rules.get(dataset_name, {})
        issues: list[dict[str, Any]] = []
        latest = scan["latest"]
        history = scan["history"]

        for row in latest:
            self._detect_missing(row, rules, issues)
            self._detect_declared_anomalies(row, rules, issues)
            self._detect_temporal_misalignment(row, rules, issues)
        self._detect_statistical_outliers(latest, rules, issues)
        self._detect_duplicates(history, issues)
        self._include_source_conflicts(scan["conflicts"], issues)

        category_counts = Counter(issue["category"] for issue in issues)
        severity_counts = Counter(issue["severity"] for issue in issues)
        status = (
            "failed"
            if severity_counts["error"]
            else "warning"
            if issues
            else "passed"
        )
        state_hash = content_hash(
            {
                "rules_version": self.rules_version,
                "dataset": dataset_name,
                "partition_key": partition_key,
                "report_date": day,
                "revision_ids": [row["revision_id"] for row in latest],
                "history_revision_ids": [row["revision_id"] for row in history],
                "conflict_ids": [row["conflict_id"] for row in scan["conflicts"]],
                "issues": [self._issue_fingerprint(issue) for issue in issues],
            }
        )
        report_id = f"DQR-{content_hash({'state_hash': state_hash})[:32]}"
        generated_at = utc_now()
        for issue in issues:
            issue["report_id"] = report_id
            issue["issue_id"] = "DQI-" + content_hash(
                {
                    "report_id": report_id,
                    **self._issue_fingerprint(issue),
                }
            )[:32]
            issue["detected_at"] = generated_at

        report = {
            "schema_version": "stock_ai.data_quality_report.v2",
            "report_id": report_id,
            "dataset": dataset_name,
            "partition_key": partition_key,
            "report_date": day,
            "knowledge_at": cutoff,
            "status": status,
            "row_count": len(latest),
            "revision_history_count": len(history),
            "missing_count": category_counts["missing"],
            "anomaly_count": category_counts["anomaly"],
            "time_misalignment_count": category_counts["time_misalignment"],
            "duplicate_count": category_counts["duplicate"],
            "conflict_count": category_counts["source_conflict"],
            "issue_count": len(issues),
            "severity_counts": {
                "error": severity_counts["error"],
                "warning": severity_counts["warning"],
                "info": severity_counts["info"],
            },
            "rules_version": self.rules_version,
            "state_hash": state_hash,
            "generated_at": generated_at,
            "issues": issues,
        }
        return self.warehouse.persist_quality_report(report)

    def run_daily_reports(
        self,
        *,
        datasets: Iterable[str] | None = None,
        report_date: str | None = None,
        knowledge_at: str | None = None,
    ) -> dict[str, Any]:
        selected = sorted(
            {
                str(item).strip()
                for item in (datasets or self.warehouse.quality_datasets())
                if str(item).strip()
            }
        )
        reports = [
            self.run_daily_report(
                dataset,
                report_date=report_date,
                knowledge_at=knowledge_at,
            )
            for dataset in selected
        ]
        return {
            "schema_version": "stock_ai.daily_data_quality_run.v1",
            "report_date": reports[0]["report_date"] if reports else report_date,
            "dataset_count": len(reports),
            "status_counts": dict(Counter(report["status"] for report in reports)),
            "issue_count": sum(int(report["issue_count"]) for report in reports),
            "reports": reports,
        }

    def list_reports(
        self,
        *,
        dataset: str | None = None,
        report_date: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = self.warehouse.list_quality_reports(
            dataset=dataset,
            report_date=report_date,
            limit=limit,
        )
        return {
            "schema_version": "stock_ai.data_quality_report_list.v1",
            "count": len(items),
            "items": items,
        }

    def report(self, report_id: str) -> dict[str, Any] | None:
        return self.warehouse.quality_report_detail(report_id)

    @staticmethod
    def _report_window(
        report_date: str | None,
        knowledge_at: str | None,
    ) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        market_timezone = ZoneInfo("Asia/Taipei")
        market_today = now.astimezone(market_timezone).date()
        try:
            day = date.fromisoformat(report_date or market_today.isoformat())
        except ValueError as exc:
            raise ValueError("report_date must use YYYY-MM-DD") from exc
        if day > market_today:
            raise ValueError("report_date cannot be in the future")
        end_of_market_day = datetime.combine(
            day,
            time.max,
            tzinfo=market_timezone,
        ).astimezone(timezone.utc)
        maximum_cutoff = now if day == market_today else end_of_market_day
        if knowledge_at:
            cutoff = normalize_timestamp(knowledge_at, required=True)
            assert cutoff is not None
            if datetime.fromisoformat(cutoff) > maximum_cutoff:
                raise ValueError("knowledge_at cannot exceed the report day's cutoff")
        elif day == market_today:
            cutoff = maximum_cutoff.isoformat()
        else:
            cutoff = end_of_market_day.isoformat()
        return day.isoformat(), cutoff

    def _detect_missing(
        self,
        row: dict[str, Any],
        rules: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        payload = row["payload"]
        for field_name in rules.get("required_fields") or []:
            value, found = self._path_value(payload, str(field_name))
            if not found or self._is_missing(value):
                issues.append(
                    self._issue(
                        row,
                        category="missing",
                        severity="error",
                        code="required_field_missing",
                        field_name=str(field_name),
                        expected="non-empty value",
                        actual=value if found else "field_absent",
                    )
                )
        if row["quality_status"] in {"invalid", "unavailable"}:
            issues.append(
                self._issue(
                    row,
                    category="missing",
                    severity="error",
                    code="revision_unavailable",
                    expected="valid or warning",
                    actual=row["quality_status"],
                )
            )

    def _detect_declared_anomalies(
        self,
        row: dict[str, Any],
        rules: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        informational = set(self.defaults.get("informational_flags") or [])
        for flag in sorted(set(row["quality_flags"]) - informational):
            issues.append(
                self._issue(
                    row,
                    category="anomaly",
                    severity="warning",
                    code="declared_quality_flag",
                    actual=flag,
                )
            )
        if row["quality_status"] == "warning" and not row["quality_flags"]:
            issues.append(
                self._issue(
                    row,
                    category="anomaly",
                    severity="warning",
                    code="unexplained_warning_status",
                    actual="warning",
                )
            )
        payload = row["payload"]
        numeric_fields = {str(item) for item in rules.get("numeric_fields") or []}
        non_negative = {str(item) for item in rules.get("non_negative_fields") or []}
        for field_name in sorted(numeric_fields):
            value, found = self._path_value(payload, field_name)
            if not found or self._is_missing(value):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                issues.append(
                    self._issue(
                        row,
                        category="anomaly",
                        severity="warning",
                        code="non_numeric_value",
                        field_name=field_name,
                        expected="finite number",
                        actual=value,
                    )
                )
                continue
            if not isfinite(float(value)):
                issues.append(
                    self._issue(
                        row,
                        category="anomaly",
                        severity="error",
                        code="non_finite_number",
                        field_name=field_name,
                        expected="finite number",
                        actual=str(value),
                    )
                )
            elif field_name in non_negative and float(value) < 0:
                issues.append(
                    self._issue(
                        row,
                        category="anomaly",
                        severity="warning",
                        code="negative_value",
                        field_name=field_name,
                        expected="greater than or equal to zero",
                        actual=value,
                    )
                )
        if "ohlc" in set(rules.get("consistency_rules") or []):
            values: dict[str, float] = {}
            for field_name in ("open", "high", "low", "close"):
                value, found = self._path_value(payload, field_name)
                if (
                    found
                    and not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and isfinite(float(value))
                ):
                    values[field_name] = float(value)
            if {"high", "low"} <= values.keys() and values["high"] < values["low"]:
                issues.append(
                    self._issue(
                        row,
                        category="anomaly",
                        severity="warning",
                        code="ohlc_high_below_low",
                        expected={"high": ">= low"},
                        actual=values,
                    )
                )
            if {"high", "low"} <= values.keys():
                for field_name in ("open", "close"):
                    if field_name in values and not (
                        values["low"] <= values[field_name] <= values["high"]
                    ):
                        issues.append(
                            self._issue(
                                row,
                                category="anomaly",
                                severity="warning",
                                code="ohlc_value_outside_range",
                                field_name=field_name,
                                expected={
                                    "minimum": values["low"],
                                    "maximum": values["high"],
                                },
                                actual=values[field_name],
                            )
                        )

    def _detect_statistical_outliers(
        self,
        rows: list[dict[str, Any]],
        rules: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        minimum = int(self.defaults.get("minimum_outlier_sample_size") or 5)
        threshold = float(self.defaults.get("mad_zscore_threshold") or 6.0)
        flat_ratio = float(self.defaults.get("flat_series_deviation_ratio") or 0.5)
        for field_name in rules.get("numeric_fields") or []:
            observations: list[tuple[dict[str, Any], float]] = []
            for row in rows:
                value, found = self._path_value(row["payload"], str(field_name))
                if (
                    found
                    and not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and isfinite(float(value))
                ):
                    observations.append((row, float(value)))
            if len(observations) < minimum:
                continue
            center = median(value for _, value in observations)
            absolute_deviations = [
                abs(value - center) for _, value in observations
            ]
            mad = median(absolute_deviations)
            for row, value in observations:
                robust_score = (
                    0.6745 * abs(value - center) / mad
                    if mad > 0
                    else abs(value - center) / max(abs(center), 1.0)
                )
                is_outlier = robust_score > (threshold if mad > 0 else flat_ratio)
                if is_outlier:
                    issues.append(
                        self._issue(
                            row,
                            category="anomaly",
                            severity="warning",
                            code="robust_statistical_outlier",
                            field_name=str(field_name),
                            expected={
                                "median": center,
                                "mad": mad,
                                "threshold": threshold if mad > 0 else flat_ratio,
                            },
                            actual={"value": value, "score": robust_score},
                        )
                    )

    def _detect_temporal_misalignment(
        self,
        row: dict[str, Any],
        rules: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        comparisons = (
            ("published_at", "available_at", "published_after_available"),
            ("available_at", "acquired_at", "available_after_acquired"),
            ("period_start", "period_end", "period_start_after_end"),
        )
        for left, right, code in comparisons:
            if row.get(left) and row.get(right) and row[left] > row[right]:
                issues.append(
                    self._issue(
                        row,
                        category="time_misalignment",
                        severity="error",
                        code=code,
                        expected={left: f"<= {right}"},
                        actual={left: row[left], right: row[right]},
                    )
                )
        if row["time_basis"] == "trade_date" and not row.get("trade_date"):
            issues.append(
                self._issue(
                    row,
                    category="time_misalignment",
                    severity="error",
                    code="trade_date_coordinate_missing",
                )
            )
        if row["time_basis"] == "fiscal_period" and not all(
            row.get(key) for key in ("fiscal_period", "period_start", "period_end")
        ):
            issues.append(
                self._issue(
                    row,
                    category="time_misalignment",
                    severity="error",
                    code="fiscal_period_coordinates_missing",
                )
            )
        payload = row["payload"]
        for field_name in rules.get("observation_date_fields") or []:
            value, found = self._path_value(payload, str(field_name))
            if not found or self._is_missing(value):
                continue
            expected = (
                row.get("trade_date")
                if str(field_name) == "trade_date"
                else row.get("fiscal_period")
                if str(field_name) == "period"
                else None
            )
            if expected and str(value)[:10] != str(expected)[:10]:
                issues.append(
                    self._issue(
                        row,
                        category="time_misalignment",
                        severity="error",
                        code="payload_time_coordinate_mismatch",
                        field_name=str(field_name),
                        expected=expected,
                        actual=value,
                    )
                )
        if (
            row.get("trade_date")
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["observation_key"])
            and row["observation_key"] != str(row["trade_date"])[:10]
        ):
            issues.append(
                self._issue(
                    row,
                    category="time_misalignment",
                    severity="error",
                    code="observation_key_trade_date_mismatch",
                    expected=row["trade_date"],
                    actual=row["observation_key"],
                )
            )

    def _detect_duplicates(
        self,
        rows: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> None:
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[
                (
                    row["entity_id"],
                    row["observation_key"],
                    row["source_id"],
                    row["payload_hash"],
                )
            ].append(row)
        for duplicate_rows in grouped.values():
            if len(duplicate_rows) < 2:
                continue
            last = duplicate_rows[-1]
            issues.append(
                self._issue(
                    last,
                    category="duplicate",
                    severity="warning",
                    code="repeated_payload_revision",
                    expected="one occurrence of a payload per logical source record",
                    actual={
                        "occurrences": len(duplicate_rows),
                        "revision_ids": [row["revision_id"] for row in duplicate_rows],
                    },
                )
            )

    def _include_source_conflicts(
        self,
        conflicts: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> None:
        for conflict in conflicts:
            issues.append(
                {
                    "category": "source_conflict",
                    "severity": "warning",
                    "code": "open_reconciliation_conflict",
                    "revision_id": conflict["left_revision_id"],
                    "entity_id": conflict["entity_id"],
                    "observation_key": conflict["observation_key"],
                    "source_id": None,
                    "field_name": conflict["field_name"],
                    "expected": {
                        "tolerance": conflict["tolerance"],
                        "status": "consistent",
                    },
                    "actual": {
                        "left_revision_id": conflict["left_revision_id"],
                        "right_revision_id": conflict["right_revision_id"],
                        "left_value": conflict["left_value"],
                        "right_value": conflict["right_value"],
                    },
                    "details": {"conflict_id": conflict["conflict_id"]},
                }
            )

    @staticmethod
    def _issue(
        row: dict[str, Any],
        *,
        category: str,
        severity: str,
        code: str,
        field_name: str | None = None,
        expected: Any = None,
        actual: Any = None,
    ) -> dict[str, Any]:
        return {
            "category": category,
            "severity": severity,
            "code": code,
            "revision_id": row["revision_id"],
            "entity_id": row["entity_id"],
            "observation_key": row["observation_key"],
            "source_id": row["source_id"],
            "field_name": field_name,
            "expected": expected,
            "actual": actual,
            "details": {},
        }

    @staticmethod
    def _issue_fingerprint(issue: dict[str, Any]) -> dict[str, Any]:
        return {
            key: issue.get(key)
            for key in (
                "category",
                "severity",
                "code",
                "revision_id",
                "entity_id",
                "observation_key",
                "source_id",
                "field_name",
                "expected",
                "actual",
                "details",
            )
        }

    @staticmethod
    def _path_value(payload: dict[str, Any], field_name: str) -> tuple[Any, bool]:
        current: Any = payload
        parts = (
            [part for part in field_name.strip("/").split("/") if part]
            if field_name.startswith("/")
            else [part for part in field_name.split(".") if part]
        )
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return None, False
            current = current[part]
        return current, True

    @staticmethod
    def _is_missing(value: Any) -> bool:
        return value is None or (
            isinstance(value, str) and not value.strip()
        ) or (
            isinstance(value, (list, dict)) and not value
        )
