import sqlite3
from types import SimpleNamespace

from fastapi import BackgroundTasks
import pytest

from stock_ai.market_intelligence import api as workspace_api
from stock_ai.market_intelligence.contracts import (
    CandidateDetail,
    DataQuality,
    MarketIntelligenceSnapshot,
    MarketRegime,
    UniverseSummary,
    WorkspaceContext,
)
from stock_ai.market_intelligence.snapshot_store import SnapshotStore
from stock_ai.market_intelligence.service import MarketIntelligenceService
from stock_ai.market_intelligence.broad_scanner import BroadScanner
from stock_ai.market_intelligence.deep_analysis import MAX_DEEP_ANALYSIS_SYMBOLS
from stock_ai.market_intelligence.candidate_ranker import rank_candidates
from stock_ai.market_intelligence.feature_store import _quality
from product_classification_fixtures import product_fixture


def candidate(
    symbol: str,
    *,
    category: str,
    rank: int,
    change: float,
    trade_value: float,
    industry: str,
) -> CandidateDetail:
    return CandidateDetail(
        symbol=symbol,
        **product_fixture(symbol),
        name=f"Name {symbol}",
        exchange="TWSE",
        industry=industry,
        category=category,
        rank=rank,
        score=80 - rank,
        latest_price=100 + rank,
        change_percent=change,
        volume=1_000 * rank,
        trade_value=trade_value,
        range_position=0.7,
        decision_label="接近條件",
        primary_reason="全市場確定性規則分類",
        trigger="放量突破當日高點",
        trigger_price=110,
        trigger_event="官方行情更新",
        invalidation="跌破當日低點",
        invalidation_price=90,
        next_review="下一個官方行情事件",
        distance_to_trigger_percent=1.2,
        risk_level="medium",
        liquidity_status="pass",
        host_risk_status="passed",
        portfolio_fit="not_held",
        data_quality=DataQuality(
            status="ready",
            score=1,
            source="TWSE",
            data_as_of="2026-07-30",
        ),
    )


def snapshot() -> MarketIntelligenceSnapshot:
    first = candidate(
        "1111.TW",
        category="near_actionable",
        rank=1,
        change=3.2,
        trade_value=900_000_000,
        industry="24",
    )
    second = candidate(
        "2222.TW",
        category="high_risk",
        rank=2,
        change=-8.1,
        trade_value=100_000_000,
        industry="15",
    )
    return MarketIntelligenceSnapshot(
        snapshot_id="MIS-TEST",
        data_as_of="2026-07-30",
        market_regime=MarketRegime(
            label="neutral",
            summary="fixture",
            risk_level="medium",
        ),
        universe=UniverseSummary(
            resolved_count=2,
            valid_data_count=2,
            exchanges={"TWSE": 2},
        ),
        rankings={
            "actionable_now": [],
            "near_actionable": ["1111.TW"],
            "wait_for_pullback": [],
            "wait_for_breakout": [],
            "avoid_now": [],
            "high_risk": ["2222.TW"],
            "insufficient_data": [],
        },
        portfolio_actions={"hold": [], "add": [], "reduce": [], "exit": []},
        candidate_details={"1111.TW": first, "2222.TW": second},
    )


def test_snapshot_store_lists_saved_watchlist_and_active_alerts(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    store.add_watchlist_symbol(watchlist_id="home-default", symbol="1111.TW")
    store.create_alert(
        alert_id="ALT-TEST",
        symbol="2222.TW",
        alert_type="market_intelligence_trigger",
        rule={"trigger_price": 110},
    )

    assert store.watchlist_symbols() == [
        {
            "watchlist_id": "home-default",
            "watchlist_name": "首頁自選",
            "symbol": "1111.TW",
            "added_at": store.watchlist_symbols()[0]["added_at"],
            "provenance": {"origin": "user_action", "surface": "home"},
        }
    ]
    alerts = store.active_alerts()
    assert alerts[0]["alert_id"] == "ALT-TEST"
    assert alerts[0]["symbol"] == "2222.TW"
    assert alerts[0]["rule"] == {"trigger_price": 110}


def test_combined_market_industry_codes_use_official_readable_labels():
    assert workspace_api._industry_label("24") == ("半導體業", "24")
    assert workspace_api._industry_label("32") == ("文化創意業", "32")
    assert workspace_api._industry_label("80") == ("管理股票", "80")
    assert workspace_api._industry_label("自訂分類") == ("自訂分類", None)


def test_workspace_context_v2_migrates_v1_without_treating_index_as_explicit_intent():
    context = WorkspaceContext.model_validate(
        {
            "schema_version": "stock_ai.workspace_context.v1",
            "selectedSymbol": "6603.two",
            "selectedUniverse": "all_taiwan_active",
            "marketSnapshotId": "MIS-OLD",
            "timeframe": "D",
            "dateRange": "1y",
            "priceBasis": "unadjusted",
            "comparisonSymbols": ["6603.two", "2330.tw"],
            "activePortfolio": "paper-default",
            "currentWorkspace": "instrument",
            "activeWorkspaceTab": "chart",
            "agentSessionId": "AS-1",
            "layoutState": {"sidebar_collapsed": True},
        }
    )

    assert context.schema_version == "stock_ai.workspace_context.v2"
    assert context.route.workspace == "instrument"
    assert context.route.tab == "chart"
    assert context.selection.symbol == "6603.TWO"
    assert context.selection.explicit_intent_symbols == []
    assert context.comparison.symbols == ["6603.TWO", "2330.TW"]
    assert context.data.market_snapshot_id == "MIS-OLD"
    assert context.agent.session_id == "AS-1"
    assert context.layout.sidebar_collapsed is True


def test_workspace_context_defaults_to_no_selected_stock_and_twii_is_display_fallback_only():
    context = WorkspaceContext()

    assert context.selection.symbol is None
    assert context.selection.explicit_intent_symbols == []
    assert context.selectedSymbol == "^TWII"


def test_workspace_context_patch_deep_merges_nested_v2_state(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    service = MarketIntelligenceService(store=store)
    service.patch_context(
        {
            "selection": {"symbol": "2330.TW", "explicit_intent_symbols": ["2330.TW"]},
            "route": {"workspace": "instrument", "tab": "chart"},
        }
    )

    updated = service.patch_context({"selection": {"candidate_id": "candidate:2330.TW"}})

    assert updated.selection.symbol == "2330.TW"
    assert updated.selection.explicit_intent_symbols == ["2330.TW"]
    assert updated.selection.candidate_id == "candidate:2330.TW"
    assert updated.route.workspace == "instrument"
    assert updated.route.tab == "chart"


def test_bootstrap_returns_chart_watchlist_alerts_and_whole_market_rankings(
    monkeypatch,
    tmp_path,
):
    current = snapshot()
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    store.add_watchlist_symbol(watchlist_id="home-default", symbol="1111.TW")
    store.create_alert(
        alert_id="ALT-TEST",
        symbol="2222.TW",
        alert_type="market_intelligence_trigger",
        rule={"trigger_price": 110},
    )
    service = SimpleNamespace(
        store=store,
        latest_snapshot=lambda: current,
        context=lambda: WorkspaceContext(),
        patch_context=lambda payload: WorkspaceContext(
            data={"market_snapshot_id": payload["data"]["market_snapshot_id"]},
        ),
    )
    monkeypatch.setattr(workspace_api, "_service", lambda: service)
    monkeypatch.setattr(
        workspace_api,
        "realtime_status",
        lambda: SimpleNamespace(as_dict=lambda: {"status": "ready"}),
    )
    monkeypatch.setattr(
        workspace_api,
        "_cached_instrument_chart",
        lambda symbol, **_kwargs: {
            "symbol": symbol,
            "source": "fixture",
            "points": [{"date": "2026-07-30", "close": 100}],
        },
    )

    payload = workspace_api.workspace_bootstrap(BackgroundTasks())

    assert payload["default_chart"]["payload"]["points"]
    assert payload["watchlist_summary"]["items"][0]["symbol"] == "1111.TW"
    assert payload["alerts_summary"]["items"][0]["alert_id"] == "ALT-TEST"
    assert payload["market_navigation"]["volume"][0]["symbol"] == "2222.TW"
    assert payload["market_navigation"]["movers"][0]["symbol"] == "2222.TW"
    assert payload["market_navigation"]["industries"][0]["kind"] == "industry"
    assert payload["market_navigation"]["industries"][0]["name"] == "半導體業"
    assert payload["market_navigation"]["industries"][0]["industry_code"] == "24"
    assert payload["market_navigation"]["anomalies"][0]["symbol"] == "2222.TW"
    assert payload["market_navigation"]["institutional"] == []
    assert (
        payload["market_navigation"]["institutional_status"]["status"]
        == "unavailable"
    )


def _feature(symbol: str, *, etf: bool = False, change: float = 2.0) -> dict:
    return {
        "symbol": symbol,
        **product_fixture(symbol, "etf" if etf else "ordinary_stock"),
        "name": symbol,
        "exchange": "TWSE",
        "industry": "24",
        "is_etf": etf,
        "is_warrant": False,
        "is_managed_stock": False,
        "is_special_security": False,
        "close": 100.0,
        "high": 102.0,
        "low": 98.0,
        "change_percent": change,
        "volume": 100_000,
        "trade_value": 200_000_000,
        "range_position": 0.9,
        "data_quality": {"status": "ready", "score": 1, "data_as_of": "2026-07-30", "source": "fixture"},
        "evidence": [],
    }


def test_candidate_contract_exposes_market_category_factors_and_model_state():
    details, _rankings, _actions, _stats = rank_candidates(
        [_feature("2330.TW")], market_regime="neutral"
    )
    item = details["2330.TW"]
    assert item.entity_id == product_fixture("2330.TW")["entity_id"]
    assert item.market_category in {"BUY_NOW", "WATCH", "FUTURE_BUY", "AVOID_NOW", "INSUFFICIENT_DATA"}
    assert item.model_status == "not_analyzed"
    assert item.factor_scores["trend_score"]["value"] is not None
    assert item.factor_scores["fundamental_score"]["status"] == "unavailable"


def test_successful_model_overlay_marks_each_candidate_and_receipt(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    saved = store.save_snapshot(snapshot())
    service = MarketIntelligenceService(store=store)
    receipt = {"status": "succeeded", "receipt_id": "MR-1"}

    updated = service.apply_model_overlay(
        saved.snapshot_id,
        succeeded=True,
        provider="ollama",
        model_id="test-model",
        summaries={"1111.TW": {"summary": "verified"}},
        receipt=receipt,
    )

    candidate = updated.candidate_details["1111.TW"]
    assert candidate.model_status == "succeeded"
    assert candidate.model_receipt == receipt
    assert candidate.model_overlay == {"summary": "verified"}


def test_fresh_snapshot_never_reuses_previous_model_success(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    service = MarketIntelligenceService(
        store=store,
        scanner=BroadScanner(feature_loader=lambda: [_feature("2330.TW")], position_loader=lambda: {}),
    )
    first = service.build_snapshot()
    service.apply_model_overlay(
        first.snapshot_id,
        succeeded=True,
        summaries={"2330.TW": {"summary": "old"}},
        receipt={"status": "succeeded"},
    )

    fresh = service.build_snapshot()
    assert fresh.model_overlay.status == "not_run"
    assert fresh.model_overlay.analyzed_symbols == []


def test_new_market_snapshot_persists_one_immutable_quality_receipt_per_decision(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    service = MarketIntelligenceService(
        store=store,
        scanner=BroadScanner(
            feature_loader=lambda: [_feature("2330.TW")],
            position_loader=lambda: {},
        ),
    )

    created = service.build_snapshot()

    receipt = created.candidate_details["2330.TW"].data_quality_receipt
    assert receipt is not None
    assert receipt.schema_version == "stock_ai.decision_data_quality_receipt.v1"
    assert receipt.freshness_status == "stale"
    assert receipt.completeness_status == "passed"
    assert receipt.source_disagreement_status == "not_observed"
    assert receipt.certification_status == "partial"
    assert created.data_quality["decision_receipt_count"] == 1
    assert store.decision_quality_receipts(created.snapshot_id) == [
        receipt.model_dump(mode="json")
    ]

    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "delete from market_decision_quality_receipts where receipt_id=?",
            (receipt.receipt_id,),
        )


def test_declared_quality_receipt_contract_rejects_missing_decision_receipts(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    current = snapshot()
    current.data_quality = {
        "decision_receipt_schema": "stock_ai.decision_data_quality_receipt.v1",
        "decision_receipt_count": len(current.candidate_details),
    }

    with pytest.raises(ValueError, match="receipt missing"):
        store.save_snapshot(current)

    assert store.snapshot(current.snapshot_id) is None


def test_market_quality_store_hash_verifies_rows_on_read(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    service = MarketIntelligenceService(
        store=store,
        scanner=BroadScanner(
            feature_loader=lambda: [_feature("2330.TW")],
            position_loader=lambda: {},
        ),
    )
    created = service.build_snapshot()
    with sqlite3.connect(store.path) as connection:
        connection.execute("drop trigger trg_market_decision_quality_receipts_immutable_update")
        connection.execute(
            "update market_decision_quality_receipts set payload_json=? where snapshot_id=?",
            ('{"receipt_id":"tampered"}', created.snapshot_id),
        )
        connection.commit()

    with pytest.raises(ValueError):
        store.decision_quality_receipts(created.snapshot_id)


def test_single_source_bulk_quote_stays_partial_and_cannot_become_buy_now():
    quality = _quality(
        close=100.0,
        volume=1_000_000,
        data_as_of="2026-08-20",
        source="TWSE_ALL_QUOTES",
    )
    assert quality["status"] == "partial"
    assert quality["source_observation"]["status"] == "not_observed"
    assert "cross_source_observation_not_available" in quality["quality_flags"]

    feature = _feature("2330.TW")
    feature["data_quality"] = quality
    details, rankings, _actions, _stats = rank_candidates([feature], market_regime="neutral")

    assert details["2330.TW"].category == "near_actionable"
    assert details["2330.TW"].decision_label == "待交叉驗證"
    assert rankings["actionable_now"] == []
    assert rankings["near_actionable"] == ["2330.TW"]


def test_partial_quote_never_promotes_an_existing_position_to_add():
    feature = _feature("0050.TW", etf=True, change=2.0)
    feature["data_quality"] = _quality(
        close=100.0,
        volume=1_000_000,
        data_as_of="2026-08-20",
        source="TWSE_ALL_QUOTES",
    )
    _details, _rankings, actions, _stats = rank_candidates(
        [feature],
        positions={"0050.TW": {"quantity_shares": 1_000, "average_cost": 90, "weight_percent": 5}},
        market_regime="neutral",
    )

    assert actions["add"] == []
    assert actions["hold"] == ["0050.TW"]


def test_model_overlay_records_running_and_failed_states_for_exact_snapshot(tmp_path):
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    saved = store.save_snapshot(snapshot())
    service = MarketIntelligenceService(store=store)

    running = service.begin_model_overlay(saved.snapshot_id, provider="codex")

    assert running.model_overlay.status == "running"
    assert running.candidate_details["1111.TW"].model_status == "queued"

    failed = service.apply_model_overlay(
        saved.snapshot_id,
        succeeded=False,
        error={"code": "provider_failed", "message": "fixture"},
    )

    assert failed.model_overlay.status == "failed"
    assert failed.candidate_details["1111.TW"].model_status == "failed"


def test_deep_analysis_funnel_and_overlay_queue_never_exceed_market_radar_contract(tmp_path):
    symbols = [f"{1000 + index}.TW" for index in range(MAX_DEEP_ANALYSIS_SYMBOLS + 8)]
    store = SnapshotStore(tmp_path / "workspace.sqlite3")
    service = MarketIntelligenceService(
        store=store,
        scanner=BroadScanner(
            feature_loader=lambda: [_feature(symbol) for symbol in symbols],
            position_loader=lambda: {},
        ),
    )

    created = service.build_snapshot()
    funnel = created.scan_statistics["deep_analysis_funnel"]

    assert len(funnel) == MAX_DEEP_ANALYSIS_SYMBOLS
    assert funnel == select_symbols_from_rankings(created.rankings)

    queued = service.begin_model_overlay(created.snapshot_id)
    queued_symbols = [
        symbol
        for symbol, candidate in queued.candidate_details.items()
        if candidate.model_status == "queued"
    ]
    assert queued_symbols == funnel


def select_symbols_from_rankings(rankings):
    """Mirror only the documented category ordering, not the production cap."""
    selected = []
    for category in (
        "actionable_now",
        "near_actionable",
        "wait_for_pullback",
        "wait_for_breakout",
        "high_risk",
    ):
        for symbol in rankings.get(category, []):
            if symbol not in selected:
                selected.append(symbol)
            if len(selected) == MAX_DEEP_ANALYSIS_SYMBOLS:
                return selected
    return selected


def test_broad_scanner_keeps_etf_research_but_excludes_it_from_entry_count():
    scanner = BroadScanner(
        feature_loader=lambda: [_feature("2330.TW"), _feature("0050.TW", etf=True)],
        position_loader=lambda: {},
    )
    result = scanner.scan()
    assert [item["symbol"] for item in result["features"]] == ["2330.TW", "0050.TW"]
    assert {key: result["universe_breakdown"][key] for key in (
        "security_master_count", "ordinary_stock_count", "etf_count", "excluded_product_count", "investable_count")} == {
        "security_master_count": 2,
        "ordinary_stock_count": 1,
        "etf_count": 1,
        "excluded_product_count": 1,
        "investable_count": 1,
    }


def test_held_etf_receives_portfolio_action_without_becoming_market_candidate():
    scanner = BroadScanner(
        feature_loader=lambda: [_feature("2330.TW"), _feature("0050.TW", etf=True, change=2.0)],
        position_loader=lambda: {
            "0050.TW": {"quantity_shares": 1_000, "average_cost": 90, "weight_percent": 5},
        },
    )

    result = scanner.scan()

    held = result["candidate_details"]["0050.TW"]
    assert held.is_etf is True
    assert held.is_position is True
    assert held.portfolio_action == "hold"
    assert "0050.TW" in result["portfolio_actions"]["hold"]
    assert "0050.TW" not in result["rankings"]["actionable_now"]
