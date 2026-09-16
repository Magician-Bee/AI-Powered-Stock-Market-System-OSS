"""Runtime wiring for durable operational alerts and dashboard diagnostics."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from open_stock_ai.research.model_drift import evaluate_model_drift

from .chaos_recovery_runtime import ChaosRecoveryRuntime
from .operational_alerts import (
    DurableOperationalAlertStore,
    OperationalAlertPolicy,
    OperationalAlertRouter,
)


class OperationalAlertRuntime:
    """Evaluate real local runtime signals without inventing live-broker health.

    The durable ledger is intentionally shared with the Agent runtime SQLite
    authority.  A read-only dashboard request may record a newly observed
    alert, but repeated refreshes are deduplicated by the immutable alert ID.
    No alert sink is configured here: on-call delivery remains an explicit
    external production requirement rather than a claimed local capability.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        storage_report: Callable[[], Mapping[str, Any]],
        broker_snapshot: Callable[[], Mapping[str, Any]],
        model_monitor: Callable[[], Mapping[str, Any]] | None = None,
        chaos_runtime: ChaosRecoveryRuntime | None = None,
        policy: OperationalAlertPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.store = DurableOperationalAlertStore(self.database_path)
        self.storage_report = storage_report
        self.broker_snapshot = broker_snapshot
        self.model_monitor = model_monitor or (lambda: evaluate_model_drift(None))
        self.chaos_runtime = chaos_runtime or ChaosRecoveryRuntime(self.database_path, clock=clock)
        self.policy = policy or OperationalAlertPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def dashboard(self) -> dict[str, Any]:
        storage = _mapping(self.storage_report())
        broker = _mapping(self.broker_snapshot())
        model = _mapping(self.model_monitor())
        chaos = self.chaos_runtime.dashboard()
        signals, coverage = self._signals(storage=storage, broker=broker, model=model, chaos=chaos)
        now = self.clock().astimezone(timezone.utc).isoformat()
        current = OperationalAlertRouter(policy=self.policy, clock=lambda: now).evaluate(signals)
        new_alerts = OperationalAlertRouter(
            policy=self.policy,
            clock=lambda: now,
            store=self.store,
        ).evaluate(signals)
        blocking = [item.code for item in current if item.stop_new_orders]
        persisted = self.store.alerts(limit=50)
        return {
            "schema_version": "open_stock_ai.operational_alert_dashboard.v1",
            "generated_at": now,
            "policy": {
                "feed_stale_seconds": self.policy.feed_stale_seconds,
                "database_pressure_ratio": self.policy.database_pressure_ratio,
            },
            "coverage": coverage,
            "current_alerts": [item.as_dict() for item in current],
            "new_alerts": [item.as_dict() for item in new_alerts],
            "recent_alerts": [item.as_dict() for item in persisted],
            "delivery": {
                "configured": False,
                "status": "not_configured",
                "blocker": "production_alert_sink_on_call_routing_not_configured",
            },
            "order_gate": {
                "live_submission": {
                    "blocked": True,
                    "reasons": ["live_order_submission_disabled", *blocking],
                },
                "paper_sandbox": {
                    "blocked": False,
                    "reason": "local_paper_orders_do_not_share_live_broker_authority",
                },
            },
            "storage": storage,
            "broker_runtime": broker,
            "model_monitor": model,
            "chaos_recovery": chaos,
        }

    def _signals(
        self,
        *,
        storage: Mapping[str, Any],
        broker: Mapping[str, Any],
        model: Mapping[str, Any],
        chaos: Mapping[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        metrics = [item for item in broker.get("metrics") or [] if isinstance(item, Mapping)]
        broker_observed = bool(metrics)
        feed_freshness = _oldest_metric_age_seconds(metrics, now=self.clock())
        unknown = sum(_integer(item.get("unknown_order_count")) for item in metrics)
        mismatches = sum(
            _integer(item.get("order_reconciliation_mismatch_count"))
            + _integer(item.get("account_reconciliation_mismatch_count"))
            for item in metrics
        )
        pressure = _filesystem_pressure(self.database_path.parent)
        storage_healthy = storage.get("healthy") is True
        signals = {
            "feed": {"enabled": broker_observed, "freshness_seconds": feed_freshness},
            "reconciliation": {"enabled": broker_observed, "mismatch_count": mismatches},
            "orders": {"enabled": broker_observed, "unknown_count": unknown},
            "database": {"enabled": True, "pressure_ratio": pressure if storage_healthy else None},
            "model": {"enabled": True, **model},
            "chaos": {
                "enabled": chaos.get("status") == "blocked",
                "triggered": chaos.get("triggered") is True,
                "fault": chaos.get("reason"),
            },
        }
        coverage = {
            "feed": _coverage(broker_observed, "broker_runtime_metrics_not_observed"),
            "reconciliation": _coverage(broker_observed, "broker_runtime_metrics_not_observed"),
            "orders": _coverage(broker_observed, "broker_runtime_metrics_not_observed"),
            "database": _coverage(True, None if storage_healthy else "database_metadata_unavailable"),
            "model": _coverage(True, None),
            "chaos": _coverage(
                chaos.get("status") != "not_configured",
                None if chaos.get("status") == "recovered" else str(chaos.get("reason") or "no_active_chaos_campaign"),
            ),
        }
        return signals, coverage


def _mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _oldest_metric_age_seconds(metrics: list[Mapping[str, Any]], *, now: datetime) -> float | None:
    observed: list[datetime] = []
    for item in metrics:
        raw = item.get("observed_at")
        try:
            stamp = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        observed.append(stamp.astimezone(timezone.utc))
    if not observed:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - min(observed)).total_seconds())


def _filesystem_pressure(path: Path) -> float | None:
    try:
        stats = os.statvfs(path)
        if stats.f_blocks <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - (stats.f_bavail / stats.f_blocks)))
    except OSError:
        return None


def _coverage(enabled: bool, blocker: str | None) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "status": "observed" if enabled else "not_configured",
        "blocker": blocker,
    }
