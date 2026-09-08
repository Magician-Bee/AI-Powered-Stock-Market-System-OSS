from __future__ import annotations

from open_stock_ai.agent_runtime.mutation_receipts import (
    build_domain_mutation_receipt,
    build_mutation_receipt,
    verify_domain_mutation_receipt,
    verify_mutation_receipt,
)
from open_stock_ai.agent_runtime.validators import ValidatorEngine
from stock_ai.agent_tools import StockAgentToolRegistry


def test_mutation_receipt_binds_request_result_and_state_hashes() -> None:
    receipt = build_mutation_receipt(
        name="project.write_file",
        arguments={"path": "notes.txt", "content": "safe"},
        result={"ok": True, "after_sha256": "a" * 64},
        before={"hash": "b" * 64},
        after={"hash": "a" * 64},
        accepted=True,
    )

    assert receipt["authority"] == "host_validator"
    assert receipt["accepted"] is True
    assert verify_mutation_receipt(receipt)
    assert not verify_mutation_receipt({**receipt, "accepted": False})


def test_domain_mutation_receipt_binds_provider_to_host_validated_receipt() -> None:
    host_receipt = build_mutation_receipt(
        name="schedule.create",
        arguments={"name": "morning brief"},
        result={"schedule_id": "SCH-1"},
        before={"schedules": []},
        after={"schedules": [{"id": "SCH-1"}]},
        accepted=True,
    )
    receipt = build_domain_mutation_receipt(
        provider="scheduler",
        tool={
            "name": "schedule.create",
            "category": "schedule",
            "execution_backend": "in_process",
        },
        mutation_receipt=host_receipt,
    )

    assert verify_domain_mutation_receipt(receipt, mutation_receipt=host_receipt)
    assert not verify_domain_mutation_receipt(
        {**receipt, "provider": "browser"},
        mutation_receipt=host_receipt,
    )
    assert not verify_domain_mutation_receipt(
        receipt,
        mutation_receipt={**host_receipt, "accepted": False},
    )


def test_validator_exposes_content_addressed_receipt_for_mutation_completion() -> None:
    validation = ValidatorEngine().validate_tool_result(
        tool={"name": "custom.mutate", "mutating": True, "output_schema": {"type": "object"}},
        arguments={"path": "notes.txt", "content": "safe"},
        result={"ok": True, "before_sha256": "b" * 64, "after_sha256": "a" * 64},
        before={"hash": "b" * 64},
        after={"hash": "a" * 64},
    )

    assert validation.passed is True
    assert validation.mutation_receipt is not None
    assert verify_mutation_receipt(validation.mutation_receipt)
    assert validation.to_dict()["mutation_receipt"]["receipt_sha256"]
    assert validation.domain_mutation_receipt is not None
    assert verify_domain_mutation_receipt(
        validation.domain_mutation_receipt,
        mutation_receipt=validation.mutation_receipt,
    )
    assert validation.to_dict()["domain_mutation_receipt"]["provider"] == "host_runtime"


def test_registry_mutation_metadata_issues_the_declared_provider_domain_receipt() -> None:
    tool = next(
        item
        for item in StockAgentToolRegistry().manifest()
        if item["name"] == "schedule.create"
    )
    validation = ValidatorEngine().validate_tool_result(
        tool=tool,
        arguments={
            "name": "morning brief",
            "objective": "review market evidence",
            "trigger_type": "interval",
            "interval_seconds": 300,
        },
        result={"schedule_id": "SCH-1", "status": "active"},
        before={"hash": "a" * 64},
        after={"hash": "b" * 64},
    )

    assert validation.passed is True
    assert validation.mutation_receipt is not None
    assert validation.domain_mutation_receipt is not None
    assert validation.domain_mutation_receipt["provider"] == "scheduler"
    assert validation.domain_mutation_receipt["category"] == "schedule"
    assert verify_domain_mutation_receipt(
        validation.domain_mutation_receipt,
        mutation_receipt=validation.mutation_receipt,
    )


def test_host_mutation_postcondition_rejects_loose_domain_id_without_accepted_host_receipt() -> None:
    validation = ValidatorEngine().validate_tool_result(
        tool={
            "name": "custom.mutate",
            "mutating": True,
            "output_schema": {"type": "object"},
            "postconditions": [{"predicate": "host_validated_mutation"}],
        },
        arguments={"value": "unsafe"},
        result={"order_id": "ORD-only", "confirmed": True},
        before={"hash": "a" * 64},
        after={"hash": "a" * 64},
    )

    assert validation.passed is False
    assert validation.mutation_receipt is not None
    assert validation.mutation_receipt["accepted"] is False
    postcondition = next(
        item for item in validation.checks
        if item["name"] == "postcondition:host_validated_mutation"
    )
    assert postcondition["passed"] is False


def test_mutation_receipt_verification_rejects_missing_or_extra_fields() -> None:
    receipt = build_mutation_receipt(
        name="custom.mutate",
        arguments={"value": "safe"},
        result={"mutation_performed": True},
        before={"hash": "a" * 64},
        after={"hash": "b" * 64},
        accepted=True,
    )

    assert verify_mutation_receipt(receipt)
    assert not verify_mutation_receipt({key: value for key, value in receipt.items() if key != "tool"})
    assert not verify_mutation_receipt({**receipt, "unexpected": True})
