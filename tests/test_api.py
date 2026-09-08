from fastapi.testclient import TestClient
from types import SimpleNamespace
from datetime import datetime, timedelta

from stock_ai.main import app
from stock_ai.data_platform.contracts import TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.ui_api import unified_data_api_contract
from stock_ai.models import (
    AITradePlanCard,
    AITradingAssistantWorkspace,
    AssetSummary,
    AssetWorkspace,
    BrokerConnectionStatus,
    ChipDataRecord,
    CompanyEventRecord,
    DailyReport,
    DailySelectionItem,
    EventItem,
    FundamentalsRecord,
    InstitutionalFlowItem,
    MarginTradingItem,
    Entity,
    MarketIndexRecord,
    NotificationPreview,
    OHLCVRecord,
    OrderCostEstimate,
    OrderExecutionRecord,
    PortfolioRecord,
    PositionDetail,
    PricePoint,
    ReadonlyWorkspaceMeta,
    RealTimeQuoteRecord,
    RevenueItem,
    RiskAlert,
    RiskSummary,
    RiskWorkspace,
    SecurityMasterItem,
    TaifexFuturesInstitutionalRecord,
    TaifexPutCallRatioRecord,
    TDCCHoldingDistributionRecord,
    TradingSignalRecord,
    TradingPreview,
    TradingWorkspace,
)

client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_instrument_chart_reuses_short_lived_server_response(monkeypatch):
    from stock_ai.market_intelligence import api as market_api

    calls: list[tuple[str, int]] = []
    payload = {
        "symbol": "^TWII",
        "points": [{"date": "2026-07-30", "close": 40825.0}],
        "source": "fixture",
    }
    monkeypatch.setattr(market_api, "_candidate", lambda _entity_id: (None, None))
    monkeypatch.setattr(
        market_api,
        "get_price_history_payload",
        lambda symbol, *, limit: calls.append((symbol, limit)) or payload,
    )
    market_api._chart_response_cache.clear()
    market_api._chart_request_locks.clear()
    try:
        first = market_api.instrument_chart("^twii", limit=5000)
        second = market_api.instrument_chart("^TWII", limit=5000)

        assert first == second == payload
        assert calls == [("^TWII", 5000)]
    finally:
        market_api._chart_response_cache.clear()
        market_api._chart_request_locks.clear()


def test_unified_ui_data_api_registers_every_declared_route_and_keeps_compatibility(
    monkeypatch,
):
    contract_response = client.get("/api/data/ui/v1/contract")
    assert contract_response.status_code == 200
    contract = contract_response.json()
    assert contract == unified_data_api_contract()
    assert contract["status"] == "enforced"
    assert contract["ui_connector_access"] is False
    assert contract["route_count"] == 54

    route_paths = {
        str(route.path)
        for route in app.routes
        if getattr(route, "path", None) is not None
    }
    assert {item["path"] for item in contract["items"]}.issubset(route_paths)

    monkeypatch.setattr("stock_ai.main.load_catalog", lambda: {"fixture": "catalog"})
    unified = client.get("/api/data/ui/v1/catalog")
    compatibility = client.get("/api/catalog")
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == {"fixture": "catalog"}


def test_short_daytrade_route_uses_unified_and_compatibility_paths(monkeypatch):
    expected = {
        "schema_version": "stock_ai.short_daytrade_history.v1",
        "symbol": "6488.TWO",
        "status": "complete",
    }
    monkeypatch.setattr(
        "stock_ai.main._paper_trade_store",
        lambda: SimpleNamespace(store=object()),
    )
    monkeypatch.setattr(
        "stock_ai.main.query_short_daytrade_history",
        lambda _store, symbol, **_kwargs: {**expected, "symbol": symbol},
    )
    unified = client.get(
        "/api/data/ui/v1/flow/chip/short-daytrade",
        params={"symbol": "6488.TWO"},
    )
    compatibility = client.get(
        "/api/flow/chip/short-daytrade",
        params={"symbol": "6488.TWO"},
    )
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == expected


def test_tdcc_history_route_uses_unified_and_compatibility_paths(monkeypatch):
    expected = {
        "schema_version": "stock_ai.tdcc_holding_history.v1",
        "symbol": "6488.TWO",
        "status": "complete",
    }
    monkeypatch.setattr(
        "stock_ai.main.query_tdcc_holding_history",
        lambda symbol, **_kwargs: {**expected, "symbol": symbol},
    )
    monkeypatch.setattr(
        "stock_ai.main._paper_trade_store",
        lambda: SimpleNamespace(store=SimpleNamespace(path="/tmp/stock-ai-test.sqlite3")),
    )
    unified = client.get(
        "/api/data/ui/v1/flow/chip/tdcc-history",
        params={"symbol": "6488.TWO"},
    )
    compatibility = client.get(
        "/api/flow/chip/tdcc-history",
        params={"symbol": "6488.TWO"},
    )
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == expected


def test_complete_data_lineage_api_lists_and_resolves_derived_artifacts(
    tmp_path,
    monkeypatch,
):
    platform = MarketDataPlatform(database_path=tmp_path / "lineage-api.sqlite")
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload={"close": 110.0},
        request_url="https://openapi.twse.com.tw/verify",
        requested_at="2026-07-27T08:00:00+00:00",
        received_at="2026-07-27T08:00:00+00:00",
    )
    revision = platform.warehouse.write_revision(
        dataset="prices_daily",
        entity_id="ENT-API-LINEAGE",
        observation_key="2026-07-27",
        source_id="twse_openapi",
        temporal=TemporalCoordinates(
            available_at="2026-07-27T08:00:00+00:00",
            acquired_at="2026-07-27T08:00:00+00:00",
            effective_at="2026-07-27T08:00:00+00:00",
        ),
        payload={"close": 110.0},
        raw_payload_id=raw_payload_id,
    )
    monkeypatch.setattr(
        "stock_ai.data_platform.api.get_market_data_platform",
        lambda: platform,
    )

    created = client.post(
        "/api/data/lineage/artifacts",
        json={
            "artifact_type": "conclusion",
            "name": "positive_close",
            "entity_id": "ENT-API-LINEAGE",
            "value": {"state": "positive"},
            "inputs": [
                {
                    "kind": "revision",
                    "id": revision.revision_id,
                    "role": "close",
                    "fields": ["/close"],
                }
            ],
            "transformation_id": "stock_ai.conclusion.positive_close.v1",
        },
    )
    assert created.status_code == 200
    artifact_id = created.json()["artifact"]["artifact_id"]
    assert created.json()["graph"]["completeness"]["status"] == "complete"

    listed = client.get("/api/data/lineage/artifacts?artifact_type=conclusion")
    graph = client.get(f"/api/data/lineage/{artifact_id}")
    revision_graph = client.get(
        f"/api/data/revisions/{revision.revision_id}/lineage"
    )
    assert listed.status_code == graph.status_code == revision_graph.status_code == 200
    assert listed.json()["items"][0]["artifact_id"] == artifact_id
    assert graph.json()["raw_payload_ids"] == [raw_payload_id]
    assert revision_graph.json()["graph"]["completeness"]["status"] == "complete"

    rejected = client.post(
        "/api/data/lineage/artifacts",
        json={
            "artifact_type": "conclusion",
            "name": "missing_input",
            "value": {},
            "inputs": [{"kind": "revision", "id": "DRV-MISSING"}],
            "transformation_id": "stock_ai.conclusion.invalid.v1",
        },
    )
    assert rejected.status_code == 404


def test_entity_registry_api_exposes_contract_and_validates_identifier():
    status = client.get("/api/data/entity-registry")
    assert status.status_code == 200
    payload = status.json()
    assert payload["schema_version"] == "stock_ai.entity_registry_status.v1"
    assert payload["status"] in {"passed", "warning"}
    assert "identifier_count" in payload
    invalid = client.get("/api/data/entity-registry/resolve?identifier=")
    assert invalid.status_code == 422


def test_system_settings_overview_reports_integrations_without_secrets():
    res = client.get("/api/system/settings-overview")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data["skills"]["items"], list)
    assert data["mcp"]["transport"] == "Codex App Server"
    assert data["mcp"]["bridge_ready"] is True
    assert any(item["provider"] == "OpenAPI" for item in data["apis"])
    serialized = res.text.lower()
    assert "bot_token" not in serialized
    assert "access_token" not in serialized
    assert "api_key\"" not in serialized


def test_system_settings_pages_have_focused_non_secret_endpoints():
    skills = client.get("/api/system/settings/skills")
    assert skills.status_code == 200
    assert set(skills.json()) == {"skills", "mcp", "security"}
    assert isinstance(skills.json()["skills"]["items"], list)
    assert "apis" not in skills.json()

    connections = client.get("/api/system/settings/connections")
    assert connections.status_code == 200
    assert set(connections.json()) == {"apis", "security"}
    assert any(item["provider"] == "OpenAPI" for item in connections.json()["apis"])
    serialized = connections.text.lower()
    assert "bot_token" not in serialized
    assert "access_token" not in serialized


def test_entities_search():
    res = client.get("/api/entities/search?q=台積電")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 1
    assert data["items"][0]["symbol"] == "2330.TW"
    assert data["items"][0]["exchange"] == "TWSE"


def test_entities_empty_search_does_not_invent_a_default_watchlist():
    res = client.get("/api/entities/search")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 0
    assert data["items"] == []


def test_etf_code_search_0050_is_real_official():
    res = client.get("/api/entities/search?q=0050")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 1
    assert data["items"][0]["symbol"] == "0050.TW"
    assert data["items"][0]["exchange"] == "TWSE"


def test_real_taiwan_etf_search_by_code():
    res = client.get("/api/entities/search?q=0050")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 1
    assert data["items"][0]["symbol"] == "0050.TW"
    assert "元大" in data["items"][0]["name"]


def test_market_summary():
    res = client.get("/api/market/2330.TW/summary")
    assert res.status_code == 200
    data = res.json()
    assert data["entity"]["name"] == "台積電"
    assert data["latest_price"]["close"] > 0
    assert data["data_source"].startswith(("TWSE MIS", "TWSE official openapi"))
    assert "示範" not in data["data_source"]
    if data["data_source"].startswith("TWSE MIS"):
        assert " " in data["latest_price"]["date"]
    else:
        assert len(data["latest_price"]["date"].split("-")) == 3


def test_taiwan_summary_refuses_stale_official_fallback_when_realtime_fails(monkeypatch):
    from stock_ai import services

    async def boom(symbol: str):
        raise RuntimeError("MIS temporary outage")

    monkeypatch.setattr(services, "fetch_twse_mis_quote", boom)
    summary = services.get_market_summary("2330.TW")
    assert summary is None


def test_market_detail_summary_uses_labeled_official_fallback(monkeypatch):
    from stock_ai import services

    async def boom(symbol: str):
        raise RuntimeError("MIS temporary outage")

    monkeypatch.setattr(services, "fetch_twse_mis_quote", boom)
    summary = services.get_market_detail_summary("2317.TW")
    assert summary is not None
    assert summary.entity.symbol == "2317.TW"
    assert summary.latest_price.close > 0
    assert summary.data_source.startswith("TWSE official")


def test_execution_price_summary_uses_official_close_when_realtime_has_only_bid_ask(monkeypatch):
    from stock_ai import services

    # Keep this test deterministic: an auction-like midpoint must never be
    # passed to the Paper Broker, even though it remains valid dashboard data.
    monkeypatch.setattr(
        services,
        "get_market_detail_summary",
        lambda _symbol: SimpleNamespace(quote_kind="bid_ask_midpoint"),
    )
    official_point = PricePoint(
        date="2026-08-28",
        open=37.0,
        high=38.0,
        low=36.5,
        close=37.5,
        volume=1_000_000,
    )
    payload = {
        "entity": Entity(
            entity_id="fixture:2887.TW",
            symbol="2887.TW",
            name="台新新光金",
            entity_type="stock",
            market="taiwan",
            exchange="TWSE",
            currency="TWD",
        ),
        "history": [official_point],
        "latest": official_point,
        "change_percent": 1.0,
        "events": [],
        "source": "TWSE official fixture",
        "data_timestamp": "2026-08-28T07:00:00+00:00",
        "freshness_note": "official close",
        "reliability_note": "fixture",
    }
    monkeypatch.setattr(services, "official_summary_payload", lambda _symbol: payload)
    monkeypatch.setattr(
        services,
        "_observe_independent_same_day_quote",
        lambda *_args, **_kwargs: {"status": "not_observed"},
    )

    summary = services.get_execution_price_summary("2887.TW")

    assert summary is not None
    assert summary.quote_kind == "official_close"
    assert summary.official_close is True
    assert summary.data_source.startswith("TWSE official")


def test_non_taiwan_summary_is_hidden_without_realtime_provider():
    from stock_ai import services

    assert services.get_market_summary("AAPL") is None


def test_taiwan_official_kline_history():
    res = client.get("/api/market/2330.TW/history")
    assert res.status_code == 200
    data = res.json()
    assert len(data["points"]) >= 20
    assert data["points"][-1]["close"] > 0


def test_query_explanation_has_sources(monkeypatch):
    from stock_ai import main as main_module

    async def unified_answer(_question):
        return (
            "market_information",
            "結論：Unified Agent Runtime 已完成。",
            {"run_id": "query-api", "validated": True},
            ["market.analyze_symbol", "model:codex/mock"],
        )

    monkeypatch.setattr(main_module, "answer_question", unified_answer)
    res = client.post("/api/query", json={"question": "台積電今天為什麼漲？"})
    assert res.status_code == 200
    data = res.json()
    assert data["route"] == "market_information"
    assert data["data"]["validated"] is True
    assert "Unified Agent Runtime" in data["answer"]
    assert data["sources"]
    assert data["sources"]


def test_linkage():
    res = client.get("/api/linkage?source=SOX&target=2330.TW")
    assert res.status_code == 200
    data = res.json()
    assert data["direction"] in {"positive", "mixed"}
    assert data["confidence"] > 0


def test_screener():
    res = client.post("/api/screener", json={"market": "taiwan", "conditions": ["real_quote", "volume > 0"]})
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == len(data["items"])
    assert data["count"] >= 0
    assert data["conditions"] == ["real_quote", "volume > 0"]
    assert data["universe"]["source"] == "none"
    assert data["universe"]["symbols"] == []
    if data["items"]:
        assert data["items"][0]["score"] >= data["items"][-1]["score"]


def test_screener_rejects_unknown_conditions_instead_of_ignoring_them():
    res = client.post("/api/screener", json={"conditions": ["flow"]})
    assert res.status_code == 422
    assert "must use" in res.json()["detail"]


def test_screener_resolves_explicit_universe_with_provenance(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(main, "run_screener", lambda _conditions, symbols: [])
    res = client.post(
        "/api/screener",
        json={
            "conditions": ["volume"],
            "universe_source": "explicit_symbols",
            "symbols": ["AAA", "BBB", "AAA"],
            "filters": {"reason": "integration-test"},
            "limit": 2,
        },
    )

    assert res.status_code == 200
    universe = res.json()["universe"]
    assert universe["source"] == "explicit_symbols"
    assert universe["symbols"] == ["AAA", "BBB"]
    assert universe["count"] == 2
    assert universe["filters"] == {"reason": "integration-test"}
    assert universe["created_at"]


def test_twse_openapi_inventory_loaded_from_swagger():
    res = client.get("/api/twse/openapi/inventory")
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] >= 100
    assert data["by_tag"]["證券交易"] >= 30


def test_twse_openapi_generic_fetch():
    res = client.get("/api/twse/openapi/fetch", params={"path": "/exchangeReport/STOCK_DAY_ALL", "limit": 1})
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"]["path"] == "/exchangeReport/STOCK_DAY_ALL"
    assert data["total_rows"] > 0
    assert len(data["data"]) == 1


def test_realtime_status_free_twse_mis_enabled():
    res = client.get("/api/realtime/status")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "twse_mis"
    assert data["enabled"] is True
    assert data["update_interval_ms"] == 5000
    assert data["quote_schema_version"] == "stock_ai.realtime_quote.v1"
    assert data["stream_schema_version"] == "stock_ai.realtime_stream_event.v1"
    assert "trading_status" in data["capabilities"]
    assert "continuous_updates" in data["capabilities"]


def test_realtime_quote_twse_mis_2330():
    res = client.get("/api/realtime/quote/2330")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "twse_mis"
    assert data["data"]["symbol"] == "2330"
    assert data["data"]["name"] == "台積電"
    quote = data["data"]
    assert data["schema_version"] == "stock_ai.realtime_quote.v1"
    assert quote["schema_version"] == "stock_ai.realtime_quote.v1"
    assert quote["reference_price"] > 0
    assert quote["trading_status"] in {
        "pre_open",
        "trading",
        "closing_auction",
        "closed",
    }
    assert quote["best_bid"] == (quote["bids"][0] if quote["bids"] else None)
    assert quote["best_ask"] == (quote["asks"][0] if quote["asks"] else None)
    if quote["last_price"] is not None:
        assert quote["bids"]
        assert quote["asks"]
    else:
        assert quote["raw"]["z"] == "-"


def test_realtime_quote_provider_outage_is_explicit_degraded_state(monkeypatch):
    from stock_ai import main

    async def boom(symbol: str):
        raise main.RealtimeProviderError("MIS temporary outage")

    monkeypatch.setattr(main, "fetch_realtime_quote", boom)
    res = client.get("/api/realtime/quote/2317.TW")
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is False
    assert data["degraded"] is True
    assert data["data"] is None


def test_intraday_candle_api_rebuilds_selected_day_and_timeframe(
    tmp_path,
    monkeypatch,
):
    from stock_ai.intraday_candles import (
        FUGLE_SOURCE,
        clear_intraday_candle_store_cache,
        get_intraday_candle_store,
    )

    monkeypatch.setenv(
        "STOCK_AI_MARKET_DATA_DB", str(tmp_path / "intraday-api.sqlite")
    )
    clear_intraday_candle_store_cache()
    store = get_intraday_candle_store()
    start = datetime.fromisoformat("2026-07-24T09:00:00+08:00")
    rows = [
        {
            "symbol": "2330.TW",
            "bucket_start": (start + timedelta(minutes=index)).isoformat(),
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100.5 + index,
            "volume": index + 1,
            "is_final": True,
            "raw": {"serial": index},
        }
        for index in range(5)
    ]
    store.record_candles(
        rows,
        source=FUGLE_SOURCE,
        is_complete=True,
        response_payload={"data": rows},
    )

    unified = client.get(
        "/api/data/ui/v1/intraday/candles/2330.TW",
        params={"date": "2026-07-24", "timeframe": 5},
    )
    compatibility = client.get(
        "/api/intraday/candles/2330.TW",
        params={"date": "2026-07-24", "timeframe": 5},
    )
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json()
    payload = unified.json()
    assert payload["schema_version"] == "stock_ai.intraday_candles.v1"
    assert payload["reconstruction_status"] == "complete"
    assert payload["timeframe_minutes"] == 5
    assert payload["source_one_minute_count"] == 5
    assert payload["candle_count"] == 1
    assert payload["points"][0]["open"] == 100
    assert payload["points"][0]["close"] == 104.5

    dates = client.get(
        "/api/data/ui/v1/intraday/candles/2330.TW/dates"
    )
    status = client.get("/api/data/ui/v1/intraday/candles/status")
    invalid = client.get(
        "/api/data/ui/v1/intraday/candles/2330.TW",
        params={"date": "2026-07-24", "timeframe": 10},
    )
    assert dates.status_code == status.status_code == 200
    assert dates.json()["dates"][0]["trading_date"] == "2026-07-24"
    assert status.json()["supported_timeframes"] == [1, 5, 15, 30, 60]
    assert invalid.status_code == 400
    clear_intraday_candle_store_cache()


def test_sources_registry_endpoint():
    res = client.get("/api/sources")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 5
    ids = {item["id"] for item in data["items"]}
    assert "twse_mis" in ids
    assert "google_news" in ids


def test_system_requirement_contract_projects_goal_objective():
    res = client.get("/api/system/requirements")
    assert res.status_code == 200
    data = res.json()

    assert data["schema_version"] == "stock_ai.requirement_contract.v1"
    assert data["coverage"]["source_tier_count"] == 3
    assert data["coverage"]["data_module_count"] == 14
    assert data["coverage"]["app_feature_count"] == 13
    assert data["coverage"]["schedule_window_count"] == 5
    assert data["coverage"]["mvp_phase_count"] == 3
    assert data["coverage"]["live_ordering_enabled"] is False
    assert data["coverage"]["broker_api_connected"] is False
    assert data["coverage"]["orders_executions_enabled"] is False
    assert data["coverage"]["schedule_guard_schema"] == "stock_ai.schedule_guard.v1"
    assert data["coverage"]["schedule_guard_endpoint"] == "/api/system/schedule"
    assert data["coverage"]["source_policy_schema"] == "stock_ai.source_policy.v1"
    assert data["coverage"]["source_policy_endpoint"] == "/api/system/source-policy"
    assert data["coverage"]["source_policy_evaluate_endpoint"] == "/api/system/source-policy/evaluate"
    assert data["coverage"]["update_runner_schema"] == "stock_ai.update_runner.v1"
    assert data["coverage"]["update_plan_endpoint"] == "/api/system/update-plan"
    assert data["coverage"]["update_run_endpoint"] == "/api/system/update-run"
    assert data["coverage"]["official_derivatives_schema"] == "stock_ai.official_derivatives.v1"
    assert data["coverage"]["official_derivatives_endpoint"] == "/api/system/official-derivatives"
    assert data["coverage"]["official_events_schema"] == "stock_ai.official_events.v1"
    assert data["coverage"]["official_events_endpoint"] == "/api/system/official-events"
    assert data["coverage"]["notification_send_endpoint"] == "/api/notifications/send"
    assert data["coverage"]["mops_company_events_endpoint"] == "/api/official/mops/company-events"
    assert data["coverage"]["mops_company_events_import_endpoint"] == "/api/official/mops/company-events/import"
    assert data["coverage"]["mops_company_events_csv_import_endpoint"] == "/api/official/mops/company-events/import-csv"
    assert data["coverage"]["tdcc_holding_distribution_endpoint"] == "/api/official/tdcc/holding-distribution"
    assert data["coverage"]["tdcc_holding_distribution_import_endpoint"] == "/api/official/tdcc/holding-distribution/import"
    assert data["coverage"]["tdcc_holding_distribution_csv_import_endpoint"] == "/api/official/tdcc/holding-distribution/import-csv"
    assert data["coverage"]["taifex_derivatives_endpoint"] == "/api/official/taifex/derivatives-summary"
    assert data["coverage"]["taifex_derivatives_import_endpoint"] == "/api/official/taifex/derivatives-summary/import"
    assert data["coverage"]["taifex_derivatives_csv_import_endpoint"] == "/api/official/taifex/derivatives-summary/import-csv"

    tiers = {item["name"]: item for item in data["source_tiers"]}
    assert set(tiers) == {"realtime_trading", "official_public", "auxiliary"}
    assert "正式券商 API" in tiers["realtime_trading"]["priority_sources"]
    assert "TWSE OpenAPI" in tiers["official_public"]["priority_sources"]
    assert "新聞資料不可當作價格資料唯一來源" in tiers["auxiliary"]["guardrail"]

    modules = {item["name"]: item for item in data["data_modules"]}
    assert set(modules) == {
        "securities_master",
        "real_time_quotes",
        "intraday_candles",
        "ohlcv",
        "liquidity",
        "market_indices",
        "institutional_investors",
        "chip_data",
        "fundamentals",
        "company_events",
        "news",
        "portfolio",
        "orders_executions",
        "ai_signals",
    }
    assert modules["orders_executions"]["implementation"]["status"] == "disabled_until_broker_api"
    assert modules["ai_signals"]["implementation"]["schema"] == "open_stock_ai.trading_signal.v1"
    assert modules["chip_data"]["implementation"]["tdcc_api"] == "/api/official/tdcc/holding-distribution"
    assert modules["chip_data"]["implementation"]["tdcc_import_api"] == "/api/official/tdcc/holding-distribution/import"
    assert modules["chip_data"]["implementation"]["tdcc_csv_import_api"] == "/api/official/tdcc/holding-distribution/import-csv"
    assert modules["company_events"]["implementation"]["mops_api"] == "/api/official/mops/company-events"
    assert modules["company_events"]["implementation"]["mops_import_api"] == "/api/official/mops/company-events/import"
    assert modules["company_events"]["implementation"]["mops_csv_import_api"] == "/api/official/mops/company-events/import-csv"
    assert modules["market_indices"]["implementation"]["taifex_contract"] == "/api/official/taifex/derivatives-summary"

    feature_keys = {item["key"] for item in data["app_features"]}
    assert feature_keys == {
        "home",
        "watchlist",
        "stock_page",
        "monitor",
        "screener",
        "news_center",
        "chips",
        "fundamentals",
        "trading",
        "assistant",
        "risk",
        "assets",
        "notifications",
    }
    features = {item["key"]: item for item in data["app_features"]}
    assert features["notifications"]["status"] == "telegram_line_explicit_send_available"
    safety = {item["code"]: item for item in data["safety_limits"]}
    assert safety["no_auto_order_without_broker_api"]["status"] == "enforced"
    assert safety["risk_gate_required"]["status"] == "enforced"
    assert safety["no_mouse_broker_app_ordering"]["rule"] == "不可以用模擬滑鼠點券商 App 的方式下單。"


def test_system_schedule_guard_maps_windows_and_blocks_unlicensed_streaming():
    pre = client.get("/api/system/schedule", params={"as_of": "2026-07-10T08:30:00+08:00"})
    assert pre.status_code == 200
    pre_data = pre.json()
    assert pre_data["schema_version"] == "stock_ai.schedule_guard.v1"
    assert pre_data["active_phase"] == "pre_market"

    intraday = client.get("/api/system/schedule", params={"as_of": "2026-07-10T09:10:00+08:00"})
    assert intraday.status_code == 200
    data = intraday.json()
    assert data["active_phase"] == "intraday"
    intraday_phase = next(item for item in data["phases"] if item["phase"] == "intraday")
    streaming = next(item for item in intraday_phase["tasks"] if item["task"] == "使用授權行情或券商 API streaming")
    assert streaming["allowed"] is False
    assert streaming["blockers"] == ["authorized_realtime_feed_required"]
    assert data["live_ordering_allowed"] is False

    authorized = client.get(
        "/api/system/schedule",
        params={
            "as_of": "2026-07-10T09:10:00+08:00",
            "authorized_realtime_feed": "true",
        },
    )
    assert authorized.status_code == 200
    authorized_phase = next(item for item in authorized.json()["phases"] if item["phase"] == "intraday")
    streaming_ok = next(item for item in authorized_phase["tasks"] if item["task"] == "使用授權行情或券商 API streaming")
    assert streaming_ok["allowed"] is True


def test_system_source_policy_blocks_auxiliary_only_price_and_requires_decision_evidence():
    status = client.get("/api/system/source-policy")
    assert status.status_code == 200
    status_data = status.json()
    assert status_data["schema_version"] == "stock_ai.source_policy.v1"
    assert status_data["guardrails_enforced"]["auxiliary_source_cannot_be_sole_price_source"] is True
    assert status_data["guardrails_enforced"]["complete_decision_evidence_allowed"] is True
    assert status_data["guardrails_enforced"]["source_conflict_requires_note"] is True

    aux_price = client.post(
        "/api/system/source-policy/evaluate",
        json={
            "payload_type": "price",
            "last_price": 100,
            "data_sources": ["Yahoo Finance", "Google News"],
        },
    )
    assert aux_price.status_code == 200
    aux_data = aux_price.json()
    assert aux_data["price_policy"]["allowed"] is False
    assert "auxiliary_source_cannot_be_sole_price_source" in aux_data["price_policy"]["blockers"]
    assert aux_data["overall_allowed"] is False

    official_price = client.post(
        "/api/system/source-policy/evaluate",
        json={
            "payload_type": "price",
            "last_price": 100,
            "data_sources": ["TWSE MIS", "Yahoo Finance"],
        },
    )
    assert official_price.status_code == 200
    official_data = official_price.json()
    assert official_data["price_policy"]["allowed"] is True
    assert official_data["source_summary"]["tier_1"] == 1
    assert official_data["source_summary"]["tier_3"] == 1

    incomplete_decision = client.post(
        "/api/system/source-policy/evaluate",
        json={
            "payload_type": "decision",
            "technical_reason": "站上均線",
            "data_sources": ["TWSE MIS"],
            "conflicting_sources": True,
        },
    )
    assert incomplete_decision.status_code == 200
    incomplete = incomplete_decision.json()
    assert incomplete["decision_policy"]["allowed"] is False
    assert "missing:籌碼面理由" in incomplete["decision_policy"]["blockers"]
    assert incomplete["conflict_policy"]["allowed"] is False
    assert "source_conflict_requires_conflict_note" in incomplete["conflict_policy"]["blockers"]

    complete_decision = client.post(
        "/api/system/source-policy/evaluate",
        json={
            "payload_type": "decision",
            "technical_reason": "站上均線",
            "chip_reason": "法人買超",
            "fundamental_reason": "月營收成長",
            "news_reason": "無重大利空",
            "risk": "波動偏高",
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 110,
            "invalid_condition": "跌破支撐",
            "confidence": 0.7,
            "confidence_type": "rule_score",
            "confidence_calibrated": False,
            "data_timestamp": "2026-07-10T09:05:00+08:00",
            "data_sources": ["TWSE MIS", "MOPS"],
            "conflicting_sources": True,
            "conflict_note": "盤中價格與新聞方向不同，先降低部位。",
        },
    )
    assert complete_decision.status_code == 200
    complete = complete_decision.json()
    assert complete["decision_policy"]["allowed"] is True
    assert complete["conflict_policy"]["allowed"] is True
    assert complete["overall_allowed"] is True


def test_system_update_runner_builds_dry_run_plan_and_blocks_unlicensed_realtime():
    intraday = client.get("/api/system/update-plan", params={"as_of": "2026-07-10T09:10:00+08:00"})
    assert intraday.status_code == 200
    data = intraday.json()
    assert data["schema_version"] == "stock_ai.update_runner.v1"
    assert data["dry_run"] is True
    assert data["phase_count"] == 5
    assert data["job_count"] == 18
    assert data["active_phase"] == "intraday"
    assert data["selected_job_count"] == 3
    intraday_phase = next(item for item in data["phases"] if item["phase"] == "intraday")
    streaming = next(item for item in intraday_phase["jobs"] if item["operation"] == "connect_authorized_realtime_stream")
    assert streaming["status"] == "blocked"
    assert "authorized_realtime_feed_required" in streaming["blockers"]
    assert streaming["dry_run"] is True

    authorized = client.get(
        "/api/system/update-plan",
        params={
            "as_of": "2026-07-10T09:10:00+08:00",
            "authorized_realtime_feed": "true",
        },
    )
    assert authorized.status_code == 200
    authorized_phase = next(item for item in authorized.json()["phases"] if item["phase"] == "intraday")
    streaming_ok = next(item for item in authorized_phase["jobs"] if item["operation"] == "connect_authorized_realtime_stream")
    assert streaming_ok["status"] == "ready"

    after_hours_run = client.post(
        "/api/system/update-run",
        params={"phase": "after_hours", "as_of": "2026-07-10T15:40:00+08:00"},
    )
    assert after_hours_run.status_code == 200
    run = after_hours_run.json()
    assert run["schema_version"] == "stock_ai.update_run.v1"
    assert run["dry_run"] is True
    assert run["mutated"] is False
    assert run["requested_phase"] == "after_hours"
    assert run["executed_job_count"] >= 3
    assert all(item["status"] in {"dry_run_ready", "blocked"} for item in run["jobs"])


def test_official_derivatives_contracts_are_explicit_and_non_live(tmp_path, monkeypatch):
    from stock_ai import official_derivatives

    monkeypatch.setattr(official_derivatives, "DEFAULT_DB_PATH", tmp_path / "empty_official_derivatives.sqlite")

    status = client.get("/api/system/official-derivatives")
    assert status.status_code == 200
    data = status.json()
    assert data["schema_version"] == "stock_ai.official_derivatives.v1"
    assert data["source_tier"] == 2
    assert data["dry_run"] is True
    assert data["live_trading_source"] is False
    assert data["connected_source_count"] == 0
    assert data["required_source_count"] == 2
    assert {item["key"] for item in data["sources"]} == {"tdcc_holding_distribution", "taifex_derivatives"}

    tdcc = client.get("/api/official/tdcc/holding-distribution", params={"symbol": "2330"})
    assert tdcc.status_code == 200
    tdcc_data = tdcc.json()
    assert tdcc_data["method"] == "tdcc_holding_distribution_contract"
    assert tdcc_data["symbol"] == "2330.TW"
    assert tdcc_data["connected"] is False
    assert tdcc_data["count"] == 0
    assert tdcc_data["sample_record"]["source"] == "TDCC official weekly holding distribution"
    assert "major_holder_1000_lot_ratio" in tdcc_data["required_fields"]
    assert tdcc_data["source"]["csv_import_endpoint"] == "/api/official/tdcc/holding-distribution/import-csv"

    taifex = client.get("/api/official/taifex/derivatives-summary")
    assert taifex.status_code == 200
    taifex_data = taifex.json()
    assert taifex_data["method"] == "taifex_derivatives_summary_contract"
    assert taifex_data["connected"] is False
    assert taifex_data["futures_institutional"]["sample_record"]["contract"] == "TX"
    assert "put_call_ratio" in taifex_data["put_call_ratio"]["required_fields"]
    assert taifex_data["source"]["csv_import_endpoint"] == "/api/official/taifex/derivatives-summary/import-csv"

    weekly = client.get("/api/system/update-plan", params={"phase": "weekly"})
    assert weekly.status_code == 200
    weekly_jobs = [job for phase in weekly.json()["phases"] for job in phase["jobs"] if job["selected_for_run"]]
    assert any("/api/official/tdcc/holding-distribution/import" in job["candidate_endpoints"] for job in weekly_jobs)
    assert any("/api/official/tdcc/holding-distribution/import-csv" in job["candidate_endpoints"] for job in weekly_jobs)


def test_mops_company_events_contract_and_imports_feed_market_events(tmp_path, monkeypatch):
    from stock_ai import official_events
    from stock_ai import services

    monkeypatch.setattr(official_events, "DEFAULT_DB_PATH", tmp_path / "official_events.sqlite")
    services.clear_market_event_caches()

    empty = client.get("/api/official/mops/company-events", params={"symbol": "2330"})
    assert empty.status_code == 200
    empty_data = empty.json()
    assert empty_data["schema_version"] == "stock_ai.official_events.v1"
    assert empty_data["source_tier"] == 2
    assert empty_data["live_trading_source"] is False
    assert empty_data["connected_source_count"] == 0
    assert empty_data["sources"][0]["csv_import_endpoint"] == "/api/official/mops/company-events/import-csv"

    imported = client.post(
        "/api/official/mops/company-events/import",
        json={
            "items": [
                {
                    "event_id": "mops-2330-1",
                    "symbol": "2330",
                    "name": "台積電",
                    "event_time": "2026-07-10T15:31:00+08:00",
                    "event_type": "重大訊息",
                    "title": "董事會決議資本支出",
                    "summary": "測試用 MOPS 重大訊息",
                    "source_url": "https://mops.twse.com.tw/mops/web/t05st01",
                }
            ]
        },
    )
    assert imported.status_code == 200
    imported_data = imported.json()
    assert imported_data["schema_version"] == "stock_ai.official_events_import.v1"
    assert imported_data["method"] == "mops_company_events_json_import"
    assert imported_data["imported_count"] == 1

    events = client.get("/api/market/2330.TW/events", params={"limit": 5})
    assert events.status_code == 200
    event_data = events.json()
    assert event_data["count"] >= 1
    assert event_data["items"][0]["event_id"] == "mops-2330-1"
    assert event_data["items"][0]["event_type"] == "material_event"
    assert event_data["items"][0]["source_url"].startswith("https://mops.twse.com.tw/")

    news = client.get("/api/news/center", params={"symbol": "2330.TW", "limit": 5})
    assert news.status_code == 200
    news_item = next(
        item
        for item in news.json()["items"]
        if item["news_id"] == "mops-2330-1"
    )
    assert news_item["title"] == "董事會決議資本支出"
    assert news_item["source"] == "MOPS"
    assert news_item["official_verified"] is True


def test_mops_company_events_csv_import_parses_official_download(tmp_path, monkeypatch):
    from stock_ai import official_events
    from stock_ai import services

    monkeypatch.setattr(official_events, "DEFAULT_DB_PATH", tmp_path / "official_events_csv.sqlite")
    services.clear_market_event_caches()
    csv_text = "\n".join(
        [
            "公司代號,公司名稱,發言日期,發言時間,事件類型,主旨,說明,網址",
            "2454,聯發科,2026-07-10,16:05:00,法說會,召開法人說明會,測試用法人說明會,https://mops.twse.com.tw/mops/web/t05st01",
        ]
    )
    imported = client.post("/api/official/mops/company-events/import-csv", json={"text": csv_text})
    assert imported.status_code == 200
    imported_data = imported.json()
    assert imported_data["method"] == "mops_company_events_csv_import"
    assert imported_data["parsed_row_count"] == 1
    assert imported_data["imported_count"] == 1

    status = client.get("/api/official/mops/company-events", params={"symbol": "2454.TW"})
    assert status.status_code == 200
    item = status.json()["mops_company_events"]["items"][0]
    assert item["event_type"] == "earnings_call"
    assert item["related_symbols"] == ["2454.TW"]
    assert item["title"] == "召開法人說明會"


def test_official_derivatives_import_apis_persist_to_local_cache(tmp_path, monkeypatch):
    from stock_ai import official_derivatives

    monkeypatch.setattr(official_derivatives, "DEFAULT_DB_PATH", tmp_path / "official_derivatives.sqlite")

    tdcc_import = client.post(
        "/api/official/tdcc/holding-distribution/import",
        json={
            "items": [
                {
                    "report_date": "2026-07-03",
                    "symbol": "2330",
                    "name": "台積電",
                    "total_holders": 1200000,
                    "total_shares": 25932000000,
                    "major_holder_1000_lot_ratio": 72.5,
                    "holder_400_lot_ratio": 81.2,
                    "small_shareholder_count": 980000,
                    "concentration_score": 84.0,
                    "source": "TDCC official weekly holding distribution",
                }
            ]
        },
    )
    assert tdcc_import.status_code == 200
    tdcc_import_data = tdcc_import.json()
    assert tdcc_import_data["schema_version"] == "stock_ai.official_derivatives_import.v1"
    assert tdcc_import_data["mutated"] is True
    assert tdcc_import_data["imported_count"] == 1

    tdcc = client.get("/api/official/tdcc/holding-distribution", params={"symbol": "2330.TW"})
    assert tdcc.status_code == 200
    tdcc_data = tdcc.json()
    assert tdcc_data["connected"] is True
    assert tdcc_data["status"] == "cache_available"
    assert tdcc_data["count"] == 1
    assert tdcc_data["items"][0]["symbol"] == "2330.TW"
    assert tdcc_data["items"][0]["major_holder_1000_lot_ratio"] == 72.5

    taifex_import = client.post(
        "/api/official/taifex/derivatives-summary/import",
        json={
            "futures_institutional": [
                {
                    "trade_date": "2026-07-03",
                    "contract": "TX",
                    "product_name": "臺股期貨",
                    "foreign_long": 10000,
                    "foreign_short": 9000,
                    "investment_trust_long": 1200,
                    "investment_trust_short": 800,
                    "dealer_long": 5000,
                    "dealer_short": 5300,
                    "open_interest": 88000,
                    "source": "TAIFEX official futures institutional open interest",
                }
            ],
            "put_call_ratio": [
                {
                    "trade_date": "2026-07-03",
                    "put_volume": 200000,
                    "call_volume": 180000,
                    "put_call_ratio": 111.1,
                    "put_open_interest": 300000,
                    "call_open_interest": 250000,
                    "open_interest_put_call_ratio": 120.0,
                    "source": "TAIFEX official put/call ratio",
                }
            ],
        },
    )
    assert taifex_import.status_code == 200
    taifex_import_data = taifex_import.json()
    assert taifex_import_data["imported_futures_count"] == 1
    assert taifex_import_data["imported_put_call_count"] == 1

    taifex = client.get("/api/official/taifex/derivatives-summary")
    assert taifex.status_code == 200
    taifex_data = taifex.json()
    assert taifex_data["connected"] is True
    assert taifex_data["futures_institutional"]["items"][0]["contract"] == "TX"
    assert taifex_data["put_call_ratio"]["items"][0]["put_call_ratio"] == 111.1

    status = client.get("/api/system/official-derivatives")
    assert status.status_code == 200
    status_data = status.json()
    assert status_data["connected_source_count"] == 2


def test_official_derivatives_csv_import_apis_parse_official_downloads(tmp_path, monkeypatch):
    from stock_ai import official_derivatives

    monkeypatch.setattr(official_derivatives, "DEFAULT_DB_PATH", tmp_path / "official_derivatives_csv.sqlite")

    tdcc_csv = "\n".join(
        [
            "資料日期,股票代號,名稱,總股東人數,總股數,千張大戶比例,400張以上比例,小股東人數,集中度分數",
            "2026-07-03,2330,台積電,\"1,200,000\",\"25,932,000,000\",72.5%,81.2%,980000,84",
        ]
    )
    tdcc_import = client.post(
        "/api/official/tdcc/holding-distribution/import-csv",
        json={"text": tdcc_csv},
    )
    assert tdcc_import.status_code == 200
    tdcc_import_data = tdcc_import.json()
    assert tdcc_import_data["method"] == "tdcc_holding_distribution_csv_import"
    assert tdcc_import_data["input_format"] == "csv_or_tsv"
    assert tdcc_import_data["parsed_row_count"] == 1
    assert tdcc_import_data["imported_count"] == 1

    tdcc = client.get("/api/official/tdcc/holding-distribution", params={"symbol": "2330"})
    assert tdcc.status_code == 200
    tdcc_item = tdcc.json()["items"][0]
    assert tdcc_item["symbol"] == "2330.TW"
    assert tdcc_item["total_holders"] == 1200000
    assert tdcc_item["total_shares"] == 25932000000
    assert tdcc_item["major_holder_1000_lot_ratio"] == 72.5

    futures_tsv = "\n".join(
        [
            "交易日期\t契約\t商品名稱\t外資多單\t外資空單\t投信多單\t投信空單\t自營商多單\t自營商空單\t未平倉",
            "2026-07-03\tTX\t臺股期貨\t10,000\t9,000\t1,200\t800\t5,000\t5,300\t88,000",
        ]
    )
    put_call_csv = "\n".join(
        [
            "交易日期,賣權成交量,買權成交量,PutCallRatio,賣權未平倉,買權未平倉,未平倉PutCallRatio",
            "2026-07-03,\"200,000\",\"180,000\",111.1%,\"300,000\",\"250,000\",120.0%",
        ]
    )
    taifex_import = client.post(
        "/api/official/taifex/derivatives-summary/import-csv",
        json={"futures_institutional_text": futures_tsv, "put_call_ratio_text": put_call_csv},
    )
    assert taifex_import.status_code == 200
    taifex_import_data = taifex_import.json()
    assert taifex_import_data["method"] == "taifex_derivatives_csv_import"
    assert taifex_import_data["parsed_futures_row_count"] == 1
    assert taifex_import_data["parsed_put_call_row_count"] == 1
    assert taifex_import_data["imported_futures_count"] == 1
    assert taifex_import_data["imported_put_call_count"] == 1

    taifex = client.get("/api/official/taifex/derivatives-summary")
    assert taifex.status_code == 200
    taifex_data = taifex.json()
    assert taifex_data["futures_institutional"]["items"][0]["foreign_long"] == 10000
    assert taifex_data["futures_institutional"]["items"][0]["open_interest"] == 88000
    assert taifex_data["put_call_ratio"]["items"][0]["put_call_ratio"] == 111.1
    assert taifex_data["put_call_ratio"]["items"][0]["open_interest_put_call_ratio"] == 120.0


def test_goal_objective_data_module_schemas_are_instantiable():
    quote = RealTimeQuoteRecord(symbol="2330.TW", time="2026-07-10T09:01:00+08:00", bids=[{"price": 100.0, "volume": 10}], asks=[{"price": 100.5, "volume": 8}], source="broker_or_authorized_feed")
    kline = OHLCVRecord(symbol="2330.TW", period="1d", timestamp="2026-07-10", open=100, high=105, low=99, close=104, volume=1000, turnover=104000, vwap=103.2, source="TWSE")
    index = MarketIndexRecord(symbol="TAIEX", name="加權指數", category="broad", timestamp="2026-07-10", price=23000, source="TWSE")
    chip = ChipDataRecord(trade_date="2026-07-10", symbol="2330.TW", margin_balance=1000, short_balance=100, source="TWSE")
    tdcc = TDCCHoldingDistributionRecord(report_date="2026-07-05", symbol="2330.TW", major_holder_1000_lot_ratio=72.5, source="TDCC")
    taifex_flow = TaifexFuturesInstitutionalRecord(trade_date="2026-07-10", contract="TX", product_name="臺股期貨", open_interest=1000, source="TAIFEX")
    put_call = TaifexPutCallRatioRecord(trade_date="2026-07-10", put_call_ratio=95.2, source="TAIFEX")
    fundamentals = FundamentalsRecord(period="2026-06", symbol="2330.TW", monthly_revenue=1000000, revenue_yoy=10.5, source="MOPS")
    event = CompanyEventRecord(event_id="evt-1", symbol="2330.TW", event_time="2026-07-10T15:30:00+08:00", event_type="material_event", title="重大訊息", summary="測試", official_verified=True, source="MOPS")
    portfolio = PortfolioRecord(symbol="2330.TW", holding_shares=1000, cost=100.0, latest_price=104.0, mode="paper")
    order = OrderExecutionRecord(order_id="preview-1", symbol="2330.TW", side="buy", order_price=100.0, order_quantity=1000, order_status="preview")
    signal = TradingSignalRecord(
        symbol="2330.TW",
        signal_time="2026-07-10T09:05:00+08:00",
        signal_type="swing",
        action="watch",
        confidence=0.7,
        technical_reason="站上均線",
        chip_reason="法人買超",
        fundamental_reason="月營收成長",
        news_reason="無重大利空",
        risk="波動偏高",
        invalid_condition="跌破支撐",
        data_sources=["TWSE", "MOPS"],
        data_timestamp="2026-07-10T09:05:00+08:00",
    )

    assert quote.bids[0].price == 100.0
    assert kline.vwap == 103.2
    assert index.category == "broad"
    assert chip.margin_balance == 1000
    assert tdcc.official_public_data is True
    assert taifex_flow.contract == "TX"
    assert put_call.put_call_ratio == 95.2
    assert fundamentals.source == "MOPS"
    assert event.official_verified is True
    assert portfolio.mode == "paper"
    assert order.broker_api_required is True
    assert signal.data_sources == ["TWSE", "MOPS"]


def test_overview_indices_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_overview_indices",
        lambda: {
            "count": 2,
            "items": [
                {"symbol": "^TWII", "label": "加權指數", "priceText": "22,001.23", "changeText": "+0.56%", "changeClass": "tw-red", "source": "Yahoo Finance latest-available", "available": True},
                {"symbol": "^SOX", "label": "費半", "priceText": "-", "changeText": "-", "changeClass": "tw-green", "source": "暫時無法取得：ValueError", "available": False},
            ],
        },
    )
    res = client.get("/api/overview/indices")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 2
    assert data["items"][0]["symbol"] == "^TWII"
    assert data["items"][1]["available"] is False


def test_market_events_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_market_events",
        lambda symbol, limit=20: [
            EventItem(
                event_id="evt-1",
                event_time="2026-07-02T09:00:00+08:00",
                related_symbols=[symbol],
                event_type="news",
                title="測試新聞",
                summary="多來源聚合測試",
                sentiment="neutral",
                estimated_impact_direction="mixed",
                confidence=0.5,
                source_url="https://example.com/news",
            )
        ],
    )
    res = client.get("/api/market/2330.TW/events")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["items"][0]["title"] == "測試新聞"


def test_market_overview_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_market_overview",
        lambda symbol: {
            "symbol": symbol,
            "summary": {"entity": {"symbol": symbol}},
            "history": [{"date": "2026-07-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
            "events": [{"event_id": "evt-1", "title": "測試事件"}],
            "sources": {"count": 2, "items": [{"id": "twse_mis"}, {"id": "google_news"}]},
        },
    )
    res = client.get("/api/market/2330.TW/overview")
    assert res.status_code == 200
    data = res.json()
    assert data["symbol"] == "2330.TW"
    assert data["summary"]["entity"]["symbol"] == "2330.TW"
    assert data["sources"]["count"] == 2


def test_securities_master_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "securities_master_status",
        lambda: {"count": 1, "by_exchange": {"TWSE": 1, "TPEx": 0}},
    )

    monkeypatch.setattr(
        main,
        "list_securities_master",
        lambda q="", market="all", limit=200, **_kwargs: [
            SecurityMasterItem(
                symbol="2330.TW",
                name="台積電",
                market="taiwan",
                exchange="TWSE",
                listing_type="listed",
                industry="半導體",
                trade_unit=1000,
                day_trade_eligible=None,
                margin_eligible=True,
                short_eligible=True,
                is_etf=False,
                is_warrant=False,
                list_date="1994-09-05",
                trading_status="active",
                source="official",
            )
        ],
    )
    res = client.get("/api/securities/master?q=2330")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["items"][0]["symbol"] == "2330.TW"


def test_institutional_flow_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "list_institutional_flows",
        lambda symbol=None, date=None, limit=100: [
            InstitutionalFlowItem(
                trade_date="2026-07-02",
                symbol="2330.TW",
                name="台積電",
                foreign_buy=10,
                foreign_sell=5,
                foreign_net=5,
                foreign_dealer_buy=0,
                foreign_dealer_sell=0,
                foreign_dealer_net=0,
                trust_buy=2,
                trust_sell=1,
                trust_net=1,
                dealer_buy=3,
                dealer_sell=1,
                dealer_net=2,
                dealer_hedge_net=1,
                total_institutional_net=8,
                source="TWSE official T86 daily institutional flow",
            )
        ],
    )
    res = client.get("/api/flow/institutional?symbol=2330.TW")
    assert res.status_code == 200
    data = res.json()
    assert data["items"][0]["total_institutional_net"] == 8


def test_margin_and_revenue_endpoints(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "list_margin_trading",
        lambda symbol=None, limit=100: [
            MarginTradingItem(
                trade_date="2026-07-02",
                symbol="2330.TW",
                name="台積電",
                margin_buy=100,
                margin_sell=80,
                margin_cash_redemption=0,
                margin_previous_balance=1000,
                margin_balance=1020,
                margin_limit=10000,
                short_buy=10,
                short_sell=12,
                short_cash_redemption=0,
                short_previous_balance=100,
                short_balance=98,
                short_limit=5000,
                offsetting=0,
                note=None,
                source="TWSE OpenAPI /exchangeReport/MI_MARGN",
            )
        ],
    )
    monkeypatch.setattr(
        main,
        "list_monthly_revenues",
        lambda symbol=None, limit=100: [
            RevenueItem(
                report_date="2026-06-17",
                period="2026-05",
                symbol="2330.TW",
                name="台積電",
                industry="半導體",
                current_revenue=1.0,
                previous_revenue=0.9,
                last_year_revenue=0.8,
                mom_change_percent=10.0,
                yoy_change_percent=25.0,
                ytd_revenue=5.0,
                last_ytd_revenue=4.0,
                ytd_change_percent=25.0,
                note=None,
                source="TWSE OpenAPI /opendata/t187ap05_L",
            )
        ],
    )
    margin = client.get("/api/flow/margin?symbol=2330.TW")
    revenue = client.get("/api/fundamentals/revenue?symbol=2330.TW")
    assert margin.status_code == 200
    assert revenue.status_code == 200
    assert margin.json()["items"][0]["margin_balance"] == 1020
    assert revenue.json()["items"][0]["period"] == "2026-05"


def test_daily_report_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "generate_daily_report",
        lambda universe, limit=5: DailyReport(
            generated_at="2026-07-02T18:00:00+08:00",
            title="量化規則每日報告",
            summary="測試摘要",
            picks=[
                DailySelectionItem(
                    symbol="2330.TW",
                    name="台積電",
                    score=3.0,
                    signal="buy",
                    reasons=["法人買超", "營收年增"],
                    risk_factors=["盤勢震盪"],
                    data_sources=["TWSE", "MOPS"],
                    event_count=2,
                )
            ],
            source_snapshot=["TWSE", "MOPS"],
            universe={"source": universe.source, "symbols": list(universe.symbols), "count": universe.count},
        ),
    )
    res = client.get("/api/reports/daily?symbols=2330.TW")
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "量化規則每日報告"
    assert data["picks"][0]["signal"] == "buy"
    assert data["universe"]["source"] == "explicit_symbols"


def test_watchlist_overview_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_watchlist_overview",
        lambda limit=10: {
            "count": 1,
            "items": [
                {
                    "symbol": "2330.TW",
                    "name": "台積電",
                    "exchange": "TWSE",
                    "industry": "半導體",
                    "latest_price": 1000.0,
                    "change_percent": 2.1,
                    "total_volume_lots": 12345,
                    "institutional_net": 5000,
                    "margin_balance": 1000,
                    "revenue_yoy": 12.5,
                    "latest_news_title": "測試新聞",
                    "latest_news_url": "https://example.com/news",
                    "alert_flags": ["漲跌幅異常", "法人偏多"],
                    "data_sources": ["TWSE MIS", "T86"],
                }
            ],
        },
    )
    res = client.get("/api/watchlist/overview?limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["items"][0]["alert_flags"][0] == "漲跌幅異常"


def test_watchlist_default_endpoint_is_neutral_empty_state():
    res = client.get("/api/watchlist/default?limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 0
    assert data["items"] == []
    assert data["universe"]["source"] == "none"


def test_news_center_endpoint(monkeypatch):
    from stock_ai import main

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        main,
        "get_news_center",
        lambda symbol=None, category=None, limit=20: captured.update({"symbol": symbol, "category": category, "limit": limit}) or {
            "count": 1,
            "items": [
                {
                    "news_id": "news-1",
                    "title": "測試新聞中心",
                    "source": "TWSE OpenAPI",
                    "published_at": "2026-07-02T09:00:00+08:00",
                    "related_symbols": ["2330.TW"],
                    "category": "company",
                    "sentiment": "neutral",
                    "impact": "mixed",
                    "credibility": 0.9,
                    "official_verified": True,
                    "summary": "測試摘要",
                    "source_url": "https://openapi.twse.com.tw/",
                }
            ],
            "category_counts": {"company": 1},
            "symbol_scope": ["2330.TW"],
        },
    )
    res = client.get("/api/news/center?symbol=2330.TW&category=company&limit=12")
    assert res.status_code == 200
    data = res.json()
    assert data["items"][0]["official_verified"] is True
    assert data["category_counts"]["company"] == 1
    assert captured == {"symbol": "2330.TW", "category": "company", "limit": 12}


def test_news_history_coverage_endpoint_is_read_only_and_uses_unified_route(monkeypatch):
    from stock_ai import main

    expected = {
        "schema_version": "stock_ai.news_history_coverage_status.v1",
        "coverage_receipt_required_for_pit": True,
        "provider_wide_historical_coverage_certified": False,
        "sources": [],
    }
    monkeypatch.setattr(main, "get_news_history_coverage_store", lambda: SimpleNamespace(status_summary=lambda: expected))

    unified = client.get("/api/data/ui/v1/news/history-coverage")
    compatibility = client.get("/api/news/history-coverage")

    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == expected


def test_news_cleaners_remove_source_tails_and_urls():
    from stock_ai.mvp_features import _clean_news_summary, _clean_news_title

    assert _clean_news_title("台積電法說會重點 - Yahoo 財經") == "台積電法說會重點"
    assert _clean_news_title("台股衝4萬8！送小孩一張台積電要繳稅了 - 三立新聞網SETN.com") == "台股衝4萬8！送小孩一張台積電要繳稅了"
    assert _clean_news_title("Anthropic explores Samsung 2nm chip partnership - The Information") == "Anthropic explores Samsung 2nm chip partnership"
    assert _clean_news_title("市場焦點整理 - 優分析UAnalyze") == "市場焦點整理"
    assert _clean_news_title("台股回檔觀察 - 今周刊") == "台股回檔觀察"
    assert _clean_news_title("官方公告 / https://www.twse.com.tw/zh/about/news") == "官方公告"
    assert _clean_news_summary("重點摘要 https://news.cnyes.com/news/id/12345 來源: 鉅亨網") == "重點摘要"
    assert _clean_news_summary("台股衝4萬8！送小孩一張台積電要繳稅了 來源: 三立新聞網SETN.com") == "台股衝4萬8！送小孩一張台積電要繳稅了"
    assert _clean_news_summary("Investing.com -- 晶片需求維持強勁") == "晶片需求維持強勁"


def test_build_news_item_keeps_clickable_summary_without_source_tail():
    from stock_ai.mvp_features import build_news_item

    event = SimpleNamespace(
        event_id="news-1",
        title="台積電營收創高 - Yahoo 財經",
        summary="台積電 6 月營收創高 https://example.com/full-story 來源: Yahoo 財經",
        event_type="news",
        sentiment="neutral",
        estimated_impact_direction="positive",
        event_time="2026-07-02T09:00:00+08:00",
        related_symbols=["2330.TW"],
        source_url="https://example.com/full-story",
    )

    item = build_news_item(event)
    assert item.title == "台積電營收創高"
    assert item.summary == "台積電 6 月營收創高"
    assert item.source_url == "https://example.com/full-story"


def test_build_news_item_extracts_google_news_anchor_text():
    from stock_ai.mvp_features import build_news_item

    event = SimpleNamespace(
        event_id="news-2",
        title="台股衝4萬8！送小孩一張台積電要繳稅了 - 三立新聞網SETN.com",
        summary='<a href="https://news.google.com/rss/articles/demo" target="_blank">台股衝4萬8！送小孩一張台積電要繳稅了 律師曝3招破解</a>&nbsp;&nbsp;<font color="#6f6f6f">三立新聞網SETN.com</font>',
        event_type="news",
        sentiment="neutral",
        estimated_impact_direction="mixed",
        event_time="2026-07-02T09:00:00+08:00",
        related_symbols=["2330.TW"],
        source_url="https://news.google.com/rss/articles/demo",
    )

    item = build_news_item(event)
    assert item.title == "台股衝4萬8！送小孩一張台積電要繳稅了"
    assert item.summary == "台股衝4萬8！送小孩一張台積電要繳稅了 律師曝3招破解"


def test_notification_endpoints(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_notification_channels",
        lambda: {
            "count": 2,
            "items": [
                {"channel": "telegram", "configured": False, "enabled": False, "mode": "preview_only", "target_hint": None, "note": "preview"},
                {"channel": "line", "configured": True, "enabled": False, "mode": "ready", "target_hint": "1234", "note": "preview"},
            ],
        },
    )
    monkeypatch.setattr(
        main,
        "get_notification_previews",
        lambda symbol=None: {
            "count": 1,
            "items": [
                {
                    "category": "daily_report",
                    "title": "AI 每日選股報告",
                    "body": "台積電 buy",
                    "channels": ["telegram", "line"],
                    "related_symbols": ["2330.TW"],
                    "dry_run": True,
                }
            ],
        },
    )
    channels = client.get("/api/notifications/channels")
    previews = client.get("/api/notifications/previews?symbol=2330.TW")
    assert channels.status_code == 200
    assert previews.status_code == 200
    assert channels.json()["items"][1]["mode"] == "ready"
    assert previews.json()["items"][0]["dry_run"] is True


def test_notification_send_dry_run_and_missing_credentials(monkeypatch):
    from stock_ai import mvp_features
    from stock_ai.config import get_settings

    called = {"count": 0}

    def fake_urlopen(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("dry-run and missing credentials must not call provider APIs")

    monkeypatch.setattr(mvp_features, "urlopen", fake_urlopen)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("LINE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINE_TARGET_ID", raising=False)
    get_settings.cache_clear()
    try:
        dry_run = client.post(
            "/api/notifications/send",
            json={"title": "測試", "body": "台積電提醒", "channels": ["telegram", "line"], "related_symbols": ["2330.TW"], "dry_run": True},
        )
        missing = client.post(
            "/api/notifications/send",
            json={"title": "測試", "body": "台積電提醒", "channels": ["telegram"], "dry_run": False},
        )
    finally:
        get_settings.cache_clear()

    assert dry_run.status_code == 200
    assert dry_run.json()["schema_version"] == "stock_ai.notification_delivery.v1"
    assert dry_run.json()["attempted_count"] == 0
    assert {item["mode"] for item in dry_run.json()["items"]} == {"dry_run"}
    assert missing.status_code == 200
    assert missing.json()["items"][0]["mode"] == "not_configured"
    assert called["count"] == 0


def test_notification_send_live_uses_configured_provider(monkeypatch):
    from stock_ai import mvp_features
    from stock_ai.config import get_settings

    requests = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=8):
        requests.append((req.full_url, req.headers, timeout))
        return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-4567")
    monkeypatch.setattr(mvp_features, "urlopen", fake_urlopen)
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/notifications/send",
            json={"title": "盤前報告", "body": "測試訊息", "channels": ["telegram"], "related_symbols": ["2330.TW"], "dry_run": False},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    data = response.json()
    assert data["attempted_count"] == 1
    assert data["sent_count"] == 1
    assert data["items"][0]["sent"] is True
    assert data["items"][0]["target_hint"] == "4567"
    assert requests[0][0] == "https://api.telegram.org/bottoken-123/sendMessage"


def test_numeric_parsers_tolerate_none_strings():
    from stock_ai.taiwan_official import _float, _int

    assert _int("None") == 0
    assert _int("null") == 0
    assert _float("None") == 0.0


def test_trading_preview_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_trading_workspace",
        lambda symbol="2330.TW", side="buy", quantity_lots=1: TradingWorkspace(
            meta=ReadonlyWorkspaceMeta(
                generated_at="2026-07-03T09:00:00+08:00",
                data_sources=["TWSE MIS", "local_preview_portfolio"],
                limitations=["preview only"],
                fallback={"reference_price": "latest_available_close"},
            ),
            broker_status=BrokerConnectionStatus(
                provider_name="Broker API not connected",
                connected=False,
                can_submit_orders=False,
                mode="preview_only",
                note="僅供預覽",
            ),
            preview=TradingPreview(
                symbol=symbol,
                name="台積電",
                side=side,
                quantity_lots=quantity_lots,
                quantity_shares=quantity_lots * 1000,
                reference_price=1000.0,
                estimated_fill_price=1000.0,
                available_cash_before=500000.0,
                available_cash_after=398575.0,
                estimated_costs=OrderCostEstimate(
                    gross_amount=100000.0,
                    estimated_fee=85.5,
                    estimated_tax=0.0,
                    estimated_total=100085.5,
                ),
                limitation_note="未接券商 API",
                fallback_fields={"broker_submission": "disabled"},
            ),
            risk_summary=RiskSummary(
                single_trade_risk_percent=8.0,
                daily_risk_percent=4.2,
                position_concentration_percent=18.0,
                industry_exposure_percent=32.0,
                max_single_trade_risk_percent=10.0,
                max_daily_risk_percent=12.0,
                max_position_concentration_percent=25.0,
                max_industry_exposure_percent=45.0,
                order_allowed=True,
                alerts=[RiskAlert(code="preview_only", level="info", title="唯讀", message="僅供預覽")],
            ),
            asset_summary=AssetSummary(
                total_assets=1500000.0,
                cash_available=500000.0,
                holdings_market_value=1000000.0,
                today_pnl=12000.0,
                unrealized_pnl=80000.0,
                realized_pnl=20000.0,
                dividend_income=5000.0,
                note="preview",
            ),
            positions=[
                PositionDetail(
                    symbol="2330.TW",
                    name="台積電",
                    industry="半導體",
                    quantity_shares=1000,
                    average_cost=920.0,
                    latest_price=1000.0,
                    market_value=1000000.0,
                    unrealized_pnl=80000.0,
                    unrealized_pnl_percent=8.7,
                    weight_percent=100.0,
                    data_sources=["TWSE MIS"],
                )
            ],
        ),
    )
    res = client.get("/api/trading/preview?symbol=2330.TW&side=buy&quantity_lots=1")
    assert res.status_code == 200
    data = res.json()
    assert data["broker_status"]["can_submit_orders"] is False
    assert data["preview"]["symbol"] == "2330.TW"
    assert data["preview"]["estimated_costs"]["estimated_total"] == 100085.5
    assert data["risk_summary"]["alerts"][0]["code"] == "preview_only"


def test_trading_preview_rejects_invalid_side():
    res = client.get("/api/trading/preview?symbol=2330.TW&side=hold&quantity_lots=1")
    assert res.status_code == 400
    assert res.json()["detail"] == "side must be buy or sell"


def test_trading_assistant_endpoint(monkeypatch):
    from stock_ai import main

    monkeypatch.setattr(
        main,
        "get_ai_trading_workspace",
        lambda symbol=None: AITradingAssistantWorkspace(
            meta=ReadonlyWorkspaceMeta(
                generated_at="2026-07-03T09:00:00+08:00",
                data_sources=["daily_report", "watchlist_overview", "notification_previews"],
                limitations=["preview only"],
                fallback={"market_snapshot": "watchlist fallback"},
            ),
            focus_symbol=symbol or "2330.TW",
            report_title="量化規則每日報告",
            cards=[
                AITradePlanCard(
                    session="pre_market",
                    symbol=symbol or "2330.TW",
                    name="台積電",
                    action_bias="buy",
                    rule_score=0.72,
                    technical_reasons=["分數高"],
                    flow_reasons=["法人偏多"],
                    fundamental_reasons=["營收年增"],
                    event_reasons=["法說會"],
                    risk_reasons=["盤勢震盪"],
                    data_sources=["daily_report"],
                    as_of="2026-07-03T08:30:00+08:00",
                    conflict_note=None,
                )
            ],
            notification_previews=[
                NotificationPreview(
                    category="daily_report",
                    title="量化規則每日報告",
                    body="台積電 buy",
                    channels=["telegram", "line"],
                    related_symbols=["2330.TW"],
                    dry_run=True,
                )
            ],
            watchlist_items=[{"symbol": "2330.TW", "alert_flags": ["法人偏多"]}],
            market_snapshot={"symbol": symbol or "2330.TW", "source": "TWSE MIS", "change_percent": 1.2},
        ),
    )
    res = client.get("/api/trading/assistant?symbol=2330.TW")
    assert res.status_code == 200
    data = res.json()
    assert data["focus_symbol"] == "2330.TW"
    assert data["cards"][0]["action_bias"] == "buy"
    assert data["notification_previews"][0]["dry_run"] is True


def test_i_j_k_l_workspace_contracts_include_status_sources_and_timestamps(monkeypatch):
    from stock_ai import main

    workspace_meta = ReadonlyWorkspaceMeta(
        generated_at="2026-07-03T09:00:00+08:00",
        data_sources=["TWSE MIS", "local_preview_portfolio"],
        limitations=["preview only"],
        fallback={"reference_price": "latest_available_close"},
    )
    asset_workspace = AssetWorkspace(
        meta=workspace_meta,
        summary=AssetSummary(
            total_assets=1200000.0,
            cash_available=300000.0,
            holdings_market_value=900000.0,
            today_pnl=6000.0,
            unrealized_pnl=50000.0,
            realized_pnl=22000.0,
            dividend_income=4800.0,
            note="preview",
        ),
        positions=[
            PositionDetail(
                symbol="2330.TW",
                name="台積電",
                industry="半導體",
                quantity_shares=1000,
                average_cost=950.0,
                latest_price=1000.0,
                market_value=1000000.0,
                unrealized_pnl=50000.0,
                unrealized_pnl_percent=5.26,
                weight_percent=100.0,
                data_sources=["TWSE MIS"],
            )
        ],
    )
    monkeypatch.setattr(
        main,
        "get_trading_workspace",
        lambda symbol="2330.TW", side="buy", quantity_lots=1: TradingWorkspace(
            meta=workspace_meta,
            broker_status=BrokerConnectionStatus(
                provider_name="Broker API not connected",
                connected=False,
                can_submit_orders=False,
                mode="preview_only",
                note="僅供預覽",
            ),
            preview=TradingPreview(
                symbol=symbol,
                name="台積電",
                side=side,
                quantity_lots=quantity_lots,
                quantity_shares=quantity_lots * 1000,
                reference_price=1000.0,
                estimated_fill_price=1000.0,
                available_cash_before=500000.0,
                available_cash_after=398575.0,
                estimated_costs=OrderCostEstimate(
                    gross_amount=100000.0,
                    estimated_fee=85.5,
                    estimated_tax=0.0,
                    estimated_total=100085.5,
                ),
                limitation_note="未接券商 API",
                fallback_fields={"broker_submission": "disabled"},
            ),
            risk_summary=RiskSummary(
                single_trade_risk_percent=8.0,
                daily_risk_percent=4.2,
                position_concentration_percent=18.0,
                industry_exposure_percent=32.0,
                max_single_trade_risk_percent=10.0,
                max_daily_risk_percent=12.0,
                max_position_concentration_percent=25.0,
                max_industry_exposure_percent=45.0,
                order_allowed=True,
                alerts=[RiskAlert(code="preview_only", level="info", title="唯讀", message="僅供預覽")],
            ),
            asset_summary=asset_workspace.summary,
            positions=asset_workspace.positions,
        ),
    )
    monkeypatch.setattr(
        main,
        "get_ai_trading_workspace",
        lambda symbol=None: AITradingAssistantWorkspace(
            meta=workspace_meta,
            focus_symbol=symbol or "2330.TW",
            report_title="量化規則每日報告",
            cards=[
                AITradePlanCard(
                    session="pre_market",
                    symbol=symbol or "2330.TW",
                    name="台積電",
                    action_bias="buy",
                    rule_score=0.72,
                    technical_reasons=["分數高"],
                    flow_reasons=["法人偏多"],
                    fundamental_reasons=["營收年增"],
                    event_reasons=["法說會"],
                    risk_reasons=["盤勢震盪"],
                    data_sources=["daily_report"],
                    as_of="2026-07-03T08:30:00+08:00",
                    conflict_note=None,
                )
            ],
            notification_previews=[
                NotificationPreview(
                    category="daily_report",
                    title="量化規則每日報告",
                    body="台積電 buy",
                    channels=["telegram", "line"],
                    related_symbols=["2330.TW"],
                    dry_run=True,
                )
            ],
            watchlist_items=[{"symbol": "2330.TW", "alert_flags": ["法人偏多"]}],
            market_snapshot={"symbol": symbol or "2330.TW", "source": "TWSE MIS", "change_percent": 1.2},
        ),
    )
    monkeypatch.setattr(
        main,
        "get_risk_workspace",
        lambda symbol="2330.TW", side="buy", quantity_lots=1: RiskWorkspace(
            meta=workspace_meta,
            summary=RiskSummary(
                single_trade_risk_percent=9.5,
                daily_risk_percent=5.2,
                position_concentration_percent=24.0,
                industry_exposure_percent=38.0,
                max_single_trade_risk_percent=10.0,
                max_daily_risk_percent=12.0,
                max_position_concentration_percent=25.0,
                max_industry_exposure_percent=45.0,
                order_allowed=True,
                alerts=[RiskAlert(code="preview_only", level="info", title="唯讀", message="僅供預覽")],
            ),
            preview_symbol=symbol,
            preview_side=side,
            position_snapshot=asset_workspace.positions,
        ),
    )
    monkeypatch.setattr(main, "get_asset_workspace", lambda limit=4: asset_workspace)

    trading = client.get("/api/trading/preview?symbol=2330.TW&side=buy&quantity_lots=1")
    assistant = client.get("/api/trading/assistant?symbol=2330.TW")
    risk = client.get("/api/risk/summary?symbol=2330.TW&side=buy&quantity_lots=1")
    assets_summary = client.get("/api/assets/summary?limit=5")
    assets_positions = client.get("/api/assets/positions?limit=5")

    for response in [trading, assistant, risk, assets_summary, assets_positions]:
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"]["status"] == "preview_only"
        assert payload["meta"]["broker_api_connected"] is False
        assert payload["meta"]["order_submission_enabled"] is False
        assert payload["meta"]["generated_at"] == "2026-07-03T09:00:00+08:00"
        assert payload["meta"]["data_sources"]

    trading_data = trading.json()
    assistant_data = assistant.json()
    risk_data = risk.json()
    assets_summary_data = assets_summary.json()
    assets_positions_data = assets_positions.json()

    assert trading_data["broker_status"]["can_submit_orders"] is False
    assert trading_data["preview"]["fallback_fields"]["broker_submission"] == "disabled"
    assert assistant_data["cards"][0]["data_sources"] == ["daily_report"]
    assert assistant_data["cards"][0]["as_of"] == "2026-07-03T08:30:00+08:00"
    assert risk_data["summary"]["alerts"][0]["code"] == "preview_only"
    assert assets_summary_data["summary"]["note"] == "preview"
    assert assets_positions_data["items"][0]["data_sources"] == ["TWSE MIS"]


def test_risk_summary_and_asset_endpoints(monkeypatch):
    from stock_ai import main

    asset_workspace = AssetWorkspace(
        meta=ReadonlyWorkspaceMeta(
            generated_at="2026-07-03T09:00:00+08:00",
            data_sources=["local_preview_portfolio"],
            limitations=["preview only"],
            fallback={"positions": "local_preview_templates"},
        ),
        summary=AssetSummary(
            total_assets=1200000.0,
            cash_available=300000.0,
            holdings_market_value=900000.0,
            today_pnl=6000.0,
            unrealized_pnl=50000.0,
            realized_pnl=22000.0,
            dividend_income=4800.0,
            note="preview",
        ),
        positions=[
            PositionDetail(
                symbol="2330.TW",
                name="台積電",
                industry="半導體",
                quantity_shares=1000,
                average_cost=950.0,
                latest_price=1000.0,
                market_value=1000000.0,
                unrealized_pnl=50000.0,
                unrealized_pnl_percent=5.26,
                weight_percent=100.0,
                data_sources=["TWSE MIS"],
            )
        ],
    )
    monkeypatch.setattr(
        main,
        "get_risk_workspace",
        lambda symbol="2330.TW", side="buy", quantity_lots=1: RiskWorkspace(
            meta=asset_workspace.meta,
            summary=RiskSummary(
                single_trade_risk_percent=9.5,
                daily_risk_percent=5.2,
                position_concentration_percent=24.0,
                industry_exposure_percent=38.0,
                max_single_trade_risk_percent=10.0,
                max_daily_risk_percent=12.0,
                max_position_concentration_percent=25.0,
                max_industry_exposure_percent=45.0,
                order_allowed=True,
                alerts=[RiskAlert(code="preview_only", level="info", title="唯讀", message="僅供預覽")],
            ),
            preview_symbol=symbol,
            preview_side=side,
            position_snapshot=asset_workspace.positions,
        ),
    )
    monkeypatch.setattr(main, "get_asset_workspace", lambda limit=4: asset_workspace)

    risk_res = client.get("/api/risk/summary?symbol=2330.TW&side=buy&quantity_lots=1")
    summary_res = client.get("/api/assets/summary?limit=5")
    positions_res = client.get("/api/assets/positions?limit=5")

    assert risk_res.status_code == 200
    assert summary_res.status_code == 200
    assert positions_res.status_code == 200
    assert risk_res.json()["summary"]["order_allowed"] is True
    assert summary_res.json()["positions_count"] == 1
    assert summary_res.json()["summary"]["cash_available"] == 300000.0
    assert positions_res.json()["items"][0]["symbol"] == "2330.TW"
