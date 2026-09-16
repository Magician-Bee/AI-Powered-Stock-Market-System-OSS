import pytest

from open_stock_ai.agent_runtime.provider_fallback import (
    ProviderFallbackPolicy,
    verify_provider_fallback_receipt,
)


def test_provider_failure_is_fail_closed_without_explicit_alternate() -> None:
    decision = ProviderFallbackPolicy(primary_driver="openai-compatible").decide(
        alternate_driver=None,
        task_kind="general_answer",
        autonomy="advisory",
    )

    assert decision.status == "blocked"
    assert decision.selected_driver is None
    assert decision.reason == "provider_failure_fail_closed"
    assert verify_provider_fallback_receipt(decision.to_dict())


def test_explicit_alternate_is_allowed_only_for_advisory_read_only_work() -> None:
    decision = ProviderFallbackPolicy(
        primary_driver="openai-compatible",
        mode="explicit_alternate",
        allowed_alternates=("codex",),
    ).decide(
        alternate_driver="codex",
        task_kind="market_information",
        autonomy="advisory",
    )

    assert decision.status == "allowed"
    assert decision.selected_driver == "codex"
    assert decision.quality_change == "provider_changed_unverified_quality"
    assert "品質可能不同" in decision.user_notice
    assert verify_provider_fallback_receipt(decision.to_dict())


@pytest.mark.parametrize(
    ("task_kind", "autonomy"),
    [("market_information", "paper_execute"), ("project_task", "advisory"), ("market_decision", "full_execute")],
)
def test_fallback_is_blocked_for_mutating_or_non_read_only_work(task_kind: str, autonomy: str) -> None:
    decision = ProviderFallbackPolicy(
        primary_driver="openai-compatible",
        mode="explicit_alternate",
        allowed_alternates=("codex",),
    ).decide(
        alternate_driver="codex",
        task_kind=task_kind,
        autonomy=autonomy,
    )

    assert decision.status == "blocked"
    assert decision.selected_driver is None
    assert decision.reason in {"fallback_forbidden_for_autonomy", "fallback_forbidden_for_task_kind"}


def test_tampered_fallback_receipt_is_rejected() -> None:
    decision = ProviderFallbackPolicy(
        primary_driver="openai-compatible",
        mode="explicit_alternate",
        allowed_alternates=("codex",),
    ).decide(
        alternate_driver="codex",
        task_kind="general_answer",
        autonomy="advisory",
    )
    receipt = decision.to_dict()
    receipt["selected_driver"] = "external-agent"

    assert not verify_provider_fallback_receipt(receipt)
