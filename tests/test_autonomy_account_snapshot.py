"""The campaign UI and model read the same isolated broker account."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.providers.result_projection import project_tool_result
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.risk.risk_engine import RiskEngine
from stock_ai import autonomous_trading_api as api
from stock_ai import autonomous_trading_service as wiring
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider


def test_api_and_model_status_use_isolated_cash_and_positions(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "accounts.sqlite")
    legacy = PaperOMS(store, account_id="legacy", initial_cash=50_000, read_environment=False)
    isolated = PaperOMS(store, account_id="autonomous-paper-v1", initial_cash=10_000, read_environment=False)
    for oms, symbol, quantity in ((legacy, "2887.TW", 10), (isolated, "2330.TW", 5)):
        oms.submit_partial_fill({"order_id": oms.account_id + "-fixture", "symbol": symbol, "market": "TW",
                                 "action": "buy", "entry_price": 100, "position_size_pct": 10, "risk_approved": True},
                                fill_quantity=quantity, fill_id=oms.account_id + "-fill")
    broker = PaperBrokerPort(PaperBrokerSimulator(store, isolated))
    campaign = SimpleNamespace(broker=broker, risk=RiskEngine(),
                              experiment_limits={"max_order_notional_pct": 5, "max_total_exposure_pct": 20},
                              status=lambda: {"account_id": broker.account_id, "mode": "paper",
                              "enabled": False, "plans": [], "research_cursor": 0, "positive_ev_qualified": False})
    monkeypatch.setattr(wiring, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(api, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(wiring, "get_autonomous_model_review", lambda service=None: SimpleNamespace(status=lambda: {"enabled": False}))
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).get("/agent/autonomy/status")
    assert response.status_code == 200
    context = AgentRunContext(run_id="AR-status", session_id="AS-status", driver_id="codex", autonomy="advisory", symbols=())
    tool = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.status", {}, context))
    for receipt in (response.json(), tool):
        assert receipt["account"]["account_id"] == "autonomous-paper-v1"
        assert receipt["account"]["cash_balance"] == 9_500
        assert [(p["symbol"], p["quantity"]) for p in receipt["account"]["positions"]] == [("2330.TW", 5)]
        assert receipt["account"]["open_order_reservations"] == []
        assert receipt["account_observed_at"]
    projected = project_tool_result(tool)
    assert projected["account"]["cash_balance"] == 9_500
    assert projected["account_positions"][0]["symbol"] == "2330.TW"
    assert "2887.TW" not in json.dumps(projected)
    decision = projected["paper_decision_context"]
    assert decision["plan_creation"]["status"] == "blocked"
    assert "host_paper_campaign_authorization_required" in decision["plan_creation"]["blockers"]
    assert decision["planning_cash"]["max_order_notional_pct"] == 5
    assert decision["positive_ev"]["required_for_bounded_experiment"] is False
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) <= 6_000


def test_status_rejects_wrong_broker_account_without_fallback():
    async def account(*, now):
        return {"account_id": "legacy", "cash_balance": 100_000}
    campaign = SimpleNamespace(broker=SimpleNamespace(account_id="autonomous-paper-v1", account=account))
    with pytest.raises(ValueError, match="broker_account_mismatch"):
        asyncio.run(wiring.autonomous_status_snapshot(campaign))


def test_coverage_api_forwards_filters_and_maps_invalid_queries_to_422(monkeypatch):
    calls = []

    class Ledger:
        def query(self, **kwargs):
            calls.append(kwargs)
            if kwargs["domain"] == "quotes":
                raise ValueError("research_coverage_domain_invalid")
            return {"schema_version": "open_stock_ai.security_research_coverage_query.v1",
                    "items": [{"symbol": "2330.TW"}]}

    monkeypatch.setattr(api, "get_autonomous_campaign",
                        lambda: SimpleNamespace(coverage_ledger=Ledger()))
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.get("/agent/autonomy/coverage", params={
        "symbol": "2330.TW", "deep_status": "never_researched", "domain": "price_history",
        "needs_update": "true", "new_entry_eligible": "true", "after": "cursor", "limit": 20,
    })
    assert response.status_code == 200 and response.json()["items"] == [{"symbol": "2330.TW"}]
    assert calls[0] == {"symbols": ["2330.TW"], "deep_status": "never_researched",
                        "domain": "price_history", "needs_update": True,
                        "new_entry_eligible": True, "after": "cursor", "limit": 20}
    rejected = client.get("/agent/autonomy/coverage", params={"domain": "quotes"})
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "research_coverage_domain_invalid"


def test_status_can_read_a_specific_owned_plan_without_hiding_account_totals(monkeypatch):
    campaign = SimpleNamespace()
    async def status(service, **kwargs):
        assert service is campaign
        return {"schema_version": "open_stock_ai.autonomous_status.v1", "account_id": "isolated",
                "account": {"account_id": "isolated", "cash_balance": 9000, "total_equity": 10000},
                "plans": [{"plan_id": identity, "symbol": "2330.TW", "status": "waiting_entry",
                           "definition": {"quantity_shares": 10}} for identity in ("owned-a", "owned-b")]}
    monkeypatch.setattr(wiring, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(wiring, "autonomous_status_snapshot", status)
    context = AgentRunContext(run_id="AR-read", session_id="AS-read", driver_id="codex", autonomy="advisory", symbols=())
    result = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.status", {"plan_ids": ["owned-b", "unknown"]}, context))
    assert result["retained_plan_count"] == 2 and result["missing_plan_ids"] == ["unknown"]
    projected = project_tool_result(result)
    assert projected["plans"][0]["plan_id"] == "owned-b"
    assert projected["account"]["total_equity"] == 10000
    assert projected["plan_index"] == [{"plan_id": "owned-b", "symbol": "2330.TW", "status": "waiting_entry"}]


def test_large_status_preserves_account_numbers_and_marks_partial_holdings():
    receipt = {"schema_version": "open_stock_ai.autonomous_status.v1", "account_id": "autonomous-paper-v1", "research_cursor": 0, "plans": [],
               "account": {"account_id": "autonomous-paper-v1", "cash_balance": 9_500, "available_cash": 9_500,
                           "total_equity": 10_000, "position_count": 100,
                           "positions": [{"symbol": f"{2300+i}.TW", "quantity": 1, "market_value": 100} for i in range(100)],
                           "open_order_reservations": [{"order_id": "owned-order", "symbol": "2330.TW", "side": "buy",
                                                        "remaining_quantity": 2, "reservation_price": 100,
                                                        "estimated_remaining_cost": 20, "valid": True, "blockers": []}]}}
    projected = project_tool_result(receipt)
    assert projected["account"]["total_equity"] == 10_000
    assert projected["retained_account_position_count"] == 100
    assert len(projected["account_positions"]) < 100 and projected["_projection_truncated"]
    assert projected["open_order_reservations"][0]["remaining_quantity"] == 2
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) <= 6_000
    receipt["plans"] = [{"plan_id": f"plan-{i}", "definition": {"quantity_shares": 100, "symbol": "2330.TW"}} for i in range(100)]
    receipt["account"]["open_order_reservations"] *= 100
    receipt["model_review"] = {"enabled": True, "used_today": 1, "remaining_today": 0}
    projected = project_tool_result(receipt)
    assert projected["model_review"]["remaining_today"] == 0
    assert len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) <= 6_000
