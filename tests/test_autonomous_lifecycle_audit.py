from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from open_stock_ai.execution.trading_plan import content_hash


def _module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT = _module("audit_autonomous_lifecycle")


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "paper.sqlite"
    _module("verify_autonomous_closed_loop").run_verification(path)
    return path


def _plan(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("select * from autonomous_trading_plans").fetchone())


def _agent_receipts(path, runtime_path, *, include_tool_receipt=True, precise_context=True, context_mutation=None):
    """Synthetic Host receipts test correlation mechanics, never real-model proof."""
    row = _plan(path)
    definition = json.loads(row["definition_json"])
    selected_model = {"provider": "codex", "model": "test-model", "reasoning_effort": "high",
                      "thread_id": "TH-audit", "resolution_source": "sdk_thread_start"}
    context = {"run_id": "AR-audit", "session_id": "AS-audit", "driver_id": "codex",
               "provider_model_metadata": selected_model}
    events = []
    for sequence, event_type in enumerate(("model.turn.started", "model.session.configured", "model.sdk.turn.started",
                                          "model.sdk.turn.completed", "model.turn.completed", "tool.started"), 1):
        event = {"type": event_type, "driver": "codex", "model": "test-model", "reasoning_effort": "high",
                 "step": 2, "event_id": "EV-" + str(sequence), "run_id": "AR-audit", "session_id": "AS-audit",
                 "timestamp": "2026-09-12T00:00:0" + str(sequence) + "Z"}
        if event_type.startswith("model.turn."):
            event["model_call_id"] = "AR-audit:turn:2"
        elif event_type == "model.session.configured":
            event.update(selected_model)
        elif event_type.startswith("model.sdk."):
            event.update(source="codex_app_server", turn={"id": "TURN-audit", "status": "completed" if event_type.endswith("completed") else "inProgress"})
        else:
            event.update(call_id="TC-audit", node_id="NODE-audit")
        events.append(event)
    with sqlite3.connect(runtime_path) as conn:
        conn.executescript("""
            create table agent_runs(run_id text, session_id text, driver text, request_json text);
            create table agent_events(run_id text, payload_json text, sequence integer, event_type text, created_at text);
            create table agent_tool_calls(run_id text, call_id text, step integer, tool_name text, status text,
                                          result_json text, validation_json text, node_id text, started_at text);
        """)
        conn.execute("insert into agent_runs values ('AR-audit','AS-audit','codex','{}')")
        for sequence, event in enumerate(events, 1):
            conn.execute("insert into agent_events values (?,?,?,?,?)", ("AR-audit", json.dumps(event), sequence, event["type"], event["timestamp"]))
        conn.execute("insert into agent_tool_calls values (?,?,?,?,?,?,?,?,?)", ("AR-audit", "TC-audit", 2,
            "autonomy.propose_plan", "running", "{}", "{}", "NODE-audit", events[-1]["timestamp"]))
    if precise_context:
        from types import SimpleNamespace
        from stock_ai.tool_providers.autonomy_model_context import retain_model_invocation_context

        def retain(kind, payload):
            if context_mutation:
                context_mutation(payload)
            evidence_id = "AE-" + content_hash({"account_id": row["account_id"], "kind": kind, "payload": payload})
            with sqlite3.connect(path) as conn:
                conn.execute("insert into autonomous_evidence values (?,?,?,?)", (evidence_id, row["account_id"], kind, json.dumps(payload)))
            return evidence_id

        kept = retain_model_invocation_context(SimpleNamespace(broker=SimpleNamespace(account_id=row["account_id"]), _retain=retain),
            name="autonomy.propose_plan", context=SimpleNamespace(run_id="AR-audit", session_id="AS-audit", driver_id="codex",
                state={"current_plan_node_id": "NODE-audit", "provider_model_metadata": selected_model}),
            run_store_loader=lambda: SimpleNamespace(path=runtime_path, _connect=lambda: sqlite3.connect(runtime_path)))
        assert kept["status"] == "host_sdk_turn_correlated", kept
        context.update(context_receipt_id=kept["context_receipt_id"], tool_call_id=kept["tool_call_id"])
    definition["strategy_id"] = AUDIT.AGENT_STRATEGY
    definition["strategy_version"] = "audit-test-version"
    definition["metadata"].update(agent_context=context, agent_context_sha256=content_hash(context),
                                    builder_source_manifest={"execution/agent_plan_proposal.py": "0" * 64})
    digest = content_hash(definition)
    with sqlite3.connect(path) as conn:
        conn.execute("update autonomous_trading_plans set definition_json=?,definition_hash=?", (json.dumps(definition), digest))
        old = json.loads(conn.execute("select payload_json from autonomous_trade_outcomes").fetchone()[0])
        old.update(strategy_id=definition["strategy_id"], strategy_version=definition["strategy_version"], plan_definition_hash=digest)
        old.pop("receipt_sha256")
        old["receipt_sha256"] = content_hash(old)
        conn.execute("update autonomous_trade_outcomes set strategy_id=?,payload_json=?,receipt_sha256=?",
                     (definition["strategy_id"], json.dumps(old), old["receipt_sha256"]))
        receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": "autonomy.propose_plan",
                   "account_id": row["account_id"], "cycle_id": definition["metadata"]["cycle_id"],
                   "plan": {"plan_id": row["plan_id"], "account_id": row["account_id"], "definition_hash": digest}}
        payload = {"arguments": {}, "result": receipt}
        evidence_id = "AE-" + content_hash({"account_id": row["account_id"], "kind": "tool_execution", "payload": payload})
        if include_tool_receipt:
            conn.execute("insert into autonomous_evidence values (?,?,?,?)", (evidence_id, row["account_id"], "tool_execution", json.dumps(payload)))
        receipt["campaign_receipt_id"] = evidence_id
    with sqlite3.connect(runtime_path) as conn:
        event = {"type": "tool.completed", "run_id": "AR-audit", "session_id": "AS-audit", "call_id": "TC-audit",
                 "result_summary": "Saved plan"}
        conn.execute("update agent_tool_calls set status='completed', result_json=?, validation_json=?", (
            json.dumps(event), json.dumps({"passed": True, "evidence_hash": content_hash(receipt)})))


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(FileNotFoundError):
        AUDIT.audit(path)
    assert not path.exists()


def test_fixture_round_trip_is_separate_from_real_model_and_m1(ledger):
    report = AUDIT.audit(ledger)
    plan = report["plans"][0]
    assert plan["decision_origin"] == "fixed_candidate"
    assert plan["research_source"]["status"] == "fixture"
    assert plan["research_source"]["cycle_hash_valid"] is True
    assert plan["model_invocation"]["status"] == "not_applicable"
    assert plan["dispatch_intent_count"] == 2
    assert plan["fill_count"] == 2
    assert plan["closed_round_trip_receipts_verified"] is True
    assert plan["m1_verified"] is report["m1_verified"] is False
    assert report["positive_ev_qualified"] is False
    assert content_hash({key: value for key, value in report.items() if key != "receipt_sha256"}) == report["receipt_sha256"]


def test_read_only_snapshot_cannot_mutate_or_migrate_database(ledger):
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    reader = AUDIT.ReadOnlyLedger(ledger)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.conn.execute("delete from autonomous_trading_plans")
    finally:
        reader.close()
    AUDIT.audit(ledger)
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before


def test_filled_order_status_cannot_substitute_for_missing_fills(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.execute("delete from paper_fills")
    plan = AUDIT.audit(ledger)["plans"][0]
    assert plan["fill_count"] == 0
    assert any(order["broker_status"] == "filled" for order in plan["orders"])
    assert plan["closed_round_trip_receipts_verified"] is False
    assert "closed_plan_not_reconciled_to_fills" in plan["lifecycle_problems"]
    assert "outcome_fills_differ_from_ledger" in plan["lifecycle_problems"]


def test_other_account_fill_and_invalid_cashflow_are_reported(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.execute("update paper_fills set account_id='unrelated',net_cash_delta=0 where side='buy'")
    plan = AUDIT.audit(ledger)["plans"][0]
    assert plan["closed_round_trip_receipts_verified"] is False
    assert any(item.startswith("order_or_fill_scope_mismatch") for item in plan["lifecycle_problems"])
    assert any(item.startswith("fill_cashflow_mismatch") for item in plan["lifecycle_problems"])


def test_old_strategy_outcome_cannot_back_current_plan_even_with_valid_hash(ledger):
    with sqlite3.connect(ledger) as conn:
        outcome = json.loads(conn.execute("select payload_json from autonomous_trade_outcomes").fetchone()[0])
        outcome["strategy_version"] = "different-version"
        outcome.pop("receipt_sha256")
        outcome["receipt_sha256"] = content_hash(outcome)
        conn.execute("update autonomous_trade_outcomes set payload_json=?,receipt_sha256=?",
                     (json.dumps(outcome), outcome["receipt_sha256"]))
    plan = AUDIT.audit(ledger)["plans"][0]
    assert plan["outcome_hash_valid"] is True
    assert plan["closed_round_trip_receipts_verified"] is False
    assert "outcome_binding_mismatch:strategy_version" in plan["lifecycle_problems"]


def test_model_metadata_and_matching_turns_without_retained_proposal_are_insufficient(ledger, tmp_path):
    runtime = tmp_path / "runtime.sqlite"
    _agent_receipts(ledger, runtime, include_tool_receipt=False)
    plan = AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]
    assert plan["decision_origin"] == "agent_proposal"
    assert plan["model_invocation"]["context_hash_valid"] is True
    assert plan["model_invocation"]["status"] == "missing_runtime_correlation"
    assert "successful_proposal_receipt_binding_missing" in plan["model_invocation"]["problems"]


def test_host_turn_and_validated_retained_receipt_correlate_without_claiming_m1(ledger, tmp_path):
    runtime = tmp_path / "runtime.sqlite"
    _agent_receipts(ledger, runtime)
    plan = AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]
    assert plan["model_invocation"]["status"] == "host_sdk_turn_correlated", plan["model_invocation"]
    assert plan["model_invocation"]["successful_proposal_call_ids"] == ["TC-audit"]
    assert plan["closed_round_trip_receipts_verified"] is True
    assert plan["current_source_comparison"]["status"] == "changed"
    assert plan["research_source"]["status"] == "fixture"
    assert plan["m1_verified"] is False
    with sqlite3.connect(runtime) as conn:
        conn.execute("update agent_tool_calls set validation_json='{}'")
    assert AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]["model_invocation"]["status"] == "missing_runtime_correlation"


def test_empty_or_legacy_database_returns_unknown_not_success(tmp_path):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    report = AUDIT.audit(path)
    assert report["plan_count"] == 0
    assert report["missing_tables_or_columns"] == ["autonomous_trading_plans"]
    assert report["m1_verified"] is False


def test_account_filter_and_corrupt_definition_are_visible(ledger):
    assert AUDIT.audit(ledger, account_id="another-account")["plan_count"] == 0
    with sqlite3.connect(ledger) as conn:
        conn.execute("update autonomous_trading_plans set definition_hash='bad'")
    plan = AUDIT.audit(ledger)["plans"][0]
    assert plan["definition_hash_valid"] is False
    assert plan["closed_round_trip_receipts_verified"] is False
    assert "plan_definition_hash_mismatch" in plan["lifecycle_problems"]


def test_legacy_turn_rows_do_not_replace_precise_context_receipt(ledger, tmp_path):
    runtime = tmp_path / "runtime.sqlite"
    _agent_receipts(ledger, runtime, precise_context=False)
    model = AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]["model_invocation"]
    assert model["successful_proposal_call_ids"] == ["TC-audit"]
    assert model["status"] == "missing_runtime_correlation"
    assert model["retained_context"]["status"] == "not_verified"


@pytest.mark.parametrize("mutation,problem", [
    (lambda p: p.update(runtime_database_path_sha256="0" * 64), "runtime_location_mismatch"),
    (lambda p: p.update(account_id="unrelated"), "context_identity_mismatch"),
    (lambda p: p.update(sdk_turn_events=[]), "sdk_turn_events_missing"),
    (lambda p: p["sdk_turn_events"][1]["turn"].update(id="TURN-another"), "sdk_turn_events_turn_mismatch"),
    (lambda p: p["model_session_events"][0].update(thread_id="TH-another"), "model_session_events_runtime_event_mismatch"),
    (lambda p: p["model_turn_events"][1].update(event_id="EV-another"), "model_turn_events_runtime_event_mismatch"),
    (lambda p: p.update(host_resumed_dispatch=True), "host_resume_not_excluded"),
])
def test_rehashed_context_cannot_substitute_another_identity_or_sdk_turn(ledger, tmp_path, mutation, problem):
    runtime = tmp_path / "runtime.sqlite"
    _agent_receipts(ledger, runtime, context_mutation=mutation)
    model = AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]["model_invocation"]
    assert model["status"] == "missing_runtime_correlation"
    assert model["retained_context"]["receipt_hash_valid"] is True
    assert any(problem in item for item in model["retained_context"]["problems"])


def test_context_content_hash_is_checked_before_runtime_correlation(ledger, tmp_path):
    runtime = tmp_path / "runtime.sqlite"
    _agent_receipts(ledger, runtime)
    with sqlite3.connect(ledger) as conn:
        conn.execute("update autonomous_evidence set payload_json='{}' where kind='model_invocation_context'")
    context = AUDIT.audit(ledger, runtime_database=runtime)["plans"][0]["model_invocation"]["retained_context"]
    assert context["receipt_hash_valid"] is False
    assert context["problems"] == ["model_invocation_context:retained_receipt_hash_mismatch"]


def _edit_buy_cash(path, mutation):
    with sqlite3.connect(path) as conn:
        rowid, encoded = conn.execute("select rowid,metadata_json from cash_ledger where entry_type='paper_buy'").fetchone()
        metadata = json.loads(encoded)
        mutation(metadata)
        conn.execute("update cash_ledger set metadata_json=? where rowid=?", (json.dumps(metadata), rowid))


def _rehash_fill(metadata):
    body = metadata["fill_evidence"]
    body.pop("receipt_sha256", None)
    body["receipt_sha256"] = content_hash(body)


def test_each_fill_receipt_is_verified_without_certifying_fixture_quotes(ledger):
    plan = AUDIT.audit(ledger)["plans"][0]
    evidence = plan["fill_provenance"]
    assert evidence["integrity_counts"] == {"verified": 2}
    assert evidence["all_fill_receipts_integrity_verified"] is True
    assert evidence["outcome_projection_status"] == "missing"  # v1 financial receipts preserve raw fills.
    assert all(fill["source_status"] == "unknown" for fill in evidence["fills"])
    assert plan["fixture_detected"] is True
    assert plan["m1_verified"] is False


@pytest.mark.parametrize("mutation,status,blocker", [
    (lambda p: p.pop("fill_evidence"), "missing", "paper_fill_evidence_missing"),
    (lambda p: p["fill_evidence"].update(receipt_sha256="0" * 64), "invalid", "paper_fill_evidence_hash_mismatch"),
    (lambda p: (p["fill_evidence"]["fill"].update(account_id="another"), _rehash_fill(p)), "invalid", "paper_fill_evidence_identity_or_facts_mismatch"),
    (lambda p: (p["fill_evidence"]["fill"].update(fill_id="another-fill"), _rehash_fill(p)), "invalid", "paper_fill_evidence_identity_or_facts_mismatch"),
])
def test_per_fill_invalid_or_missing_never_borrows_order_or_other_fill(ledger, mutation, status, blocker):
    _edit_buy_cash(ledger, mutation)
    plan = AUDIT.audit(ledger)["plans"][0]
    evidence = plan["fill_provenance"]
    assert evidence["integrity_counts"] == {status: 1, "verified": 1}
    affected = next(fill for fill in evidence["fills"] if fill["integrity_status"] == status)
    assert blocker in affected["blockers"]
    assert affected["market_observation_recorded"] is False
    assert affected["receipt_sha256"] is None
    assert evidence["outcome_projection_status"] == "missing"
    assert plan["closed_round_trip_receipts_verified"] is True  # Pure accounting is a separate result.


def test_legacy_outcome_and_missing_fill_metadata_remain_explicit(ledger):
    with sqlite3.connect(ledger) as conn:
        for rowid, encoded in conn.execute("select rowid,metadata_json from cash_ledger where entry_type in ('paper_buy','paper_sell')").fetchall():
            metadata = json.loads(encoded)
            metadata.pop("fill_evidence", None)
            conn.execute("update cash_ledger set metadata_json=? where rowid=?", (json.dumps(metadata), rowid))
        outcome = json.loads(conn.execute("select payload_json from autonomous_trade_outcomes").fetchone()[0])
        for fill in outcome["fills"]:
            fill.pop("fill_evidence", None)
            fill.pop("fill_evidence_verification", None)
        outcome.pop("receipt_sha256")
        outcome["receipt_sha256"] = content_hash(outcome)
        conn.execute("update autonomous_trade_outcomes set payload_json=?,receipt_sha256=?", (json.dumps(outcome), outcome["receipt_sha256"]))
    plan = AUDIT.audit(ledger)["plans"][0]
    assert plan["closed_round_trip_receipts_verified"] is True
    assert plan["fill_provenance"]["integrity_counts"] == {"missing": 2}
    assert plan["fill_provenance"]["outcome_projection_status"] == "missing"


def test_fill_context_is_its_own_committed_observation(ledger):
    fresh = {"source_id": "fixture-observation-after-submit", "nested": {"quote": {"volume": 321}}}
    _edit_buy_cash(ledger, lambda p: (p["fill_evidence"].update(market_context=fresh), _rehash_fill(p)))
    plan = AUDIT.audit(ledger)["plans"][0]
    assert any(fill["market_context_sha256"] == content_hash(fresh) for fill in plan["fill_provenance"]["fills"])
    assert plan["fill_provenance"]["execution_quote_source_status"] == "unknown"


@pytest.mark.parametrize("cash_change", ["amount=amount+1", "created_at='wrong'", "entry_type='paper_sell'"])
def test_fill_evidence_requires_cash_row_facts(ledger, cash_change):
    with sqlite3.connect(ledger) as conn:
        conn.execute("update cash_ledger set " + cash_change + " where entry_type='paper_buy'")
    evidence = AUDIT.audit(ledger)["plans"][0]["fill_provenance"]
    assert evidence["integrity_counts"] == {"invalid": 1, "verified": 1}
    assert any("paper_fill_cash_entry_mismatch" in fill["blockers"] for fill in evidence["fills"])


def _deployment_receipts(path, source_root, monkeypatch, *, missing_config=False):
    from types import SimpleNamespace
    from stock_ai import autonomous_deployment as deployment

    for relative in ("src/open_stock_ai/example.py", "src/stock_ai/example.py", "config/test.yaml"):
        if missing_config and relative.startswith("config/"):
            continue
        source = source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# fixture source observation\n", encoding="utf-8")
    snapshot = deployment._source_snapshot(source_root, capture_stage="service_startup")
    monkeypatch.setattr(deployment, "initialize_source_snapshot", lambda: snapshot)
    account_id = _plan(path)["account_id"]

    def retain(kind, payload):
        identifier = "AE-" + content_hash({"account_id": account_id, "kind": kind, "payload": payload})
        with sqlite3.connect(path) as conn:
            conn.execute("insert into autonomous_evidence values (?,?,?,?)", (identifier, account_id, kind, json.dumps(payload)))
        return identifier

    context = deployment.bind_campaign_execution_context(SimpleNamespace(
        broker=SimpleNamespace(account_id=account_id, mode="paper"),
        plans=SimpleNamespace(store=SimpleNamespace(path=path)), _retain=retain))
    return context, retain


@pytest.mark.parametrize("missing_config", [False, True])
def test_deployment_hash_chain_is_host_observation_with_explicit_source_gaps(ledger, tmp_path, monkeypatch, missing_config):
    context, _ = _deployment_receipts(ledger, tmp_path / "observed", monkeypatch, missing_config=missing_config)
    reader = AUDIT.ReadOnlyLedger(ledger)
    try:
        result = AUDIT._deployment_audit(reader, context, account_id=_plan(ledger)["account_id"])
    finally:
        reader.close()
    assert result["status"] == "host_receipt_integrity_verified", result
    assert result["source_snapshot_hash_valid"] is True
    assert result["source_scope_complete"] is (not missing_config)
    assert result["loaded_code_independently_verified"] is False


@pytest.mark.parametrize("mutation,problem", [
    (lambda r: r.update(account_id="another-account"), "deployment_account_or_mode_mismatch"),
    (lambda r: r["database_bindings"].update(trading_database_path_sha256="0" * 64), "deployment_database_location_mismatch_or_missing"),
    (lambda r: r.update(source_snapshot_sha256="0" * 64), "deployment_source_snapshot_missing_or_invalid"),
    (lambda r: r.update(source_error_count=1), "deployment_source_snapshot_identity_mismatch"),
])
def test_rehashed_deployment_receipt_requires_account_location_and_source_chain(ledger, tmp_path, monkeypatch, mutation, problem):
    context, retain = _deployment_receipts(ledger, tmp_path / "observed", monkeypatch)
    receipt = context["deployment_receipt"]
    mutation(receipt)
    receipt.pop("receipt_sha256")
    receipt["receipt_sha256"] = content_hash(receipt)
    context["deployment_receipt_id"] = retain("deployment_context", receipt)
    reader = AUDIT.ReadOnlyLedger(ledger)
    try:
        result = AUDIT._deployment_audit(reader, context, account_id=_plan(ledger)["account_id"])
    finally:
        reader.close()
    assert result["status"] == "not_verified"
    assert result["receipt_hash_valid"] is True
    assert problem in result["problems"]


def test_fill_deployment_uses_receipt_from_that_fill_only(ledger, tmp_path, monkeypatch):
    context, _ = _deployment_receipts(ledger, tmp_path / "observed", monkeypatch)
    _edit_buy_cash(ledger, lambda metadata: (metadata["fill_evidence"]["execution_context"].update(context), _rehash_fill(metadata)))
    evidence = AUDIT.audit(ledger)["plans"][0]["fill_provenance"]
    assert sorted(fill["deployment"]["status"] for fill in evidence["fills"]) == ["host_receipt_integrity_verified", "unknown"]
    assert evidence["execution_quote_source_status"] == "unknown"
