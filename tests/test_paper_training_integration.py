from pathlib import Path

from fastapi.testclient import TestClient

from open_stock_ai.runtime import clear_runtime_engine_cache, get_runtime_engine
from stock_ai.main import app


ROOT = Path(__file__).resolve().parents[1]


def test_paper_training_routes_are_wired_into_fastapi():
    client = TestClient(app)
    paths = set(app.openapi().get("paths") or {})
    expected = {
        "/api/open-stock-ai/agent/research-pack",
        "/api/open-stock-ai/agent/paper-training/account",
        "/api/open-stock-ai/agent/paper-training/reset",
        "/api/open-stock-ai/agent/paper-training/preview",
        "/api/open-stock-ai/agent/paper-training/order",
        "/api/open-stock-ai/agent/paper-training/orders",
        "/api/open-stock-ai/agent/paper-training/fills",
        "/api/open-stock-ai/agent/paper-training/orders/{order_id}/cancel",
        "/api/open-stock-ai/agent/paper-training/orders/{order_id}/replace",
        "/api/open-stock-ai/agent/paper-training/mark-to-market",
        "/api/open-stock-ai/agent/paper-training/evaluate",
        "/api/open-stock-ai/agent/paper-training/reflection",
        "/api/open-stock-ai/agent/paper-training/governance/artifacts",
        "/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback",
    }
    assert expected.issubset(paths)
    account = client.get("/api/open-stock-ai/agent/paper-training/account")
    assert account.status_code == 200
    payload = account.json()
    assert payload["schema_version"] == "open_stock_ai.paper_training_account.v1"
    assert {"open_orders", "recent_orders", "recent_fills"}.issubset(payload)


def test_training_order_contract_never_accepts_a_caller_supplied_fill_price():
    from stock_ai.paper_training_api import PaperOrderRequest

    fields = set(PaperOrderRequest.model_fields)
    assert "price" not in fields
    assert "entry_price" not in fields
    assert "fill_price" not in fields
    assert {
        "symbol",
        "side",
        "order_type",
        "time_in_force",
        "lot_type",
        "session",
        "quantity_lots",
        "quantity_shares",
        "limit_price",
        "stop_price",
        "expires_at",
        "actor",
        "rationale",
    }.issubset(fields)


def test_paper_order_api_preserves_a_borrow_locate_only_for_short_sales():
    from stock_ai.paper_training_api import PaperOrderRequest, _ticket

    request = PaperOrderRequest.model_validate(
        {
            "symbol": "2330.TW",
            "side": "short_sell",
            "quantity_shares": 10,
            "borrow_receipt": {
                "receipt_id": "LOC-API-1",
                "source": "verified-paper-fixture",
                "verified_at": "2026-07-14T01:00:00+00:00",
                "expires_at": "2026-07-30T01:00:00+00:00",
                "available_quantity": 10,
                "annual_fee_bps": 1600,
            },
        }
    )
    ticket = _ticket(request, {"symbol": "2330.TW", "market": "TW"}, None)

    assert ticket["side"] == "short_sell"
    assert ticket["borrow_receipt"]["receipt_id"] == "LOC-API-1"


def test_runtime_paper_broker_factory_uses_the_authoritative_retention_ledger(tmp_path, monkeypatch):
    from stock_ai import paper_training_api

    database = tmp_path / "runtime-paper-broker.sqlite"
    monkeypatch.setenv("OPEN_STOCK_AI_SQLITE_PATH", str(database))
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "runtime-paper-broker")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "10000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_LOT_SIZE", "1")
    clear_runtime_engine_cache()
    try:
        engine = get_runtime_engine()
        broker = paper_training_api._broker()
        assert engine.governance is not None
        assert broker.retention_ledger is engine.governance.retention
        assert broker.retention_ledger.store is engine.governance.retention_store
        result = broker.submit(
            {
                "order_id": "PB-RUNTIME-RETENTION-1",
                "symbol": "2330.TW",
                "market": "TW",
                "side": "buy",
                "order_type": "market",
                "time_in_force": "rod",
                "lot_type": "odd_lot",
                "session": "regular",
                "quantity_shares": 10,
                "actor": "user",
            },
            {
                "symbol": "2330.TW",
                "market": "TW",
                "price": 100,
                "price_source": "test_exchange_last_trade",
                "source_timestamp": "2026-08-26T02:00:00+00:00",
                "is_realtime": True,
                "is_fallback": False,
                "odd_lot_auction_matched": True,
            },
        )
        assert result["order"]["status"] == "filled"
        assert result["retention"]["critical"] is True
        assert result["retention"]["kind"] == "execution_broker_state"

        clear_runtime_engine_cache()
        restarted = get_runtime_engine()
        assert restarted.governance is not None
        records = [
            record for record in restarted.governance.retention._records.values()
            if record["kind"] == "execution_broker_state"
        ]
        assert len(records) == 1
        assert records[0]["payload"]["result"]["order"]["order_id"] == "PB-RUNTIME-RETENTION-1"
    finally:
        clear_runtime_engine_cache()


def test_paper_training_governance_api_rolls_back_only_the_selected_artifact_lane(tmp_path, monkeypatch):
    database = tmp_path / "runtime-artifact-rollback.sqlite"
    monkeypatch.setenv("OPEN_STOCK_AI_SQLITE_PATH", str(database))
    clear_runtime_engine_cache()
    try:
        engine = get_runtime_engine()
        assert engine.governance is not None
        registry = engine.governance.artifact_rollback
        for artifact_id, digest in (("strategy-api-v1", "a" * 64), ("strategy-api-v2", "b" * 64)):
            registry.approve(
                artifact_id,
                digest,
                approved_by="owner",
                approved_at="2026-08-26T08:00:00+00:00",
                metadata={"strategy_id": artifact_id},
            )
            registry.activate(artifact_id)

        client = TestClient(app)
        before = client.get("/api/open-stock-ai/agent/paper-training/governance/artifacts")
        assert before.status_code == 200
        assert before.json()["scopes"]["strategy"]["current_artifact_id"] == "strategy-api-v2"

        response = client.post(
            "/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback",
            json={
                "artifact_scope": "strategy",
                "reason": "verified_shadow_drawdown_breach",
                "approved_by": "owner",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["receipt"]["from_artifact_id"] == "strategy-api-v2"
        assert payload["receipt"]["to_artifact_id"] == "strategy-api-v1"
        assert payload["governance"]["scopes"]["strategy"]["current_artifact_id"] == "strategy-api-v1"
    finally:
        clear_runtime_engine_cache()


def test_research_pack_accepts_attributed_non_executable_market_price(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from stock_ai import paper_training_api

    now = datetime.now(timezone.utc)
    summary = SimpleNamespace(
        latest_price=SimpleNamespace(close=100.0, date=now),
        data_source="official indicative feed",
        provider_id="test-provider",
        connector_id="test-connector",
        quote_kind="indicative",
        data_timestamp=now,
        max_age_seconds=300,
        authorized=True,
        realtime=False,
        delayed=True,
        official_close=False,
        freshness_note="research only",
        reliability_note="attributed indicative quote",
        entity=SimpleNamespace(
            symbol="2330.TW",
            name="台積電",
            market="taiwan",
            exchange="TWSE",
            industry="半導體",
        ),
    )
    monkeypatch.setattr(paper_training_api, "get_execution_price_summary", lambda _symbol: summary)

    price = paper_training_api._verified_price("2330.TW", require_execution_quote=False)

    assert price["price"] == 100.0
    assert price["source_kind"] == "research_indicative"
    assert price["research_eligible"] is True
    assert price["execution_eligibility"]["execution_eligible"] is False
    assert price["exchange_rules_enforced"] is True


def test_tool_manifest_exposes_training_without_reducing_computer_use():
    client = TestClient(app)
    payload = client.get("/api/open-stock-ai/agent/tool-manifest").json()

    assert "/api/open-stock-ai/agent/research-pack" in payload["analysis_endpoints"]
    assert "POST /api/open-stock-ai/agent/paper-training/order" in payload["paper_training_endpoints"]
    assert "Computer Use" in payload["computer_use_role"]
    assert any("bypass research and risk gates" in rule for rule in payload["rules"])


def test_final_light_theme_and_complete_broker_ui_are_loaded():
    index = (ROOT / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "src/stock_ai/ui/static/ui-fixes-v2.css").read_text(encoding="utf-8")
    script = (ROOT / "src/stock_ai/ui/static/paper-training.js").read_text(encoding="utf-8")

    assert "/static/ui-fixes-v2.css" in index
    assert "/static/paper-training.js" in index
    assert 'id="paperTrainingPanel"' in index
    assert "/static/paper-trading-broker-v4.css" in script
    assert 'id="paperTrainingBuySide"' in script
    assert 'id="paperTrainingSellSide"' in script
    assert 'id="paperTrainingOrderType"' in script
    assert 'id="paperTrainingLotType"' in script
    assert 'id="paperTrainingTimeInForce"' in script
    assert 'id="paperTrainingOpenOrders"' in script
    assert 'id="paperTrainingFills"' in script
    assert 'id="paperTrainingSubmitOrder"' in script
    assert 'id="paperTrainingReflection"' in script
    assert 'id="paperTrainingExchangeRuleHint"' in script
    assert 'id="paperTrainingRollbackScope"' in script
    assert 'id="paperTrainingRollbackArtifact"' in script
    assert "/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback" in script
    assert "requiresLimitRod" in script

    assert ".decision-next b" in css
    assert ".decision-timing b" in css
    assert ".decision-trigger b" in css
    assert "#newsScopeBar button" in css
    assert "#newsFilterBar button" in css
    assert ".settings-panel::after" in css


def test_training_ui_hides_internal_requirement_copy_but_keeps_server_validation():
    script = (ROOT / "src/stock_ai/ui/static/paper-training.js").read_text(encoding="utf-8")
    api = (ROOT / "src/stock_ai/paper_training_api.py").read_text(encoding="utf-8")

    assert "REAL MARKET DATA · VIRTUAL CAPITAL" not in script
    assert "禁止假股票" not in script
    assert "不能自行填入價格" not in script
    assert "/api/open-stock-ai/agent/paper-training/preview" in script
    assert "/api/open-stock-ai/agent/paper-training/order" in script
    assert "/api/open-stock-ai/agent/paper-training/orders/${encodeURIComponent(orderId)}/replace" in script
    assert "paperTrainingExpiresAt" in script
    assert "payload.price" not in script
    assert "payload.entry_price" not in script
    assert "get_execution_price_summary" in api
    assert "A last trade or exchange close is required" in api
    assert "AUTO_MARK_INTERVAL_MS = 5 * 60 * 1000" in script
    assert "wait_for_exchange_session" in script
    assert "marketRules.session?.name" in script
    assert "settlement_due_at" in script
    assert "unsettled_receivable" in script
    assert "loadAccount({ refreshPrices: true })" in script
    assert "visibilitychange" in script
    assert "if (document.hidden || resetInFlight) return" in script
