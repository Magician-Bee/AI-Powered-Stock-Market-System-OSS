from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "open_stock_ai" / "agent_runtime"


def test_interaction_persistence_is_owned_by_its_domain_store() -> None:
    coordinator = (RUNTIME / "final_runtime.py").read_text(encoding="utf-8")
    interaction_store = (RUNTIME / "interaction" / "store.py").read_text(
        encoding="utf-8"
    )

    assert "self.interaction_store = DurableInteractionStore" in coordinator
    assert "class DurableInteractionStore" in interaction_store
    for statement in (
        "insert into agent_decision_checkpoints",
        "update agent_decision_checkpoints",
        "insert into agent_user_proposals",
        "insert into agent_proposal_evaluations",
    ):
        assert statement not in coordinator
        assert statement in interaction_store

    # Keep the product coordinator from absorbing the extracted domain again.
    assert len(coordinator.splitlines()) < 1_400


def test_provider_transcript_projection_is_owned_by_provider_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    transcript = (RUNTIME / "providers" / "transcript.py").read_text(
        encoding="utf-8"
    )

    assert "def provider_transcript_v2(" in transcript
    assert "def provider_conversation_history(" in transcript
    assert "def compact_replay_result(" in transcript
    assert "def safe_arguments(" in transcript
    for definition in (
        "def _provider_transcript_v2(",
        "def _provider_conversation_history(",
        "def _compact_replay_result(",
        "def _safe_arguments(",
    ):
        assert definition not in orchestrator

    # Keep provider projection and redaction out of the execution coordinator.
    assert len(orchestrator.splitlines()) < 10_200


def test_recovery_policy_is_owned_by_repair_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    recovery_policy = (RUNTIME / "repair" / "recovery_policy.py").read_text(
        encoding="utf-8"
    )

    for definition in (
        "def _repair_provider_plan_patch(",
        "def _reusable_observation(",
        "def _prior_repeated_tool_failure(",
        "def _recovery_tool_surface(",
        "def _host_recovery_calls(",
        "def _recovery_links_for_call(",
        "def _failure_recovery_coverage(",
        "def _unresolved_failure_nodes(",
    ):
        assert definition in recovery_policy
        assert definition not in orchestrator

    # Recovery policy must remain independently testable instead of growing
    # back into the execution coordinator.
    assert len(orchestrator.splitlines()) < 9_400


def test_completion_policy_is_owned_by_completion_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    completion_policy = (RUNTIME / "completion_policy.py").read_text(
        encoding="utf-8"
    )

    for definition in (
        "def _host_completion_evaluation(",
        "def _objective_contract_check(",
        "def _should_host_finalize_from_validated_evidence(",
        "def _should_host_finalize_verified_paper_order(",
        "def _should_host_finalize_analysis_only_data_unavailable(",
        "def _verified_analysis_only_summary(",
        "def _evidence_requirement_met(",
        "def _completed_critic_observation(",
    ):
        assert definition in completion_policy
        assert definition not in orchestrator

    # Completion remains a separately testable Host policy rather than a
    # collection of special cases embedded in the run loop.
    assert len(orchestrator.splitlines()) < 8_760


def test_paper_protocol_is_owned_by_paper_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    paper_protocol = (RUNTIME / "paper_protocol.py").read_text(encoding="utf-8")

    for definition in (
        "def _should_host_submit_verified_paper_order(",
        "def _host_explicit_paper_protocol_call(",
        "def _host_explicit_market_coverage_calls(",
        "def _extend_explicit_paper_market_coverage_capabilities(",
        "def _explicit_paper_order_from_objective(",
        "def _routine_paper_wait_suppression_reason(",
        "def _safe_public_paper_turn_summary(",
        "def _pending_verified_paper_order(",
    ):
        assert definition in paper_protocol
        assert definition not in orchestrator

    # Paper execution remains a deterministic, independently testable Host
    # protocol instead of accumulating more special cases in the run loop.
    assert len(orchestrator.splitlines()) < 8_310


def test_evidence_projection_is_owned_by_evidence_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    evidence_projection = (RUNTIME / "evidence_projection.py").read_text(
        encoding="utf-8"
    )

    for definition in (
        "def _evidence_feedback(",
        "def _evidence_claim_text(",
        "def _canonical_tool_evidence(",
        "def _canonical_source_claim(",
        "def _canonical_freshness(",
        "def _workspace_evidence_details(",
        "def _reasoning_step_summary(",
    ):
        assert definition in evidence_projection
        assert definition not in orchestrator

    # Evidence normalization, grounding feedback and public evidence summaries
    # stay independently testable outside the execution coordinator.
    assert len(orchestrator.splitlines()) < 7_795


def test_model_metadata_projection_is_owned_by_provider_module() -> None:
    orchestrator = (RUNTIME / "orchestrator.py").read_text(encoding="utf-8")
    metadata = (RUNTIME / "providers" / "model_metadata.py").read_text(encoding="utf-8")

    for definition in (
        "def restore_provider_model_metadata(",
        "def provider_session_metadata(",
        "def model_invocation_receipts(",
    ):
        assert definition in metadata
        assert definition not in orchestrator
    assert "def _model_invocation_receipts(" not in orchestrator
