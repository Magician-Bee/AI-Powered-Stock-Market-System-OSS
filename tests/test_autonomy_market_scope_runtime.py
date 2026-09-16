"""Whole-market Host scope survives receipt identifiers and checkpoint restoration."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime import AgentOrchestrator, AgentToolSpec
from open_stock_ai.agent_runtime.routing import UnifiedMultiIntentRouter, _explicit_symbols, normalized_market_symbols
from stock_ai.capability_registry import CapabilityRegistry, BoundCapabilityProvider
from stock_ai.autonomous_model_review import AutonomousModelReview
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider


CYCLE = "AC-10e956e38feb07cb40bca835021781b4a826e80d4ae44edb12bb5226c5274b6d"
EVIDENCE = "AE-" + "b" * 64
OBJECTIVE = (
    '[MARKET_SCOPE] This is a whole-market autonomous campaign.\n'
    '請啟動台灣上市櫃全市場自主紙上交易流水線。'
    f'先呼叫 autonomy.research(cycle_id="{CYCLE}") 回讀證據，'
    f'用 autonomy.evidence 讀取 {EVIDENCE}；視決策需要搭配基本面與風險工具。'
    '再用 autonomy.activate 啟動同一個 cycle，use_candidate_plans=false。'
)


@pytest.mark.parametrize("text,expected", [
    (CYCLE, []), (EVIDENCE, []), ("AR-" + "a" * 32, []),
    ("2330.TW", ["2330.TW"]), ("6538.TWO", ["6538.TWO"]),
    ("AAPL", ["AAPL"]), ("BTC-USD", ["BTC-USD"]),
    (f"比較 AAPL 和 {CYCLE}", ["AAPL"]),
])
def test_receipt_parts_are_not_tickers_but_complete_identifiers_remain(text, expected):
    assert _explicit_symbols(text) == expected


def test_whole_market_examples_do_not_become_instrument_restrictions():
    objective = OBJECTIVE + " 比較全市場，例如 AAPL、2330.TW；不得只研究例子。"
    routing = UnifiedMultiIntentRouter().route(objective, task_kind_hint="market_decision")
    assert normalized_market_symbols(routing, objective=objective, supplied_symbols=()) == ()
    assert not routing.requires_symbol
    assert not any(row.symbol for row in routing.symbol_contexts)


def test_explicit_workflow_instrument_keeps_its_restriction():
    routing = UnifiedMultiIntentRouter().route(OBJECTIVE, task_kind_hint="market_decision", supplied_symbols=("6538.TWO",))
    assert normalized_market_symbols(routing, objective=OBJECTIVE, supplied_symbols=("6538.TWO",)) == ("6538.TWO",)


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("objective_source", ["api", "daily_background_review"])
def test_actual_registry_research_evidence_activate_keep_empty_scope_on_fresh_and_resume(monkeypatch, resumed, objective_source):
    calls, lifecycle_scopes = [], []
    state = {"enabled": False}
    now = datetime.now(timezone.utc).isoformat()
    cycle = {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "cycle_id": CYCLE,
             "account_id": "offline-isolated", "created_at": now, "deep_success_count": 1,
             "results": [{"symbol": "2330.TW", "history_id": EVIDENCE, "last_bar": "2026-09-10T05:30:00+00:00"}]}
    objective = AutonomousModelReview._objective(cycle) if objective_source == "daily_background_review" else OBJECTIVE
    def configure(*, enabled, authorized_at):
        assert authorized_at == now
        state["enabled"] = enabled
        return dict(state)
    async def manage():
        return {"account_id": "offline-isolated", "enabled": state["enabled"], "results": [], "errors": []}
    service = SimpleNamespace(broker=SimpleNamespace(account_id="offline-isolated"), cycle=lambda _id: cycle,
        status=lambda: {**state, "plans": []}, configure=configure, manage=manage,
        assert_activation_authorized=lambda **kwargs: None, _retain=lambda *args: "AE-" + "c" * 64)
    review = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(get_run=lambda _id: {"created_at": now})),
        register_current_review=lambda **kwargs: None, configure=lambda **kwargs: None,
        status=lambda: {"enabled": True})
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: service)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _: review)
    monkeypatch.setattr("open_stock_ai.execution.agent_campaign_actions.retained_records", lambda *_args: {
        EVIDENCE: {"kind": "history", "payload": {"symbol": "2330.TW", "rows": []}}})

    class ObservedAutonomy(AutonomousTradingToolProvider):
        async def execute(self, name, arguments, context):
            calls.append((name, tuple(context.symbols)))
            assert not any(row.get("symbol") for row in context.state["routing"]["symbol_contexts"])
            return await super().execute(name, arguments, context)

    async def forbidden_monthly(name, arguments, context):
        pytest.fail("Host must not inject AC monthly revenue into a whole-market campaign")
    monthly = AgentToolSpec(name="market.monthly_revenue", category="market_research", description="Offline monthly revenue guard",
        input_schema={"type": "object", "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}}})
    registry = CapabilityRegistry([ObservedAutonomy(), BoundCapabilityProvider(
        provider_id="offline_market", specs={monthly.name: monthly}, executor=forbidden_monthly)])

    class Driver:
        driver_id = "scripted"
        def __init__(self):
            self.pending = [
                {"id": "read-cycle", "name": "autonomy.research", "arguments": {"cycle_id": CYCLE}},
                {"id": "read-evidence", "name": "autonomy.evidence", "arguments": {"evidence_id": EVIDENCE}},
                {"id": "activate", "name": "autonomy.activate", "arguments": {"cycle_id": CYCLE, "use_candidate_plans": False}},
            ]
        def describe(self):
            return {"id": self.driver_id, "configured": True}
        async def start_run(self, context):
            lifecycle_scopes.append(tuple(context.symbols))
        async def close_run(self, run_id):
            pass
        async def decide(self, turn):
            return {"state": "continue" if self.pending else "complete", "summary": "核對保留研究與紙上啟用收據。",
                    "tool_calls": [self.pending.pop(0)] if self.pending else [], "decision": None}

    old_routing = UnifiedMultiIntentRouter().route("研究 AC 基本面及風險", task_kind_hint="market_decision").model_dump()
    resume_state = {"checkpoint": {"payload": {"context_state": {
        "task_kind": "market_decision", "routing": old_routing, "symbols": ["AC"],
        "explicit_autonomous_campaign_authorized": True}}}} if resumed else None
    driver = Driver()
    result = asyncio.run(AgentOrchestrator(drivers={"scripted": driver}, tools=registry, default_driver="scripted").run(
        objective=objective, symbols=[], autonomy="paper_execute", max_steps=4,
        run_id="AR-offline-scope", session_id="AS-offline-scope", resume_state=resume_state))
    assert lifecycle_scopes == [()]
    assert calls == [("autonomy.research", ()), ("autonomy.evidence", ()), ("autonomy.activate", ())]
    assert state["enabled"] is True
    activation = next(row for row in result["tool_trace"] if row["tool"] == "autonomy.activate")
    assert activation["ok"] and activation["result"]["management"]["enabled"] is True
    assert activation["result"]["campaign_receipt_id"].startswith("AE-")
