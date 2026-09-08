from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    worker_id: str
    run_id: str
    step_id: str
    worker_type: str
    timeout_seconds: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    """Host-owned execution boundary for one capability family.

    A profile is intentionally independent of the model.  It makes timeout,
    cancellation and the isolation supplied by the underlying host tool visible
    in the durable audit record.
    """

    worker_type: str
    isolation: str
    default_timeout_seconds: int
    maximum_timeout_seconds: int
    supports_cancellation: bool = True

    def timeout_for(self, requested: int) -> int:
        value = int(requested or self.default_timeout_seconds)
        return max(1, min(value, self.maximum_timeout_seconds))

    def describe(self) -> dict[str, Any]:
        return {
            "worker_type": self.worker_type,
            "isolation": self.isolation,
            "default_timeout_seconds": self.default_timeout_seconds,
            "maximum_timeout_seconds": self.maximum_timeout_seconds,
            "supports_cancellation": self.supports_cancellation,
        }


IN_PROCESS_PROFILE = WorkerProfile("in_process", "supervised_task", 30, 120)
AGENT_RUNTIME_PROFILE = WorkerProfile(
    "agent_runtime",
    "supervised_recursive_agent_tasks",
    300,
    1800,
)


def profile_for(worker_type: str) -> WorkerProfile:
    from .broker_worker import PROFILE as broker
    from .browser_worker import PROFILE as browser
    from .external_worker import PROFILE as external
    from .project_worker import PROFILE as project
    from .terminal_worker import PROFILE as terminal

    return {
        AGENT_RUNTIME_PROFILE.worker_type: AGENT_RUNTIME_PROFILE,
        broker.worker_type: broker,
        project.worker_type: project,
        terminal.worker_type: terminal,
        browser.worker_type: browser,
        external.worker_type: external,
    }.get(worker_type, IN_PROCESS_PROFILE)


class WorkerCrashedError(RuntimeError):
    pass


class WorkerToolError(RuntimeError):
    """A capability failed inside a healthy worker process."""

    pass
