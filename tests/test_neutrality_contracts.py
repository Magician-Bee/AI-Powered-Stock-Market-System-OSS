from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from open_stock_ai.analysis_contracts import AnalysisProvenance, DecisionEnvelope
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.environment_snapshot import EnvironmentSnapshotBuilder
from open_stock_ai.agent_runtime.routing import UnifiedMultiIntentRouter
from open_stock_ai.config.settings import load_settings
from open_stock_ai.main import analyze_to_dict
from open_stock_ai.pipeline import SignalPipeline
from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from open_stock_ai.types import (
    MissingSymbolError,
    StockRequest,
    SymbolContext,
    UniverseRequest,
    UniverseSnapshot,
)
from stock_ai.main import daily_report, default_watchlist
from stock_ai.models import SecurityMasterItem
from stock_ai.query import answer_question, guess_symbol
from stock_ai.services import run_screener, search_entities
from stock_ai.universe import UniverseResolutionError, resolve_universe


def test_new_install_has_no_default_watchlist_or_batch_symbol(tmp_path: Path) -> None:
    config = tmp_path / "open_stock_ai.yaml"
    config.write_text("system:\n  mode: paper\n", encoding="utf-8")

    settings = load_settings(config)
    pipeline = SignalPipeline(  # type: ignore[arg-type]
        market_data=None,
        intelligence=None,
        strategy=None,
        research=None,
        risk=None,
        execution=None,
    )

    assert settings.watchlist == ()
    assert pipeline.batch_requests == ()
    assert default_watchlist()["items"] == []


def test_missing_symbol_is_preserved_and_symbol_required_operations_fail() -> None:
    context = SymbolContext()

    assert context.symbol is None
    assert context.source == "none"
    with pytest.raises(MissingSymbolError):
        context.require_symbol()
    with pytest.raises(MissingSymbolError):
        StockRequest(symbol="")
    with pytest.raises(MissingSymbolError):
        analyze_to_dict()


def test_empty_universe_is_valid_and_does_not_scan_or_report_picks() -> None:
    snapshot = resolve_universe(None)
    report = daily_report()

    assert snapshot.source == "none"
    assert snapshot.symbols == ()
    assert snapshot.count == 0
    assert run_screener(symbols=()) == []
    assert report["universe"]["source"] == "none"
    assert report["picks"] == []
    assert "未分析任何股票" in report["summary"]


def test_explicit_universe_records_source_filters_time_and_count() -> None:
    snapshot = resolve_universe(
        UniverseRequest(
            source="explicit_symbols",
            symbols=("AAA", "BBB", "AAA"),
            filters={"reason": "test"},
        )
    )

    assert snapshot.source == "explicit_symbols"
    assert snapshot.symbols == ("AAA", "BBB")
    assert snapshot.filters == {"reason": "test"}
    assert snapshot.created_at
    assert snapshot.count == 2


def test_empty_search_and_general_question_never_choose_a_stock(monkeypatch) -> None:
    from stock_ai import query as query_module

    class Runtime:
        async def create_run(self, **kwargs):
            assert kwargs["symbols"] == []
            return {"run_id": "query-general"}

        async def wait(self, _run_id):
            return {
                "status": "completed",
                "task_kind": "project_task",
                "summary": "Agent Runtime 使用 Host-owned 工具迴圈。",
                "decision": None,
                "completion_validation": {"passed": True},
                "model_invocations": [
                    {
                        "call_id": "query-general:turn:1",
                        "provider": "codex",
                        "model_id": "mock",
                        "status": "succeeded",
                    }
                ],
                "tool_trace": [],
            }

    monkeypatch.setattr(query_module, "get_agent_run_runtime", lambda: Runtime())
    route, answer, data, _sources = asyncio.run(
        answer_question("請解釋 Agent Runtime 架構", driver_id="codex")
    )

    assert search_entities("") == []
    assert guess_symbol("請解釋 Agent Runtime 架構") is None
    assert data["symbol"] is None
    assert data["symbol_source"] == "none"
    assert data["validated"] is True
    assert route == "project_task"
    assert "Host-owned" in answer


def test_unified_query_screener_resolves_nonempty_universe_and_uses_runtime(monkeypatch) -> None:
    from stock_ai import query as query_module

    captured: dict = {}

    class Runtime:
        async def create_run(self, **kwargs):
            captured.update(kwargs)
            return {"run_id": "query-screen"}

        async def wait(self, _run_id):
            return {
                "status": "completed",
                "task_kind": "market_information",
                "summary": "已完成多檔篩選。",
                "completion_validation": {"passed": True},
                "model_invocations": [
                    {
                        "call_id": "query-screen:turn:2",
                        "provider": "openai-compatible",
                        "model_id": "mock",
                        "status": "succeeded",
                    }
                ],
                "tool_trace": [
                    {"tool": "market.analyze_universe", "ok": True}
                ],
            }

    monkeypatch.setattr(query_module, "get_agent_run_runtime", lambda: Runtime())
    monkeypatch.setattr(
        query_module,
        "resolve_universe",
        lambda request: UniverseSnapshot(
            source=request.source,
            symbols=("AAA", "BBB"),
            filters={"provider": "official_volume"},
        ),
    )

    route, _answer, data, sources = asyncio.run(
        answer_question("找出今日量價最強的股票", driver_id="openai-compatible")
    )

    assert captured["symbols"] == ["AAA", "BBB"]
    assert captured["run_metadata"]["universe"]["source"] == "top_by_volume"
    assert route == "market_information"
    assert data["validated"] is True
    assert "market.analyze_universe" in sources


def test_decision_envelope_keeps_rule_model_risk_and_execution_separate() -> None:
    envelope = DecisionEnvelope(
        observation={"symbol": "AAA"},
        rule_analysis={"rule_score": 0.4},
        model_analysis=None,
        risk_evaluation={"execution_allowed": False},
        execution_status={"submitted": False},
        provenance=AnalysisProvenance(
            origin="rule_strategy",
            rule_set_id="test.rule.v1",
            universe_source="explicit_symbols",
            symbols_considered=["AAA"],
            data_ready=True,
        ),
    )

    assert envelope.model_analysis is None
    assert envelope.rule_analysis == {"rule_score": 0.4}
    assert envelope.risk_evaluation == {"execution_allowed": False}
    assert envelope.provenance.model_call_succeeded is False


def test_context_broker_does_not_attach_market_account_or_ui_to_general_tasks(
    tmp_path: Path,
) -> None:
    calls = {"ui": 0, "account": 0, "external": 0}

    def counted(name: str, value: dict) -> dict:
        calls[name] += 1
        return value

    builder = EnvironmentSnapshotBuilder(
        project_root=tmp_path,
        capability_manifest=lambda: [
            {"name": "market.quote", "category": "market"},
            {"name": "project.read", "category": "project"},
        ],
        ui_snapshot=lambda: counted("ui", {"view": "stock", "symbol": "AAA"}),
        account_snapshot=lambda: counted("account", {"positions": [{"symbol": "AAA"}]}),
        external_snapshot=lambda: counted("external", {"framework": "example"}),
    )
    context = AgentRunContext(
        run_id="run-general",
        session_id="session-general",
        symbols=("AAA",),
        autonomy="advisory",
    )
    context.state["task_kind"] = "general_answer"

    snapshot = builder.build(context, memories=[{"content": "private project memory"}])

    assert calls == {"ui": 0, "account": 0, "external": 0}
    assert snapshot.state["market"]["symbols"] == []
    assert snapshot.state["account"]["exposed"] is False
    assert snapshot.state["project"]["exposed"] is False
    assert snapshot.state["capabilities"]["items"] == []
    assert snapshot.state["memory"] == []


def test_context_broker_market_decision_exposes_account_but_not_project_or_ui(
    tmp_path: Path,
) -> None:
    builder = EnvironmentSnapshotBuilder(
        project_root=tmp_path,
        capability_manifest=lambda: [
            {"name": "market.quote", "category": "market"},
            {"name": "paper.preview", "category": "paper"},
            {"name": "project.read", "category": "project"},
        ],
        ui_snapshot=lambda: pytest.fail("UI context must not be called"),
        account_snapshot=lambda: {"available": True, "positions": []},
        external_snapshot=lambda: pytest.fail("External project context must not be called"),
    )
    context = AgentRunContext(
        run_id="run-market",
        session_id="session-market",
        symbols=("AAA",),
        autonomy="advisory",
    )
    context.state["task_kind"] = "market_decision"

    snapshot = builder.build(context)
    names = [item["name"] for item in snapshot.state["capabilities"]["items"]]

    assert snapshot.state["market"]["symbols"] == ["AAA"]
    assert snapshot.state["account"]["available"] is True
    assert snapshot.state["project"]["exposed"] is False
    assert names == ["market.quote", "paper.preview"]


def test_multi_intent_router_keeps_ui_symbol_as_unconfirmed_candidate() -> None:
    routing = UnifiedMultiIntentRouter().route(
        "請檢查這個專案的 API 2330 錯誤碼",
        task_kind_hint="project_task",
        supplied_symbols=("AAA",),
        supplied_symbol_source="ui_selected",
    )

    assert routing.requires_project_context is True
    assert routing.requires_market_context is False
    assert routing.symbol_contexts[-1].source == "ui_selected"
    assert routing.symbol_contexts[-1].user_confirmed is False


def test_multi_intent_router_preserves_negative_market_constraint() -> None:
    routing = UnifiedMultiIntentRouter().route(
        "不要分析股票，請解釋 Agent Runtime 架構",
        task_kind_hint="general_answer",
        supplied_symbols=("AAA",),
    )

    assert routing.primary_task_kind == "general_answer"
    assert "do_not_analyze_stocks" in routing.negative_constraints
    assert routing.requires_market_context is False


def test_multi_intent_router_routes_natural_company_risk_comparison_to_market_research() -> None:
    routing = UnifiedMultiIntentRouter().route(
        "比較台新新光金的風險差異。",
        task_kind_hint="general_answer",
    )

    assert routing.primary_task_kind == "market_information"
    assert routing.requires_market_context is True


def test_multi_intent_router_does_not_activate_market_for_a_no_lookup_constraint() -> None:
    routing = UnifiedMultiIntentRouter().route(
        "請只確認工作流偏好；不要查市場、不要交易。",
        task_kind_hint="general_answer",
    )

    assert routing.primary_task_kind == "general_answer"
    assert "do_not_query_market" in routing.negative_constraints
    assert routing.requires_market_context is False
    corrected = UnifiedMultiIntentRouter().apply_model_correction(
        routing,
        {"primary_task_kind": "market_information"},
    )
    assert corrected.primary_task_kind == "general_answer"
    assert corrected.requires_market_context is False


def test_multi_intent_router_accepts_model_correction_but_keeps_host_constraints() -> None:
    router = UnifiedMultiIntentRouter()
    current = router.route(
        "請解釋這個混合任務",
        task_kind_hint="general_answer",
    )
    corrected = router.apply_model_correction(
        current,
        {
            "primary_task_kind": "project_task",
            "intents": [{"type": "project_task", "confidence": 0.97}],
            "reason_summary": "需要檢查專案",
        },
    )
    constrained = router.route(
        "不要分析股票，請說明原因",
        task_kind_hint="general_answer",
    )
    blocked_market = router.apply_model_correction(
        constrained,
        {
            "primary_task_kind": "market_decision",
            "intents": [{"type": "market_decision", "confidence": 0.99}],
        },
    )

    assert corrected.primary_task_kind == "project_task"
    assert corrected.requires_project_context is True
    assert blocked_market.primary_task_kind == "general_answer"
    assert blocked_market.requires_market_context is False


def test_evidence_recovery_context_cannot_be_corrected_into_ui_control() -> None:
    router = UnifiedMultiIntentRouter()
    routing = router.route(
        "請針對 Fishbone 的失敗資料來源做局部修復，先分析根因並使用替代來源證據。",
        task_kind_hint="market_information",
    )
    corrected = router.apply_model_correction(
        routing,
        {"primary_task_kind": "ui_task", "reason_summary": "mistook artifact context for a UI request"},
    )

    assert "evidence_recovery_not_ui" in routing.negative_constraints
    assert corrected.primary_task_kind == "market_information"
    assert corrected.requires_market_context is True


def test_evidence_recovery_negative_ui_phrase_never_overrides_host_market_route() -> None:
    objective = (
        "請針對 Fishbone 的失敗資料來源做局部修復，先分析根因並使用替代來源證據；"
        "只做分析，不要操作介面。"
    )
    routing = UnifiedMultiIntentRouter().route(
        objective,
        task_kind_hint="market_information",
    )

    assert routing.primary_task_kind == "market_information"
    assert "do_not_operate_ui" in routing.negative_constraints
    assert routing.requires_market_context is True


def test_universe_resolver_supports_formal_market_sector_and_ranking_sources(monkeypatch) -> None:
    from stock_ai import universe as universe_module

    rows = [
        SecurityMasterItem(
            symbol="AAA",
            name="Alpha",
            market="taiwan",
            exchange="TWSE",
            listing_type="listed",
            industry="Semiconductor",
            trading_status="active",
            source="TWSE",
        ),
        SecurityMasterItem(
            symbol="BBB",
            name="Beta",
            market="taiwan",
            exchange="TWSE",
            listing_type="listed",
            industry="Finance",
            trading_status="active",
            source="TWSE",
        ),
        SecurityMasterItem(
            symbol="CCC",
            name="Inactive",
            market="taiwan",
            exchange="TWSE",
            listing_type="listed",
            industry="Semiconductor",
            trading_status="suspended",
            source="TWSE",
        ),
        SecurityMasterItem(
            symbol="DDD",
            name="Delta",
            market="taiwan",
            exchange="TPEx",
            listing_type="otc",
            industry="Semiconductor",
            trading_status="active",
            source="TPEx",
        ),
    ]

    def securities(**kwargs):
        market = kwargs.get("market")
        query = kwargs.get("q")
        selected = rows
        if market == "twse":
            selected = [item for item in selected if item.exchange == "TWSE"]
        elif market == "tpex":
            selected = [item for item in selected if item.exchange == "TPEx"]
        if query:
            selected = [item for item in selected if query.casefold() in item.name.casefold()]
        return selected

    monkeypatch.setattr(universe_module, "list_securities_master", securities)
    monkeypatch.setattr(
        universe_module,
        "twse_quotes",
        lambda: [
            {"Code": "AAA", "TradeVolume": "100"},
            {"Code": "BBB", "TradeVolume": "300"},
        ],
    )
    monkeypatch.setattr(
        universe_module,
        "tpex_quotes",
        lambda: [{"SecuritiesCompanyCode": "DDD", "TradingShares": "200"}],
    )

    full = resolve_universe(UniverseRequest(source="all_twse_active", limit=10))
    tpex = resolve_universe(UniverseRequest(source="all_tpex_active", limit=10))
    sector = resolve_universe(
        UniverseRequest(
            source="sector_members",
            filters={"sector": "semiconductor", "market": "twse"},
            limit=10,
        )
    )
    screening = resolve_universe(
        UniverseRequest(
            source="screening_query",
            filters={"query": "Alpha", "market": "twse"},
            limit=10,
        )
    )
    top_volume = resolve_universe(UniverseRequest(source="top_by_volume", limit=2))

    assert full.symbols == ("AAA", "BBB")
    assert tpex.symbols == ("DDD",)
    assert sector.symbols == ("AAA",)
    assert sector.filters["sector"] == "semiconductor"
    assert screening.symbols == ("AAA",)
    assert top_volume.symbols == ("BBB.TW", "DDD.TWO")
    assert top_volume.filters["ranking_metric"] == "official_trade_volume_shares"


def test_context_owned_universe_sources_call_real_providers(monkeypatch) -> None:
    from stock_ai import universe as universe_module

    monkeypatch.setattr(
        universe_module,
        "_user_watchlist_symbols",
        lambda _filters: (["AAA", "BBB"], {"provider": "sqlite.user_watchlists"}),
    )
    monkeypatch.setattr(
        universe_module,
        "_portfolio_position_symbols",
        lambda: (["CCC"], {"provider": "paper_oms_positions"}),
    )

    watchlist = resolve_universe(UniverseRequest(source="user_watchlist", limit=1))
    portfolio = resolve_universe(UniverseRequest(source="portfolio_positions", limit=5))
    workflow = resolve_universe(
        UniverseRequest(
            source="workflow_parameters",
            filters={"workflow_parameters": {"symbols": ["DDD", "EEE"]}},
            limit=5,
        )
    )
    discovered = resolve_universe(
        UniverseRequest(
            source="tool_discovered",
            filters={"tool_result": {"items": [{"symbol": "FFF"}, {"symbol": "GGG"}]}},
            limit=5,
        )
    )

    assert watchlist.symbols == ("AAA",)
    assert watchlist.filters["resolution"]["provider"] == "sqlite.user_watchlists"
    assert portfolio.symbols == ("CCC",)
    assert portfolio.filters["resolution"]["provider"] == "paper_oms_positions"
    assert workflow.symbols == ("DDD", "EEE")
    assert workflow.filters["resolution"]["provider"] == "agent_workflow_store"
    assert discovered.symbols == ("FFF", "GGG")
    assert discovered.filters["resolution"]["provider"] == "validated_tool_results"


def test_agent_market_radar_resolves_formal_universe_before_starting_run(monkeypatch) -> None:
    from stock_ai import agent_api

    captured: dict = {}

    class Runtime:
        async def create_run(self, **kwargs):
            captured.update(kwargs)
            return {"run_id": "run-universe", "status": "queued"}

    monkeypatch.setattr(agent_api, "get_agent_run_runtime", lambda: Runtime())
    monkeypatch.setattr(agent_api, "get_agent_service", lambda: SimpleNamespace(
        default_driver="codex",
        drivers={"codex": SimpleNamespace(describe=lambda: {"configured": True})},
    ))
    monkeypatch.setattr(
        agent_api,
        "resolve_universe",
        lambda request: UniverseSnapshot(
            source=request.source,
            symbols=("AAA", "BBB"),
            filters={**request.filters, "provider": "official"},
        ),
    )

    payload = asyncio.run(
        agent_api.create_market_radar_run(
            agent_api.MarketRadarRunRequest(
                universe_source="all_twse_active",
                filters={"active": True},
                limit=2,
                market_snapshot_id="snapshot-current-only",
            )
        )
    )

    assert captured["symbols"] == ["AAA", "BBB"]
    assert captured["run_metadata"]["universe_source"] == "all_twse_active"
    assert captured["run_metadata"]["market_snapshot_id"] == "snapshot-current-only"
    assert payload["universe"]["symbols"] == ("AAA", "BBB")
    assert payload["universe"]["filters"]["provider"] == "official"


def _valid_market_radar_result() -> dict:
    return {
        "schema_version": "stock_ai.market_radar_result.v1",
        "generated_at": "2026-07-24T01:00:00+00:00",
        "summary": "兩檔股票皆已完成模型分析。",
        "items": [
            {
                "symbol": symbol,
                "name": name,
                "action": action,
                "confidence": confidence,
                "confidence_type": "model_self_reported",
                "reason": reason,
                "next_action": next_action,
                "timing": "下一交易日",
                "trigger": "重新取得市場證據",
                "observation_ids": [f"OBS-{symbol}"],
                "evidence_ids": ["market-evidence"],
            }
            for symbol, name, action, confidence, reason, next_action in (
                ("AAA", "Alpha", "wait_to_buy", 0.62, "等待量價確認", "持續觀察"),
                ("BBB", "Beta", "hold", 0.55, "風險訊號中性", "暫不進場"),
            )
        ],
        "groups": {
            "buy_now": [],
            "sell_now": [],
            "wait_to_buy": ["AAA"],
            "wait_to_sell": [],
            "hold": ["BBB"],
        },
        "action_plan": {
            "headline": "等待更多市場證據",
            "next_review": "下一交易日",
            "steps": ["更新量價與風險證據"],
        },
        "observations": [
            {
                "observation_id": f"OBS-{symbol}",
                "symbol": symbol,
                "statement": statement,
                "evidence_ids": ["market-evidence"],
                "source_type": "tool",
            }
            for symbol, statement in (
                ("AAA", "量價尚未確認"),
                ("BBB", "風險訊號中性"),
            )
        ],
        "rule_analysis": {
            "summary": "規則只作量價參考。",
            "evidence_ids": ["market-evidence"],
        },
        "model_analysis": {
            "summary": "模型比較兩檔後給出觀察結論。",
            "evidence_ids": ["market-evidence"],
        },
        "risk_evaluation": {
            "summary": "Host 風控未允許執行。",
            "risk_level": "medium",
            "evidence_ids": ["market-evidence"],
            "host_limits_applied": True,
        },
        "provenance": {
            "origin": "model",
            "provider": "placeholder",
            "model_id": "placeholder",
            "model_call_id": "placeholder",
            "model_call_succeeded": True,
            "universe_source": "explicit_symbols",
            "symbols_considered": ["AAA", "BBB"],
            "fallback_used": False,
        },
    }


def test_market_radar_completed_run_returns_validated_model_cards(monkeypatch) -> None:
    from stock_ai import agent_api

    run = {
        "run_id": "radar-valid",
        "status": "completed",
        "request": {
            "driver_id": "openai-compatible",
            "symbols": ["AAA", "BBB"],
            "metadata": {"universe_source": "explicit_symbols"},
        },
        "result": {
            "status": "completed",
            "completion_validation": {"passed": True},
            "structured_result": _valid_market_radar_result(),
            "model_invocations": [
                {
                    "call_id": "radar-valid:turn:2",
                    "provider": "openai-compatible",
                    "model_id": "qwen",
                    "status": "succeeded",
                }
            ],
            "tool_trace": [
                {
                    "call_id": "market-evidence",
                    "node_id": "node-market",
                    "ok": True,
                    "validation": {"evidence_hash": "hash-market"},
                }
            ],
        },
    }
    monkeypatch.setattr(
        agent_api,
        "get_agent_run_runtime",
        lambda: SimpleNamespace(get_run=lambda _run_id: run),
    )
    monkeypatch.setattr(
        agent_api,
        "get_agent_service",
        lambda: SimpleNamespace(
            drivers={
                "openai-compatible": SimpleNamespace(
                    describe=lambda: {"model": "qwen"}
                )
            }
        ),
    )

    payload = agent_api.get_market_radar_run("radar-valid")

    assert payload["market_radar_status"] == "succeeded"
    assert payload["provenance"]["model_call_succeeded"] is True
    assert [item["symbol"] for item in payload["market_radar_result"]["items"]] == [
        "AAA",
        "BBB",
    ]
    assert payload["market_radar_result"]["groups"]["wait_to_buy"][0]["symbol"] == "AAA"


def test_market_radar_max_steps_is_never_promoted_to_model_success(monkeypatch) -> None:
    from stock_ai import agent_api

    run = {
        "run_id": "radar-incomplete",
        "status": "completed",
        "request": {
            "driver_id": "codex",
            "symbols": ["AAA", "BBB"],
            "metadata": {"universe_source": "explicit_symbols"},
        },
        "result": {
            "status": "max_steps_reached",
            "completion_validation": {"passed": False},
            "structured_result": _valid_market_radar_result(),
            "model_invocations": [
                {
                    "call_id": "radar-incomplete:turn:6",
                    "provider": "codex",
                    "model_id": "codex",
                    "status": "succeeded",
                }
            ],
        },
    }
    monkeypatch.setattr(
        agent_api,
        "get_agent_run_runtime",
        lambda: SimpleNamespace(get_run=lambda _run_id: run),
    )
    monkeypatch.setattr(
        agent_api,
        "get_agent_service",
        lambda: SimpleNamespace(
            drivers={"codex": SimpleNamespace(describe=lambda: {"model": "codex"})}
        ),
    )

    payload = agent_api.get_market_radar_run("radar-incomplete")

    assert payload["market_radar_status"] == "failed"
    assert payload["market_radar_result"] is None
    assert payload["provenance"]["origin"] == "none"
    assert payload["provenance"]["model_call_succeeded"] is False


def test_agent_market_radar_rejects_empty_explicit_universe_without_fallback() -> None:
    from stock_ai import agent_api

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            agent_api.create_market_radar_run(
                agent_api.MarketRadarRunRequest(
                    universe_source="explicit_symbols",
                    symbols=[],
                )
            )
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "universe_required"


def test_market_radar_request_uses_the_shared_deep_analysis_symbol_cap() -> None:
    from pydantic import ValidationError
    from stock_ai import agent_api
    from stock_ai.market_intelligence.deep_analysis import MAX_DEEP_ANALYSIS_SYMBOLS

    valid_symbols = [f"{1000 + index}.TW" for index in range(MAX_DEEP_ANALYSIS_SYMBOLS)]
    request = agent_api.MarketRadarRunRequest(symbols=valid_symbols, limit=MAX_DEEP_ANALYSIS_SYMBOLS)
    assert request.symbols == valid_symbols
    assert request.limit == MAX_DEEP_ANALYSIS_SYMBOLS

    with pytest.raises(ValidationError):
        agent_api.MarketRadarRunRequest(
            symbols=valid_symbols + ["9999.TW"],
            limit=MAX_DEEP_ANALYSIS_SYMBOLS + 1,
        )


def test_universe_resolver_rejects_unknown_rankings_invalid_symbols_and_limits() -> None:
    with pytest.raises(UniverseResolutionError):
        resolve_universe(UniverseRequest(source="top_by_market_cap"))
    with pytest.raises(ValueError, match="invalid symbols"):
        UniverseRequest(source="explicit_symbols", symbols=("!!!",))
    with pytest.raises(ValueError, match="between 1 and 5000"):
        UniverseRequest(source="all_twse_active", limit=5001)


def test_neutral_analysis_migration_creates_all_provenance_tables() -> None:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn)
    tables = {
        row[0]
        for row in conn.execute("select name from sqlite_master where type='table'")
    }

    assert {
        "user_watchlists",
        "user_watchlist_symbols",
        "universe_snapshots",
        "analysis_runs",
        "model_invocations",
        "analysis_provenance",
        "rule_strategy_results",
        "model_analysis_results",
        "validation_reports",
    }.issubset(tables)
    assert conn.execute("pragma user_version").fetchone()[0] == LATEST_SCHEMA_VERSION


def test_neutrality_ci_guard_passes_current_production_tree() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_neutrality_ci.py"],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_market_radar_model_failure_does_not_promote_rule_result(monkeypatch) -> None:
    from stock_ai import codex_market

    class FailedRuntime:
        async def run_structured(self, *_args, **_kwargs):
            raise TimeoutError("model timeout")

    monkeypatch.setattr(
        codex_market,
        "build_agent_watchlist",
        lambda **_kwargs: {
            "universe": {
                "source": "explicit_symbols",
                "symbols": ["AAA"],
                "count": 1,
            },
            "items": [
                {
                    "symbol": "AAA",
                    "recommendation_bucket": "watch",
                    "execution_permission": "blocked",
                    "signal_summary": {
                        "action": "hold",
                        "rule_score": 0.2,
                        "confidence_type": "rule_score",
                    },
                    "data_status": {"decision_ready": True},
                    "research_status": {"execution_evidence_eligible": False},
                    "risk_summary": {"approved": False},
                }
            ],
        },
    )

    payload = asyncio.run(
        codex_market._build_market_radar(FailedRuntime(), limit=1, explain=False)
    )

    assert payload["badge"] == "MODEL ERROR"
    assert payload["items"] == []
    assert payload["model_items"] == []
    assert len(payload["rule_items"]) == 1
    assert payload["decision_envelope"]["model_analysis"] is None
    assert payload["provenance"]["fallback_used"] is False
