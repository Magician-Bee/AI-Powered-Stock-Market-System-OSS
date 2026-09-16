from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .store import AutomationStore


@dataclass(frozen=True, slots=True)
class TimePollResult:
    due: int
    claimed: int
    skipped: int
    completed: int
    failed: int
    recovered: int
    outcomes: tuple[Any, ...]


class ProductTimePoller:
    """Drive durable internal time triggers from the product process.

    The poller has no private in-memory clock. Every claim, occurrence key,
    next deadline, and callback receipt lives in SQLite, so a new process can
    continue after downtime without silently losing or duplicating a deadline.
    The owning product decides how often to call :meth:`poll`.
    """

    def __init__(
        self,
        store: AutomationStore,
        dispatch: Callable[..., Any],
        *,
        max_catch_up_per_poll: int = 32,
    ) -> None:
        if max_catch_up_per_poll < 1:
            raise ValueError("max_catch_up_per_poll must be positive")
        self.store = store
        self.dispatch = dispatch
        self.max_catch_up_per_poll = max_catch_up_per_poll

    def poll(self, *, now: datetime | None = None, recover: bool = True) -> TimePollResult:
        current = _utc(now)
        outcomes: list[Any] = []
        due_count = claimed = skipped = completed = failed = recovered = 0

        if recover:
            for receipt in self.store.list_incomplete_time_callbacks():
                recovered += 1
                try:
                    outcomes.append(self._dispatch_receipt(receipt, recovery=True, now=current))
                    completed += 1
                except Exception:
                    failed += 1

        per_schedule: dict[str, int] = {}
        while True:
            due_schedules = self.store.list_due_schedules(now=current)
            if not due_schedules:
                break
            made_progress = False
            for schedule in due_schedules:
                schedule_id = str(schedule["schedule_id"])
                if per_schedule.get(schedule_id, 0) >= self.max_catch_up_per_poll:
                    continue
                due_count += 1
                claim = self.store.claim_due_schedule(schedule_id, now=current)
                if claim.get("skipped"):
                    skipped += 1
                    made_progress = True
                    continue
                if not claim.get("claimed"):
                    continue
                made_progress = True
                claimed += 1
                per_schedule[schedule_id] = per_schedule.get(schedule_id, 0) + 1
                receipt = dict(claim.get("receipt") or {})
                try:
                    outcomes.append(self._dispatch_receipt(receipt, recovery=False, now=current))
                    completed += 1
                except Exception:
                    failed += 1
            if not made_progress:
                break

        return TimePollResult(
            due_count,
            claimed,
            skipped,
            completed,
            failed,
            recovered,
            tuple(outcomes),
        )

    async def poll_async(
        self,
        *,
        now: datetime | None = None,
        recover: bool = True,
    ) -> TimePollResult:
        """Async polling path for provider-backed durable Agent Runs."""

        current = _utc(now)
        outcomes: list[Any] = []
        due_count = claimed = skipped = completed = failed = recovered = 0
        if recover:
            for receipt in self.store.list_incomplete_time_callbacks():
                recovered += 1
                try:
                    outcomes.append(
                        await self._dispatch_receipt_async(receipt, recovery=True, now=current)
                    )
                    completed += 1
                except Exception:
                    failed += 1

        per_schedule: dict[str, int] = {}
        while True:
            due_schedules = self.store.list_due_schedules(now=current)
            if not due_schedules:
                break
            made_progress = False
            for schedule in due_schedules:
                schedule_id = str(schedule["schedule_id"])
                if per_schedule.get(schedule_id, 0) >= self.max_catch_up_per_poll:
                    continue
                due_count += 1
                claim = self.store.claim_due_schedule(schedule_id, now=current)
                if claim.get("skipped"):
                    skipped += 1
                    made_progress = True
                    continue
                if not claim.get("claimed"):
                    continue
                made_progress = True
                claimed += 1
                per_schedule[schedule_id] = per_schedule.get(schedule_id, 0) + 1
                try:
                    outcomes.append(
                        await self._dispatch_receipt_async(
                            dict(claim.get("receipt") or {}),
                            recovery=False,
                            now=current,
                        )
                    )
                    completed += 1
                except Exception:
                    failed += 1
            if not made_progress:
                break
        return TimePollResult(
            due_count,
            claimed,
            skipped,
            completed,
            failed,
            recovered,
            tuple(outcomes),
        )

    def _dispatch_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        recovery: bool,
        now: datetime,
    ) -> Any:
        event = dict(receipt.get("request") or {})
        if recovery:
            event["restart_recovery"] = True
        kwargs = {"now": now, "receipt_id": str(receipt["receipt_id"])}
        try:
            signature = inspect.signature(self.dispatch)
            supports_kwargs = any(
                parameter.kind is parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            accepted = set(signature.parameters)
        except (TypeError, ValueError):
            supports_kwargs, accepted = True, set()
        if supports_kwargs or {"now", "receipt_id"}.issubset(accepted):
            return self.dispatch(str(receipt["schedule_id"]), event, **kwargs)
        return self.dispatch(str(receipt["schedule_id"]), event)

    async def _dispatch_receipt_async(
        self,
        receipt: Mapping[str, Any],
        *,
        recovery: bool,
        now: datetime,
    ) -> Any:
        event = dict(receipt.get("request") or {})
        if recovery:
            event["restart_recovery"] = True
        kwargs = {"now": now, "receipt_id": str(receipt["receipt_id"])}
        value = self.dispatch(str(receipt["schedule_id"]), event, **kwargs)
        return await value if inspect.isawaitable(value) else value


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
