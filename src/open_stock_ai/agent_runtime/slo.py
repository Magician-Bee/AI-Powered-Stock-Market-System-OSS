"""Deterministic SLO contracts for data, API, Agent and execution paths."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from open_stock_ai.storage.migrations import ManagedSQLiteConnection


SCHEMA_VERSION = "open_stock_ai.slo_report.v1"
SLOStatus = Literal["pass", "breach", "no_data"]


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SLOTarget:
    service: str
    max_p95_latency_ms: float
    min_success_ratio: float
    max_freshness_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.service.strip():
            raise ValueError("slo service is required")
        if self.max_p95_latency_ms < 0 or not 0 <= self.min_success_ratio <= 1:
            raise ValueError("slo target bounds are invalid")
        if self.max_freshness_seconds is not None and self.max_freshness_seconds < 0:
            raise ValueError("slo freshness bound is invalid")


@dataclass(frozen=True, slots=True)
class SLOObservation:
    observed_at: datetime
    latency_ms: float
    success: bool
    data_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("slo latency cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat(),
            "latency_ms": self.latency_ms,
            "success": self.success,
            "data_at": self.data_at.isoformat() if self.data_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SLOObservation":
        observed_at = datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00"))
        data_at_raw = payload.get("data_at")
        data_at = datetime.fromisoformat(str(data_at_raw).replace("Z", "+00:00")) if data_at_raw else None
        return cls(observed_at=observed_at, latency_ms=float(payload["latency_ms"]), success=bool(payload["success"]), data_at=data_at)


@dataclass(frozen=True, slots=True)
class SLOReport:
    service: str
    status: SLOStatus
    samples: int
    p95_latency_ms: float | None
    success_ratio: float | None
    max_freshness_seconds: float | None
    breaches: tuple[str, ...]
    evaluated_at: str
    report_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "service": self.service,
            "status": self.status,
            "samples": self.samples,
            "p95_latency_ms": self.p95_latency_ms,
            "success_ratio": self.success_ratio,
            "max_freshness_seconds": self.max_freshness_seconds,
            "breaches": list(self.breaches),
            "evaluated_at": self.evaluated_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "report_sha256": self.report_sha256}

    def verify(self) -> bool:
        expected = hashlib.sha256(_canonical(self.payload())).hexdigest()
        return hmac.compare_digest(expected, self.report_sha256)


def evaluate_slo(
    target: SLOTarget,
    observations: Iterable[SLOObservation],
    *,
    evaluated_at: datetime | None = None,
) -> SLOReport:
    """Evaluate p95 latency, success ratio and optional data freshness."""

    items = tuple(observations)
    evaluated = (evaluated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not items:
        status: SLOStatus = "no_data"
        breaches = ("slo_observations_missing",)
        return _report(target.service, status, 0, None, None, None, breaches, evaluated)
    latencies = sorted(float(item.latency_ms) for item in items)
    rank = max(0, math.ceil(0.95 * len(latencies)) - 1)
    p95 = latencies[rank]
    success_ratio = sum(1 for item in items if item.success) / len(items)
    freshness_values: list[float] = []
    breaches: list[str] = []
    if p95 > target.max_p95_latency_ms:
        breaches.append("p95_latency_slo_breached")
    if success_ratio < target.min_success_ratio:
        breaches.append("success_ratio_slo_breached")
    if target.max_freshness_seconds is not None:
        if any(item.data_at is None for item in items):
            breaches.append("freshness_observation_missing")
        else:
            freshness_values = [max(0.0, (evaluated - item.data_at).total_seconds()) for item in items if item.data_at]
            max_freshness = max(freshness_values, default=0.0)
            if max_freshness > target.max_freshness_seconds:
                breaches.append("freshness_slo_breached")
    max_freshness = max(freshness_values, default=None)
    status = "breach" if breaches else "pass"
    return _report(target.service, status, len(items), p95, success_ratio, max_freshness, tuple(breaches), evaluated)


def _report(
    service: str,
    status: SLOStatus,
    samples: int,
    p95: float | None,
    success_ratio: float | None,
    max_freshness: float | None,
    breaches: tuple[str, ...],
    evaluated: datetime,
) -> SLOReport:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "service": service,
        "status": status,
        "samples": samples,
        "p95_latency_ms": p95,
        "success_ratio": success_ratio,
        "max_freshness_seconds": max_freshness,
        "breaches": list(breaches),
        "evaluated_at": evaluated.isoformat(),
    }
    return SLOReport(
        service=service,
        status=status,
        samples=samples,
        p95_latency_ms=p95,
        success_ratio=success_ratio,
        max_freshness_seconds=max_freshness,
        breaches=breaches,
        evaluated_at=evaluated.isoformat(),
        report_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
    )


def default_slo_targets() -> tuple[SLOTarget, ...]:
    return (
        SLOTarget("data.freshness", 2_000, 0.99, 86_400),
        SLOTarget("api.request", 1_000, 0.995),
        SLOTarget("agent.run", 10_000, 0.99),
        SLOTarget("broker.feed", 500, 0.999),
        SLOTarget("order.lifecycle", 2_000, 0.999),
    )


class SLORegistry:
    def __init__(self, targets: Iterable[SLOTarget] | None = None) -> None:
        self._targets = {target.service: target for target in (targets or default_slo_targets())}

    def evaluate(
        self,
        observations: dict[str, Iterable[SLOObservation]],
        *,
        evaluated_at: datetime | None = None,
    ) -> dict[str, SLOReport]:
        return {
            service: evaluate_slo(target, observations.get(service, ()), evaluated_at=evaluated_at)
            for service, target in self._targets.items()
        }

    def target(self, service: str) -> SLOTarget:
        """Return the declared target for one concrete runtime surface."""

        normalized = str(service).strip()
        try:
            return self._targets[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown SLO service: {normalized}") from exc

    def services(self) -> tuple[str, ...]:
        return tuple(self._targets)


class DurableSLOStore:
    """Persist SLO observations and hash-verified reports across restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("pragma journal_mode = wal")
            connection.execute("pragma synchronous = full")
            connection.executescript(
                """
                create table if not exists slo_observations (
                    observation_id text primary key,
                    service text not null,
                    observation_sha256 text not null unique,
                    observation_json text not null,
                    persisted_at text not null
                );
                create table if not exists slo_reports (
                    report_sha256 text primary key,
                    service text not null,
                    report_json text not null,
                    persisted_at text not null
                );
                create trigger if not exists slo_observations_immutable_update
                    before update on slo_observations
                    begin select raise(abort, 'slo observations are immutable'); end;
                create trigger if not exists slo_observations_immutable_delete
                    before delete on slo_observations
                    begin select raise(abort, 'slo observations are immutable'); end;
                create trigger if not exists slo_reports_immutable_update
                    before update on slo_reports
                    begin select raise(abort, 'slo reports are immutable'); end;
                create trigger if not exists slo_reports_immutable_delete
                    before delete on slo_reports
                    begin select raise(abort, 'slo reports are immutable'); end;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, factory=ManagedSQLiteConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout = 5000")
        return connection

    def record_observation(
        self,
        service: str,
        observation: SLOObservation,
        *,
        observation_id: str | None = None,
    ) -> str:
        normalized_service = str(service).strip()
        if not normalized_service:
            raise ValueError("slo observation service is required")
        body = {"service": normalized_service, **observation.as_dict()}
        serialized = _canonical(body).decode("utf-8")
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        identity = str(observation_id or f"SLOO-{digest[:32]}").strip()
        if not identity:
            raise ValueError("slo observation id is required")
        with self._connect() as connection:
            existing = connection.execute(
                "select observation_sha256, observation_json from slo_observations where observation_id=?",
                (identity,),
            ).fetchone()
            if existing is not None:
                if str(existing["observation_sha256"]) != digest or str(existing["observation_json"]) != serialized:
                    raise ValueError("slo observation identity is bound to different evidence")
                return identity
            connection.execute(
                "insert into slo_observations values (?, ?, ?, ?, ?)",
                (identity, normalized_service, digest, serialized, datetime.now(timezone.utc).isoformat()),
            )
        return identity

    def observations(self, service: str | None = None) -> dict[str, list[SLOObservation]]:
        query = "select service, observation_json from slo_observations"
        params: tuple[str, ...] = ()
        if service:
            query += " where service=?"
            params = (str(service).strip(),)
        query += " order by persisted_at, observation_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        grouped: dict[str, list[SLOObservation]] = {}
        for row in rows:
            body = json.loads(str(row["observation_json"]))
            grouped.setdefault(str(row["service"]), []).append(SLOObservation.from_dict(body))
        return grouped

    def record_report(self, report: SLOReport) -> SLOReport:
        if not report.verify() or not verify_slo_report(report.as_dict()):
            raise ValueError("slo report hash verification failed")
        serialized = _canonical(report.as_dict()).decode("utf-8")
        with self._connect() as connection:
            existing = connection.execute(
                "select report_json from slo_reports where report_sha256=?",
                (report.report_sha256,),
            ).fetchone()
            if existing is not None and str(existing["report_json"]) != serialized:
                raise ValueError("slo report hash is bound to different evidence")
            if existing is None:
                connection.execute(
                    "insert into slo_reports values (?, ?, ?, ?)",
                    (report.report_sha256, report.service, serialized, datetime.now(timezone.utc).isoformat()),
                )
        return report

    def reports(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select report_json from slo_reports order by persisted_at, report_sha256"
            ).fetchall()
        return [json.loads(str(row["report_json"])) for row in rows]

    def evaluate_and_record(
        self,
        registry: SLORegistry,
        *,
        evaluated_at: datetime | None = None,
    ) -> dict[str, SLOReport]:
        reports = registry.evaluate(self.observations(), evaluated_at=evaluated_at)
        for report in reports.values():
            self.record_report(report)
        return reports

    def evaluate_service_and_record(
        self,
        registry: SLORegistry,
        service: str,
        *,
        evaluated_at: datetime | None = None,
    ) -> SLOReport:
        """Record a report only for the service that received new evidence.

        Dashboard reads must not manufacture reports, and a high-volume API
        surface must not append fresh no-data reports for unrelated services.
        """

        target = registry.target(service)
        report = evaluate_slo(
            target,
            self.observations(target.service).get(target.service, ()),
            evaluated_at=evaluated_at,
        )
        return self.record_report(report)

    def dashboard(self) -> dict[str, Any]:
        reports = self.reports()
        statuses = {str(item.get("service")): str(item.get("status")) for item in reports}
        payload = {
            "schema_version": "open_stock_ai.slo_dashboard.v1",
            "report_count": len(reports),
            "reports": reports,
            "all_services_passing": bool(reports) and all(status == "pass" for status in statuses.values()),
            "missing_or_breached_services": sorted(
                service for service, status in statuses.items() if status != "pass"
            ),
        }
        payload["dashboard_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
        return payload


def verify_slo_report(report: dict[str, Any]) -> bool:
    required = {"schema_version", "service", "status", "samples", "p95_latency_ms", "success_ratio", "max_freshness_seconds", "breaches", "evaluated_at", "report_sha256"}
    if set(report) != required or report.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = hashlib.sha256(_canonical({key: report[key] for key in required if key != "report_sha256"})).hexdigest()
    return hmac.compare_digest(expected, str(report.get("report_sha256") or ""))
