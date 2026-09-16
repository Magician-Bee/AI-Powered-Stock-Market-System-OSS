from __future__ import annotations

import json
from copy import deepcopy

import pytest

from open_stock_ai.agent_runtime.context_broker_v2 import ContextBrokerV2
from open_stock_ai.agent_runtime.providers.transcript import (
    estimate_context_tokens,
    provider_conversation_history,
    provider_transcript_v2,
    safe_arguments,
)


def test_safe_arguments_redacts_nested_credentials_without_mutating_input() -> None:
    original = {
        "symbol": "2330.TW",
        "connection": {
            "api_token": "secret-value",
            "authorization": "Bearer value",
        },
        "items": [{"password_hint": "hidden", "count": 3}],
    }

    projected = safe_arguments(original)

    assert projected == {
        "symbol": "2330.TW",
        "connection": {
            "api_token": "[redacted]",
            "authorization": "[redacted]",
        },
        "items": [{"password_hint": "[redacted]", "count": 3}],
    }
    assert original["connection"]["api_token"] == "secret-value"


def test_provider_conversation_history_is_bounded_and_marks_old_market_answers() -> None:
    history = [
        {
            "role": "assistant" if index % 2 else "user",
            "content": f"message-{index}",
            "api_key": f"credential-{index}",
        }
        for index in range(45)
    ]

    projected = provider_conversation_history(
        history,
        task_kind="market_information",
    )

    assert len(projected) == 40
    assert projected[0]["role"] == "assistant"
    assert projected[1]["content"] == "message-6"
    assert all(item["api_key"] == "[redacted]" for item in projected)
    assistant = next(item for item in projected if item["role"] == "assistant")
    assert assistant["source"]["evidence_scope"] == "historical_non_evidentiary"
    assert "Do not cite or copy" in assistant["content"]["summary"]


def test_provider_transcript_bounds_history_trace_and_host_budget_projection() -> None:
    package = ContextBrokerV2().assemble(
        current_objective="整理既有資料",
        current_user_message="整理既有資料",
        active_branch={"branch_id": "branch-1", "objective": "整理既有資料"},
    )
    trace = [
        {
            "call_id": f"CALL-{index}",
            "tool": "market.snapshot",
            "ok": True,
            "result": {"schema_version": "stock_ai.snapshot.v1", "symbol": "2330.TW"},
            "validation": {"passed": True},
        }
        for index in range(25)
    ]
    history = [
        {"role": "user", "content": f"message-{index}", "token": "hidden"}
        for index in range(12)
    ]

    projected = provider_transcript_v2(
        package=package,
        trace=trace,
        transcript=[{"role": "host", "type": "conversation_history", "content": history}],
    )

    provider_history = next(
        item for item in projected if item["type"] == "conversation_history"
    )["content"]
    tool_results = next(item for item in projected if item["type"] == "tool_results")[
        "content"
    ]
    branch_result = next(
        item for item in projected if item["type"] == "branch_result.compressed"
    )["content"]
    assert len(provider_history) == 8
    assert provider_history[0]["content"] == "message-4"
    assert all(item["token"] == "[redacted]" for item in provider_history)
    assert len(tool_results) == 12
    assert tool_results[0]["call_id"] == "CALL-13"
    assert len(branch_result["evidence_ids"]) == 20
    assert estimate_context_tokens(projected) > 0


def test_tool_evidence_reaches_codex_prompt_with_values_sources_and_trust_boundary() -> None:
    from open_stock_ai.agent_runtime.contracts import AgentTurnInput
    from open_stock_ai.agent_runtime.untrusted_content import content_sha256
    from stock_ai.agent_drivers import _turn_prompt

    package = ContextBrokerV2().assemble(
        current_objective="分析2330技術面", current_user_message="分析2330技術面",
        active_branch={"branch_id": "stock-analysis"},
    )
    trace = [{
        "call_id": "research-2330", "tool": "market.research_pack", "ok": True,
        "validation": {"passed": True, "evidence_hash": "verified-market-result"},
        "result": {
            "schema_version": "open_stock_ai.agent_research_pack.v1", "symbol": "2330.TW",
            "generated_at": "2026-09-10T17:56:56Z",
            "market_price": {
                "price": 2450.0, "price_source": "TWSE MIS", "source_timestamp": "2026-09-11T01:56:45+08:00",
                "trading_state": "closed", "source_envelope": {
                    "exchange_timestamp": "20260910 13:30:00", "received_at": "2026-09-11T01:56:45+08:00",
                },
            },
            "technical_features": {"available": True, "history_points": 145, "rsi_14": 64.0625, "sma_20": 2409.5},
            "recent_history": [{"date": "2026-09-09", "close": 2465.0, "volume": 17798845}],
            "pipeline_workspace": {"risk_summary": {"approved": False}, "execution_permission": "blocked"},
        },
    }, {
        "call_id": "web-research", "tool": "web.research", "ok": True,
        "result": {
            "schema_version": "open_stock_ai.web_research.v1", "source_count": 3,
            "search_result_count": 100,
            "search_results": "This metadata must not crowd out the source bodies. " * 1000,
            "sources": [
                {"title": f"source-{i}", "url": f"https://example.test/{i}", "content": f"Evidence body {i}. " + "paragraph " * 2000}
                for i in range(3)
            ],
        },
    }, {
        "call_id": "web-fetch", "tool": "web.fetch", "ok": True,
        "result": {"schema_version": "open_stock_ai.web_fetch.v1", "final_url": "https://example.test/daily", "content": "Official daily OHLCV response."},
    }, {
        "call_id": "broker-status", "tool": "broker.list_connections", "ok": True,
        "result": {"schema_version": "broker.connections.v1", "brokers": [{"id": "test", "status": "not_configured", "api_token": "never-send-this"}]},
    }]
    original = deepcopy(trace)
    projected = provider_transcript_v2(package=package, trace=trace, transcript=[])
    tool_results = next(item["content"] for item in projected if item["type"] == "tool_results")
    research = tool_results[0]["result"]
    assert research["content"]["technical_features"]["rsi_14"] == 64.0625
    assert research["content"]["history_window"]["last_date"] == "2026-09-09"
    assert research["content"]["recent_history"][-1]["volume"] == 17798845
    assert research["content"]["market_price"]["trading_state"] == "closed"
    assert research["content"]["market_price"]["source_envelope"]["exchange_timestamp"] == "20260910 13:30:00"
    assert research["trust_level"] == "untrusted_data"
    assert research["instruction_authority"] == "none"
    assert research["content_hash"] == content_sha256(research["content"])
    sources = tool_results[1]["result"]["content"]["sources"]
    assert all(source["content"].startswith(f"Evidence body {index}.") for index, source in enumerate(sources))
    assert len(sources) == 3
    assert tool_results[2]["result"]["content"]["content"] == "Official daily OHLCV response."
    turn = AgentTurnInput(
        run_id="stock-analysis", objective="分析2330技術面", system_prompt="Use verified observations.",
        transcript=tuple(projected), tools=(), output_schema={"type": "object"}, metadata={},
    )
    prompt = _turn_prompt(turn)
    for evidence in ("64.0625", "2409.5", "2450.0", "2026-09-09", "https://example.test/2", "Evidence body 2", "Official daily OHLCV"):
        assert evidence in prompt
    assert "never-send-this" not in prompt
    assert "[redacted]" in prompt
    assert len(prompt) < 48_000
    assert trace == original


def test_generic_result_is_bounded_and_not_replaced_with_null() -> None:
    from open_stock_ai.agent_runtime.providers.transcript import compact_replay_result

    observation = {"ok": True, "result": {
        "schema_version": "new_tool_schema.v1",
        "items": [{"content": "value " * 20_000} for _ in range(50)],
    }}
    projected = compact_replay_result(observation, max_chars=2_000)
    assert projected["schema_version"] == "new_tool_schema.v1"
    assert projected["items"][0]["content"].startswith("value ")
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) <= 2_000
    assert "_omitted_items" in json.dumps(projected)


def test_second_prompt_compaction_preserves_tool_identity_and_rehashes_data_envelope() -> None:
    from open_stock_ai.agent_runtime.untrusted_content import content_sha256, label_tool_observation
    from stock_ai.agent_drivers import _model_transcript

    result = label_tool_observation({
        "schema_version": "open_stock_ai.agent_research_pack.v1", "symbol": "2330.TW",
        "technical_features": {"rsi_14": 64.0625}, "history_window": {"last_date": "2026-09-09"},
        "recent_events": [{"title": "Source headline", "summary": "evidence " * 5000}],
    }, tool="market.research_pack", call_id="research-call")
    compacted = _model_transcript(({
        "type": "tool_results", "role": "host", "content": [{
            "call_id": "research-call", "tool": "market.research_pack", "ok": True,
            "result": result, "validation": {"passed": True},
        }],
    },))
    observation = compacted[0]["content"]["observations"][0]
    assert observation["call_id"] == "research-call"
    assert observation["tool"] == "market.research_pack"
    envelope = observation["result"]
    assert envelope["content"]["technical_features"]["rsi_14"] == 64.0625
    assert envelope["content"]["history_window"]["last_date"] == "2026-09-09"
    assert envelope["instruction_authority"] == "none"
    assert envelope["content_hash"] == content_sha256(envelope["content"])
    assert result["content_hash"] == content_sha256(result["content"])


@pytest.mark.parametrize("compaction", ["summarized", "omitted"])
@pytest.mark.parametrize("protocol", ["advanced_v1", "universal_v1"])
def test_current_completion_criteria_reach_model_after_context_package_compaction(compaction, protocol) -> None:
    from open_stock_ai.agent_runtime.contracts import AgentTurnInput
    from stock_ai.agent_drivers import _model_transcript, _turn_prompt

    criteria = [
        "已以 Host 驗證證據說明 2330.TW 的趨勢、動能、波動或成交量訊號",
        "已明確列出資料日期、缺漏、來源或模型推論限制",
        "未將候選訊號表述為交易許可，且未虛構價格、持倉或成交",
    ]
    plan = {"plan_id": "AP-resumed", "revision_number": 3, "completion_criteria": criteria, "nodes": []}
    original = deepcopy(plan)
    package = ContextBrokerV2().assemble(
        current_objective="分析台積電技術面與資料限制。",
        current_user_message="分析台積電技術面與資料限制。",
        active_branch={"branch_id": "AR-resumed", "completion_criteria": criteria},
        plan=plan,
        output_contract={"completion_criteria": criteria},
        # This is a supported context size that triggers the driver's second
        # compaction pass, as the native resumed analysis did.
        tool_schemas=[{"name": f"market.tool_{index}", "description": "schema " * 350} for index in range(12)],
        turn=4,
    )
    transcript = provider_transcript_v2(package=package, trace=[], transcript=[])
    assert 24_000 < len(json.dumps(package.to_provider_payload(), ensure_ascii=False, separators=(",", ":"))) < 48_000
    if compaction == "omitted":
        transcript.append({"role": "host", "type": "policy_feedback", "content": {"summary": "validated feedback " * 1200}})
    compacted = _model_transcript(tuple(transcript), universal=protocol == "universal_v1")
    if compaction == "summarized":
        assert next(item for item in compacted if item["type"] == "context_package.v2")["content"]["semantic_compaction"] is True
    else:
        assert not any(item["type"] == "context_package.v2" for item in compacted)
    # Ordinary history compaction is allowed to drop this data, but the
    # authoritative completion contract must still reach the actual prompt.
    assert all(criterion not in json.dumps(compacted, ensure_ascii=False) for criterion in criteria)
    prompt = _turn_prompt(AgentTurnInput(
        run_id="AR-resumed", objective="分析台積電技術面與資料限制。", system_prompt="Use verified evidence.",
        transcript=tuple(transcript), tools=(), output_schema={"type": "object"},
        metadata={"provider_protocol": protocol, "plan_graph": plan},
    ))
    encoded_contract = prompt.split("HOST_COMPLETION_CONTRACT=", 1)[1].split("\n", 1)[0]
    contract = json.loads(encoded_contract)
    assert contract["plan_id"] == "AP-resumed"
    assert contract["plan_revision"] == 3
    assert contract["completion_criteria"] == criteria
    assert prompt.index("HOST_COMPLETION_CONTRACT=") < prompt.index("RUNTIME_INSTRUCTIONS_END")
    assert "criterion_results" not in contract
    assert "met" not in contract
    assert ("completion_evaluation.criterion_results" in contract["instruction"]) is (protocol == "advanced_v1")
    assert plan == original


def test_completion_contract_uses_current_host_plan_instead_of_stale_checkpoint_context() -> None:
    from open_stock_ai.agent_runtime.contracts import AgentTurnInput
    from stock_ai.agent_drivers import _turn_prompt

    prompt = _turn_prompt(AgentTurnInput(
        run_id="AR-resume", objective="Analyse the stock.", system_prompt="Use verified evidence.",
        tools=(), output_schema={"type": "object"},
        transcript=({"type": "context_package.v2", "role": "host", "content": {
            "plan": {"revision_number": 1, "completion_criteria": ["Old checkpoint criterion"]},
        }},),
        metadata={"plan_graph": {"plan_id": "AP-current", "revision_number": 4, "completion_criteria": ["Current verified criterion"]}},
    ))
    contract = json.loads(prompt.split("HOST_COMPLETION_CONTRACT=", 1)[1].split("\n", 1)[0])
    assert contract["plan_revision"] == 4
    assert contract["completion_criteria"] == ["Current verified criterion"]


@pytest.mark.parametrize("passed", [True, False])
def test_validation_details_remain_once_with_compact_envelope_provenance(passed) -> None:
    from open_stock_ai.agent_runtime.untrusted_content import content_sha256

    validation = {
        "schema_version": "open_stock_ai.validation_result.v1",
        "validator": "host_tool_validator",
        "passed": passed,
        "evidence_hash": "verified-original-receipt-hash",
        "checks": [{
            "name": "source_reconciliation", "passed": passed,
            "details": "Keep the complete reconciliation reason and its evidence. " * 30,
            "missing_fields": [] if passed else ["exchange_timestamp"],
        }],
        "mutation_receipt": {"receipt_id": "MUT-preserved", "idempotency_key": "order-once"},
        "domain_mutation_receipt": {"campaign_receipt_id": "AE-preserved", "account_id": "isolated"},
    }
    trace = [{
        "call_id": "verified-read", "tool": "market.research_pack", "ok": True,
        "result": {"schema_version": "open_stock_ai.agent_research_pack.v1", "symbol": "2330.TW"},
        "validation": validation,
    }]
    original = deepcopy(trace)
    package = ContextBrokerV2().assemble(
        current_objective="Review retained evidence", current_user_message="Review retained evidence",
        active_branch={"branch_id": "retained"},
    )
    projected = provider_transcript_v2(package=package, trace=trace, transcript=[])
    observation = next(item["content"][0] for item in projected if item["type"] == "tool_results")
    if passed:
        assert observation["validation"]["checks"] == [{"name": "source_reconciliation", "passed": True}]
    else:
        assert observation["validation"] == validation
        assert observation["validation"]["checks"][0]["missing_fields"] == ["exchange_timestamp"]
    assert observation["validation"]["mutation_receipt"]["idempotency_key"] == "order-once"
    assert observation["validation"]["domain_mutation_receipt"]["campaign_receipt_id"] == "AE-preserved"
    for key in ("result", "result_summary"):
        envelope = observation[key]
        assert envelope["provenance"] == {
            field: validation[field] for field in ("schema_version", "passed", "validator", "evidence_hash")
        }
        assert envelope["instruction_authority"] == "none"
        assert envelope["content_hash"] == content_sha256(envelope["content"])
    # Reconstruct the previous three-copy projection, keeping everything else
    # equal. Only duplicated validation details disappear from the model input.
    previous = deepcopy(projected)
    old_observation = next(item["content"][0] for item in previous if item["type"] == "tool_results")
    for key in ("result", "result_summary"):
        old_observation[key]["provenance"] = deepcopy(validation)
    assert estimate_context_tokens(previous) - estimate_context_tokens(projected) > 800
    assert json.dumps(projected).count("source_reconciliation") == 1
    assert trace == original


def test_policy_feedback_uses_actual_disclosure_and_preserves_failed_repair_details() -> None:
    disclosed = {"autonomy.status", "market.analyze_symbol"}
    package = ContextBrokerV2().assemble(
        current_objective="Review retained evidence", current_user_message="Review retained evidence",
        active_branch={"branch_id": "retained"},
        tool_schemas=[{"name": name, "input_schema": {"type": "object"}} for name in sorted(disclosed)],
    )
    failed_check = {"name": "failed_plan_nodes_are_nonblocking", "passed": False,
                    "unresolved_failed_nodes": ["obsolete-scope-repair"],
                    "details": "Keep this complete explanation for a legal repair. " * 40}
    feedback = {
        "error": "Repair the unresolved node using retained evidence.",
        "failed_tools": ["autonomy.activate"],
        "verified_evidence_summary": "Retained evidence without truncation. " * 100,
        "grounded_rewrite_rule": "Never rewrite the original failed tool receipt.",
        "available_tools": [f"legacy.long_capability_name_{index}" for index in range(435)] + sorted(disclosed),
        "completion_validation": {"passed": False, "evidence_hash": "completion-hash",
            "mutation_receipt": {"receipt_id": "retain-receipt"},
            "checks": [{"name": "semantic_validation_layer", "passed": True, "metadata": "supported claim " * 1000}, failed_check]},
    }
    interaction = {"instruction": "The original user mandate delegates these choices to the Agent."}
    transcript = [{"role": "host", "type": "policy_feedback", "content": feedback},
                  {"role": "host", "type": "interaction_response", "content": interaction}]
    original = deepcopy(transcript)
    projected = provider_transcript_v2(package=package, trace=[], transcript=transcript)
    actual = next(item["content"] for item in projected if item["type"] == "policy_feedback")
    assert set(actual["available_tools"]) == disclosed == {item["name"] for item in package.tool_schemas}
    assert actual["available_tools_scope"] == "current_host_disclosed_tool_schemas"
    assert actual["full_registry_tool_count"] == 437
    for key in ("error", "failed_tools", "verified_evidence_summary", "grounded_rewrite_rule"):
        assert actual[key] == feedback[key]
    validation = actual["completion_validation"]
    assert validation["checks"] == [{"name": "semantic_validation_layer", "passed": True}, failed_check]
    assert validation["evidence_hash"] == "completion-hash"
    assert validation["mutation_receipt"] == {"receipt_id": "retain-receipt"}
    assert next(item["content"] for item in projected if item["type"] == "interaction_response") == interaction
    assert estimate_context_tokens(feedback) - estimate_context_tokens(actual) > 4_000
    assert transcript == original


def test_failed_tool_details_and_autonomy_mutation_receipt_survive_projection() -> None:
    error = {"category": "execution_failure", "retryable": False,
             "message": "Submission outcome unknown; reconcile the original idempotency key."}
    receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper",
               "action": "autonomy.activate", "campaign_receipt_id": "AE-preserved",
               "account_id": "autonomous-paper-v1", "cycle_id": "AC-retained",
               "management": {"enabled": True, "results": [], "errors": []}, "plans": []}
    trace = [
        {"call_id": "failed-fetch", "tool": "web.fetch", "ok": False, "result": None,
         "error": error, "validation": {"passed": False, "evidence_hash": "failure-hash",
                                          "checks": [{"name": "failed_tool", "passed": False, "error": error}]}},
        {"call_id": "activate", "tool": "autonomy.activate", "ok": True, "result": receipt,
         "validation": {"passed": True, "evidence_hash": "mutation-hash", "domain_mutation_receipt": receipt}},
    ]
    original = deepcopy(trace)
    package = ContextBrokerV2().assemble(
        current_objective="Reconcile retained receipts", current_user_message="Reconcile retained receipts",
        active_branch={"branch_id": "retained"},
    )
    projected = provider_transcript_v2(package=package, trace=trace, transcript=[])
    failed, activated = next(item["content"] for item in projected if item["type"] == "tool_results")
    assert failed["result_summary"]["content"] == error
    assert failed["validation"] == original[0]["validation"]
    assert activated["result"]["campaign_receipt_id"] == "AE-preserved"
    assert activated["validation"]["domain_mutation_receipt"] == receipt
    assert activated["validation"]["evidence_hash"] == "mutation-hash"
    assert trace == original
