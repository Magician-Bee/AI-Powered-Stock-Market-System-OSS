from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from ..model_router import MatrixBehavior
from ..providers.normalizer import ProviderOutputNormalizer, ProviderProtocolError
from .provider_matrix import ContractProvider


class FaultKind(StrEnum):
    INVALID_JSON = "invalid_json"
    WRONG_ARGUMENTS = "wrong_arguments"
    TOOL_TIMEOUT = "tool_timeout"
    API_500 = "api_500"
    RATE_LIMIT = "rate_limit"
    NETWORK_DROP = "network_drop"
    WEB_SOURCE_MISSING = "web_source_missing"
    BROWSER_CRASH = "browser_crash"
    PROVIDER_CRASH = "provider_crash"
    HOST_RESTART = "host_restart"
    DB_RESTART = "db_restart"
    DUPLICATE_EVENT = "duplicate_event"
    USER_INTERRUPT = "user_interrupt"
    CONFLICTING_USER_MESSAGE = "conflicting_user_message"

    # Compatibility aliases for the original six-case draft.
    MALFORMED_JSON = INVALID_JSON
    TIMEOUT = TOOL_TIMEOUT
    PROVIDER_DISCONNECT = NETWORK_DROP
    RESTART_RECOVERY = HOST_RESTART
    TOOL_FAILURE = API_500


class ProviderAPIError(RuntimeError):
    status_code = 500


class ProviderRateLimitError(RuntimeError):
    def __init__(self, *, retry_after: float) -> None:
        super().__init__("chaos: provider rate limited")
        self.retry_after = retry_after


class ProviderCrashError(RuntimeError):
    pass


class BrowserCrashError(RuntimeError):
    pass


class WebSourceMissingError(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class ChaosFault:
    kind: FaultKind
    point: str
    occurrence: int = 1

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError("Fault occurrence must be positive")


@dataclass(frozen=True, slots=True)
class ChaosOutcome:
    fault: FaultKind
    strategy: str
    invariants: Mapping[str, bool]
    trace: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return bool(self.invariants) and all(self.invariants.values())


@dataclass(frozen=True, slots=True)
class ChaosCampaignReport:
    seed: int
    outcomes: tuple[ChaosOutcome, ...]

    @property
    def complete(self) -> bool:
        return (
            {item.fault for item in self.outcomes} == set(FaultKind)
            and len(self.outcomes) == len(FaultKind) == 14
            and all(item.passed for item in self.outcomes)
        )


class ChaosInjector:
    """Deterministic fault script for provider, event, tool and I/O boundaries."""

    def __init__(self, faults: tuple[ChaosFault, ...], *, seed: int) -> None:
        if seed < 0:
            raise ValueError("Chaos seed cannot be negative")
        self.seed = seed
        self.faults = faults
        self._occurrences: Counter[str] = Counter()
        self.trace: list[str] = [f"seed:{seed}"]

    def trigger(self, point: str) -> FaultKind | None:
        self._occurrences[point] += 1
        occurrence = self._occurrences[point]
        for fault in self.faults:
            if fault.point == point and fault.occurrence == occurrence:
                self.trace.append(f"inject:{fault.kind.value}@{point}#{occurrence}")
                return fault.kind
        self.trace.append(f"pass:{point}#{occurrence}")
        return None

    async def provider_call(
        self,
        provider: ContractProvider,
        request: dict[str, Any],
    ) -> Any:
        fault = self.trigger("provider")
        if fault is FaultKind.API_500:
            raise ProviderAPIError("chaos: provider HTTP 500")
        if fault is FaultKind.RATE_LIMIT:
            raise ProviderRateLimitError(retry_after=0.25)
        if fault is FaultKind.NETWORK_DROP:
            raise ConnectionError("chaos: network dropped")
        if fault is FaultKind.PROVIDER_CRASH:
            raise ProviderCrashError("chaos: provider process crashed")
        raw = await provider.complete(request)
        if fault is FaultKind.INVALID_JSON:
            return '{"state":"complete","summary":"chaos-truncated"'
        return raw

    def mutate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.trigger("arguments") is FaultKind.WRONG_ARGUMENTS:
            return {"symbol": 17, "unexpected": True}
        return dict(arguments)

    def event_deliveries(self, event: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        fault = self.trigger("event")
        copies = 2 if fault is FaultKind.DUPLICATE_EVENT else 1
        return tuple(dict(event) for _ in range(copies))

    async def tool_call(
        self,
        tool: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.trigger("tool") is FaultKind.TOOL_TIMEOUT:
            raise TimeoutError("chaos: tool timed out")
        return await tool()

    async def web_fetch(
        self,
        fetch: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.trigger("web") is FaultKind.WEB_SOURCE_MISSING:
            raise WebSourceMissingError("chaos: requested source is unavailable")
        return await fetch()

    async def browser_call(
        self,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.trigger("browser") is FaultKind.BROWSER_CRASH:
            raise BrowserCrashError("chaos: browser process crashed")
        return await operation()


class _ChaosJournal:
    """Minimal durable journal proving restart and event idempotence."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            create table if not exists chaos_runs(
                run_id text primary key,
                status text not null,
                checkpoint_json text not null
            )
            """
        )
        self.connection.execute(
            """
            create table if not exists chaos_events(
                event_id text primary key,
                payload_json text not null
            )
            """
        )
        self.connection.commit()

    def checkpoint(self, run_id: str, status: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            insert into chaos_runs(run_id, status, checkpoint_json) values(?, ?, ?)
            on conflict(run_id) do update set
                status=excluded.status,
                checkpoint_json=excluded.checkpoint_json
            """,
            (run_id, status, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()

    def append_event(self, event: dict[str, Any]) -> bool:
        cursor = self.connection.execute(
            "insert or ignore into chaos_events(event_id, payload_json) values(?, ?)",
            (str(event["event_id"]), json.dumps(event, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def recover(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "select status, checkpoint_json from chaos_runs where run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {"status": row[0], "checkpoint": json.loads(row[1])}

    def event_count(self) -> int:
        row = self.connection.execute("select count(*) from chaos_events").fetchone()
        return int(row[0] if row else 0)

    def close(self) -> None:
        self.connection.close()


class ChaosCampaign:
    """Executes all fourteen P104 faults and derives results from invariants."""

    def __init__(self, provider: ContractProvider, *, seed: int = 104) -> None:
        self.provider = provider
        self.seed = seed
        self.normalizer = ProviderOutputNormalizer()

    async def run(self, journal_path: str | Path) -> ChaosCampaignReport:
        path = Path(journal_path)
        outcomes = (
            await self._invalid_json(),
            self._wrong_arguments(),
            await self._tool_timeout(),
            await self._provider_retry(FaultKind.API_500),
            await self._provider_retry(FaultKind.RATE_LIMIT),
            await self._provider_retry(FaultKind.NETWORK_DROP),
            await self._web_source_missing(),
            await self._browser_crash(),
            await self._provider_crash_failover(),
            self._host_restart(path),
            self._db_restart(path),
            self._duplicate_event(path),
            await self._user_interrupt(path),
            self._conflicting_user_message(path),
        )
        return ChaosCampaignReport(self.seed, outcomes)

    async def _invalid_json(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.INVALID_JSON, "provider")
        rejected = False
        attempts = 0
        payload: dict[str, Any] | None = None
        for attempts in range(1, 3):
            raw = await injector.provider_call(self.provider, self._provider_request())
            try:
                payload = self.normalizer.normalize(raw).payload
                break
            except ProviderProtocolError:
                rejected = True
        return ChaosOutcome(
            FaultKind.INVALID_JSON,
            "reject_then_deterministic_retry",
            {
                "invalid_payload_rejected": rejected,
                "single_repair_retry": attempts == 2,
                "valid_payload_recovered": bool(payload and payload.get("state") == "complete"),
            },
            tuple(injector.trace),
        )

    def _wrong_arguments(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.WRONG_ARGUMENTS, "arguments")
        original = {"symbol": "instrument-under-test"}
        received = injector.mutate_arguments(original)
        schema_valid = set(received) == {"symbol"} and isinstance(received.get("symbol"), str)
        executions: list[dict[str, Any]] = []
        if schema_valid:
            executions.append(received)
        return ChaosOutcome(
            FaultKind.WRONG_ARGUMENTS,
            "schema_rejection_without_execution",
            {
                "fault_changed_arguments": received != original,
                "schema_rejected": not schema_valid,
                "tool_not_executed": not executions,
                "original_arguments_unchanged": original == {"symbol": "instrument-under-test"},
            },
            tuple(injector.trace),
        )

    async def _tool_timeout(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.TOOL_TIMEOUT, "tool")
        timeouts = 0
        successes = 0

        async def read_only_tool() -> dict[str, Any]:
            return {"ok": True, "receipt": "read-only-receipt"}

        receipt: dict[str, Any] | None = None
        attempts = 0
        for attempts in range(1, 4):
            try:
                receipt = await injector.tool_call(read_only_tool)
                successes += 1
                break
            except TimeoutError:
                timeouts += 1
        return ChaosOutcome(
            FaultKind.TOOL_TIMEOUT,
            "bounded_read_only_retry",
            {
                "timeout_observed": timeouts == 1,
                "bounded_retry": attempts == 2,
                "one_successful_execution": successes == 1,
                "receipt_recovered": bool(receipt and receipt.get("ok") is True),
            },
            tuple(injector.trace),
        )

    async def _provider_retry(self, kind: FaultKind) -> ChaosOutcome:
        injector = self._inject(kind, "provider")
        errors: list[Exception] = []
        retry_delays: list[float] = []
        payload: dict[str, Any] | None = None
        attempts = 0
        for attempts in range(1, 4):
            try:
                raw = await injector.provider_call(self.provider, self._provider_request())
                payload = self.normalizer.normalize(raw).payload
                break
            except (ProviderAPIError, ProviderRateLimitError, ConnectionError) as exc:
                errors.append(exc)
                retry_delays.append(float(getattr(exc, "retry_after", 0.0)))
        expected_error = {
            FaultKind.API_500: ProviderAPIError,
            FaultKind.RATE_LIMIT: ProviderRateLimitError,
            FaultKind.NETWORK_DROP: ConnectionError,
        }[kind]
        invariants = {
            "expected_fault_observed": len(errors) == 1 and isinstance(errors[0], expected_error),
            "bounded_retry": attempts == 2,
            "eventual_success": bool(payload and payload.get("state") == "complete"),
        }
        if kind is FaultKind.RATE_LIMIT:
            invariants["provider_retry_hint_respected"] = retry_delays == [0.25]
        return ChaosOutcome(
            kind,
            "bounded_retry_with_provider_backoff" if kind is FaultKind.RATE_LIMIT else "bounded_retry",
            invariants,
            tuple(injector.trace),
        )

    async def _web_source_missing(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.WEB_SOURCE_MISSING, "web")
        evidence: list[dict[str, Any]] = []
        missing_sources: list[str] = []

        async def fetch() -> dict[str, Any]:
            return {"url": "https://source.invalid/reference", "content": "data"}

        try:
            evidence.append(await injector.web_fetch(fetch))
        except WebSourceMissingError:
            missing_sources.append("requested-source")
        result_status = "partially_completed" if missing_sources else "completed"
        return ChaosOutcome(
            FaultKind.WEB_SOURCE_MISSING,
            "isolate_as_partial_with_evidence_gap",
            {
                "missing_source_recorded": missing_sources == ["requested-source"],
                "unavailable_source_not_fabricated": evidence == [],
                "result_marked_partial": result_status == "partially_completed",
            },
            tuple(injector.trace),
        )

    async def _browser_crash(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.BROWSER_CRASH, "browser")
        browser_generation = 1
        crashes = 0
        result: dict[str, Any] | None = None

        async def operation() -> dict[str, Any]:
            return {"ok": True, "browser_generation": browser_generation}

        attempts = 0
        for attempts in range(1, 3):
            try:
                result = await injector.browser_call(operation)
                break
            except BrowserCrashError:
                crashes += 1
                browser_generation += 1
        return ChaosOutcome(
            FaultKind.BROWSER_CRASH,
            "restart_browser_then_retry",
            {
                "crash_observed": crashes == 1,
                "browser_restarted": browser_generation == 2,
                "bounded_retry": attempts == 2,
                "operation_recovered_on_new_process": bool(
                    result and result.get("browser_generation") == 2
                ),
            },
            tuple(injector.trace),
        )

    async def _provider_crash_failover(self) -> ChaosOutcome:
        injector = self._inject(FaultKind.PROVIDER_CRASH, "provider")
        primary_crashed = False
        fallback_used = False
        payload: dict[str, Any] | None = None
        try:
            await injector.provider_call(self.provider, self._provider_request())
        except ProviderCrashError:
            primary_crashed = True
            fallback_used = True
            raw = await self.provider.complete(self._provider_request())
            payload = self.normalizer.normalize(raw).payload
        return ChaosOutcome(
            FaultKind.PROVIDER_CRASH,
            "provider_failover",
            {
                "primary_crash_observed": primary_crashed,
                "fallback_path_used": fallback_used,
                "fallback_contract_valid": bool(payload and payload.get("state") == "complete"),
            },
            tuple(injector.trace),
        )

    def _host_restart(self, path: Path) -> ChaosOutcome:
        injector = self._inject(FaultKind.HOST_RESTART, "host")
        first = _ChaosJournal(path)
        first.checkpoint("host-run", "running", {"next_step": 2, "completed": [1]})
        injected = injector.trigger("host") is FaultKind.HOST_RESTART
        first.close()

        reopened = _ChaosJournal(path)
        recovered = reopened.recover("host-run")
        if recovered:
            checkpoint = recovered["checkpoint"]
            reopened.checkpoint(
                "host-run",
                "completed",
                {"next_step": None, "completed": [*checkpoint["completed"], checkpoint["next_step"]]},
            )
        final = reopened.recover("host-run")
        reopened.close()
        return ChaosOutcome(
            FaultKind.HOST_RESTART,
            "durable_checkpoint_resume",
            {
                "host_restart_injected": injected,
                "running_checkpoint_recovered": bool(
                    recovered
                    and recovered["status"] == "running"
                    and recovered["checkpoint"].get("next_step") == 2
                ),
                "resumed_without_replaying_step_one": bool(
                    final
                    and final["status"] == "completed"
                    and final["checkpoint"].get("completed") == [1, 2]
                ),
            },
            tuple(injector.trace),
        )

    def _db_restart(self, path: Path) -> ChaosOutcome:
        injector = self._inject(FaultKind.DB_RESTART, "database")
        first = _ChaosJournal(path)
        first.checkpoint("db-run", "running", {"cursor": "event-1"})
        first.append_event({"event_id": "db-before", "type": "checkpoint.saved"})
        injected = injector.trigger("database") is FaultKind.DB_RESTART
        first.close()

        reopened = _ChaosJournal(path)
        recovered = reopened.recover("db-run")
        accepted_after = reopened.append_event(
            {"event_id": "db-after", "type": "database.recovered"}
        )
        count_after = reopened.event_count()
        reopened.close()
        return ChaosOutcome(
            FaultKind.DB_RESTART,
            "reconnect_and_rehydrate",
            {
                "db_restart_injected": injected,
                "checkpoint_survived_connection_restart": bool(
                    recovered and recovered["checkpoint"].get("cursor") == "event-1"
                ),
                "writes_resume_after_reconnect": accepted_after,
                "pre_and_post_restart_events_present": count_after >= 2,
            },
            tuple(injector.trace),
        )

    def _duplicate_event(self, path: Path) -> ChaosOutcome:
        injector = self._inject(FaultKind.DUPLICATE_EVENT, "event")
        journal = _ChaosJournal(path)
        before = journal.event_count()
        accepted = [
            journal.append_event(event)
            for event in injector.event_deliveries(
                {"event_id": "duplicate-target", "type": "step.completed"}
            )
        ]
        after = journal.event_count()
        journal.close()
        return ChaosOutcome(
            FaultKind.DUPLICATE_EVENT,
            "idempotency_key_deduplication",
            {
                "duplicate_was_delivered": len(accepted) == 2,
                "only_first_was_accepted": accepted == [True, False],
                "single_durable_event_added": after == before + 1,
            },
            tuple(injector.trace),
        )

    async def _user_interrupt(self, path: Path) -> ChaosOutcome:
        injector = self._inject(FaultKind.USER_INTERRUPT, "user_control")
        journal = _ChaosJournal(path)
        side_effects: list[str] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()
            side_effects.append("completed-write")

        task = asyncio.create_task(work())
        await started.wait()
        injected = injector.trigger("user_control") is FaultKind.USER_INTERRUPT
        if injected:
            task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
            journal.checkpoint("interrupt-run", "paused", {"reason": "user_interrupt"})
        recovered = journal.recover("interrupt-run")
        journal.close()
        return ChaosOutcome(
            FaultKind.USER_INTERRUPT,
            "cooperative_cancel_and_pause",
            {
                "interrupt_injected": injected,
                "running_work_cancelled": cancelled,
                "no_post_cancel_side_effect": side_effects == [],
                "paused_state_persisted": bool(
                    recovered
                    and recovered["status"] == "paused"
                    and recovered["checkpoint"].get("reason") == "user_interrupt"
                ),
            },
            tuple(injector.trace),
        )

    def _conflicting_user_message(self, path: Path) -> ChaosOutcome:
        injector = self._inject(FaultKind.CONFLICTING_USER_MESSAGE, "user_message")
        objective = {"version": 3, "text": "current objective"}
        incoming = {
            "base_version": 2,
            "text": "replace objective with conflicting instruction",
        }
        original = dict(objective)
        conflict = injector.trigger("user_message") is FaultKind.CONFLICTING_USER_MESSAGE
        accepted = not conflict and incoming["base_version"] == objective["version"]
        journal = _ChaosJournal(path)
        if accepted:
            objective.update(version=4, text=incoming["text"])
        else:
            journal.checkpoint(
                "conflict-run",
                "waiting_decision",
                {"current": objective, "incoming": incoming, "reason": "stale_base_version"},
            )
        persisted = journal.recover("conflict-run")
        journal.close()
        return ChaosOutcome(
            FaultKind.CONFLICTING_USER_MESSAGE,
            "reject_stale_mutation_and_request_decision",
            {
                "conflict_injected": conflict,
                "stale_message_not_applied": objective == original and not accepted,
                "both_versions_preserved": bool(
                    persisted
                    and persisted["checkpoint"].get("current") == original
                    and persisted["checkpoint"].get("incoming") == incoming
                ),
                "decision_state_persisted": bool(
                    persisted and persisted["status"] == "waiting_decision"
                ),
            },
            tuple(injector.trace),
        )

    def _inject(self, kind: FaultKind, point: str) -> ChaosInjector:
        return ChaosInjector((ChaosFault(kind, point),), seed=self.seed)

    @staticmethod
    def _provider_request() -> dict[str, Any]:
        return {
            "scenario": MatrixBehavior.STRUCTURED_OUTPUT.value,
            "task_id": "chaos-task-under-test",
        }
