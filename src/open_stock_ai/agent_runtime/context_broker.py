from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextExposureProfile:
    task_kind: str
    include_ui: bool = False
    include_market: bool = False
    include_account: bool = False
    include_project: bool = False
    include_external: bool = False
    include_memory: bool = False
    capability_prefixes: tuple[str, ...] = ()


class ContextBroker:
    """Select the minimum Host context required by the current task hypothesis."""

    PROFILES = {
        "general_answer": ContextExposureProfile(task_kind="general_answer"),
        # Artifact work is a bounded local mutation.  It must not inherit the
        # much wider project/terminal/Git surface merely because it writes a
        # run-scoped document that the user can later revise in the Agent UI.
        "artifact_task": ContextExposureProfile(
            task_kind="artifact_task",
            capability_prefixes=("artifact.",),
        ),
        "project_task": ContextExposureProfile(
            task_kind="project_task",
            include_project=True,
            include_external=True,
            include_memory=True,
            capability_prefixes=(
                "project.",
                "terminal.",
                "git.",
                "artifact.",
                "workflow.",
                "agent.",
                "external.",
                "skills.",
                "mcp.",
                "memory.",
                "system.",
            ),
        ),
        "market_information": ContextExposureProfile(
            task_kind="market_information",
            include_market=True,
            capability_prefixes=(
                "agent.",
                "market.",
                "broker.list_connections",
                "broker.health",
                "broker.market.",
                "web.",
                "system.",
            ),
        ),
        "market_radar": ContextExposureProfile(
            task_kind="market_radar",
            include_market=True,
            include_account=True,
            capability_prefixes=(
                "market.",
                "portfolio.",
                "broker.list_connections",
                "broker.health",
                "broker.market.",
                "broker.account.",
                "risk.",
                "web.",
                "browser.",
                "system.",
            ),
        ),
        "market_decision": ContextExposureProfile(
            task_kind="market_decision",
            include_market=True,
            include_account=True,
            capability_prefixes=(
                "agent.",
                "market.",
                "paper.",
                "broker.",
                "risk.",
                "web.",
                "browser.",
                "system.",
            ),
        ),
        "paper_execution": ContextExposureProfile(
            task_kind="paper_execution",
            include_market=True,
            include_account=True,
            capability_prefixes=("market.", "paper.", "portfolio.", "system."),
        ),
        "ui_task": ContextExposureProfile(
            task_kind="ui_task",
            include_ui=True,
            capability_prefixes=("ui.", "system."),
        ),
        "current_information": ContextExposureProfile(
            task_kind="current_information",
            capability_prefixes=("web.", "browser.", "system."),
        ),
    }

    def profile(self, task_kind: str) -> ContextExposureProfile:
        return self.PROFILES.get(
            task_kind,
            ContextExposureProfile(
                task_kind=task_kind,
                capability_prefixes=("system.",),
            ),
        )

    def filter_capabilities(
        self,
        manifest: list[dict[str, Any]],
        *,
        task_kind: str,
        task_kinds: list[str] | tuple[str, ...] | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        composed_kinds = tuple(dict.fromkeys((task_kind, *(task_kinds or ()))))
        prefixes = tuple(
            dict.fromkeys(
                prefix
                for kind in composed_kinds
                for prefix in self.profile(kind).capability_prefixes
            )
        )
        if not prefixes:
            return []
        filtered = [
            item
            for item in manifest
            if str(item.get("name") or "").startswith(prefixes)
        ]
        if task_kind == "market_information":
            # ``web.search`` only discovers URLs and is never sufficient evidence
            # for a final current-information answer. Smaller local models tend to
            # spend their full step budget repeatedly selecting it instead of
            # opening the results. Expose the end-to-end ``web.research`` and
            # explicit ``web.fetch`` tools for these tasks instead.
            filtered = [
                item for item in filtered if str(item.get("name") or "") != "web.search"
            ]
        if task_kind == "market_decision":
            # A routine market decision already has a bounded Host path:
            # evidence -> deterministic risk -> preview -> local paper order.
            # Exposing recursive subtasks here lets smaller tool-using models
            # delegate a simple order into an unbounded tree before they have
            # inspected a single concrete candidate. Keep that capability for
            # explicit multi-agent/critic workflows, not ordinary decisions.
            filtered = [
                item
                for item in filtered
                if str(item.get("name") or "") != "agent.run_subtasks"
            ]
        if "paper_execution" in composed_kinds:
            # An explicit local paper order has a narrow Host protocol. Do not
            # expose web/subtask/auxiliary tools that turn one simulation into
            # an unbounded research tree.
            allowed = {
                "market.analyze_symbol",
                "market.research_pack",
                "portfolio.snapshot",
                "paper.preview_order",
                "paper.submit_order",
                "system.capabilities",
            }
            filtered = [item for item in filtered if str(item.get("name") or "") in allowed]
        filtered = self._filter_phase(filtered, phase=phase)
        known_prefixes = (
            "project.",
            "terminal.",
            "git.",
            "artifact.",
            "workflow.",
            "agent.",
            "skills.",
            "mcp.",
            "memory.",
            "market.",
            "paper.",
            "portfolio.",
            "risk.",
            "ui.",
            "web.",
            "browser.",
            "system.",
            "external.",
            "broker.",
        )
        if (
            not filtered
            and task_kind != "general_answer"
            and manifest
            and not any(
                str(item.get("name") or "").startswith(known_prefixes)
                for item in manifest
            )
        ):
            # Isolated embedders and tests may register their own namespace.
            # Their entire registry is already the task-local capability surface,
            # unlike the production global registry.
            return list(manifest)
        return filtered

    @staticmethod
    def _filter_phase(
        manifest: list[dict[str, Any]],
        *,
        phase: str | None,
    ) -> list[dict[str, Any]]:
        """Apply stage disclosure after composing every detected intent."""

        normalized = str(phase or "").strip().casefold()
        if not normalized:
            return manifest
        phase_prefixes = {
            "research": (
                "agent.", "market.", "portfolio.", "web.", "browser.", "artifact.",
                "interaction.", "system.",
            ),
            "decision": (
                "agent.", "market.", "portfolio.", "risk.", "analysis.", "artifact.",
                "interaction.", "system.",
            ),
            "automation": (
                "scheduler.preview", "automation.plan", "automation.inspect",
                "artifact.", "interaction.", "system.",
            ),
            "confirmed": (
                "automation.", "scheduler.", "notification.", "paper.",
                "broker.paper_order", "artifact.", "system.",
            ),
        }
        allowed = phase_prefixes.get(normalized)
        if allowed is None:
            raise ValueError(f"Unsupported capability disclosure phase: {phase}")
        return [
            item for item in manifest
            if str(item.get("name") or "").startswith(allowed)
        ]

    def disclose_capabilities(
        self,
        manifest: list[dict[str, Any]],
        *,
        task_kind: str,
        step: int,
        task_kinds: list[str] | tuple[str, ...] | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        """Progressively disclose only task-relevant tool schemas.

        The first provider turn receives a compact read-only discovery surface.
        Later turns may receive the rest of the same task-scoped profile, but
        never the unrelated global manifest.
        """

        filtered = self.filter_capabilities(
            manifest,
            task_kind=task_kind,
            task_kinds=task_kinds,
            phase=phase,
        )
        if step > 1 or len(filtered) <= 24:
            return filtered
        preferred_names = {
            "market.analyze_universe",
            "market.analyze_symbol",
            "market.research_pack",
            "market.scan_watchlist",
            "market.search_taiwan_securities",
            "portfolio.snapshot",
            "project.list_files",
            "project.search_text",
            "project.read_file",
            "web.research",
            "web.fetch",
            "ui.get_state",
            "ui.navigate",
            "system.capabilities",
        }
        first = [
            item
            for item in filtered
            if str(item.get("name") or "") in preferred_names
            or (
                item.get("mutating") is not True
                and str(item.get("risk_class") or "read_only") == "read_only"
            )
        ][:24]
        return first or filtered[:24]
