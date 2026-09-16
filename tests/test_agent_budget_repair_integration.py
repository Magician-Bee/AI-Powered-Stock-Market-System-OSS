from __future__ import annotations

import asyncio
from typing import Any

import pytest

from open_stock_ai.agent_runtime.model_router import (
    HostModelBudget,
    HostModelExecutor,
    ModelBudgetExceeded,
    ModelProfile,
    ModelRole,
    ModelRouter,
    ProviderMode,
    ProviderNeutralRequest,
    estimate_tokens,
)
from open_stock_ai.agent_runtime.repair import (
    ErrorReceipt,
    HostModelRepairPipeline,
    PatchValidationError,
)


def _profile(
    model_id: str,
    *,
    roles: frozenset[ModelRole],
    context_window: int,
    cost_tier: int,
    reliability: float,
    local: bool = False,
) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        provider="test-provider",
        mode=ProviderMode.STRUCTURED_JSON,
        roles=roles,
        context_window=context_window,
        cost_tier=cost_tier,
        reliability=reliability,
        local=local,
    )


def _request(*, oversized: bool = False) -> ProviderNeutralRequest:
    repeated = "historical-output-" * (500 if oversized else 1)
    return ProviderNeutralRequest(
        task="Repair the failed local fragment only. " + repeated,
        context={
            "current_objective": "repair tool arguments",
            "historical_trace": repeated,
            "failure_ledger": [{"error": repeated} for _ in range(20 if oversized else 1)],
        },
        tool_schemas=tuple(
            {"name": f"tool-{index}", "description": repeated}
            for index in range(20 if oversized else 1)
        ),
        evidence=tuple({"claim": repeated} for _ in range(20 if oversized else 1)),
        plan={"nodes": [{"summary": repeated} for _ in range(20 if oversized else 1)]},
        memory=tuple({"content": repeated} for _ in range(20 if oversized else 1)),
        output_contract={
            "type": "object",
            "required": ["repair_for", "patch"],
        },
    )


def _receipt() -> ErrorReceipt:
    return ErrorReceipt(
        category="invalid_arguments",
        component="web.research",
        location="$.tool_calls[0].arguments",
        expected="object",
        actual="string",
        retryable=True,
        branch_id="BR-repair",
    )


def _document_schema() -> dict[str, Any]:
    return {
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
                            "additionalProperties": False,
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                },
            },
            "protected": {"type": "string"},
        },
    }


def test_host_model_executor_compacts_routes_and_settles_actual_budget() -> None:
    local_repair = _profile(
        "local-repair",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=1_600,
        cost_tier=0,
        reliability=0.8,
        local=True,
    )
    cloud_repair = _profile(
        "cloud-repair",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=32_000,
        cost_tier=2,
        reliability=0.99,
    )
    received: list[ProviderNeutralRequest] = []

    async def local_provider(request: ProviderNeutralRequest) -> dict[str, Any]:
        received.append(request)
        return {"repair_for": "ERR-test", "patch": []}

    async def cloud_provider(request: ProviderNeutralRequest) -> dict[str, Any]:
        raise AssertionError("costlier model must not be called")

    budget = HostModelBudget(
        scope_id="branch:repair",
        limit=2_000,
        max_cost_tier=1,
    )
    executor = HostModelExecutor(
        ModelRouter([cloud_repair, local_repair]),
        {
            "local-repair": local_provider,
            "cloud-repair": cloud_provider,
        },
    )
    result = asyncio.run(
        executor.execute(
            ModelRole.REPAIR,
            _request(oversized=True),
            budget=budget,
            reserve_output_tokens=256,
        )
    )

    assert result.route.profile is local_repair
    assert result.route.compacted is True
    assert result.route.estimated_input_tokens < local_repair.context_window - 256
    assert len(received) == 1
    assert received[0].output_contract == _request().output_contract
    assert len(received[0].tool_schemas) <= 12
    assert budget.consumed == result.input_tokens + result.output_tokens
    assert result.output_tokens == estimate_tokens(result.output)


def test_host_model_executor_rejects_oversized_output_without_exceeding_ledger() -> None:
    profile = _profile(
        "bounded",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=4_096,
        cost_tier=0,
        reliability=0.8,
    )

    async def provider(_: ProviderNeutralRequest) -> dict[str, Any]:
        return {"payload": "x" * 8_000}

    budget = HostModelBudget(scope_id="turn:repair", limit=2_000)
    executor = HostModelExecutor(ModelRouter([profile]), {"bounded": provider})

    with pytest.raises(ModelBudgetExceeded, match="exceeded reserved output"):
        asyncio.run(
            executor.execute(
                ModelRole.REPAIR,
                _request(),
                budget=budget,
                reserve_output_tokens=64,
            )
        )

    assert budget.consumed <= budget.limit
    assert budget.remaining >= 0


def test_repair_pipeline_runs_receipt_bound_model_patch_then_schema_validates() -> None:
    profile = _profile(
        "cheap-repair",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=8_192,
        cost_tier=0,
        reliability=0.85,
        local=True,
    )
    receipt = _receipt()
    provider_requests: list[ProviderNeutralRequest] = []

    async def provider(request: ProviderNeutralRequest) -> dict[str, Any]:
        provider_requests.append(request)
        return {
            "structured_result": {
                "repair_for": receipt.error_id,
                "patch": [
                    {
                        "op": "replace",
                        "path": "$.tool_calls[0].arguments",
                        "value": {"query": "Wistron US demand"},
                    }
                ],
            }
        }

    original = {
        "tool_calls": [{"name": "web.research", "arguments": "Wistron demand"}],
        "protected": "unchanged",
    }
    budget = HostModelBudget(scope_id="branch:BR-repair", limit=4_096, max_cost_tier=0)
    pipeline = HostModelRepairPipeline(
        HostModelExecutor(ModelRouter([profile]), {profile.model_id: provider})
    )
    result = asyncio.run(
        pipeline.repair(
            original,
            receipt=receipt,
            allowed_scopes=("$.tool_calls[0].arguments",),
            expected_schema=_document_schema(),
            budget=budget,
            reserve_output_tokens=512,
        )
    )

    assert result.strategy == "model_local_patch"
    assert result.model_id == "cheap-repair"
    assert result.value["tool_calls"][0]["arguments"] == {
        "query": "Wistron US demand"
    }
    assert original["tool_calls"][0]["arguments"] == "Wistron demand"
    assert result.value["protected"] == "unchanged"
    assert len(provider_requests) == 1
    assert provider_requests[0].tool_schemas == ()
    assert provider_requests[0].context["error_receipt"]["error_id"] == receipt.error_id
    assert provider_requests[0].context["allowed_patch_scopes"] == [
        "$.tool_calls[0].arguments"
    ]
    assert provider_requests[0].output_contract["x-target-document-schema"] == _document_schema()
    assert budget.consumed == result.input_tokens + result.output_tokens


def test_repair_pipeline_rejects_out_of_scope_model_patch_atomically() -> None:
    profile = _profile(
        "repair",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=8_192,
        cost_tier=0,
        reliability=0.8,
    )
    receipt = _receipt()

    async def malicious_provider(_: ProviderNeutralRequest) -> dict[str, Any]:
        return {
            "repair_for": receipt.error_id,
            "patch": [
                {"op": "replace", "path": "$.protected", "value": "changed"}
            ],
        }

    original = {
        "tool_calls": [{"name": "web.research", "arguments": "Wistron demand"}],
        "protected": "unchanged",
    }
    pipeline = HostModelRepairPipeline(
        HostModelExecutor(ModelRouter([profile]), {profile.model_id: malicious_provider})
    )

    with pytest.raises(PatchValidationError, match="outside allowed scope"):
        asyncio.run(
            pipeline.repair(
                original,
                receipt=receipt,
                allowed_scopes=("$.tool_calls[0].arguments",),
                expected_schema=_document_schema(),
                budget=HostModelBudget(scope_id="branch:malicious", limit=4_096),
                reserve_output_tokens=512,
            )
        )

    assert original == {
        "tool_calls": [{"name": "web.research", "arguments": "Wistron demand"}],
        "protected": "unchanged",
    }


def test_repair_pipeline_uses_safe_host_repair_without_model_tokens() -> None:
    profile = _profile(
        "unused-repair",
        roles=frozenset({ModelRole.REPAIR}),
        context_window=8_192,
        cost_tier=0,
        reliability=0.8,
    )
    calls = 0

    async def provider(_: ProviderNeutralRequest) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    receipt = _receipt()
    original = {
        "tool_calls": [
            {
                "name": "web.research",
                "arguments": '```json\n{"query":"Wistron US demand"}\n```',
            }
        ],
        "protected": "unchanged",
    }
    budget = HostModelBudget(scope_id="branch:deterministic", limit=4_096)
    pipeline = HostModelRepairPipeline(
        HostModelExecutor(ModelRouter([profile]), {profile.model_id: provider})
    )
    result = asyncio.run(
        pipeline.repair(
            original,
            receipt=receipt,
            allowed_scopes=("$.tool_calls[0].arguments",),
            expected_schema=_document_schema(),
            budget=budget,
        )
    )

    assert result.strategy == "host_repair"
    assert result.model_id is None
    assert result.value["tool_calls"][0]["arguments"] == {
        "query": "Wistron US demand"
    }
    assert calls == 0
    assert budget.consumed == 0
