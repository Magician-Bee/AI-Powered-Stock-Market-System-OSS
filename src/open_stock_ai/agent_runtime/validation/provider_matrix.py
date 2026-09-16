from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..model_router import (
    HostModelBudget,
    HostModelExecutor,
    MatrixBehavior,
    ModelProfile,
    ModelRole,
    ModelRouter,
    ProviderClass,
    ProviderMode,
    ProviderNeutralRequest,
)
from ..providers.normalizer import ProviderOutputNormalizer, ProviderProtocolError
from ..repair import ErrorReceipt, HostModelRepairPipeline


class ContractProvider(Protocol):
    """Small provider-neutral boundary usable by real and test adapters."""

    provider_class: ProviderClass

    async def complete(self, request: dict[str, Any]) -> Any: ...


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class MatrixCaseResult:
    provider_class: ProviderClass
    behavior: MatrixBehavior
    observations: Mapping[str, bool]
    detail: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(self.observations.values())


@dataclass(frozen=True, slots=True)
class MatrixReport:
    cases: tuple[MatrixCaseResult, ...]

    @property
    def complete(self) -> bool:
        expected = {
            (provider, behavior)
            for provider in ProviderClass
            for behavior in MatrixBehavior
        }
        observed = {(case.provider_class, case.behavior) for case in self.cases}
        return observed == expected and all(case.passed for case in self.cases)

    @property
    def failures(self) -> tuple[MatrixCaseResult, ...]:
        return tuple(case for case in self.cases if not case.passed)


class ReferenceContractProvider:
    """Deterministic provider fixtures that exercise each adapter shape.

    This is not a pass/fail stub: every response is consumed by the same host
    protocol, schema checks, tool runner and approval gate as an external
    provider.  A live adapter can be supplied to ``ProviderContractMatrix`` by
    implementing ``ContractProvider``.
    """

    def __init__(self, provider_class: ProviderClass) -> None:
        self.provider_class = provider_class
        self.calls: Counter[str] = Counter()
        self.task_ids: set[str] = set()

    async def complete(self, request: dict[str, Any]) -> Any:
        scenario = str(request["scenario"])
        self.calls[scenario] += 1
        self.task_ids.add(str(request["task_id"]))
        turn = int(request.get("turn", 0))
        payload = self._payload(scenario, turn, request)
        return self._encode(payload, malformed=bool(request.get("request_malformed")))

    def _payload(
        self,
        scenario: str,
        turn: int,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "task_id": str(request["task_id"]),
            "state": "complete",
            "summary": f"{self.provider_class.value}:{scenario}",
            "structured_result": {"scenario": scenario, "valid": True},
            "tool_calls": [],
        }
        if scenario == MatrixBehavior.TOOL_CALL.value:
            instrument_id = str(request["instrument_id"])
            base.update(
                state="continue",
                tool_calls=[
                    {
                        "id": "quote-1",
                        "name": "market.quote",
                        "arguments": {"symbol": instrument_id},
                    }
                ],
            )
        elif scenario == MatrixBehavior.PARALLEL_BRANCHES.value:
            base.update(
                state="continue",
                tool_calls=[
                    {"id": "branch-a", "name": "research.branch", "arguments": {"topic": "fundamental"}},
                    {"id": "branch-b", "name": "research.branch", "arguments": {"topic": "technical"}},
                ],
                structured_result={
                    "branches": [
                        {"branch_id": "branch-a", "dependencies": []},
                        {"branch_id": "branch-b", "dependencies": []},
                    ]
                },
            )
        elif scenario == MatrixBehavior.LONG_SESSION.value:
            history = request.get("history") or []
            digest = hashlib.sha256(
                json.dumps(history, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            base["structured_result"] = {
                "turn": turn,
                "history_size": len(history),
                "history_digest": digest,
            }
        elif scenario == MatrixBehavior.ERROR_REPAIR.value and isinstance(
            request.get("repair_request"), dict
        ):
            repair_request = request["repair_request"]
            receipt = repair_request.get("error_receipt") or {}
            scopes = repair_request.get("allowed_patch_scopes") or []
            base["structured_result"] = {
                "repair_for": receipt.get("error_id"),
                "patch": [
                    {
                        "op": "replace",
                        "path": scopes[0],
                        "value": {
                            "query": "Wistron US demand",
                            "max_sources": 3,
                        },
                    }
                ],
            }
        elif scenario == MatrixBehavior.APPROVAL_INTERACTION.value:
            instrument_id = str(request["instrument_id"])
            base.update(
                state="continue",
                tool_calls=[
                    {
                        "id": "order-1",
                        "name": "broker.paper_order",
                        "arguments": {"symbol": instrument_id, "side": "buy", "quantity": 1},
                    }
                ],
                structured_result={"interaction": "approval", "risk": "mutation"},
            )
        elif scenario == MatrixBehavior.REFLECTION.value:
            base["structured_result"] = {
                "reflection": {
                    "observations": ["initial evidence coverage is incomplete"],
                    "evidence_ids": ["evidence-under-test"],
                    "remaining_gaps": ["second-source verification"],
                    "strategy_patch": {"research_depth": "expanded"},
                }
            }
        elif scenario == MatrixBehavior.AUTOMATION.value:
            base.update(
                state="continue",
                tool_calls=[
                    {
                        "id": "automation-preview-1",
                        "name": "automation.preview",
                        "arguments": {"automation_id": "automation-under-test"},
                    }
                ],
                structured_result={
                    "automation_plan": {
                        "automation_id": "automation-under-test",
                        "goal": "re-evaluate the tracked objective",
                        "trigger": {"kind": "schedule", "expression": "scenario-trigger"},
                        "action": {"kind": "agent_run", "objective": "tracked-objective"},
                        "confirmation_required": True,
                    }
                },
            )
        return base

    def _encode(self, payload: dict[str, Any], *, malformed: bool) -> Any:
        if malformed:
            return '{"state":"complete","summary":"truncated"'
        if self.provider_class in {
            ProviderClass.STRONG_CLOUD,
            ProviderClass.LOCAL_TOOL_NATIVE,
            ProviderClass.CODEX,
        }:
            return payload
        if self.provider_class is ProviderClass.SMALL_CLOUD:
            compact = {
                "task_id": payload["task_id"],
                "status": "final" if payload["state"] == "complete" else "need_tools",
                "message": payload["summary"],
                "actions": [
                    {
                        "id": item["id"],
                        "tool": item["name"],
                        "arguments": item["arguments"],
                    }
                    for item in payload["tool_calls"]
                ],
                "result": payload["structured_result"],
            }
            return json.dumps(compact, ensure_ascii=False)
        return "<think>private chain omitted</think>\n```json\n" + json.dumps(
            payload, ensure_ascii=False
        ) + "\n```"


class ProviderContractMatrix:
    LONG_SESSION_TURNS = 40

    def __init__(
        self,
        providers: Mapping[ProviderClass, ContractProvider],
        *,
        tool_executor: ToolExecutor | None = None,
        instrument_id: str = "instrument-under-test",
        task_id: str = "contract-task-under-test",
    ) -> None:
        canonical = set(ProviderClass)
        missing = canonical.difference(providers)
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"Missing provider classes: {names}")
        self.providers = {item: providers[item] for item in ProviderClass}
        self.normalizer = ProviderOutputNormalizer()
        self.tool_executor = tool_executor or self._default_tool_executor
        self.instrument_id = instrument_id
        self.task_id = task_id

    @classmethod
    def reference(cls) -> "ProviderContractMatrix":
        return cls({item: ReferenceContractProvider(item) for item in ProviderClass})

    async def run(self) -> MatrixReport:
        cases: list[MatrixCaseResult] = []
        for provider_class, provider in self.providers.items():
            for behavior in MatrixBehavior:
                try:
                    observations = await self._run_case(provider, behavior)
                    detail = ""
                except Exception as exc:  # case failures belong in the report
                    observations = {"completed_without_exception": False}
                    detail = f"{type(exc).__name__}: {exc}"
                cases.append(
                    MatrixCaseResult(provider_class, behavior, observations, detail)
                )
        return MatrixReport(tuple(cases))

    async def _run_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        if behavior is MatrixBehavior.STRUCTURED_OUTPUT:
            payload = await self._invoke(provider, behavior)
            return self._contract_observations(payload)
        if behavior is MatrixBehavior.TOOL_CALL:
            payload = await self._invoke(provider, behavior)
            calls = self._valid_tool_calls(payload)
            results = [
                await self.tool_executor(item["name"], item["arguments"])
                for item in calls
            ]
            return {
                "one_valid_tool_call": len(calls) == 1,
                "arguments_preserved": calls[0]["arguments"] == {"symbol": self.instrument_id},
                "tool_executed": len(results) == 1 and results[0].get("ok") is True,
            }
        if behavior is MatrixBehavior.PARALLEL_BRANCHES:
            return await self._parallel_case(provider, behavior)
        if behavior is MatrixBehavior.LONG_SESSION:
            return await self._long_session_case(provider, behavior)
        if behavior is MatrixBehavior.ERROR_REPAIR:
            return await self._repair_case(provider, behavior)
        if behavior is MatrixBehavior.APPROVAL_INTERACTION:
            return await self._approval_case(provider, behavior)
        if behavior is MatrixBehavior.REFLECTION:
            return await self._reflection_case(provider, behavior)
        return await self._automation_case(provider, behavior)

    async def _invoke(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
        **extra: Any,
    ) -> dict[str, Any]:
        raw = await provider.complete(
            {
                "scenario": behavior.value,
                "task_id": self.task_id,
                "instrument_id": self.instrument_id,
                **extra,
            }
        )
        payload = self.normalizer.normalize(raw).payload
        if payload.get("task_id") != self.task_id:
            raise ValueError("Provider response lost or changed the matrix task identity")
        return payload

    async def _parallel_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        payload = await self._invoke(provider, behavior)
        calls = self._valid_tool_calls(payload)
        branches = (payload.get("structured_result") or {}).get("branches") or []
        active = 0
        peak_active = 0
        lock = asyncio.Lock()
        release = asyncio.Event()

        async def execute(item: dict[str, Any]) -> dict[str, Any]:
            nonlocal active, peak_active
            async with lock:
                active += 1
                peak_active = max(peak_active, active)
                if active == len(calls):
                    release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            result = await self.tool_executor(item["name"], item["arguments"])
            async with lock:
                active -= 1
            return result

        results = await asyncio.gather(*(execute(item) for item in calls))
        return {
            "two_independent_branches": len(branches) == 2
            and all(not item.get("dependencies") for item in branches),
            "two_tool_calls": len(calls) == 2,
            "overlap_observed": peak_active >= 2,
            "all_branches_completed": all(item.get("ok") is True for item in results),
        }

    async def _long_session_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        history: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        expected_digest = ""
        for turn in range(1, self.LONG_SESSION_TURNS + 1):
            expected_digest = hashlib.sha256(
                json.dumps(history, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            final = await self._invoke(
                provider,
                behavior,
                turn=turn,
                history=list(history),
            )
            history.append({"turn": turn, "summary": final.get("summary")})
        result = final.get("structured_result") or {}
        return {
            "all_turns_completed": len(history) == self.LONG_SESSION_TURNS,
            "final_turn_preserved": result.get("turn") == self.LONG_SESSION_TURNS,
            "history_not_truncated": result.get("history_size") == self.LONG_SESSION_TURNS - 1,
            "history_integrity": result.get("history_digest") == expected_digest,
        }

    async def _repair_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        rejected = False
        try:
            await self._invoke(provider, behavior, request_malformed=True)
        except ProviderProtocolError:
            rejected = True
        document = {
            "tool_calls": [
                {
                    "name": "web.research",
                    # This fragment is intentionally semantic and cannot be
                    # converted to an argument object by deterministic JSON
                    # cleanup. It must take the L1 model-local patch path.
                    "arguments": "Wistron demand",
                }
            ],
            "protected": "unchanged",
        }
        schema = {
            "type": "object",
            "required": ["tool_calls", "protected"],
            "additionalProperties": False,
            "properties": {
                "tool_calls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "arguments"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "arguments": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {
                                    "query": {"type": "string"},
                                    "max_sources": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
                "protected": {"type": "string"},
            },
        }
        receipt = ErrorReceipt(
            category="invalid_arguments",
            component="web.research",
            location="$.tool_calls[0].arguments",
            expected="object",
            actual="string",
            retryable=True,
            branch_id="repair-contract-branch",
        )
        mode = (
            ProviderMode.NATIVE_TOOL_CALLING
            if provider.provider_class
            in {
                ProviderClass.STRONG_CLOUD,
                ProviderClass.LOCAL_TOOL_NATIVE,
                ProviderClass.CODEX,
            }
            else ProviderMode.TEXT_TO_JSON_ADAPTER
        )
        profile = ModelProfile(
            model_id=f"{provider.provider_class.value}-repair",
            provider=provider.provider_class.value,
            mode=mode,
            roles=frozenset({ModelRole.REPAIR}),
            context_window=8_192,
            cost_tier=0,
            reliability=0.9,
            local=provider.provider_class
            in {ProviderClass.LOCAL_TOOL_NATIVE, ProviderClass.LOCAL_TEXT_ONLY},
        )

        async def repair_provider(request: ProviderNeutralRequest) -> Any:
            return await provider.complete(
                {
                    "scenario": behavior.value,
                    "task_id": self.task_id,
                    "instrument_id": self.instrument_id,
                    "repair_request": dict(request.context),
                    "output_contract": request.output_contract,
                }
            )

        budget = HostModelBudget(
            scope_id=f"matrix:{provider.provider_class.value}:repair",
            limit=4_096,
            max_cost_tier=0,
        )
        pipeline = HostModelRepairPipeline(
            HostModelExecutor(
                ModelRouter([profile]),
                {profile.model_id: repair_provider},
            )
        )
        outcome = await pipeline.repair(
            document,
            receipt=receipt,
            allowed_scopes=("$.tool_calls[0].arguments",),
            expected_schema=schema,
            budget=budget,
            reserve_output_tokens=1_024,
        )
        arguments = outcome.value["tool_calls"][0]["arguments"]
        executed = await self.tool_executor("web.research", arguments)
        return {
            "malformed_output_rejected": rejected,
            "model_patch_was_host_validated": outcome.strategy == "model_local_patch",
            "repair_model_was_budget_routed": outcome.model_id == profile.model_id
            and 0 < budget.consumed <= budget.limit,
            "protected_state_preserved": outcome.value["protected"] == "unchanged"
            and document["tool_calls"][0]["arguments"] == "Wistron demand",
            "repaired_arguments_executed": executed.get("ok") is True
            and executed.get("arguments") == {
                "query": "Wistron US demand",
                "max_sources": 3,
            },
        }

    async def _approval_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        payload = await self._invoke(provider, behavior)
        calls = self._valid_tool_calls(payload)
        executed: list[dict[str, Any]] = []
        waiting = bool(calls) and calls[0]["name"] == "broker.paper_order"
        before_approval = len(executed)
        approved = waiting  # deterministic stand-in for an explicit user decision
        if approved:
            executed.append(await self.tool_executor(calls[0]["name"], calls[0]["arguments"]))
        return {
            "approval_interaction_emitted": (payload.get("structured_result") or {}).get("interaction") == "approval",
            "mutation_blocked_before_approval": before_approval == 0,
            "exactly_once_after_approval": len(executed) == 1,
            "approved_arguments_preserved": bool(executed)
            and executed[0].get("arguments") == calls[0]["arguments"],
        }

    async def _reflection_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        payload = await self._invoke(provider, behavior)
        reflection = (payload.get("structured_result") or {}).get("reflection") or {}
        strategy = {"research_depth": "standard"}
        patch = reflection.get("strategy_patch") or {}
        allowed_patch = (
            set(patch) == {"research_depth"}
            and patch.get("research_depth") in {"standard", "expanded"}
        )
        before = dict(strategy)
        if allowed_patch and reflection.get("evidence_ids"):
            strategy.update(patch)
        return {
            "reflection_contract_valid": isinstance(reflection.get("observations"), list)
            and isinstance(reflection.get("remaining_gaps"), list),
            "reflection_is_evidence_grounded": reflection.get("evidence_ids") == ["evidence-under-test"],
            "host_validated_strategy_patch": allowed_patch,
            "strategy_actually_changed": before != strategy
            and strategy.get("research_depth") == "expanded",
        }

    async def _automation_case(
        self,
        provider: ContractProvider,
        behavior: MatrixBehavior,
    ) -> dict[str, bool]:
        payload = await self._invoke(provider, behavior)
        plan = (payload.get("structured_result") or {}).get("automation_plan") or {}
        calls = self._valid_tool_calls(payload)
        previews = [
            await self.tool_executor(item["name"], item["arguments"])
            for item in calls
            if item["name"] == "automation.preview"
        ]
        activations: list[dict[str, Any]] = []
        before_confirmation = len(activations)
        complete_plan = (
            isinstance(plan.get("goal"), str)
            and bool(plan["goal"])
            and isinstance(plan.get("trigger"), dict)
            and isinstance(plan.get("action"), dict)
            and plan.get("confirmation_required") is True
        )
        explicitly_confirmed = complete_plan and len(previews) == 1
        if explicitly_confirmed:
            activations.append(
                await self.tool_executor(
                    "automation.activate",
                    {"automation_id": plan.get("automation_id")},
                )
            )
        return {
            "goal_trigger_action_present": complete_plan,
            "preview_executed": len(previews) == 1 and previews[0].get("ok") is True,
            "not_activated_before_confirmation": before_confirmation == 0,
            "activated_exactly_once_after_confirmation": len(activations) == 1
            and activations[0].get("ok") is True,
        }

    @staticmethod
    def _valid_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item
            for item in payload.get("tool_calls") or []
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and bool(item["name"])
            and isinstance(item.get("arguments"), dict)
        ]

    @staticmethod
    def _contract_observations(payload: dict[str, Any]) -> dict[str, bool]:
        return {
            "schema_valid": payload.get("state") in {"continue", "complete"}
            and isinstance(payload.get("summary"), str)
            and isinstance(payload.get("structured_result"), dict)
            and isinstance(payload.get("tool_calls"), list),
            "structured_result_present": bool(payload.get("structured_result")),
            "summary_present": bool(payload.get("summary")),
        }

    @staticmethod
    async def _default_tool_executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"ok": True, "tool": name, "arguments": dict(arguments)}
