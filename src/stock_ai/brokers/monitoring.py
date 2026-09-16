from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId
from .feed_quality import BrokerSourceSwitchRecord
from .oms import BrokerOrderManagementGateway


class BrokerRuntimeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: BrokerId
    login_connected: bool | None = None
    certificate_seconds_remaining: int | None = None
    websocket_connected: bool | None = None
    subscription_count: int | None = Field(default=None, ge=0)
    rate_limit_remaining: int | None = Field(default=None, ge=0)
    market_latency_ms: float | None = Field(default=None, ge=0)
    sequence_gap_count: int = Field(default=0, ge=0)
    sdk_crash_count: int = Field(default=0, ge=0)
    reconnect_count: int = Field(default=0, ge=0)
    order_report_latency_ms: float | None = Field(default=None, ge=0)
    unknown_order_count: int = Field(default=0, ge=0)
    order_reconciliation_mismatch_count: int = Field(default=0, ge=0)
    account_reconciliation_mismatch_count: int = Field(default=0, ge=0)
    duplicate_order_risk: bool = False
    worker_version: str | None = None
    sdk_version: str | None = None
    observed_at: datetime


class BrokerAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_alert.v1"] = (
        "stock_ai.broker_alert.v1"
    )
    severity: Literal["INFO", "WARNING", "CRITICAL", "EMERGENCY"]
    broker_ids: list[BrokerId]
    code: str
    message: str
    stop_new_orders: bool
    kill_switch_activated: bool
    observed_at: datetime


class BrokerRuntimeStatusSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_runtime_status.v1"] = (
        "stock_ai.broker_runtime_status.v1"
    )
    metrics: list[BrokerRuntimeMetrics] = Field(default_factory=list)
    source_switches: list[BrokerSourceSwitchRecord] = Field(default_factory=list)
    selected_sources: dict[str, BrokerId | None] = Field(default_factory=dict)
    generated_at: datetime


class BrokerRuntimeStatusRegistry:
    """Host-owned runtime evidence; an empty registry means no feed was observed."""

    def __init__(self) -> None:
        self._metrics: dict[BrokerId, BrokerRuntimeMetrics] = {}
        self._switches: list[BrokerSourceSwitchRecord] = []

    def record_metrics(self, metrics: BrokerRuntimeMetrics) -> None:
        existing = self._metrics.get(metrics.broker_id)
        if existing is not None and metrics.observed_at < existing.observed_at:
            raise ValueError("broker runtime metrics cannot move backwards in time")
        self._metrics[metrics.broker_id] = metrics

    def record_source_switch(self, record: BrokerSourceSwitchRecord) -> None:
        self._switches.append(record)

    def snapshot(self) -> BrokerRuntimeStatusSnapshot:
        selected: dict[str, BrokerId | None] = {}
        for item in self._switches:
            selected[f"{item.instrument_id}:{item.market_session}"] = (
                item.selected_broker_id
            )
        return BrokerRuntimeStatusSnapshot(
            metrics=[
                self._metrics[broker_id]
                for broker_id in sorted(self._metrics)
            ],
            source_switches=list(self._switches),
            selected_sources=selected,
            generated_at=datetime.now(timezone.utc),
        )


class BrokerObservabilityMonitor:
    """Convert host metrics to deterministic alerts and OMS safety actions."""

    def __init__(
        self,
        oms: BrokerOrderManagementGateway,
        *,
        market_latency_warning_ms: float = 1500,
    ) -> None:
        self.oms = oms
        self.market_latency_warning_ms = market_latency_warning_ms

    def evaluate(
        self,
        metrics: list[BrokerRuntimeMetrics],
    ) -> list[BrokerAlert]:
        now = datetime.now(timezone.utc)
        alerts: list[BrokerAlert] = []
        disconnected = [
            item.broker_id
            for item in metrics
            if item.login_connected is False
        ]
        duplicate_risk = [
            item.broker_id for item in metrics if item.duplicate_order_risk
        ]
        if len(disconnected) >= 2 or duplicate_risk:
            self.oms.activate_kill_switch()
            alerts.append(
                BrokerAlert(
                    severity="EMERGENCY",
                    broker_ids=sorted(set(disconnected + duplicate_risk)),
                    code=(
                        "duplicate_order_risk"
                        if duplicate_risk
                        else "multiple_brokers_disconnected"
                    ),
                    message="Broker safety boundary requires the global kill switch.",
                    stop_new_orders=True,
                    kill_switch_activated=True,
                    observed_at=now,
                )
            )
        for item in metrics:
            reconciliation_risk = (
                item.unknown_order_count
                + item.order_reconciliation_mismatch_count
                + item.account_reconciliation_mismatch_count
            )
            if reconciliation_risk:
                self.oms.activate_kill_switch()
                alerts.append(
                    BrokerAlert(
                        severity="CRITICAL",
                        broker_ids=[item.broker_id],
                        code="broker_reconciliation_required",
                        message="Unknown order or account state blocks new orders.",
                        stop_new_orders=True,
                        kill_switch_activated=True,
                        observed_at=now,
                    )
                )
            if (
                item.market_latency_ms is not None
                and item.market_latency_ms > self.market_latency_warning_ms
            ) or item.sequence_gap_count:
                alerts.append(
                    BrokerAlert(
                        severity="WARNING",
                        broker_ids=[item.broker_id],
                        code="market_feed_degraded",
                        message="Market latency or a sequence gap requires fallback evaluation.",
                        stop_new_orders=False,
                        kill_switch_activated=False,
                        observed_at=now,
                    )
                )
            if item.sdk_crash_count:
                alerts.append(
                    BrokerAlert(
                        severity="WARNING",
                        broker_ids=[item.broker_id],
                        code="broker_sdk_crashed",
                        message="The isolated broker SDK worker crashed.",
                        stop_new_orders=False,
                        kill_switch_activated=False,
                        observed_at=now,
                    )
                )
        return alerts
