from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .contracts import normalize_timestamp, utc_now
from .source_registry import SourceRegistry
from .warehouse import MarketDataWarehouse, content_hash
from .observability_notifications import ObservabilityNotificationLedger, NotificationSender


DEFAULT_OBSERVABILITY_RULES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "data_observability_rules.yaml"
)


class SourceObservabilityService:
    """Derive source SLO, freshness and revision alerts from immutable records."""

    def __init__(
        self,
        warehouse: MarketDataWarehouse,
        registry: SourceRegistry,
        *,
        rules_path: str | Path = DEFAULT_OBSERVABILITY_RULES_PATH,
        clock: Callable[[], str] = utc_now,
        notification_sender: NotificationSender | None = None,
    ) -> None:
        self.warehouse = warehouse
        self.registry = registry
        self.rules_path = Path(rules_path).expanduser().resolve()
        payload = yaml.safe_load(self.rules_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "stock_ai.data_observability_rules.v1"
        ):
            raise ValueError(
                "Invalid data observability rules; expected "
                "stock_ai.data_observability_rules.v1"
            )
        self.defaults = dict(payload.get("defaults") or {})
        self.partition_expectations = self._expectations(
            payload.get("partition_expectations") or []
        )
        self.clock = clock
        self.notification_sender = notification_sender
        self.notification_ledger = ObservabilityNotificationLedger(warehouse.path)

    @staticmethod
    def _expectations(items: list[Any]) -> dict[tuple[str, str], list[str]]:
        expected: dict[tuple[str, str], list[str]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("partition_expectations entries must be mappings")
            source_id = str(item.get("source_id") or "").strip()
            dataset = str(item.get("dataset") or "").strip()
            partition_keys = sorted(
                {
                    str(value).strip()
                    for value in list(item.get("partition_keys") or [])
                    if str(value).strip()
                }
            )
            if not source_id or not dataset or not partition_keys:
                raise ValueError(
                    "partition expectation requires source_id, dataset and partition_keys"
                )
            key = (source_id, dataset)
            if key in expected:
                raise ValueError(
                    f"duplicate partition expectation for {source_id}/{dataset}"
                )
            expected[key] = partition_keys
        return expected

    def dashboard(self, *, as_of: str | None = None) -> dict[str, Any]:
        cutoff = normalize_timestamp(as_of or self.clock(), required=True)
        assert cutoff is not None
        cutoff_dt = datetime.fromisoformat(cutoff)
        window_hours = max(1, int(self.defaults.get("window_hours") or 24))
        window_start = (cutoff_dt - timedelta(hours=window_hours)).isoformat()
        inputs = self.warehouse.source_observability_inputs(
            window_start=window_start,
            as_of=cutoff,
        )
        records = self._records(inputs)
        expected_keys = set(self.partition_expectations)
        keys = sorted(set(records) | expected_keys)
        alerts: list[dict[str, Any]] = []
        sources = [
            self._source_dashboard(
                source_id=source_id,
                dataset=dataset,
                record=records.get((source_id, dataset), {}),
                cutoff=cutoff_dt,
                alerts=alerts,
            )
            for source_id, dataset in keys
        ]
        severity_counts = Counter(str(item["severity"]) for item in alerts)
        return {
            "schema_version": "stock_ai.source_observability_dashboard.v1",
            "generated_at": cutoff,
            "window": {"start": window_start, "end": cutoff, "hours": window_hours},
            "source_count": len(sources),
            "alert_count": len(alerts),
            "severity_counts": {
                "error": severity_counts["error"],
                "warning": severity_counts["warning"],
                "info": severity_counts["info"],
            },
            "status": (
                "failed"
                if severity_counts["error"]
                else "warning"
                if severity_counts["warning"]
                else "passed"
            ),
            "automatic_alert_evaluation": True,
            "notification_delivery_configured": self.notification_sender is not None,
            "sources": sources,
            "alerts": alerts,
        }

    def notify(self, *, as_of: str | None = None) -> dict[str, Any]:
        """Evaluate alerts and persist one auditable delivery attempt per alert."""

        dashboard = self.dashboard(as_of=as_of)
        receipts = self.notification_ledger.dispatch(
            dashboard["alerts"],
            sender=self.notification_sender,
            now=dashboard["generated_at"],
        )
        counts = Counter(str(item["status"]) for item in receipts)
        return {
            **dashboard,
            "notification_delivery_configured": self.notification_sender is not None,
            "notification_attempt_count": len(receipts),
            "notification_status_counts": dict(counts),
            "notification_receipts": receipts,
        }

    @staticmethod
    def _records(inputs: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], dict[str, Any]]:
        records: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "attempts": [],
                "checkpoints": {},
                "cache_entries": [],
                "revision": {"latest_acquired_at": None, "correction_count": 0},
            }
        )
        for row in inputs["attempts"]:
            records[(str(row["source_id"]), str(row["dataset"]))]["attempts"].append(row)
        for row in inputs["checkpoints"]:
            records[(str(row["source_id"]), str(row["dataset"]))]["checkpoints"][
                str(row["partition_key"])
            ] = row
        for row in inputs["cache_entries"]:
            records[(str(row["source_id"]), str(row["dataset"]))]["cache_entries"].append(row)
        for row in inputs["revisions"]:
            record = records[(str(row["source_id"]), str(row["dataset"]))]["revision"]
            record["latest_acquired_at"] = row["latest_acquired_at"]
            record["correction_count"] = int(row["correction_count"])
        return records

    def _source_dashboard(
        self,
        *,
        source_id: str,
        dataset: str,
        record: dict[str, Any],
        cutoff: datetime,
        alerts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        attempts = list(record.get("attempts") or [])
        succeeded = sum(item.get("status") == "succeeded" for item in attempts)
        attempt_count = len(attempts)
        success_rate = succeeded / attempt_count if attempt_count else None
        minimum = float(self.defaults.get("success_rate_minimum") or 0.95)
        slo_status = (
            "not_measured"
            if success_rate is None
            else "passed"
            if success_rate >= minimum
            else "failed"
        )
        source = self.registry.sources.get(source_id)
        frequency_seconds = int(source.update_frequency_seconds) if source else None
        candidates = [
            record.get("revision", {}).get("latest_acquired_at"),
            *[
                item.get("last_success_at")
                for item in record.get("checkpoints", {}).values()
            ],
            *[item.get("refreshed_at") for item in record.get("cache_entries", [])],
            *[
                item.get("completed_at")
                for item in attempts
                if item.get("status") == "succeeded"
            ],
        ]
        latest_success = max((value for value in candidates if value), default=None)
        lag_seconds = None
        if latest_success:
            lag_seconds = max(
                0.0,
                (cutoff - datetime.fromisoformat(str(latest_success))).total_seconds(),
            )
        lag_threshold = (
            frequency_seconds * max(1, float(self.defaults.get("lag_multiplier") or 3))
            if frequency_seconds
            else None
        )
        has_expectation = (source_id, dataset) in self.partition_expectations
        lag_status = (
            "missing"
            if latest_success is None and has_expectation
            else "not_measured"
            if latest_success is None
            else "stale"
            if lag_threshold is not None and lag_seconds is not None and lag_seconds > lag_threshold
            else "passed"
        )
        expected_partitions = self.partition_expectations.get((source_id, dataset), [])
        checkpoints = dict(record.get("checkpoints") or {})
        missing_partitions = [
            {
                "partition_key": partition_key,
                "status": "missing" if partition_key not in checkpoints else str(checkpoints[partition_key]["status"]),
                "last_success_at": (
                    checkpoints[partition_key].get("last_success_at")
                    if partition_key in checkpoints
                    else None
                ),
            }
            for partition_key in expected_partitions
            if partition_key not in checkpoints
            or checkpoints[partition_key].get("status") != "succeeded"
        ]
        revision = dict(record.get("revision") or {})
        correction_count = int(revision.get("correction_count") or 0)
        anomaly_minimum = max(1, int(self.defaults.get("revision_anomaly_minimum") or 3))
        revision_status = "warning" if correction_count >= anomaly_minimum else "passed"
        item = {
            "source_id": source_id,
            "dataset": dataset,
            "source_display_name": source.display_name if source else source_id,
            "window_attempt_count": attempt_count,
            "window_success_count": succeeded,
            "success_rate": success_rate,
            "success_rate_minimum": minimum,
            "slo_status": slo_status,
            "latest_success_at": latest_success,
            "lag_seconds": lag_seconds,
            "lag_threshold_seconds": lag_threshold,
            "lag_status": lag_status,
            "expected_partition_count": len(expected_partitions),
            "missing_partitions": missing_partitions,
            "revision_correction_count": correction_count,
            "revision_anomaly_minimum": anomaly_minimum,
            "revision_status": revision_status,
        }
        if slo_status == "failed":
            alerts.append(self._alert(item, "source_slo_breach", "error", {
                "actual_success_rate": success_rate,
                "minimum_success_rate": minimum,
            }))
        if lag_status in {"missing", "stale"}:
            alerts.append(self._alert(item, "source_lag", "error" if lag_status == "missing" else "warning", {
                "lag_seconds": lag_seconds,
                "lag_threshold_seconds": lag_threshold,
            }))
        for partition in missing_partitions:
            alerts.append(self._alert(item, "missing_partition", "error", partition))
        if revision_status == "warning":
            alerts.append(self._alert(item, "revision_anomaly", "warning", {
                "correction_count": correction_count,
                "minimum": anomaly_minimum,
            }))
        return item

    @staticmethod
    def _alert(
        item: dict[str, Any],
        code: str,
        severity: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        alert = {
            "source_id": item["source_id"],
            "dataset": item["dataset"],
            "code": code,
            "severity": severity,
            "details": details,
        }
        return {
            "alert_id": "DSO-" + content_hash(alert)[:32],
            **alert,
        }
