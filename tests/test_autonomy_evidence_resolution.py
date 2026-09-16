"""Host-only citation bridging; no model, network, or production writes."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.checkpoint_store import CheckpointStore
from open_stock_ai.agent_runtime.contracts import AgentRunContext, build_runtime_event
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.execution.trading_plan import content_hash
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from stock_ai.tool_providers.autonomy_evidence import resolve_proposal_evidence_ids
from test_agent_campaign_actions import setup
from test_autonomous_campaign import history_fixture


@pytest.fixture
def host(tmp_path, monkeypatch):
    from stock_ai import autonomous_trading_service as wiring
    now = datetime.now(timezone.utc)
    campaign, _ = setup(tmp_path, symbols=("2330.TW",),
                        history=lambda symbol, instant: history_fixture(symbol, now=instant-timedelta(days=1)))
    cycle = asyncio.run(campaign.research(now=now, deep_limit=1))
    store = AgentRunStore(tmp_path / "runs.sqlite")
    context = AgentRunContext(run_id="AR-citations", session_id="AS-citations", driver_id="codex",
                               autonomy="paper_execute", symbols=(), allow_paper_orders=True)
    context.state.update(explicit_autonomous_campaign_authorized=True,
                         provider_model_metadata={"model": "gpt-5.6-sol", "reasoning_effort": "medium"})
    AgentSessionStore(store.path).create(session_id=context.session_id, title="Offline citations", namespace="test")
    store.create_run(context.run_id, {"driver_id": "codex", "session_id": context.session_id,
                                     "objective": "請啟動全市場自主紙上交易流程。"})
    monitor = SimpleNamespace(runtime=SimpleNamespace(store=store))
    monkeypatch.setattr(wiring, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(wiring, "get_autonomous_model_review", lambda service=None: monitor)
    return {"campaign": campaign, "cycle": cycle, "store": store, "context": context,
            "checkpoint": CheckpointStore(store.path), "trace": [], "sequence": 0, "now": now}


def save_checkpoint(host):
    host["sequence"] += 1
    return host["checkpoint"].create(session_id=host["context"].session_id, run_id=host["context"].run_id,
        plan_revision=1, sequence=host["sequence"], payload={"trace": deepcopy(host["trace"]), "context_state": {}})


def record(host, call_id, tool, result, *, arguments=None, success=True, risk_class="read_only"):
    args = arguments or {}
    validation = {"schema_version": "open_stock_ai.validation_result.v1", "passed": success,
                  "validator": "default", "evidence_hash": content_hash(result), "checks": []}
    provenance = {"tool": tool, "provider": "fixture_host_tool", "source": "fixture_host_provider:" + tool,
                  "success": success, "request_id": call_id, "source_url": "https://example.invalid/fixture"}
    for event_type, extra in (("tool.started", {"risk_class": risk_class}),
                              ("tool.completed" if success else "tool.failed", {"validation": validation,
                                "provenance": provenance, "result_summary": {"schema_version": result.get("schema_version")}})):
        host["sequence"] += 1
        event = build_runtime_event(event_type, sequence=host["sequence"], run_id=host["context"].run_id,
            session_id=host["context"].session_id, payload={"step": 1, "call_id": call_id, "tool": tool,
                                                          "arguments": args, **extra})
        host["store"].append_event(host["context"].run_id, event)
    host["trace"].append({"call_id": call_id, "tool": tool, "arguments": deepcopy(args), "ok": success,
                          "result": deepcopy(result), "validation": validation})
    save_checkpoint(host)


def resolve(host, ids):
    return resolve_proposal_evidence_ids(host["campaign"], identifiers=ids,
                                        context=host["context"], run_store=host["store"])


def native_style_calls(host):
    campaign, cycle = host["campaign"], host["cycle"]
    history_id = cycle["results"][0]["history_id"]
    history = campaign._evidence(history_id)
    record(host, "tc-research-cycle", "autonomy.research", cycle, arguments={"cycle_id": cycle["cycle_id"]})
    record(host, "tc-account-status", "autonomy.status", {"account_id": campaign.broker.account_id,
        "account": {"account_id": campaign.broker.account_id, "cash_balance": 100000, "positions": []}})
    record(host, "tc-history-2330", "autonomy.evidence", {"evidence_id": history_id, "kind": "price_history",
        "retained_payload_sha256": content_hash(history), "payload_view": {"rows": history["rows"][-10:]}},
        arguments={"evidence_id": history_id, "bar_limit": 10})
    record(host, "tc-universe-five", "market.analyze_universe", {"items": [{"symbol": "2330.TW", "volatility": 38.5}]})
    record(host, "tc-pack-2330", "market.research_pack", {"symbol": "2330.TW", "technical_features": {"sma20": 100},
        "paper_account_summary": {"account_id": "legacy-shared", "cash_balance": 999537},
        "pipeline_workspace": {"portfolio_status": "legacy"}, "paper_position": {"quantity": 9},
        "learning_for_symbol": []})
    record(host, "tc-taifex-risk", "market.taifex_foreign_open_interest", {"source": {"url": "https://example.invalid/taifex"},
        "position": {"net": -83918}, "schema_version": "offline.taifex.fixture"})
    return [row["call_id"] for row in host["trace"]]


def test_six_native_style_citations_resolve_and_provider_retries_reuse_one_plan(host):
    ids = native_style_calls(host)
    original = deepcopy(host["trace"])
    resolved = resolve(host, ids)
    assert len(resolved) == 6 and all(key.startswith("AE-") for key in resolved)
    assert resolved[:3:2] == [host["cycle"]["bulk_evidence_id"], host["cycle"]["results"][0]["history_id"]]
    evidence = host["campaign"]._evidence(resolved[4])
    assert evidence["tool_call_id"] == "tc-pack-2330" and evidence["run_id"] == host["context"].run_id
    assert evidence["result_sha256"] == content_hash(original[4]["result"])
    assert evidence["source_provenance_verified"] is False
    assert "source_id" not in evidence
    assert "paper_account_summary" not in evidence["result_view"]
    assert evidence["omitted_non_campaign_fields"] and host["trace"] == original
    args = {"cycle_id": host["cycle"]["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
            "stop_loss": 95, "target_price": 130, "rationale": "Use retained observations for a future condition.",
            "evidence_ids": ids, "not_before": (host["now"]+timedelta(days=3)).isoformat()}
    first = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.propose_plan", args, host["context"]))
    save_checkpoint(host)
    second = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.propose_plan", args, host["context"]))
    assert first["plan"]["plan_id"] == second["plan"]["plan_id"] and args["evidence_ids"] == ids
    assert len(host["campaign"].plans.list(account_id=host["campaign"].broker.account_id)) == 1
    assert not host["campaign"].broker.submissions
    assert resolve(host, ids) == resolved


def test_batch_evidence_compacts_twenty_style_comparison_and_resolves_owned_ids(host):
    campaign, cycle = host["campaign"], host["cycle"]
    requested = [cycle["results"][0]["history_id"], cycle["bulk_evidence_id"]]
    arguments = {"evidence_ids": requested, "view": "summary"}

    result = asyncio.run(AutonomousTradingToolProvider().execute(
        "autonomy.evidence", arguments, host["context"]))

    assert result["schema_version"] == "open_stock_ai.autonomy_evidence_batch.v1"
    assert result["count"] == 2
    assert [item["evidence_id"] for item in result["evidence"]] == requested
    assert result["evidence"][0]["summary"]["symbol"] == "2330.TW"
    assert result["evidence"][0]["summary"]["sma"]["20"] == 100.3
    assert result["evidence"][1]["summary"]["feature_count"] == 2
    assert "rows" not in str(result) and "features" not in str(result)

    record(host, "tc-batch-evidence", "autonomy.evidence", result, arguments=arguments)
    assert resolve(host, ["tc-batch-evidence"]) == requested


@pytest.mark.parametrize("defect", ["fabricated", "failed", "different_session", "other_run", "hash", "checkpoint_hash", "account", "mutation"])
def test_untrusted_or_foreign_references_never_reach_plan_creation(host, defect):
    result = {"account_id": "other-account" if defect == "account" else host["campaign"].broker.account_id,
              "observations": [{"symbol": "2330.TW", "close": 106}]}
    record(host, "tc-observation", "market.research", result, success=defect != "failed",
           risk_class="mutating" if defect == "mutation" else "read_only")
    identifier = "not-a-real-call" if defect == "fabricated" else "tc-observation"
    if defect == "different_session":
        host["context"].session_id = "AS-other"
    if defect == "other_run":
        host["context"].run_id = "AR-other"
        host["store"].create_run("AR-other", {"session_id": host["context"].session_id, "driver_id": "codex"})
        host["trace"] = []
        save_checkpoint(host)
    if defect == "hash":
        host["trace"][0]["result"]["observations"][0]["close"] = 999
        save_checkpoint(host)
    if defect == "checkpoint_hash":
        with host["store"]._connect() as conn:
            conn.execute("update agent_checkpoints set snapshot_hash=?", ("0"*64,))
            conn.commit()
    args = {"cycle_id": host["cycle"]["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
            "stop_loss": 95, "target_price": 130, "rationale": "Offline boundary probe.", "evidence_ids": [identifier]}
    with pytest.raises(ValueError, match="proposal_evidence_reference_invalid"):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.propose_plan", args, host["context"]))
    assert not host["campaign"].plans.list(account_id=host["campaign"].broker.account_id)
    assert not host["campaign"].broker.submissions


def test_canonical_owned_evidence_is_verified_and_foreign_account_id_rejected(host):
    from test_autonomous_trading_plans import BrokerFixture
    own = host["cycle"]["results"][0]["history_id"]
    assert resolve(host, [own, own]) == [own]
    broker = BrokerFixture()
    broker.account_id = "foreign-account"
    other, _ = setup(host["store"].path.parent / "foreign", broker=broker, shared=host["campaign"].plans.store)
    foreign = other._retain("price_history", {"rows": []})
    with pytest.raises(ValueError, match="owned_retained_evidence_required"):
        resolve(host, [foreign])
