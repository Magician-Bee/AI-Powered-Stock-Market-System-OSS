from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.agent_runtime.observability import (
    KPICollector,
    P104_FAILURE_CATEGORIES,
    classify_failure,
)
from open_stock_ai.agent_runtime.observability_store import DurableRuntimeObservability
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.model_router import MatrixBehavior, ProviderClass
from open_stock_ai.agent_runtime.validation.chaos import (
    ChaosCampaign,
    ChaosFault,
    ChaosInjector,
    FaultKind,
)
from open_stock_ai.agent_runtime.validation.provider_matrix import (
    ProviderContractMatrix,
    ReferenceContractProvider,
)
from stock_ai.agent_run_store import AgentRunStore


def test_provider_classes_and_behaviors_are_the_p103_contract() -> None:
    assert [item.value for item in ProviderClass] == [
        "strong_cloud",
        "small_cloud",
        "local_tool_native",
        "local_text_only",
        "codex",
    ]
    assert [item.value for item in MatrixBehavior] == [
        "structured_output",
        "tool_call",
        "parallel_branches",
        "long_session",
        "error_repair",
        "approval_interaction",
        "reflection",
        "automation",
    ]
    assert ProviderClass.OPENAI_CODEX is ProviderClass.CODEX
    assert ProviderClass.OLLAMA_WEAK is ProviderClass.LOCAL_TEXT_ONLY
    assert MatrixBehavior.REPAIR is MatrixBehavior.ERROR_REPAIR


def test_all_five_provider_classes_execute_every_contract() -> None:
    matrix = ProviderContractMatrix.reference()
    report = asyncio.run(matrix.run())

    assert len(report.cases) == 5 * 8
    assert report.complete, [
        (item.provider_class.value, item.behavior.value, item.detail, item.observations)
        for item in report.failures
    ]
    assert {
        (item.provider_class, item.behavior)
        for item in report.cases
    } == {
        (provider, behavior)
        for provider in ProviderClass
        for behavior in MatrixBehavior
    }
    assert all(
        provider.task_ids == {"contract-task-under-test"}
        for provider in matrix.providers.values()
        if isinstance(provider, ReferenceContractProvider)
    )


def test_matrix_observes_real_tool_execution_parallel_overlap_and_approval_gate() -> None:
    active = 0
    peak = 0
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        calls.append((name, dict(arguments)))
        active -= 1
        return {"ok": True, "tool": name, "arguments": dict(arguments)}

    matrix = ProviderContractMatrix(
        {item: ReferenceContractProvider(item) for item in ProviderClass},
        tool_executor=tool,
        instrument_id="neutral-instrument",
    )
    report = asyncio.run(matrix.run())

    assert report.complete
    assert peak >= 2
    assert sum(name == "market.quote" for name, _ in calls) == 5
    assert sum(name == "research.branch" for name, _ in calls) == 10
    assert sum(name == "broker.paper_order" for name, _ in calls) == 5
    assert sum(name == "automation.preview" for name, _ in calls) == 5
    assert sum(name == "automation.activate" for name, _ in calls) == 5
    assert all(
        arguments.get("symbol") == "neutral-instrument"
        for name, arguments in calls
        if name in {"market.quote", "broker.paper_order"}
    )


def test_matrix_cannot_be_made_green_by_a_provider_returning_an_empty_object() -> None:
    class BrokenProvider:
        provider_class = ProviderClass.SMALL_CLOUD

        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            return {}

    providers: dict[ProviderClass, Any] = {
        item: ReferenceContractProvider(item) for item in ProviderClass
    }
    providers[ProviderClass.SMALL_CLOUD] = BrokenProvider()
    report = asyncio.run(ProviderContractMatrix(providers).run())
    failures = [
        item for item in report.failures
        if item.provider_class is ProviderClass.SMALL_CLOUD
    ]

    assert not report.complete
    assert failures
    assert any(not all(item.observations.values()) for item in failures)


def test_chaos_campaign_executes_all_fourteen_faults_and_recovers(tmp_path: Path) -> None:
    provider = ReferenceContractProvider(ProviderClass.LOCAL_TEXT_ONLY)
    report = asyncio.run(
        ChaosCampaign(provider, seed=20260808).run(tmp_path / "chaos.sqlite")
    )

    assert report.complete, [
        (item.fault.value, item.invariants, item.trace)
        for item in report.outcomes
        if not item.passed
    ]
    assert {item.fault for item in report.outcomes} == set(FaultKind)
    assert len(report.outcomes) == len(FaultKind) == 14
    assert [item.fault.value for item in report.outcomes] == [
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
    ]
    assert all(item.strategy for item in report.outcomes)
    assert {item.fault: item.strategy for item in report.outcomes} == {
        FaultKind.INVALID_JSON: "reject_then_deterministic_retry",
        FaultKind.WRONG_ARGUMENTS: "schema_rejection_without_execution",
        FaultKind.TOOL_TIMEOUT: "bounded_read_only_retry",
        FaultKind.API_500: "bounded_retry",
        FaultKind.RATE_LIMIT: "bounded_retry_with_provider_backoff",
        FaultKind.NETWORK_DROP: "bounded_retry",
        FaultKind.WEB_SOURCE_MISSING: "isolate_as_partial_with_evidence_gap",
        FaultKind.BROWSER_CRASH: "restart_browser_then_retry",
        FaultKind.PROVIDER_CRASH: "provider_failover",
        FaultKind.HOST_RESTART: "durable_checkpoint_resume",
        FaultKind.DB_RESTART: "reconnect_and_rehydrate",
        FaultKind.DUPLICATE_EVENT: "idempotency_key_deduplication",
        FaultKind.USER_INTERRUPT: "cooperative_cancel_and_pause",
        FaultKind.CONFLICTING_USER_MESSAGE: "reject_stale_mutation_and_request_decision",
    }
    assert all(any(entry.startswith("inject:") for entry in item.trace) for item in report.outcomes)


def test_chaos_script_is_reproducible_for_same_seed(tmp_path: Path) -> None:
    async def campaign(path: Path):
        provider = ReferenceContractProvider(ProviderClass.STRONG_CLOUD)
        return await ChaosCampaign(provider, seed=73).run(path)

    first = asyncio.run(campaign(tmp_path / "first.sqlite"))
    second = asyncio.run(campaign(tmp_path / "second.sqlite"))

    assert [item.trace for item in first.outcomes] == [
        item.trace for item in second.outcomes
    ]
    assert [dict(item.invariants) for item in first.outcomes] == [
        dict(item.invariants) for item in second.outcomes
    ]


def test_fault_occurrence_is_exact_and_does_not_repeat_accidentally() -> None:
    injector = ChaosInjector(
        (ChaosFault(FaultKind.API_500, "provider", occurrence=2),),
        seed=11,
    )

    assert FaultKind.TOOL_FAILURE is FaultKind.API_500
    assert injector.trigger("provider") is None
    assert injector.trigger("provider") is FaultKind.API_500
    assert injector.trigger("provider") is None
    assert injector.trace == [
        "seed:11",
        "pass:provider#1",
        "inject:api_500@provider#2",
        "pass:provider#3",
    ]


def test_provider_matrix_requires_all_five_provider_classes() -> None:
    providers = {
        item: ReferenceContractProvider(item)
        for item in ProviderClass
        if item is not ProviderClass.CODEX
    }

    try:
        ProviderContractMatrix(providers)
    except ValueError as exc:
        assert "codex" in str(exc)
    else:  # pragma: no cover - makes the required failure explicit
        raise AssertionError("Missing provider class must be rejected")


def _durable_observability(tmp_path: Path, *run_ids: str) -> tuple[DurableRuntimeObservability, Path]:
    database = tmp_path / "observability.sqlite"
    AgentSessionStore(database).create(
        session_id="AS-observability",
        title="Long-term KPI acceptance",
        namespace="test",
    )
    run_store = AgentRunStore(database)
    for run_id in run_ids:
        run_store.create_run(
            run_id,
            {
                "objective": f"KPI sample {run_id}",
                "driver_id": "test-provider",
                "session_id": "AS-observability",
            },
        )
    return DurableRuntimeObservability(database), database


def test_p105_durable_kpis_use_deduplicated_terminal_runs_and_long_term_windows(tmp_path: Path) -> None:
    runtime, database = _durable_observability(
        tmp_path,
        "AR-old",
        "AR-30d",
        "AR-7d",
        "AR-24h",
    )
    rows = (
        ("old-final", "AR-old", "result_final", 1, "2026-06-20T12:00:00+00:00"),
        ("old-tools", "AR-old", "tool_call", 4, "2026-06-20T12:00:00+00:00"),
        ("30d-failed", "AR-30d", "run_failed", 1, "2026-07-20T12:00:00+00:00"),
        ("30d-tools", "AR-30d", "tool_call", 2, "2026-07-20T12:00:00+00:00"),
        ("7d-final-result", "AR-7d", "result_final", 1, "2026-08-04T12:00:00+00:00"),
        # A second terminal-success event for the same Run must not inflate the denominator.
        ("7d-final-run", "AR-7d", "run_completed", 1, "2026-08-04T12:00:01+00:00"),
        ("7d-tools", "AR-7d", "tool_call", 3, "2026-08-04T12:00:00+00:00"),
        ("24h-failed", "AR-24h", "run_failed", 1, "2026-08-09T06:00:00+00:00"),
        ("24h-tools", "AR-24h", "tool_call", 1, "2026-08-09T06:00:00+00:00"),
    )
    with sqlite3.connect(database) as conn:
        conn.executemany(
            """
            insert into agent_kpi_events(
                kpi_event_id, session_id, run_id, metric, value, created_at, payload_json
            ) values (?, 'AS-observability', ?, ?, ?, ?, '{}')
            """,
            rows,
        )
        conn.execute(
            "insert or ignore into agent_kpi_events values (?, 'AS-observability', ?, ?, ?, ?, '{}')",
            rows[-1],
        )

    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    all_time = runtime.kpis(window="all", now=now)
    thirty_days = runtime.kpis(window="30d", now=now)
    seven_days = runtime.kpis(window="7d", now=now)
    one_day = runtime.kpis(window="24h", now=now)

    assert all_time["task_completion_rate"] == 0.5
    assert all_time["sample_counts"]["tasks"] == 4
    assert thirty_days["task_completion_rate"] == 1 / 3
    assert thirty_days["sample_counts"]["tasks"] == 3
    assert seven_days["task_completion_rate"] == 0.5
    assert seven_days["average_tool_calls"] == 2.0
    assert one_day["task_completion_rate"] == 0.0
    assert one_day["sample_counts"]["tasks"] == 1
    assert one_day["window_start"] == "2026-08-08T12:00:00+00:00"
    assert runtime.kpis(window="24h", now=now) == one_day


def test_p104_failures_are_classified_idempotently_with_real_denominators(tmp_path: Path) -> None:
    runtime, _ = _durable_observability(tmp_path, "AR-chaos")
    events = (
        {
            "event_id": "model-rate-limit",
            "type": "model.provider.failed",
            "timestamp": "2026-08-09T01:00:00+00:00",
            "payload": {"provider": "ollama", "error": {"type": "HTTPError", "message": "429 rate limit"}},
        },
        {
            "event_id": "tool-timeout",
            "type": "tool.failed",
            "timestamp": "2026-08-09T01:01:00+00:00",
            "payload": {"tool": "web.open", "call_id": "TC-timeout", "error": {"type": "TimeoutError"}},
        },
        {
            "event_id": "invalid-json-receipt",
            "type": "error.receipt.created",
            "timestamp": "2026-08-09T01:02:00+00:00",
            "payload": {
                "error_receipt": {"error_id": "ERR-json", "category": "invalid_json"},
                "failure_fingerprint": {"digest": "FP-json"},
            },
        },
        {
            "event_id": "browser-branch",
            "type": "branch.failed",
            "timestamp": "2026-08-09T01:03:00+00:00",
            "payload": {"error": {"type": "BrowserCrashError", "message": "browser crashed"}},
        },
    )
    for event in events:
        runtime.project("AR-chaos", dict(event))
    runtime.project("AR-chaos", dict(events[1]))

    metrics = runtime.kpis(window="24h", now="2026-08-09T12:00:00+00:00")
    assert metrics["error_rate"] == 1.0
    assert metrics["tool_error_rate"] == 1.0
    assert metrics["sample_counts"]["execution_attempts"] == 2
    assert metrics["sample_counts"]["failure_events"] == 4
    assert metrics["failure_classification"] == {
        "browser_crash": 1,
        "invalid_json": 1,
        "rate_limit": 1,
        "tool_timeout": 1,
    }
    assert metrics["metric_status"]["repair_success_rate"] == "no_data"


def test_p83_question_budget_and_p105_no_sample_state_are_observable(tmp_path: Path) -> None:
    runtime, _ = _durable_observability(tmp_path, "AR-questions")
    for event_id, questions in (("INT-safe", ["q1", "q2"]), ("INT-breach", ["q1", "q2", "q3", "q4"])):
        runtime.project(
            "AR-questions",
            {
                "event_id": event_id,
                "type": "interaction.requested",
                "timestamp": "2026-08-09T02:00:00+00:00",
                "payload": {
                    "interaction_id": event_id,
                    "waiting_state": "waiting_decision",
                    "questions": questions,
                },
            },
        )

    metrics = runtime.kpis(window="7d", now="2026-08-09T12:00:00+00:00")
    assert metrics["average_questions_per_checkpoint"] == 3.0
    assert metrics["question_budget_breach_rate"] == 0.5
    assert metrics["metric_samples"]["question_budget_breach_rate"] == 2
    assert metrics["metric_status"]["task_completion_rate"] == "no_data"
    dashboard = runtime.dashboard(now="2026-08-09T12:00:00+00:00")
    assert dashboard["default_window"] == "30d"
    assert set(dashboard["windows"]) == {"24h", "7d", "30d", "all"}


def test_p105_evidence_freshness_uses_publication_time_not_recent_observation(tmp_path: Path) -> None:
    runtime, _ = _durable_observability(tmp_path, "AR-evidence")
    runtime.project(
        "AR-evidence",
        {
            "event_id": "old-publication-observed-now",
            "type": "research.evidence_added",
            "timestamp": "2026-08-09T03:00:00+00:00",
            "payload": {
                "evidence_ids": ["EV-old"],
                "evidence": {
                    "evidence_id": "EV-old",
                    "claim": "A historical filing was opened today",
                    "source_type": "company_ir",
                    "published_at": "2026-06-20T03:00:00+00:00",
                    "observed_at": "2026-08-09T03:00:00+00:00",
                },
            },
        },
    )

    metrics = runtime.kpis(window="24h", now="2026-08-09T12:00:00+00:00")
    assert metrics["evidence_freshness"] == 0.25
    assert metrics["research_source_diversity"] == 1
    assert metrics["sample_counts"]["evidence"] == 1


def test_p104_taxonomy_and_in_memory_failure_samples_remain_stable() -> None:
    assert {
        classify_failure("error.receipt.created", {"fault": category})
        for category in P104_FAILURE_CATEGORIES
    } == set(P104_FAILURE_CATEGORIES)
    collector = KPICollector()
    collector.record_failure(category="provider_crash")
    collector.record_failure(category="provider_crash")
    snapshot = collector.snapshot()
    assert snapshot["failure_classification"] == {"provider_crash": 2}
    assert snapshot["sample_counts"]["failure_events"] == 2


def test_observability_ui_exposes_window_samples_and_failure_classification() -> None:
    source = Path(
        "src/stock_ai/ui/static/js/features/agent/agent-observability-dashboard.js"
    ).read_text(encoding="utf-8")
    assert "KPI Window" in source
    assert "default_window" in source
    assert "sample_counts" in source
    assert "metric_status" in source
    assert "Failure classification" in source
    assert "No classified failure samples" in source
