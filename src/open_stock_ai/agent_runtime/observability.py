from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4


KPI_WINDOWS: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}

P104_FAILURE_CATEGORIES = (
    "invalid_json",
    "wrong_arguments",
    "tool_timeout",
    "api_500",
    "rate_limit",
    "network_drop",
    "web_source_missing",
    "browser_crash",
    "provider_crash",
    "host_restart",
    "db_restart",
    "duplicate_event",
    "user_interrupt",
    "conflicting_user_message",
)


def resolve_kpi_window(
    window: str,
    *,
    now: datetime | str | None = None,
) -> tuple[datetime | None, datetime]:
    """Return an inclusive UTC KPI window with a deterministic test clock."""

    if window not in KPI_WINDOWS:
        raise ValueError(f"Unsupported KPI window: {window}")
    if isinstance(now, str):
        resolved_now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    else:
        resolved_now = now or datetime.now(timezone.utc)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=timezone.utc)
    resolved_now = resolved_now.astimezone(timezone.utc)
    duration = KPI_WINDOWS[window]
    return (resolved_now - duration if duration is not None else None, resolved_now)


def classify_failure(event_type: str, payload: dict[str, Any]) -> str:
    """Map runtime-specific error shapes onto the stable P104 fault taxonomy."""

    error = payload.get("error") or payload.get("error_receipt") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    text = " ".join(
        str(value or "")
        for value in (
            payload.get("fault"),
            payload.get("category"),
            payload.get("component"),
            error.get("category"),
            error.get("type"),
            error.get("code"),
            error.get("component"),
            error.get("message"),
        )
    ).lower()
    exact = next((item for item in P104_FAILURE_CATEGORIES if item in text), None)
    if exact:
        return exact
    rules = (
        ("invalid_json", ("invalid json", "malformed json", "jsondecode")),
        ("wrong_arguments", ("wrong argument", "invalid_argument", "schema", "validation")),
        ("rate_limit", ("rate limit", "ratelimit", "429")),
        ("api_500", ("http 500", "status 500", "internal server error")),
        ("tool_timeout", ("timeout", "timed out", "deadline")),
        ("web_source_missing", ("source missing", "not found", "404", "filenotfound")),
        ("browser_crash", ("browser", "webkit", "playwright")),
        ("db_restart", ("database", "sqlite", "db restart", "locked")),
        ("network_drop", ("network", "disconnect", "connection reset", "transport")),
        ("host_restart", ("safe shutdown", "host restart", "process restart")),
        ("user_interrupt", ("user interrupt", "userpaused", "cancelled by the user")),
        ("conflicting_user_message", ("conflict", "stale mutation", "objective mismatch")),
        ("duplicate_event", ("duplicate", "idempotency")),
        ("provider_crash", ("provider", "model server", "ollama")),
    )
    for category, needles in rules:
        if any(needle in text for needle in needles):
            return category
    if event_type == "model.provider.failed":
        return "provider_crash"
    if event_type == "tool.failed":
        return "tool_failure"
    if event_type == "branch.failed":
        return "branch_failure"
    if event_type == "run.failed":
        return "run_failure"
    return "unknown"


@dataclass(frozen=True, slots=True)
class ToolResultProvenance:
    tool: str
    provider: str
    timestamp: str
    freshness: str
    source: str
    request_id: str
    latency_ms: float
    success: bool
    model_provider: str | None = None
    worker_id: str | None = None
    source_url: str | None = None
    published_at: str | None = None
    observed_at: str | None = None
    schema_version: str = "open_stock_ai.tool_result_provenance.v1"

    @classmethod
    def create(
        cls,
        *,
        tool: str,
        provider: str,
        source: str,
        latency_ms: float,
        success: bool,
        freshness: str = "unknown",
        request_id: str | None = None,
        timestamp: str | None = None,
        model_provider: str | None = None,
        worker_id: str | None = None,
        source_url: str | None = None,
        published_at: str | None = None,
        observed_at: str | None = None,
    ) -> "ToolResultProvenance":
        if not tool or not provider or not source:
            raise ValueError("tool, provider, and source are required provenance")
        if latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        return cls(
            tool=tool,
            provider=provider,
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            freshness=freshness,
            source=source,
            request_id=request_id or f"REQ-{uuid4().hex}",
            latency_ms=float(latency_ms),
            success=bool(success),
            model_provider=model_provider,
            worker_id=worker_id,
            source_url=source_url,
            published_at=published_at,
            observed_at=observed_at or timestamp or datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def with_provenance(result: Any, provenance: ToolResultProvenance) -> dict[str, Any]:
    return {
        "schema_version": "open_stock_ai.provenanced_tool_result.v1",
        "result": result,
        "provenance": provenance.to_dict(),
    }


@dataclass(slots=True)
class _Metrics:
    active_sessions: set[str] = field(default_factory=set)
    active_branches: set[str] = field(default_factory=set)
    tool_latencies: list[float] = field(default_factory=list)
    model_latencies: list[float] = field(default_factory=list)
    tool_calls: int = 0
    tool_failures: int = 0
    tasks: int = 0
    tasks_completed: int = 0
    repairs: int = 0
    repairs_succeeded: int = 0
    repeated_failures: int = 0
    branches_recovered: int = 0
    branches_failed: int = 0
    token_usage: int = 0
    token_cost: float = 0.0
    web_research_cost: float = 0.0
    automation_count: int = 0
    automation_duplicates: int = 0
    notification_count: int = 0
    false_notifications: int = 0
    user_interventions: int = 0
    user_corrections: int = 0
    corrections_incorporated: int = 0
    research_sources: set[str] = field(default_factory=set)
    evidence_freshness_total: float = 0.0
    evidence_count: int = 0
    context_retrieval_relevant: int = 0
    context_retrieval_returned: int = 0
    failure_classification: dict[str, int] = field(default_factory=dict)


class KPICollector:
    """Thread-safe in-memory aggregator; callers may persist snapshots elsewhere."""

    def __init__(self) -> None:
        self._metrics = _Metrics()
        self._lock = threading.Lock()

    def set_session_active(self, session_id: str, active: bool) -> None:
        with self._lock:
            (self._metrics.active_sessions.add if active else self._metrics.active_sessions.discard)(session_id)

    def set_branch_active(self, branch_id: str, active: bool) -> None:
        with self._lock:
            (self._metrics.active_branches.add if active else self._metrics.active_branches.discard)(branch_id)

    def record_tool(self, *, latency_ms: float, success: bool) -> None:
        with self._lock:
            self._metrics.tool_calls += 1
            self._metrics.tool_latencies.append(max(0.0, float(latency_ms)))
            self._metrics.tool_failures += int(not success)

    def record_model(self, *, latency_ms: float, tokens: int = 0, cost: float = 0.0) -> None:
        with self._lock:
            self._metrics.model_latencies.append(max(0.0, float(latency_ms)))
            self._metrics.token_usage += max(0, int(tokens))
            self._metrics.token_cost += max(0.0, float(cost))

    def record_task(self, *, completed: bool, user_intervened: bool = False) -> None:
        with self._lock:
            self._metrics.tasks += 1
            self._metrics.tasks_completed += int(completed)
            self._metrics.user_interventions += int(user_intervened)

    def record_repair(self, *, succeeded: bool, repeated_identical: bool = False) -> None:
        with self._lock:
            self._metrics.repairs += 1
            self._metrics.repairs_succeeded += int(succeeded)
            self._metrics.repeated_failures += int(repeated_identical)

    def record_branch_recovery(self, *, succeeded: bool) -> None:
        with self._lock:
            self._metrics.branches_recovered += int(succeeded)
            self._metrics.branches_failed += int(not succeeded)

    def record_research(self, *, sources: set[str], freshness_scores: list[float], cost: float = 0.0) -> None:
        with self._lock:
            self._metrics.research_sources.update(sources)
            self._metrics.evidence_freshness_total += sum(max(0.0, min(1.0, item)) for item in freshness_scores)
            self._metrics.evidence_count += len(freshness_scores)
            self._metrics.web_research_cost += max(0.0, float(cost))

    def record_automation(self, *, duplicate: bool = False) -> None:
        with self._lock:
            self._metrics.automation_count += 1
            self._metrics.automation_duplicates += int(duplicate)

    def record_notification(self, *, false_trigger: bool = False) -> None:
        with self._lock:
            self._metrics.notification_count += 1
            self._metrics.false_notifications += int(false_trigger)

    def record_correction(self, *, incorporated: bool) -> None:
        with self._lock:
            self._metrics.user_corrections += 1
            self._metrics.corrections_incorporated += int(incorporated)

    def record_context_retrieval(self, *, relevant: int, returned: int) -> None:
        with self._lock:
            self._metrics.context_retrieval_relevant += max(0, relevant)
            self._metrics.context_retrieval_returned += max(0, returned)

    def record_failure(self, *, category: str) -> None:
        normalized = str(category or "unknown").strip().lower() or "unknown"
        with self._lock:
            self._metrics.failure_classification[normalized] = (
                self._metrics.failure_classification.get(normalized, 0) + 1
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            m = self._metrics
            rate = lambda numerator, denominator: numerator / denominator if denominator else 0.0
            return {
                "schema_version": "open_stock_ai.runtime_kpis.v1",
                "active_sessions": len(m.active_sessions),
                "active_branches": len(m.active_branches),
                "tool_latency_ms": _average(m.tool_latencies),
                "model_latency_ms": _average(m.model_latencies),
                "error_rate": rate(m.tool_failures, m.tool_calls),
                "repair_success_rate": rate(m.repairs_succeeded, m.repairs),
                "repeated_failure_rate": rate(m.repeated_failures, m.repairs),
                "task_completion_rate": rate(m.tasks_completed, m.tasks),
                "branch_recovery_rate": rate(m.branches_recovered, m.branches_recovered + m.branches_failed),
                "user_intervention_rate": rate(m.user_interventions, m.tasks),
                "false_automation_notification_rate": rate(m.false_notifications, m.notification_count),
                "average_tool_calls": rate(m.tool_calls, m.tasks),
                "average_token_cost": rate(m.token_cost, m.tasks),
                "token_usage": m.token_usage,
                "web_research_cost": m.web_research_cost,
                "research_source_diversity": len(m.research_sources),
                "evidence_freshness": rate(m.evidence_freshness_total, m.evidence_count),
                "user_correction_incorporation_rate": rate(m.corrections_incorporated, m.user_corrections),
                "automation_count": m.automation_count,
                "automation_duplicate_rate": rate(m.automation_duplicates, m.automation_count),
                "notification_count": m.notification_count,
                "session_context_retrieval_precision": rate(m.context_retrieval_relevant, m.context_retrieval_returned),
                "failure_classification": dict(sorted(m.failure_classification.items())),
                "sample_counts": {
                    "tasks": m.tasks,
                    "tool_calls": m.tool_calls,
                    "model_calls": len(m.model_latencies),
                    "repairs": m.repairs,
                    "branch_recoveries": m.branches_recovered + m.branches_failed,
                    "notifications": m.notification_count,
                    "automations": m.automation_count,
                    "evidence": m.evidence_count,
                    "corrections": m.user_corrections,
                    "context_items": m.context_retrieval_returned,
                    "failure_events": sum(m.failure_classification.values()),
                },
            }


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class BackgroundWorkQueue:
    """Dispatch post-result memory/title/analytics work without blocking final output."""

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="agent-background")

    def submit(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        return self._executor.submit(function, *args, **kwargs)

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)
