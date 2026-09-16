import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_stock_ai.config.settings import load_settings
from open_stock_ai.data.data_quality import quality_summary
from open_stock_ai.data.market_data_hub import MarketDataHub
from open_stock_ai.data.news_source import NewsSource
from open_stock_ai.data.twse_source import TWSESource
from open_stock_ai.data.yahoo_source import YahooSource
from open_stock_ai.external_sources.ai_trader_source import AITraderSource
from open_stock_ai.external_sources.broker_import_governance import BrokerAccountImportGovernance
from open_stock_ai.external_sources.fingpt_source import FinGPTSource
from open_stock_ai.external_sources.finrl_source import FinRLSource
from open_stock_ai.external_sources.finrobot_source import FinRobotSource
from open_stock_ai.external_sources.qlib_source import QlibSource
from open_stock_ai.external_sources.registry import EXPECTED_ORIGINS, EXPECTED_REPOSITORY_LOCKS, ExternalProjectRegistry
from open_stock_ai.external_sources.runtime_governance import RuntimeConnectorGovernance
from open_stock_ai.external_sources.tradingagents_source import TradingAgentsSource
from open_stock_ai.execution.paper_executor import PaperExecutor
from open_stock_ai.execution.live_executor_disabled import LiveExecutorDisabled
from open_stock_ai.execution.order_store import OrderStore
from open_stock_ai.intelligence.financial_report_intelligence import FinancialReportIntelligence
from open_stock_ai.intelligence.fundamental_intelligence import FundamentalIntelligence
from open_stock_ai.intelligence.reflection_intelligence import ReflectionIntelligence
from open_stock_ai.intelligence.sentiment_intelligence import SentimentIntelligence
from open_stock_ai.intelligence.technical_intelligence import TechnicalIntelligence
from open_stock_ai.main import build_engine, integration_audit_to_dict, session_to_dict
from open_stock_ai.llm.llm_router import LLMRouter
from open_stock_ai.llm.openai_compatible_client import OpenAICompatibleClient
from open_stock_ai.notify.line_notifier import LineNotifier
from open_stock_ai.notify.telegram_notifier import TelegramNotifier
from open_stock_ai.research.backtest_research import BacktestResearch
from open_stock_ai.research.factor_research import FactorResearch
from open_stock_ai.research.outcome_attribution import OutcomeAttribution
from open_stock_ai.research.portfolio_attribution import PortfolioAttribution
from open_stock_ai.research.portfolio_construction import PortfolioConstruction
from open_stock_ai.research.portfolio_risk_context import (
    PointInTimePortfolioRiskContextBuilder,
    PortfolioRiskContextStore,
)
from open_stock_ai.risk.kill_switch import DurableRiskControlStore, KillSwitch
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.decision_log_store import DecisionLogStore
from open_stock_ai.storage.report_store import ReportStore
from open_stock_ai.storage.signal_store import SignalStore
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore
from open_stock_ai.strategy.breakout_strategy import BreakoutStrategy
from open_stock_ai.strategy.llm_strategy import LLMStrategy
from open_stock_ai.strategy.ma_strategy import MAStrategy
from open_stock_ai.strategy.rsi_macd_strategy import RSIMACDStrategy
from open_stock_ai.types import MarketSnapshot, ResearchResult, RiskDecision, StockRequest, TradingSignal
from stock_ai.main import app


client = TestClient(app)


def _use_tmp_open_stock_ai_config(monkeypatch, tmp_path, db_name: str = "open-stock-ai.sqlite") -> Path:
    db_path = tmp_path / db_name
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f"""
storage:
  sqlite_path: "{db_path.as_posix()}"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_STOCK_AI_CONFIG", str(config_path))
    return db_path


def test_open_stock_ai_build_engine_uses_yaml_settings(tmp_path, monkeypatch):
    db_path = tmp_path / "configured.sqlite"
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f"""
system:
  mode: "paper"
  live_trading_enabled: false
  require_human_confirm: true
  external_runtime_connectors_enabled: false
  broker_account_imports_enabled: false
  external_credentials_enabled: false
risk:
  min_rule_score_threshold: 0.40
  max_position_size_pct: 3
  require_backtest_passed: false
storage:
  sqlite_path: "{db_path.as_posix()}"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
""",
        encoding="utf-8",
    )

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=120.0,
            ohlcv=[{"close": float(90 + day)} for day in range(1, 8)],
            news=[{"title": "Company reports strong growth and profit"}],
            financials={"revenue": {"yoy_change_percent": 20.0}},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    settings = load_settings(config_path)
    engine = build_engine(settings=settings)
    decision = engine.analyze_stock(StockRequest(symbol="2330.TW", market="TW"))
    payload = asdict(decision)

    assert engine.pipeline.risk.min_rule_score_threshold == 0.40
    assert engine.pipeline.risk.max_position_size_pct == 3
    assert engine.pipeline.risk.require_backtest_passed is False
    assert engine.pipeline.execution.trading_mode == "paper"
    assert settings.external_runtime_connectors_enabled is False
    assert settings.broker_account_imports_enabled is False
    assert settings.external_credentials_enabled is False
    assert payload["risk"]["approved"] is False
    assert payload["risk"]["adjusted_position_size_pct"] == 0
    assert payload["execution"]["executed"] is False
    assert payload["execution"]["mode"] == "paper"
    assert payload["research"]["raw"]["storage"]["db_path"] == str(db_path)


def test_open_stock_ai_batch_sessions_use_configured_watchlist(tmp_path, monkeypatch):
    db_path = tmp_path / "batch.sqlite"
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f"""
storage:
  sqlite_path: "{db_path.as_posix()}"
watchlist:
  - symbol: "2330.TW"
    market: "TW"
  - symbol: "0050.TW"
    market: "TW"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
""",
        encoding="utf-8",
    )

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=100.0,
            ohlcv=[{"date": f"2026-06-{day:02d}", "close": float(100 + day)} for day in range(1, 31)],
            news=[{"title": "Company reports strong growth and record profit"}],
            financials={"revenue": {"yoy_change_percent": 24.0}},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    engine = build_engine(settings=load_settings(config_path))
    pre_market = engine.run_pre_market()
    intraday = engine.run_intraday()
    after_market = engine.run_after_market()

    assert [item.request.symbol for item in pre_market] == ["2330.TW", "0050.TW"]
    assert {item.request.horizon for item in pre_market} == {"swing"}
    assert {item.request.horizon for item in intraday} == {"intraday"}
    assert {item.request.horizon for item in after_market} == {"weekly"}
    assert all(item.risk.schema_version == "open_stock_ai.risk_decision.v1" for item in intraday)
    assert SQLiteStore(db_path=db_path).count("signals") == 6


def test_open_stock_ai_batch_session_api_keeps_missing_watchlist_empty(tmp_path, monkeypatch):
    _use_tmp_open_stock_ai_config(monkeypatch, tmp_path)

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=100.0,
            ohlcv=[{"date": f"2026-06-{day:02d}", "close": float(100 + day)} for day in range(1, 31)],
            news=[{"title": "Company reports strong growth and record profit"}],
            financials={"revenue": {"yoy_change_percent": 24.0}},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    res = client.get("/api/open-stock-ai/session/intraday")

    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "open_stock_ai_batch_session"
    assert data["schema_version"] == "open_stock_ai.batch_session.v1"
    assert data["session"] == "intraday"
    assert data["count"] == 0
    assert data["portfolio_construction"]["schema_version"] == "open_stock_ai.portfolio_construction.v5"
    assert data["portfolio_construction"]["method"] == "covariance_and_factor_constrained_paper_portfolio"
    assert data["portfolio_construction"]["item_count"] == 0
    assert data["portfolio_construction"]["constraints"]["paper_only"] is True
    assert data["portfolio_construction"]["execution_boundary"] == "paper_only_covariance_constrained_no_order_authority"
    assert data["portfolio_construction"]["positions"] == []
    assert data["items"] == []


def test_batch_session_consumes_one_durable_pit_portfolio_context_for_all_targets(tmp_path, monkeypatch):
    db_path = tmp_path / "open-stock-ai.sqlite"
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f'''\
storage:
  sqlite_path: "{db_path.as_posix()}"
watchlist:
  - symbol: "AAA.TW"
    market: "TW"
  - symbol: "BBB.TW"
    market: "TW"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_STOCK_AI_CONFIG", str(config_path))
    as_of = "2026-08-20T09:00:00+00:00"
    manifest = "a" * 64
    symbols = ("AAA.TW", "BBB.TW")
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    instruments = {}
    for offset, symbol in enumerate(symbols):
        observations = []
        for day in range(20):
            timestamp = start + timedelta(days=day)
            observations.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "return": (0.001 if day % 2 == 0 else -0.0005) * (offset + 1),
                    "available_at": timestamp.isoformat(),
                    "source_revision_id": f"return-{symbol}-{day}",
                }
            )
        instruments[symbol] = {
            "symbol": symbol,
            "dataset_manifest_hash": manifest,
            "historical_pit_eligible": True,
            "production_contract_covered": True,
            "metadata": {
                "issuer_id": symbol.split(".")[0],
                "industry": "technology",
                "currency": "TWD",
                "broker_id": "paper",
                "account_alias": "paper-default",
                "adv_value": 10_000_000.0,
                "adv_available_at": as_of,
                "adv_source_revision_id": f"adv-{symbol}",
            },
            "returns": observations,
            "factor_exposures": {
                "market": {"value": 1.0 + offset / 10, "available_at": as_of, "source_revision_id": f"factor-{symbol}"}
            },
            "sizing_inputs": {
                "annualized_volatility_pct": {"value": 20.0, "available_at": as_of, "source_revision_id": f"vol-{symbol}"},
                "expected_win_probability": {"value": 0.60, "available_at": as_of, "source_revision_id": f"prob-{symbol}"},
                "expected_win_pct": {"value": 8.0, "available_at": as_of, "source_revision_id": f"win-{symbol}"},
                "expected_loss_pct": {"value": 4.0, "available_at": as_of, "source_revision_id": f"loss-{symbol}"},
                "max_loss_pct": {"value": 5.0, "available_at": as_of, "source_revision_id": f"loss-cap-{symbol}"},
                "risk_budget_pct": {"value": 1.0, "available_at": as_of, "source_revision_id": f"budget-{symbol}"},
            },
        }
    policy = {
        "policy_id": "paper-risk-policy",
        "policy_version": "2026-08-20.v1",
        "account_scope": "paper",
        "account_alias": "paper-default",
        "account_equity": 1_000_000.0,
        "concentration_limits": {"issuer": 60.0, "industry": 80.0, "factor": 1.0, "currency": 100.0, "broker": 100.0, "account": 100.0},
        "factor_limits": {"market": 1.0},
        "max_participation_rate": 0.20,
        "max_days_to_liquidate": 0.20,
        "cvar_confidence": 0.95,
        "max_cvar_pct": 5.0,
        "stress_scenarios": [{"scenario_id": "market_gap", "shocks": {symbol: -0.20 for symbol in symbols}}],
        "max_stress_loss_pct": 5.0,
        "approval_status": "approved",
        "approved_by": "paper-owner",
        "approved_at": "2026-08-19T09:00:00+00:00",
    }
    policy["approval_receipt_sha256"] = PointInTimePortfolioRiskContextBuilder().issue_account_policy_receipt(policy)

    materialization = {
        "schema_version": "open_stock_ai.portfolio_risk_materialization_input.v1",
        "as_of": as_of,
        "dataset_manifest_hash": manifest,
        "instruments": instruments,
        "account_policy": policy,
    }

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=100.0,
            ohlcv=[{"date": f"2026-07-{day:02d}", "close": float(100 + day)} for day in range(1, 31)],
            raw={"portfolio_risk_materialization": materialization},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    data = session_to_dict("pre_market")

    context = data["portfolio_risk_context"]
    assert context["status"] == "verified"
    assert data["portfolio_construction"]["portfolio_risk_context_source"] == "durable_store"
    assert data["portfolio_construction"]["portfolio_risk_context_receipt"] == context["risk_context_receipt_sha256"]
    assert PortfolioRiskContextStore(db_path).by_receipt(context["risk_context_receipt_sha256"]) == context


def test_portfolio_construction_projects_batch_decisions_without_live_execution():
    decision = {
        "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "confidence": 0.82},
        "research": {
            "sharpe": 2.1,
            "max_drawdown_pct": 8.0,
            "raw": {
                "finrl": {"backtest_projection": {"schema_version": "open_stock_ai.finrl_backtest_projection.v1"}},
                "qlib": {
                    "factor_projection": {
                        "schema_version": "open_stock_ai.qlib_factor_projection.v1",
                        "model_score": 0.61,
                        "rank_ic_proxy": 0.22,
                    }
                },
            },
        },
        "risk": {
            "approved": True,
            "reason": "Approved for paper trading.",
            "adjusted_position_size_pct": 7.5,
            "schema_version": "open_stock_ai.risk_decision.v1",
        },
        "execution": {"executed": False, "mode": "paper"},
        "portfolio_risk_sizing": {
            "annualized_volatility_pct": 20.0,
            "expected_win_probability": 0.60,
            "expected_win_pct": 8.0,
            "expected_loss_pct": 4.0,
            "max_loss_pct": 10.0,
            "risk_budget_pct": 1.0,
        },
    }

    context = {
        "as_of": "2026-08-20T09:00:00+08:00",
        "dataset_manifest_hash": "e" * 64,
        "covariance_matrix": {"2330.TW": {"2330.TW": 0.04}},
        "factor_exposures": {"2330.TW": {"market": 1.0}},
        "factor_limits": {"market": 0.2},
        "risk_context_status": "verified",
        "risk_context_receipt_sha256": "d" * 64,
        "account_risk_status": "verified",
        "account_risk_receipt_sha256": "e" * 64,
        "position_risk_metadata": {
            "2330.TW": {
                "issuer_id": "2330",
                "industry": "semiconductor",
                "currency": "TWD",
                "broker_id": "paper",
                "account_alias": "paper-default",
                "adv_value": 10_000_000.0,
            }
        },
        "concentration_limits": {
            "issuer": 60.0,
            "industry": 100.0,
            "factor": 1.0,
            "currency": 100.0,
            "broker": 100.0,
            "account": 100.0,
        },
        "account_equity": 1_000_000.0,
        "max_participation_rate": 0.20,
        "max_days_to_liquidate": 0.20,
        "historical_returns": {"2330.TW": [0.01, -0.005] * 10},
        "cvar_confidence": 0.95,
        "max_cvar_pct": 5.0,
        "stress_scenarios": [{"scenario_id": "crash", "shocks": {"2330.TW": -0.20}}],
        "max_stress_loss_pct": 5.0,
    }
    projection = PortfolioConstruction().build([decision], session="pre_market", portfolio_risk_context=context)

    assert projection["schema_version"] == "open_stock_ai.portfolio_construction.v5"
    assert projection["method"] == "covariance_and_factor_constrained_paper_portfolio"
    assert projection["session"] == "pre_market"
    assert projection["proposed_total_weight_pct"] == 7.5
    assert projection["constraints"]["paper_only"] is True
    assert projection["constraints"]["live_order_submission"] is False
    assert projection["positions"][0]["target_weight_pct"] == 7.5
    assert projection["positions"][0]["priority_score"] > 0
    assert "open_stock_ai.finrl_backtest_projection.v1" in projection["evidence_schemas"]
    assert "open_stock_ai.qlib_factor_projection.v1" in projection["evidence_schemas"]
    assert "open_stock_ai.portfolio_risk_constraints_receipt.v1" in projection["evidence_schemas"]
    assert projection["portfolio_risk_constraints"]["status"] == "verified"
    assert projection["portfolio_ready"] is True


def test_portfolio_attribution_projects_decision_review_without_execution():
    review = {
        "schema_version": "open_stock_ai.decision_review.v1",
        "symbol": None,
        "items": [
            {
                "symbol": "2330.TW",
                "market": "TW",
                "rating": "Buy",
                "trader_action": "Buy",
                "signal_action": "buy",
                "confidence": 0.8,
                "position_size_pct": 5.0,
                "risk_approved": True,
                "executed": False,
                "lesson": "Risk approved but no paper execution occurred.",
            },
            {
                "symbol": "0050.TW",
                "market": "TW",
                "rating": "Hold",
                "trader_action": "Hold",
                "signal_action": "hold",
                "confidence": 0.6,
                "position_size_pct": 0.0,
                "risk_approved": False,
                "executed": False,
                "lesson": "Risk gates blocked all decisions.",
            },
        ],
    }

    attribution = PortfolioAttribution().build(review)

    assert attribution["schema_version"] == "open_stock_ai.portfolio_attribution.v1"
    assert attribution["method"] == "local_decision_log_portfolio_attribution"
    assert attribution["decision_count"] == 2
    assert attribution["symbol_count"] == 2
    assert attribution["approved_count"] == 1
    assert attribution["blocked_count"] == 1
    assert attribution["proposed_position_size_pct"] == 5.0
    assert attribution["positions"][0]["symbol"] in {"2330.TW", "0050.TW"}
    assert attribution["execution_boundary"] == "replay_attribution_only_no_order_execution"


def test_runtime_connector_governance_blocks_external_runtime_by_default():
    settings = load_settings()
    registry = ExternalProjectRegistry()
    projects = {
        key: asdict(registry.profile(key, capability_terms=[]))
        for key in EXPECTED_REPOSITORY_LOCKS
    }

    governance = RuntimeConnectorGovernance().build(settings=settings, projects=projects)

    assert governance["schema_version"] == "open_stock_ai.runtime_connector_governance.v1"
    assert governance["method"] == "local_external_runtime_connector_policy"
    assert governance["global_connector_enabled"] is False
    assert governance["paper_boundary_enforced"] is True
    assert governance["connector_count"] == 7
    assert governance["blocked_count"] == 7
    assert governance["remote_order_submission_allowed"] is False
    assert governance["risk_engine_required"] is True
    assert governance["paper_executor_required"] is True
    assert governance["available"] is True
    assert any(
        item["key"] == "ai_trader"
        and item["connector_status"] == "blocked_missing_license_review"
        and "license_manual_review_required" in item["blockers"]
        for item in governance["entries"]
    )
    assert any(
        item["key"] == "finrl"
        and item["runtime_role"]["can_submit_remote_orders"] is True
        and item["live_order_submission_allowed"] is False
        for item in governance["entries"]
    )


def test_broker_account_import_governance_blocks_real_account_imports_by_default(monkeypatch):
    monkeypatch.delenv("FUGLE_MARKETDATA_API_KEY", raising=False)
    monkeypatch.delenv("FUGLE_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("FUBON_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("FUBON_CERT_PATH", raising=False)
    monkeypatch.delenv("YUSHAN_BROKER_API_KEY", raising=False)
    monkeypatch.delenv("AI_TRADER_API_KEY", raising=False)
    settings = load_settings()
    registry = ExternalProjectRegistry()
    projects = {
        key: asdict(registry.profile(key, capability_terms=[]))
        for key in EXPECTED_REPOSITORY_LOCKS
    }

    governance = BrokerAccountImportGovernance().build(settings=settings, projects=projects)

    assert governance["schema_version"] == "open_stock_ai.broker_account_import_governance.v1"
    assert governance["method"] == "local_broker_account_import_policy"
    assert governance["broker_account_imports_enabled"] is False
    assert governance["external_credentials_enabled"] is False
    assert governance["paper_ledger_only"] is True
    assert governance["connector_count"] == 3
    assert governance["blocked_count"] == 3
    assert governance["credential_count"] == 0
    assert governance["remote_broker_mutation_allowed"] is False
    assert governance["paper_outcome_import_allowed"] is False
    assert governance["decision_log_replay_allowed"] is True
    assert governance["available"] is True
    assert {item["key"] for item in governance["entries"]} == {"finrl_trading", "finrl", "ai_trader"}
    assert all(item["remote_broker_mutation_allowed"] is False for item in governance["entries"])
    assert all("broker_account_imports_disabled" in item["blockers"] for item in governance["entries"])


def test_outcome_attribution_replays_next_signal_price_without_execution():
    items = [
        {
            "id": 1,
            "created_at": "2026-07-03T00:00:00+00:00",
            "payload": {
                "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
                "market_snapshot": {"price": 100.0},
                "signal": {
                    "symbol": "2330.TW",
                    "market": "TW",
                    "horizon": "swing",
                    "action": "buy",
                    "confidence": 0.8,
                    "entry_price": 100.0,
                    "target_price": 108.0,
                    "stop_loss": 95.0,
                },
            },
        },
        {
            "id": 2,
            "created_at": "2026-07-04T00:00:00+00:00",
            "payload": {
                "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
                "market_snapshot": {"price": 110.0},
                "signal": {
                    "symbol": "2330.TW",
                    "market": "TW",
                    "horizon": "swing",
                    "action": "hold",
                    "confidence": 0.5,
                    "entry_price": 110.0,
                },
            },
        },
    ]

    attribution = OutcomeAttribution().build(items)

    assert attribution["schema_version"] == "open_stock_ai.paper_outcome_attribution.v1"
    assert attribution["method"] == "local_signal_ledger_forward_replay"
    assert attribution["evaluated_count"] == 1
    assert attribution["positive_count"] == 1
    assert attribution["target_hit_count"] == 1
    assert attribution["stop_hit_count"] == 0
    assert attribution["average_directional_return_pct"] == 10.0
    assert attribution["outcomes"][0]["observation_method"] == "next_signal_same_symbol_price"
    assert attribution["execution_boundary"] == "ledger_replay_only_no_order_execution"


def test_open_stock_ai_engine_runs_with_stubbed_market_data(monkeypatch):
    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=100.0,
            ohlcv=[
                {"date": "2026-06-26", "close": 90.0},
                {"date": "2026-06-29", "close": 92.0},
                {"date": "2026-06-30", "close": 94.0},
                {"date": "2026-07-01", "close": 96.0},
                {"date": "2026-07-02", "close": 98.0},
                {"date": "2026-07-03", "close": 100.0},
            ],
            news=[{"title": "sample news"}],
            financials={"revenue": {"yoy_change_percent": 12.5}},
            chips={"margin": {"margin_balance": 1000}},
            raw={"source": "unit-test"},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    decision = build_engine().analyze_stock(StockRequest(symbol="2330.TW", market="TW"))
    payload = asdict(decision)

    assert payload["schema_version"] == "open_stock_ai.stock_decision.v1"
    assert payload["request"]["schema_version"] == "open_stock_ai.stock_request.v1"
    assert payload["market_snapshot"]["schema_version"] == "open_stock_ai.market_snapshot.v1"
    assert payload["intelligence"]["schema_version"] == "open_stock_ai.intelligence_result.v1"
    assert payload["signal"]["schema_version"] == "open_stock_ai.trading_signal.v1"
    assert payload["research"]["schema_version"] == "open_stock_ai.research_result.v1"
    assert payload["execution"]["schema_version"] == "open_stock_ai.execution_result.v1"
    assert payload["request"]["symbol"] == "2330.TW"
    assert payload["market_snapshot"]["price"] == 100.0
    assert payload["intelligence"]["raw"].keys() == {"fingpt", "finrobot", "tradingagents"}
    assert payload["intelligence"]["raw"]["fingpt"]["external_project"]["origin_verified"] is True
    assert payload["intelligence"]["raw"]["finrobot"]["external_project"]["origin_verified"] is True
    assert payload["intelligence"]["raw"]["tradingagents"]["external_project"]["origin_verified"] is True
    assert [item["source_key"] for item in payload["intelligence"]["adapter_results"]] == [
        "fingpt",
        "finrobot",
        "tradingagents",
    ]
    for source_key in ["fingpt", "finrobot", "tradingagents"]:
        adapter_result = payload["intelligence"]["raw"][source_key]["adapter_result"]
        assert adapter_result["schema_version"] == "open_stock_ai.adapter_result.v1"
        assert adapter_result["source_key"] == source_key
        assert adapter_result["role"] == "intelligence"
        assert adapter_result["origin_verified"] is True
        assert adapter_result["lock_verified"] is True
        if source_key in {"fingpt", "finrobot"}:
            assert adapter_result["loaded"] is False
            assert adapter_result["status"] == "disabled"
            provenance = adapter_result["metrics"]["model_provenance"]
            assert provenance["provenance_type"] == "local_baseline"
            assert provenance["model_output"] is False
        else:
            assert adapter_result["loaded"] is True
        assert "adapter_result" not in adapter_result["capability_contract"]
    assert payload["research"]["raw"]["finrl"]["external_projects"]["finrl"]["origin_verified"] is True
    assert payload["research"]["raw"]["finrl"]["external_projects"]["finrl_trading"]["origin_verified"] is True
    assert payload["research"]["raw"]["qlib"]["external_project"]["origin_verified"] is True
    assert [item["source_key"] for item in payload["research"]["adapter_results"]] == ["finrl", "qlib"]
    for source_key in ["finrl", "qlib"]:
        adapter_result = payload["research"]["raw"][source_key]["adapter_result"]
        assert adapter_result["schema_version"] == "open_stock_ai.adapter_result.v1"
        assert adapter_result["source_key"] == source_key
        assert adapter_result["role"] == "research"
        assert adapter_result["origin_verified"] is True
        assert adapter_result["lock_verified"] is True
        assert adapter_result["loaded"] is True
        assert "adapter_result" not in adapter_result["capability_contract"]
    assert payload["research"]["raw"]["finrl"]["method"] == "local_ohlcv_backtest_plus_contract"
    assert payload["research"]["raw"]["finrl"]["capability_contract"]["finrl_contract"]["paper_example_count"] >= 1
    finrl_projection = payload["research"]["raw"]["finrl"]["backtest_projection"]
    assert finrl_projection["schema_version"] == "open_stock_ai.finrl_backtest_projection.v1"
    assert finrl_projection["method"] == "local_finrl_backtest_contract_projection"
    assert finrl_projection["contract"]["has_backtest_engine"] is True
    assert finrl_projection["contract"]["adaptive_rotation_file_count"] >= 1
    assert payload["research"]["raw"]["finrl"]["adapter_result"]["metrics"]["backtest_projection"]["schema_version"] == (
        "open_stock_ai.finrl_backtest_projection.v1"
    )
    assert payload["research"]["raw"]["qlib"]["method"] == "local_factor_score_plus_contract"
    assert payload["research"]["raw"]["qlib"]["capability_contract"]["workflow_contract"]["workflow_count"] >= 1
    assert payload["research"]["raw"]["qlib"]["workflow_summary"]["schema_version"] == "open_stock_ai.qlib_workflow_summary.v1"
    assert payload["research"]["raw"]["qlib"]["workflow_summary"]["selected_workflow"]["model"]
    assert payload["research"]["raw"]["qlib"]["adapter_result"]["metrics"]["workflow_summary"]["method"] == "local_qlib_workflow_projection"
    qlib_projection = payload["research"]["raw"]["qlib"]["factor_projection"]
    assert qlib_projection["schema_version"] == "open_stock_ai.qlib_factor_projection.v1"
    assert qlib_projection["method"] == "local_qlib_factor_projection"
    assert qlib_projection["workflow_summary_schema_version"] == "open_stock_ai.qlib_workflow_summary.v1"
    assert qlib_projection["selected_model"]
    assert qlib_projection["model_score"] is not None
    assert qlib_projection["rank_ic_proxy"] is not None
    assert qlib_projection["research_report"]["schema_version"] == "open_stock_ai.research_artifacts.v1"
    assert Path(qlib_projection["research_report"]["path"]).exists()
    assert payload["research"]["raw"]["qlib"]["adapter_result"]["metrics"]["factor_projection"]["schema_version"] == (
        "open_stock_ai.qlib_factor_projection.v1"
    )
    assert Path(payload["research"]["report_path"]).exists()
    report_text = Path(payload["research"]["report_path"]).read_text(encoding="utf-8")
    assert "## Qlib Factor Projection" in report_text
    assert "Rank IC proxy" in report_text
    assert Path(payload["research"]["raw"]["artifacts"]["backtest_path"]).exists()
    assert payload["research"]["artifacts"]["schema_version"] == "open_stock_ai.research_artifacts.v1"
    assert payload["research"]["artifacts"]["method"] == "local_markdown_report_and_backtest_json"
    assert {item["kind"] for item in payload["research"]["artifacts"]["artifacts"]} == {
        "research_report",
        "backtest_payload",
    }
    for artifact in payload["research"]["artifacts"]["artifacts"]:
        assert artifact["exists"] is True
        assert Path(artifact["path"]).exists()
    backtest_payload = json.loads(Path(payload["research"]["artifacts"]["backtest_path"]).read_text(encoding="utf-8"))
    assert [item["source_key"] for item in backtest_payload["adapter_results"]] == ["finrl", "qlib"]
    assert payload["research"]["raw"]["storage"]["saved"] is True
    assert payload["research"]["raw"]["storage"]["id"]
    assert payload["research"]["raw"]["decision_log"]["saved"] is True
    assert payload["research"]["raw"]["decision_log"]["id"]
    assert payload["intelligence"]["raw"]["fingpt"]["method"] == "local_baseline_sentiment_news_summary_not_fingpt_inference"
    assert payload["intelligence"]["raw"]["fingpt"]["capability_contract"]["benchmark_contract"]["sentiment_template_count"] >= 1
    fingpt_projection = payload["intelligence"]["raw"]["fingpt"]["forecast_projection"]
    assert fingpt_projection["schema_version"] == "open_stock_ai.fingpt_forecast_projection.v1"
    assert fingpt_projection["method"] == "deterministic_forecaster_contract_projection"
    assert fingpt_projection["prompt_contract"]["has_get_all_prompts"] is True
    assert fingpt_projection["prompt_contract"]["has_prompt_end"] is True
    assert payload["intelligence"]["raw"]["fingpt"]["adapter_result"]["metrics"]["forecast_projection"]["schema_version"] == (
        "open_stock_ai.fingpt_forecast_projection.v1"
    )
    finrobot_projection = payload["intelligence"]["raw"]["finrobot"]["report_projection"]
    assert finrobot_projection["schema_version"] == "open_stock_ai.finrobot_report_projection.v1"
    assert finrobot_projection["method"] == "local_equity_report_contract_projection"
    assert finrobot_projection["report_contract"]["section_count"] >= 6
    assert finrobot_projection["agent_contract"]["required_tools_present"]["analyze_income_stmt"] is True
    assert finrobot_projection["agent_contract"]["required_tools_present"]["get_risk_assessment"] is True
    assert payload["intelligence"]["raw"]["finrobot"]["adapter_result"]["metrics"]["report_projection"]["schema_version"] == (
        "open_stock_ai.finrobot_report_projection.v1"
    )
    assert payload["intelligence"]["raw"]["finrobot"]["method"] == "local_baseline_revenue_snapshot_not_finrobot_runtime"
    assert payload["intelligence"]["raw"]["finrobot"]["model_provenance"]["model_output"] is False
    assert payload["intelligence"]["raw"]["tradingagents"]["method"] == "local_structured_analyst_view_plus_contract"
    assert payload["intelligence"]["raw"]["tradingagents"]["capability_contract"]["schema_contract"]["class_count"] >= 1
    assert payload["intelligence"]["raw"]["tradingagents"]["technical_metrics"]["technical_view"] == "uptrend"
    assert payload["signal"]["action"] in {"buy", "add", "hold"}
    assert payload["signal"]["decision_schema"]["rating"] in {"Buy", "Overweight", "Hold"}
    assert payload["signal"]["decision_schema"]["action"] in {"Buy", "Hold"}
    assert "FinGPT forecast:" in payload["signal"]["reason"]
    assert payload["signal"]["ai_trader_validation"]["schema_version"] == "open_stock_ai.ai_trader_signal_validation.v1"
    assert payload["signal"]["ai_trader_validation"]["valid"] is True
    assert payload["signal"]["ai_trader_signal_row"]["symbol"] == "2330.TW"
    assert payload["signal"]["ai_trader_interop_projection"]["schema_version"] == (
        "open_stock_ai.ai_trader_interop_projection.v1"
    )
    assert payload["signal"]["ai_trader_interop_projection"]["method"] == "local_signal_schema_interop_projection"
    assert payload["signal"]["ai_trader_interop_projection"]["valid"] is True
    assert payload["signal"]["ai_trader_interop_projection"]["publishing_boundary"] == "validated_locally_no_remote_publish"
    assert payload["signal"]["ai_trader_skill_route"]["schema_version"] == "open_stock_ai.ai_trader_skill_route.v1"
    assert payload["signal"]["ai_trader_skill_route"]["method"] == "local_skill_frontmatter_route_projection"
    assert payload["signal"]["ai_trader_skill_route"]["route_ready"] is True
    assert "ai-trader" in payload["signal"]["ai_trader_skill_route"]["selected_skill_names"]
    assert payload["signal"]["ai_trader_skill_route"]["execution_boundary"] == (
        "read_only_skill_route_no_remote_ai_trader_publish"
    )
    assert payload["signal"]["external_risk_evidence"]["schema_version"] == "open_stock_ai.tradingagents_risk_debate.v1"
    assert payload["signal"]["external_risk_evidence"]["risk_debator_count"] == 3
    assert payload["signal"]["external_report_evidence"]["schema_version"] == "open_stock_ai.finrobot_report_projection.v1"
    if payload["signal"]["action"] in {"buy", "add"}:
        # The normal interactive data path is intentionally not an immutable
        # historical replay source, so it must not invent a fixed target/stop.
        assert payload["signal"]["target_price"] is None
        assert payload["signal"]["stop_loss"] is None
        assert payload["signal"]["price_method_id"] is None
        assert payload["signal"]["decision_schema"]["source_ratings"]["target_stop_calibration"]["status"] == "unavailable"
        assert payload["signal"]["position_size_pct"] == 0.0
        assert payload["risk"]["max_position_size_pct"] > 0
    assert payload["risk"]["approved"] is False
    assert payload["risk"]["schema_version"] == "open_stock_ai.risk_decision.v1"
    assert payload["risk"]["policy"]["max_position_size_pct"] == 10.0
    assert {item["code"] for item in payload["risk"]["gate_checks"]}.issuperset(
        {
            "rule_score_threshold",
            "tradingagents_risk_evidence",
            "finrobot_report_evidence",
            "finrl_backtest_evidence",
            "qlib_factor_evidence",
            "research_validation",
            "max_drawdown",
            "position_size_required",
            "position_size_cap",
            "stop_loss_required",
            "estimated_daily_loss",
            "paper_exposure",
        }
    )
    assert payload["execution"]["executed"] is False
    assert payload["execution"]["mode"] == "paper"


def test_risk_engine_rejects_drawdown_above_configured_limit():
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        stop_loss=95.0,
        position_size_pct=3.0,
    )
    research = ResearchResult(
        passed=True,
        summary="test",
        sharpe=2.0,
        max_drawdown_pct=12.0,
    )

    decision = RiskEngine(max_total_drawdown_pct=10.0).evaluate(
        StockRequest(symbol="2330.TW", market="TW"),
        signal,
        research,
    )

    assert decision.approved is False
    assert decision.schema_version == "open_stock_ai.risk_decision.v1"
    assert decision.reason == "Backtest drawdown exceeds configured risk limit."
    assert "Drawdown 12.00% > limit 10.00%." in decision.risk_notes
    drawdown_gate = next(item for item in decision.gate_checks if item["code"] == "max_drawdown")
    assert drawdown_gate["passed"] is False


def test_risk_engine_blocks_signal_when_durable_scope_switch_is_active(tmp_path):
    control = DurableRiskControlStore(tmp_path / "risk-control.sqlite")
    control.activate("symbol", "2330.TW", "manual review", "test")
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        stop_loss=95.0,
        position_size_pct=3.0,
    )
    research = ResearchResult(passed=True, summary="test", sharpe=2.0, max_drawdown_pct=2.0)

    decision = RiskEngine(risk_control=control).evaluate(
        StockRequest(symbol="2330.TW", market="TW"), signal, research
    )

    assert decision.approved is False
    assert decision.reason == "Durable risk control switch is active."
    assert decision.gate_checks[0]["code"] == "durable_risk_control"


@pytest.mark.parametrize("unsafe_size", [None, 0.0, -1.0, float("nan"), float("inf"), float("-inf")])
def test_risk_engine_never_turns_missing_zero_negative_or_nonfinite_sizing_into_maximum_position(unsafe_size):
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        stop_loss=95.0,
        position_size_pct=unsafe_size,
    )
    research = ResearchResult(passed=True, summary="test", sharpe=2.0, max_drawdown_pct=2.0)

    decision = RiskEngine().evaluate(StockRequest(symbol="2330.TW", market="TW"), signal, research)

    assert decision.approved is False
    assert decision.adjusted_position_size_pct == 0.0
    assert decision.reason == "Executable signal is missing an explicit positive position size."
    sizing_gate = next(item for item in decision.gate_checks if item["code"] == "position_size_required")
    assert sizing_gate["passed"] is False


def test_risk_engine_rejects_stop_exposure_above_daily_loss_limit():
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        stop_loss=50.0,
        position_size_pct=10.0,
    )
    research = ResearchResult(
        passed=True,
        summary="test",
        sharpe=2.0,
        max_drawdown_pct=2.0,
    )

    decision = RiskEngine(max_daily_loss_pct=3.0).evaluate(
        StockRequest(symbol="2330.TW", market="TW"),
        signal,
        research,
    )

    assert decision.approved is False
    assert decision.schema_version == "open_stock_ai.risk_decision.v1"
    assert decision.reason == "Estimated stop-loss exposure exceeds configured daily loss limit."
    assert "Estimated loss 5.00% > daily limit 3.00%." in decision.risk_notes
    daily_loss_gate = next(item for item in decision.gate_checks if item["code"] == "estimated_daily_loss")
    assert daily_loss_gate["passed"] is False


def test_risk_engine_rejects_symbol_exposure_above_configured_limit():
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="test",
        entry_price=100.0,
        stop_loss=95.0,
        position_size_pct=5.0,
    )
    research = ResearchResult(passed=True, summary="test", sharpe=2.0, max_drawdown_pct=2.0)
    paper_exposure = {
        "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
        "total_position_size_pct": 18.0,
        "symbols": {"2330.TW": {"position_size_pct": 18.0}},
    }

    decision = RiskEngine(max_symbol_exposure_pct=20.0).evaluate(
        StockRequest(symbol="2330.TW", market="TW"),
        signal,
        research,
        paper_exposure=paper_exposure,
    )

    assert decision.approved is False
    assert decision.schema_version == "open_stock_ai.risk_decision.v1"
    assert decision.reason == "Paper portfolio symbol exposure exceeds configured limit."
    assert "Symbol exposure 23.00% > limit 20.00%." in decision.risk_notes
    exposure_gate = next(item for item in decision.gate_checks if item["code"] == "paper_exposure")
    assert exposure_gate["passed"] is False


def test_open_stock_ai_api_contract(monkeypatch):
    def fake_analyze_to_dict(symbol="2330.TW", market="TW", horizon="swing"):
        return {
            "request": {"symbol": symbol, "market": market, "horizon": horizon},
            "market_snapshot": {"price": 100.0},
            "signal": {"action": "hold", "confidence": 0.5},
            "risk": {"approved": False},
            "execution": {"executed": False, "mode": "paper"},
        }

    monkeypatch.setattr("open_stock_ai.api.analyze_to_dict", fake_analyze_to_dict)
    res = client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")

    assert res.status_code == 200
    data = res.json()
    assert data["request"]["symbol"] == "2330.TW"
    assert data["execution"]["mode"] == "paper"
    assert data["risk"]["approved"] is False


def test_open_stock_ai_routes_are_owned_by_open_stock_ai_router():
    import stock_ai.main as stock_main
    from open_stock_ai.api import router

    route_paths = {str(path) for route in router.routes if (path := getattr(route, 'path', None)) is not None}

    assert not hasattr(stock_main, "analyze_to_dict")
    assert "/api/open-stock-ai/analyze" in route_paths
    assert "/api/open-stock-ai/integration-audit" in route_paths
    assert "/api/open-stock-ai/broker-import-governance" in route_paths
    assert "/api/open-stock-ai/optional-external-sources" in route_paths


def test_open_stock_ai_rejects_invalid_market_or_horizon():
    bad_market = client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=JP")
    bad_horizon = client.get("/api/open-stock-ai/analyze?symbol=2330.TW&horizon=seconds")
    missing_symbol = client.get("/api/open-stock-ai/analyze?symbol=")

    assert bad_market.status_code == 422
    assert bad_horizon.status_code == 422
    assert missing_symbol.status_code == 422
    assert "explicit symbol" in missing_symbol.json()["detail"]


def test_open_stock_ai_optional_external_sources_endpoint_excludes_freqtrade():
    res = client.get("/api/open-stock-ai/optional-external-sources")

    assert res.status_code == 200
    data = res.json()
    assert data["schema_version"] == "open_stock_ai.optional_external_source_registry.v1"
    assert data["method"] == "integration_brief_optional_source_policy"
    assert data["optional_count"] == 1
    assert data["excluded_count"] == 1
    assert data["unexpected_clone_count"] == 0
    assert data["available"] is True
    assert data["entries"][0]["key"] == "freqtrade"
    assert data["entries"][0]["repository"] == "https://github.com/freqtrade/freqtrade"
    assert data["entries"][0]["approved"] is False
    assert data["entries"][0]["source_lock_member"] is False
    assert data["entries"][0]["status"] == "not_approved_not_cloned"
    assert data["entries"][0]["allowed_boundary"] == "not_loaded_not_imported_not_in_source_lock"


def test_open_stock_ai_external_source_lock_endpoint_reports_clone_commands():
    res = client.get("/api/open-stock-ai/external-source-lock")

    assert res.status_code == 200
    data = res.json()
    assert data["schema_version"] == "open_stock_ai.external_source_lock.v1"
    assert data["method"] == "local_git_origin_head_lock"
    assert data["count"] == 7
    assert data["lock_verified_count"] == 7
    assert data["all_locked"] is True
    commands = {item["key"]: item["clone_command"] for item in data["entries"]}
    assert commands["tradingagents"] == "git clone https://github.com/TauricResearch/TradingAgents.git external/TradingAgents"
    for item in data["entries"]:
        expected = EXPECTED_REPOSITORY_LOCKS[item["key"]]
        assert item["expected_origin"] == expected["origin"]
        assert item["expected_head"] == expected["head"]
        assert item["head"] == expected["head"]
        assert item["origin_verified"] is True
        assert item["head_verified"] is True
        assert item["lock_verified"] is True


def test_open_stock_ai_storage_endpoint_reports_sqlite_counts():
    res = client.get("/api/open-stock-ai/storage")

    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "open_stock_ai_storage_stats"
    assert data["schema_version"] == "open_stock_ai.storage_stats.v1"
    assert data["configured"] is True
    assert data["db_path"].endswith("open_stock_ai.sqlite")
    assert data["signals"] >= 0
    assert data["reports"] >= 0
    assert data["trades"] >= 0
    assert data["decision_logs"] >= 0


def test_open_stock_ai_signals_endpoint_reports_ai_trader_schema_validation(tmp_path, monkeypatch):
    _use_tmp_open_stock_ai_config(monkeypatch, tmp_path)

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=140.0,
            ohlcv=[
                {"date": f"2026-06-{day:02d}", "close": float(100 + day)}
                for day in range(1, 31)
            ],
            news=[{"title": "Company reports strong growth and record profit"}],
            financials={"revenue": {"yoy_change_percent": 24.0}},
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)
    client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")
    res = client.get("/api/open-stock-ai/signals?limit=5")

    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "open_stock_ai_signal_ledger"
    assert data["schema_version"] == "open_stock_ai.signal_ledger.v1"
    assert data["count"] >= 1
    assert data["items"][0]["schema_version"] == "open_stock_ai.signal_ledger_row.v1"
    assert data["items"][0]["ai_trader_valid"] is True
    assert data["items"][0]["ai_trader_schema"] == "signals.csv research export row"
    assert data["items"][0]["payload"]["signal"]["ai_trader_validation"]["valid"] is True
    assert data["items"][0]["payload"]["signal"]["ai_trader_signal_row"]["symbol"] == "2330.TW"


def test_open_stock_ai_storage_endpoint_uses_configured_sqlite_path(tmp_path, monkeypatch):
    db_path = tmp_path / "api-configured.sqlite"
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f"""
storage:
  sqlite_path: "{db_path.as_posix()}"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_STOCK_AI_CONFIG", str(config_path))

    res = client.get("/api/open-stock-ai/storage")

    assert res.status_code == 200
    assert res.json()["db_path"] == str(db_path)


def test_open_stock_ai_decision_log_endpoint_reports_recent_entries(tmp_path, monkeypatch):
    _use_tmp_open_stock_ai_config(monkeypatch, tmp_path)
    client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")
    res = client.get("/api/open-stock-ai/decision-log?limit=5")

    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "open_stock_ai_decision_log_ledger"
    assert data["schema_version"] == "open_stock_ai.decision_log_ledger.v1"
    assert data["count"] >= 1
    assert data["items"][0]["schema_version"] == "open_stock_ai.decision_log_row.v1"
    assert data["items"][0]["symbol"]
    assert data["items"][0]["rating"] in {"Buy", "Overweight", "Hold", "Underweight", "Sell"}
    assert data["items"][0]["trader_action"] in {"Buy", "Hold", "Sell"}


def test_open_stock_ai_decision_review_endpoint_reports_replay_summary(tmp_path, monkeypatch):
    _use_tmp_open_stock_ai_config(monkeypatch, tmp_path)
    client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")
    res = client.get("/api/open-stock-ai/decision-review?symbol=2330.TW&limit=20")

    assert res.status_code == 200
    data = res.json()
    assert data["method"] == "tradingagents_style_decision_replay"
    assert data["schema_version"] == "open_stock_ai.decision_review.v1"
    assert data["symbol"] == "2330.TW"
    assert data["total"] >= 1
    assert 0 <= data["approval_rate"] <= 1
    assert 0 <= data["execution_rate"] <= 1
    assert data["ratings"]
    assert data["latest"]["symbol"] == "2330.TW"
    assert data["lessons"]
    assert data["reflection_replay"]["schema_version"] == "open_stock_ai.tradingagents_reflection_replay.v1"
    assert data["reflection_replay"]["method"] == "local_tradingagents_reflection_memory_projection"
    assert data["reflection_replay"]["review_schema_version"] == "open_stock_ai.decision_review.v1"
    assert data["reflection_replay"]["memory_ready"] is True
    assert data["reflection_replay"]["reflection_contract"]["path"] == "tradingagents/graph/reflection.py"
    assert data["reflection_replay"]["execution_boundary"] == (
        "read_only_reflection_replay_no_tradingagents_runtime_import"
    )
    assert data["portfolio_attribution"]["schema_version"] == "open_stock_ai.portfolio_attribution.v1"
    assert data["portfolio_attribution"]["method"] == "local_decision_log_portfolio_attribution"
    assert data["portfolio_attribution"]["available"] is True
    assert data["portfolio_attribution"]["symbol_count"] >= 1
    assert data["portfolio_attribution"]["decision_count"] == data["total"]
    assert data["portfolio_attribution"]["execution_boundary"] == "replay_attribution_only_no_order_execution"


def test_open_stock_ai_integration_audit_reports_system_invariants(tmp_path, monkeypatch):
    _use_tmp_open_stock_ai_config(monkeypatch, tmp_path)
    client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")
    res = client.get("/api/open-stock-ai/integration-audit?symbol=2330.TW&limit=20")

    assert res.status_code == 200
    data = res.json()
    invariant_names = {item["name"] for item in data["invariants"]}

    assert data["method"] == "open_stock_ai_integration_audit"
    assert data["entrypoint"] == "src/open_stock_ai"
    assert data["external_sources"]["verified_count"] == 7
    assert data["external_sources"]["all_verified"] is True
    assert data["external_sources"]["lock_verified_count"] == 7
    assert data["external_sources"]["all_locked"] is True
    assert data["external_source_lock"]["schema_version"] == "open_stock_ai.external_source_lock.v1"
    assert data["external_source_lock"]["all_locked"] is True
    assert data["external_source_lock"]["missing_projects"] == []
    assert data["optional_external_sources"]["available"] is True
    assert data["optional_external_sources"]["schema_version"] == "open_stock_ai.optional_external_source_registry.v1"
    assert data["optional_external_sources"]["optional_count"] == 1
    assert data["optional_external_sources"]["excluded_count"] == 1
    assert data["optional_external_sources"]["unexpected_clone_count"] == 0
    assert data["optional_external_sources"]["entries"][0]["key"] == "freqtrade"
    assert data["optional_external_sources"]["entries"][0]["source_lock_member"] is False
    assert data["contracts"]["loaded_count"] == 6
    assert data["contracts"]["missing"] == []
    assert data["adapter_schema"]["version"] == "open_stock_ai.adapter_result.v1"
    assert data["adapter_schema"]["contract_count"] == 6
    assert data["adapter_schema"]["missing"] == []
    assert data["external_evidence_lineage"]["available"] is True
    assert data["external_evidence_lineage"]["schema_version"] == "open_stock_ai.external_evidence_lineage.v1"
    assert data["external_evidence_lineage"]["expected_count"] == 7
    assert data["external_evidence_lineage"]["traced_count"] == 7
    assert data["external_evidence_lineage"]["missing_sources"] == []
    assert data["external_evidence_lineage"]["unverified_sources"] == []
    assert data["external_evidence_lineage"]["artifact_count"] >= 7
    assert set(data["external_evidence_lineage"]["traced_sources"]) == {
        "tradingagents",
        "fingpt",
        "finrobot",
        "finrl_trading",
        "finrl",
        "qlib",
        "ai_trader",
    }
    assert all(item["lock_verified"] is True for item in data["external_evidence_lineage"]["entries"])
    assert data["external_license_footprint"]["available"] is True
    assert data["external_license_footprint"]["schema_version"] == "open_stock_ai.external_license_footprint.v1"
    assert data["external_license_footprint"]["project_count"] == 7
    assert data["external_license_footprint"]["reviewed_count"] == 7
    assert data["external_license_footprint"]["licensed_count"] == 6
    assert data["external_license_footprint"]["missing_license_count"] == 1
    assert any(
        item["key"] == "ai_trader"
        and item["license_status"] == "license_metadata_detected_missing_license_text"
        and item["license_name"] == "MIT"
        and item["license_evidence_path"]
        for item in data["external_license_footprint"]["items"]
    )
    assert data["runtime_connector_governance"]["available"] is True
    assert data["runtime_connector_governance"]["schema_version"] == "open_stock_ai.runtime_connector_governance.v1"
    assert data["runtime_connector_governance"]["blocked_count"] == 7
    assert data["runtime_connector_governance"]["connector_count"] == 7
    assert data["runtime_connector_governance"]["remote_order_submission_allowed"] is False
    assert data["runtime_connector_governance"]["risk_engine_required"] is True
    assert data["runtime_connector_governance"]["paper_executor_required"] is True
    assert data["broker_import_governance"]["available"] is True
    assert (
        data["broker_import_governance"]["schema_version"]
        == "open_stock_ai.broker_account_import_governance.v1"
    )
    assert data["broker_import_governance"]["broker_account_imports_enabled"] is False
    assert data["broker_import_governance"]["external_credentials_enabled"] is False
    assert data["broker_import_governance"]["connector_count"] == 3
    assert data["broker_import_governance"]["blocked_count"] == 3
    assert data["broker_import_governance"]["credential_count"] == 0
    assert data["broker_import_governance"]["remote_broker_mutation_allowed"] is False
    assert data["broker_import_governance"]["paper_outcome_import_allowed"] is False
    assert data["broker_import_governance"]["decision_log_replay_allowed"] is True
    assert {item["key"] for item in data["broker_import_governance"]["entries"]} == {
        "finrl_trading",
        "finrl",
        "ai_trader",
    }
    assert data["external_project_contribution_matrix"]["available"] is True
    assert (
        data["external_project_contribution_matrix"]["schema_version"]
        == "open_stock_ai.external_project_contribution_matrix.v1"
    )
    assert data["external_project_contribution_matrix"]["project_count"] == 7
    assert data["external_project_contribution_matrix"]["traced_count"] == 7
    assert data["external_project_contribution_matrix"]["locked_count"] == 7
    assert data["external_project_contribution_matrix"]["remote_order_bypass_count"] == 0
    contribution_rows = {
        item["key"]: item for item in data["external_project_contribution_matrix"]["rows"]
    }
    assert set(contribution_rows) == {
        "tradingagents",
        "finrobot",
        "fingpt",
        "finrl_trading",
        "finrl",
        "qlib",
        "ai_trader",
    }
    assert contribution_rows["finrl_trading"]["contract_source_key"] == "finrl"
    assert "open_stock_ai.portfolio_construction.v5" in contribution_rows["finrl_trading"]["projection_schemas"]
    assert {"intelligence", "risk", "replay"}.issubset(contribution_rows["tradingagents"]["pipeline_stages"])
    assert "open_stock_ai.tradingagents_reflection_replay.v1" in contribution_rows["tradingagents"]["projection_schemas"]
    assert "open_stock_ai.ai_trader_interop_projection.v1" in contribution_rows["ai_trader"]["projection_schemas"]
    assert "open_stock_ai.ai_trader_skill_route.v1" in contribution_rows["ai_trader"]["projection_schemas"]
    assert all(item["requires_central_risk_engine"] is True for item in contribution_rows.values())
    assert all(item["can_bypass_risk_engine"] is False for item in contribution_rows.values())
    assert data["data_source_schema"]["available"] is True
    assert data["data_source_schema"]["schema_version"] == "open_stock_ai.data_source_envelope.v1"
    assert set(data["data_source_schema"]["sources"]) == {"twse", "tpex", "yahoo", "mops", "news"}
    assert data["stock_decision_schema"]["available"] is True
    assert data["stock_decision_schema"]["schema_version"] == "open_stock_ai.stock_decision.v1"
    assert data["stock_decision_schema"]["missing"] == []
    assert data["module_surface"]["complete"] is True
    assert data["module_surface"]["dirs_complete"] is True
    assert data["module_surface"]["no_placeholders"] is True
    assert data["module_surface"]["missing"] == []
    assert data["module_surface"]["missing_dirs"] == []
    assert data["api_router"]["mounted"] is True
    assert data["api_router"]["router_path"] == "src\\open_stock_ai\\api.py" or data["api_router"]["router_path"] == "src/open_stock_ai/api.py"
    assert data["api_router"]["route_count"] >= 10
    assert data["api_router"]["direct_app_route_count"] == 0
    assert data["design_system_contract"]["available"] is True
    assert data["design_system_contract"]["schema_version"] == "open_stock_ai.design_system_contract.v1"
    assert data["design_system_contract"]["component_count"] == 5
    assert data["design_system_contract"]["satisfied_count"] == 5
    assert data["design_system_contract"]["external_ui_sources"]["liquidgl"]["origin"] == "https://github.com/naughtyduk/liquidGL.git"
    assert data["design_system_contract"]["external_ui_sources"]["liquidgl"]["head"] == "2cef983b7fe593d3e0878dc78e5b79b47038a953"
    assert data["design_system_contract"]["external_ui_sources"]["liquidgl"]["loaded"] is True
    assert data["design_system_contract"]["external_ui_sources"]["liquidgl"]["mode"] == "webgl_realtime_document_uv_canvas_patch_multi_lens"
    assert data["design_system_contract"]["external_ui_sources"]["glinui"]["origin"] == "https://github.com/glincker/glinui.git"
    assert data["design_system_contract"]["external_ui_sources"]["glinui"]["head"] == "2e19376efbb601f915239ed9571b1c9a3fa9fb7e"
    assert data["design_system_contract"]["external_ui_sources"]["glinui"]["loaded"] is True
    assert data["design_system_contract"]["external_ui_sources"]["glinui"]["mode"] == "token_bridge_plus_native_webgl"
    assert data["design_system_contract"]["external_ui_sources"]["attribution_exists"] is True
    assert data["stock_app_requirement_contract"]["schema_version"] == "stock_ai.requirement_contract.v1"
    assert data["stock_app_requirement_contract"]["coverage"]["source_tier_count"] == 3
    assert data["stock_app_requirement_contract"]["coverage"]["data_module_count"] == 14
    assert data["stock_app_requirement_contract"]["coverage"]["app_feature_count"] == 13
    assert data["stock_app_requirement_contract"]["coverage"]["schedule_window_count"] == 5
    assert data["stock_app_requirement_contract"]["coverage"]["live_ordering_enabled"] is False
    assert data["stock_app_requirement_contract"]["coverage"]["source_policy_schema"] == "stock_ai.source_policy.v1"
    assert data["stock_app_requirement_contract"]["coverage"]["update_runner_schema"] == "stock_ai.update_runner.v1"
    assert data["stock_app_requirement_contract"]["coverage"]["official_derivatives_schema"] == "stock_ai.official_derivatives.v1"
    assert data["stock_app_requirement_contract"]["coverage"]["official_events_schema"] == "stock_ai.official_events.v1"
    assert data["stock_app_requirement_contract"]["coverage"]["mops_company_events_import_endpoint"] == "/api/official/mops/company-events/import"
    assert data["stock_app_requirement_contract"]["coverage"]["tdcc_holding_distribution_import_endpoint"] == "/api/official/tdcc/holding-distribution/import"
    assert data["stock_app_requirement_contract"]["coverage"]["taifex_derivatives_import_endpoint"] == "/api/official/taifex/derivatives-summary/import"
    assert data["stock_app_source_policy"]["schema_version"] == "stock_ai.source_policy.v1"
    assert data["stock_app_source_policy"]["guardrails_enforced"]["auxiliary_source_cannot_be_sole_price_source"] is True
    assert data["stock_app_source_policy"]["guardrails_enforced"]["complete_decision_evidence_allowed"] is True
    assert data["stock_app_source_policy"]["guardrails_enforced"]["source_conflict_requires_note"] is True
    assert data["stock_app_update_plan"]["schema_version"] == "stock_ai.update_runner.v1"
    assert data["stock_app_update_plan"]["dry_run"] is True
    assert data["stock_app_update_plan"]["job_count"] == 18
    assert data["stock_app_update_run"]["schema_version"] == "stock_ai.update_run.v1"
    assert data["stock_app_update_run"]["mutated"] is False
    assert data["stock_app_official_derivatives"]["schema_version"] == "stock_ai.official_derivatives.v1"
    assert data["stock_app_official_derivatives"]["dry_run"] is True
    assert data["stock_app_official_derivatives"]["live_trading_source"] is False
    assert data["stock_app_official_derivatives"]["required_source_count"] == 2
    assert data["stock_app_official_events"]["schema_version"] == "stock_ai.official_events.v1"
    assert data["stock_app_official_events"]["dry_run"] is True
    assert data["stock_app_official_events"]["live_trading_source"] is False
    assert data["stock_app_official_events"]["required_source_count"] == 1
    assert "stock_app_goal_requirement_contract_available" in invariant_names
    assert "stock_app_source_policy_gate_available" in invariant_names
    assert "stock_app_official_derivatives_contract_available" in invariant_names
    assert "stock_app_official_events_contract_available" in invariant_names
    assert "stock_app_update_runner_dry_run_available" in invariant_names
    assert {item["key"] for item in data["design_system_contract"]["components"]} == {
        "shadcn_ui",
        "magic_ui",
        "aceternity_ui",
        "agent_elements",
        "tailwind_tokens",
    }
    assert data["runtime"]["paper_only"] is True
    assert data["paper_ledger"]["available"] is True
    assert data["paper_ledger"]["schema_version"] == "open_stock_ai.paper_order.v1"
    assert data["paper_ledger"]["row_schema_version"] == "open_stock_ai.paper_order.v1"
    assert data["research_artifacts"]["available"] is True
    assert data["research_artifacts"]["schema_version"] == "open_stock_ai.research_artifacts.v1"
    assert data["research_artifacts"]["existing_count"] == data["research_artifacts"]["artifact_count"]
    assert data["qlib_workflow_summary"]["available"] is True
    assert data["qlib_workflow_summary"]["schema_version"] == "open_stock_ai.qlib_workflow_summary.v1"
    assert data["qlib_workflow_summary"]["selected_model"]
    assert data["qlib_factor_projection"]["available"] is True
    assert data["qlib_factor_projection"]["schema_version"] == "open_stock_ai.qlib_factor_projection.v1"
    assert data["qlib_factor_projection"]["selected_model"]
    assert data["qlib_factor_projection"]["model_score"] is not None
    assert data["qlib_factor_projection"]["rank_ic_proxy"] is not None
    assert data["qlib_factor_projection"]["research_report_exists"] is True
    assert data["finrl_backtest_projection"]["available"] is True
    assert data["finrl_backtest_projection"]["schema_version"] == "open_stock_ai.finrl_backtest_projection.v1"
    assert data["finrl_backtest_projection"]["has_backtest_engine"] is True
    assert data["fingpt_forecast_projection"]["available"] is True
    assert data["fingpt_forecast_projection"]["schema_version"] == "open_stock_ai.fingpt_forecast_projection.v1"
    assert data["fingpt_forecast_projection"]["direction"] in {"up", "down", "flat"}
    assert data["finrobot_report_projection"]["available"] is True
    assert data["finrobot_report_projection"]["schema_version"] == "open_stock_ai.finrobot_report_projection.v1"
    assert data["finrobot_report_projection"]["report_view"] in {"positive", "negative", "neutral"}
    assert data["ai_trader_trade_schema"]["available"] is True
    assert data["ai_trader_trade_schema"]["validated_count"] == data["ai_trader_trade_schema"]["checked_count"]
    assert data["ai_trader_signal_schema"]["available"] is True
    assert data["ai_trader_signal_schema"]["validated_count"] == data["ai_trader_signal_schema"]["checked_count"]
    assert data["ai_trader_interop_projection"]["available"] is True
    assert data["ai_trader_interop_projection"]["schema_version"] == "open_stock_ai.ai_trader_interop_projection.v1"
    assert data["ai_trader_interop_projection"]["signal_valid"] is True
    assert data["ai_trader_skill_route"]["available"] is True
    assert data["ai_trader_skill_route"]["schema_version"] == "open_stock_ai.ai_trader_skill_route.v1"
    assert data["ai_trader_skill_route"]["route_ready"] is True
    assert "ai-trader" in data["ai_trader_skill_route"]["selected_skill_names"]
    assert data["signal_ledger"]["available"] is True
    assert data["signal_ledger"]["schema_version"] == "open_stock_ai.signal_ledger.v1"
    assert data["signal_ledger"]["row_schema_version"] == "open_stock_ai.signal_ledger_row.v1"
    assert data["paper_exposure"]["available"] is True
    assert data["paper_exposure"]["schema_version"] == "open_stock_ai.paper_portfolio_exposure.v1"
    assert data["portfolio_construction"]["available"] is True
    assert data["portfolio_construction"]["schema_version"] == "open_stock_ai.portfolio_construction.v5"
    assert data["portfolio_construction"]["method"] == "covariance_and_factor_constrained_paper_portfolio"
    assert data["portfolio_construction"]["positions"]
    assert data["portfolio_construction"]["constraints"]["paper_only"] is True
    assert "open_stock_ai.finrl_backtest_projection.v1" in data["portfolio_construction"]["evidence_schemas"]
    assert "open_stock_ai.qlib_factor_projection.v1" in data["portfolio_construction"]["evidence_schemas"]
    assert data["outcome_attribution"]["available"] is True
    assert data["outcome_attribution"]["schema_version"] == "open_stock_ai.paper_outcome_attribution.v1"
    assert data["outcome_attribution"]["method"] == "local_signal_ledger_forward_replay"
    assert data["outcome_attribution"]["observation_count"] >= 1
    assert data["outcome_attribution"]["evaluated_count"] >= 0
    assert data["outcome_attribution"]["execution_boundary"] == "ledger_replay_only_no_order_execution"
    assert data["requirement_matrix"]["available"] is True, data["requirement_matrix"]["unsatisfied"]
    assert data["requirement_matrix"]["schema_version"] == "open_stock_ai.integration_requirement_matrix.v1"
    assert data["requirement_matrix"]["requirement_count"] >= 11
    assert data["requirement_matrix"]["satisfied_count"] == data["requirement_matrix"]["requirement_count"]
    assert data["requirement_matrix"]["unsatisfied_count"] == 0
    assert any(
        item["type"] == "external_license_review" and item["key"] == "ai_trader"
        for item in data["requirement_matrix"]["review_items"]
    )
    assert any(
        item["type"] == "broker_account_import_disabled" and item["key"] == "ai_trader"
        for item in data["requirement_matrix"]["review_items"]
    )
    assert data["runtime"]["live_trading_enabled"] is False
    assert data["runtime_boundary"]["clean"] is True
    assert data["runtime_boundary"]["scanned_files"] >= 1
    assert data["risk"]["require_backtest_passed"] is True
    assert data["risk_decision_schema"]["available"] is True
    assert data["risk_decision_schema"]["schema_version"] == "open_stock_ai.risk_decision.v1"
    assert "paper_exposure" in data["risk_decision_schema"]["codes"]
    assert "tradingagents_risk_evidence" in data["risk_decision_schema"]["codes"]
    assert "finrobot_report_evidence" in data["risk_decision_schema"]["codes"]
    assert "finrl_backtest_evidence" in data["risk_decision_schema"]["codes"]
    assert "qlib_factor_evidence" in data["risk_decision_schema"]["codes"]
    assert (
        data["risk_decision_schema"]["tradingagents_risk_evidence"]["schema_version"]
        == "open_stock_ai.tradingagents_risk_debate.v1"
    )
    assert (
        data["risk_decision_schema"]["finrobot_report_evidence"]["schema_version"]
        == "open_stock_ai.finrobot_report_projection.v1"
    )
    assert (
        data["risk_decision_schema"]["finrl_backtest_evidence"]["schema_version"]
        == "open_stock_ai.finrl_backtest_projection.v1"
    )
    assert (
        data["risk_decision_schema"]["qlib_factor_evidence"]["schema_version"]
        == "open_stock_ai.qlib_factor_projection.v1"
    )
    assert data["risk_decision_schema"]["qlib_factor_evidence"]["model_score"] is not None
    assert data["risk_decision_schema"]["qlib_factor_evidence"]["rank_ic_proxy"] is not None
    assert data["storage"]["configured"] is True
    assert data["storage"]["schema_version"] == "open_stock_ai.storage_stats.v1"
    assert data["replay"]["method"] == "tradingagents_style_decision_replay"
    assert data["replay"]["schema_version"] == "open_stock_ai.decision_review.v1"
    assert data["reflection_replay"]["available"] is True
    assert data["reflection_replay"]["schema_version"] == "open_stock_ai.tradingagents_reflection_replay.v1"
    assert data["reflection_replay"]["reflection_exists"] is True
    assert data["reflection_replay"]["risk_debator_count"] == 3
    assert data["portfolio_attribution"]["available"] is True
    assert data["portfolio_attribution"]["schema_version"] == "open_stock_ai.portfolio_attribution.v1"
    assert data["portfolio_attribution"]["decision_count"] >= 1
    assert data["portfolio_attribution"]["execution_boundary"] == "replay_attribution_only_no_order_execution"
    assert data["decision_log_ledger"]["available"] is True
    assert data["decision_log_ledger"]["schema_version"] == "open_stock_ai.decision_log_ledger.v1"
    assert data["decision_log_ledger"]["row_schema_version"] == "open_stock_ai.decision_log_row.v1"
    assert data["ready"] is True
    assert data["invariants_passed"] == data["invariants_total"]
    assert {
        "src_open_stock_ai_entrypoint",
        "open_stock_ai_design_system_contract_available",
        "required_open_stock_ai_module_surface_available",
        "all_expected_external_sources_verified",
        "external_source_git_lock_verified",
        "optional_external_sources_excluded",
        "all_required_contracts_loaded",
        "unified_adapter_result_schema_available",
        "external_evidence_lineage_available",
        "external_license_footprint_reviewed",
        "runtime_connector_governance_enforced",
        "broker_account_import_governance_enforced",
        "external_project_contribution_matrix_available",
        "unified_market_data_source_envelopes_available",
        "unified_stock_decision_schema_available",
        "unified_risk_gate_configured",
        "unified_risk_decision_schema_available",
        "paper_execution_only",
        "paper_order_ledger_available",
        "research_artifacts_available",
        "qlib_workflow_summary_available",
        "qlib_factor_projection_available",
        "finrl_backtest_projection_available",
        "fingpt_forecast_projection_available",
        "finrobot_report_projection_available",
        "signals_ai_trader_schema_validated",
        "ai_trader_interop_projection_available",
        "ai_trader_skill_route_available",
        "signal_ledger_schema_available",
        "paper_orders_ai_trader_schema_validated",
        "paper_portfolio_exposure_available",
        "portfolio_construction_projection_available",
        "paper_outcome_attribution_available",
        "no_external_runtime_imports",
        "decision_replay_available",
        "tradingagents_reflection_replay_available",
        "portfolio_attribution_projection_available",
        "decision_log_ledger_schema_available",
        "integration_requirement_matrix_available",
    }.issubset(invariant_names)


def test_open_stock_ai_integration_audit_builder_uses_main_entrypoint():
    data = integration_audit_to_dict(symbol="2330.TW", limit=5)

    assert data["method"] == "open_stock_ai_integration_audit"
    assert data["entrypoint"] == "src/open_stock_ai"
    assert data["settings"]["mode"] == "paper"
    assert data["runtime"]["paper_only"] is True
    assert data["runtime_boundary"]["clean"] is True


def test_sqlite_storage_saves_signals_reports_and_trades(tmp_path):
    store = SQLiteStore(tmp_path / "open_stock_ai.sqlite")
    signal_result = SignalStore(store=store).save(
        {
            "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
            "signal": {"action": "hold", "confidence": 0.5},
            "risk": {"approved": False},
            "execution": {"executed": False},
        }
    )
    report_result = ReportStore(store=store).save_preview("sample report")
    trade_result = TradeStore(store=store).save_preview({"symbol": "2330.TW", "action": "hold"})
    decision_log_result = DecisionLogStore(store=store).save(
        {
            "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
            "signal": {
                "action": "buy",
                "confidence": 0.8,
                "entry_price": 100.0,
                "target_price": 112.0,
                "stop_loss": 92.0,
                "position_size_pct": 5.0,
                "decision_schema": {"rating": "Buy", "action": "Buy"},
            },
            "risk": {"approved": True},
            "execution": {"executed": True},
        }
    )

    assert signal_result["saved"] is True
    assert report_result["saved"] is True
    assert trade_result["saved"] is True
    assert decision_log_result["saved"] is True
    assert decision_log_result["schema_version"] == "open_stock_ai.decision_log_row.v1"
    assert decision_log_result["lesson"] == "Buy/Buy converted to paper order for review."
    assert store.count("signals") == 1
    assert store.count("reports") == 1
    assert store.count("trades") == 1
    assert store.count("decision_logs") == 1
    assert store.recent_decision_logs(limit=1)[0]["schema_version"] == "open_stock_ai.decision_log_row.v1"
    assert store.recent_decision_logs(limit=1)[0]["rating"] == "Buy"
    assert store.recent_signals(limit=1)[0]["symbol"] == "2330.TW"
    assert set(store.recent_signals(limit=1)[0]["payload"]) <= {
        "schema_version",
        "request",
        "market_snapshot",
        "intelligence",
        "research",
        "signal",
        "risk",
        "execution",
    }
    review = DecisionLogStore(store=store).review(symbol="2330.TW", limit=5)
    assert review["method"] == "tradingagents_style_decision_replay"
    assert review["schema_version"] == "open_stock_ai.decision_review.v1"
    assert review["total"] == 1
    assert review["approved"] == 1
    assert review["executed"] == 1
    assert review["ratings"] == {"Buy": 1}
    assert review["trader_actions"] == {"Buy": 1}
    assert review["average_confidence"] == 0.8


def test_local_external_capability_extractors_are_executable():
    news_result = SentimentIntelligence().analyze(
        [
            {"title": "Company reports strong growth and record profit"},
            {"title": "Analyst warning highlights pressure"},
        ]
    )
    technical_result = TechnicalIntelligence().analyze(
        [{"close": value} for value in [10, 11, 12, 12.5, 13, 13.5, 14]]
    )
    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=14,
        ohlcv=[{"close": value} for value in range(10, 40)],
    )
    signal = build_engine().pipeline.strategy.generate_signal(
        StockRequest(symbol="2330.TW", market="TW"),
        snapshot,
        build_engine().pipeline.intelligence.analyze(StockRequest(symbol="2330.TW", market="TW"), snapshot),
    )
    backtest_result = BacktestResearch().evaluate(StockRequest(symbol="2330.TW", market="TW"), signal, snapshot)
    factor_result = FactorResearch().evaluate(StockRequest(symbol="2330.TW", market="TW"), signal, snapshot)

    assert news_result["coverage"] == 2
    assert news_result["sentiment_label"] in {"bullish", "bearish", "neutral"}
    assert technical_result["technical_view"] == "uptrend"
    assert backtest_result["backtest_id"]
    assert backtest_result["sharpe"] is not None
    assert backtest_result["transaction_costs_included"] is False
    assert "transaction_cost_schedule_missing" in backtest_result["approval_blockers"]
    assert factor_result["score"] is not None


def test_open_stock_ai_data_sources_emit_unified_envelopes():
    twse = TWSESource().normalize("2330.TW", "TW", {"close": 100.0})
    yahoo = YahooSource().normalize("2330.TW", "TW", {"ohlcv": [{"close": 100.0}]})
    news = NewsSource().normalize("2330.TW", "TW", [{"title": "Company reports growth"}])
    quality = quality_summary([{"close": 100.0}])

    assert twse["schema_version"] == "open_stock_ai.data_source_envelope.v1"
    assert twse["source_key"] == "twse"
    assert yahoo["role"] == "market_price_history_news"
    assert news["quality"]["item_count"] == 1
    assert quality["schema_version"] == "open_stock_ai.data_quality.v1"
    assert quality["keys"] == ["close"]


def test_open_stock_ai_local_strategy_intelligence_and_executor_modules_are_executable():
    ohlcv = [{"close": float(value)} for value in range(100, 130)]
    financials = {
        "revenue": {"yoy_change_percent": 18.0},
        "eps": {"value": 4.2},
    }
    chips = {"margin": {"margin_balance": 1000}}
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.9,
        horizon="swing",
        reason="unit",
        entry_price=100.0,
        stop_loss=95.0,
        position_size_pct=5.0,
    )
    risk = RiskDecision(
        approved=True,
        reason="Approved for paper trading.",
        max_position_size_pct=10.0,
        adjusted_position_size_pct=5.0,
    )

    ma = MAStrategy().evaluate(ohlcv)
    breakout = BreakoutStrategy().evaluate(ohlcv)
    rsi_macd = RSIMACDStrategy().evaluate(ohlcv)
    llm_prompt = LLMStrategy().build_prompt({"symbol": "2330.TW", "horizon": "swing"})
    report = FinancialReportIntelligence().analyze(financials)
    fundamental = FundamentalIntelligence().analyze(financials, chips)
    reflection = ReflectionIntelligence().review(
        [
            {
                "risk": {"approved": False, "reason": "Signal confidence too low."},
                "execution": {"executed": False},
            }
        ]
    )
    replay_projection = ReflectionIntelligence().replay_projection(
        {
            "method": "tradingagents_style_decision_replay",
            "schema_version": "open_stock_ai.decision_review.v1",
            "total": 1,
            "approval_rate": 0.0,
            "execution_rate": 0.0,
            "lessons": ["Risk gates blocked all decisions."],
            "items": [
                {
                    "risk_approved": False,
                    "executed": False,
                    "lesson": "Risk gates blocked all decisions.",
                }
            ],
        },
        tradingagents_contract=TradingAgentsSource().load_capability_contract(),
    )
    execution = PaperExecutor().submit(StockRequest(symbol="2330.TW", market="TW"), signal, risk)

    assert ma["method"] == "moving_average_cross"
    assert breakout["method"] == "breakout_range"
    assert rsi_macd["method"] == "rsi_macd"
    assert llm_prompt["method"] == "codex_agent_strategy_prompt_boundary"
    assert report["financial_view"] == "positive"
    assert fundamental["fundamental_view"] == "positive"
    assert reflection["method"] == "tradingagents_style_reflection_summary"
    assert replay_projection["schema_version"] == "open_stock_ai.tradingagents_reflection_replay.v1"
    assert replay_projection["memory_ready"] is True
    assert replay_projection["cohorts"]["blocked_by_risk"] == 1
    assert replay_projection["reflection_contract"]["path"] == "tradingagents/graph/reflection.py"
    assert execution.executed is False
    assert execution.mode == "paper_pending_oms"


def test_open_stock_ai_disabled_legacy_model_interface_notification_and_live_boundaries_are_explicit():
    route = LLMRouter().route("explain_risk")
    preview = OpenAICompatibleClient(
        base_url="http://127.0.0.1:8080/v1",
        api_key="no-key",
        model="local-model",
    ).preview_chat_request([{"role": "user", "content": "review"}])
    telegram = TelegramNotifier().send_preview("paper signal")
    line = LineNotifier().send_preview("paper signal")
    live = LiveExecutorDisabled().submit({"symbol": "2330.TW", "side": "buy"})
    order_preview = OrderStore().append_preview({"symbol": "2330.TW", "side": "buy"})
    kill_switch = KillSwitch().status()

    assert route["schema_version"] == "open_stock_ai.llm_route.v1"
    assert route["execution_boundary"] == "route_only_no_network_call"
    assert preview["schema_version"] == "open_stock_ai.llm_request_preview.v1"
    assert preview["execution_boundary"] == "preview_only_no_network_call"
    assert telegram["delivery_boundary"] == "preview_only_no_remote_send"
    assert line["delivery_boundary"] == "preview_only_no_remote_send"
    assert live["schema_version"] == "open_stock_ai.live_execution_disabled.v1"
    assert live["submitted"] is False
    assert order_preview["schema_version"] == "open_stock_ai.order_preview.v1"
    assert kill_switch["schema_version"] == "open_stock_ai.risk_control.v2"
    assert kill_switch["live_trading_allowed"] is False


def test_ai_trader_source_parses_local_agent_contracts():
    result = AITraderSource().load_skill_schema()
    signals_schema = result["schema_contract"]["schemas"]["signals"]
    trades_schema = result["schema_contract"]["schemas"]["trades"]
    skill_names = {item["name"] for item in result["skill_contract"]["skills"]}

    assert result["loaded"] is True
    assert signals_schema["path"] == "research/schemas/signals.schema.json"
    assert "symbol" in signals_schema["core_fields"]
    assert "entry_price" in trades_schema["core_fields"]
    assert "ai-trader" in skill_names
    assert result["schema_contract"]["publishing_boundary"] == "read_only_contract_no_remote_publish"


def test_ai_trader_source_validates_open_stock_ai_paper_order_locally():
    result = AITraderSource().validate_paper_order(
        {
            "order_id": "PAPER-UNIT",
            "created_at": "2026-07-03T00:00:00+00:00",
            "market": "TW",
            "symbol": "2330.TW",
            "horizon": "swing",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 5.0,
        }
    )

    assert result["method"] == "local_trades_schema_validation"
    assert result["loaded"] is True
    assert result["valid"] is True
    assert result["schema_title"] == "trades.csv research export row"
    assert result["publishing_boundary"] == "validated_locally_no_remote_publish"
    assert result["row"]["agent_hash"] == "open_stock_ai"
    assert result["row"]["side"] == "buy"
    assert set(result["row"].keys()) == set(result["row"].keys()).intersection(
        set(AITraderSource()._read_json(Path("external/AI-Trader/research/schemas/trades.schema.json"))["properties"].keys())
    )


def test_ai_trader_source_validates_open_stock_ai_signal_locally():
    result = AITraderSource().validate_signal(
        {
            "symbol": "2330.TW",
            "market": "TW",
            "horizon": "swing",
            "action": "buy",
            "reason": "unit signal",
            "entry_price": 100.0,
            "target_price": 110.0,
            "position_size_pct": 5.0,
            "source_modules": ["fingpt", "tradingagents"],
        },
        created_at="2026-07-03T00:00:00+00:00",
    )

    assert result["method"] == "local_signals_schema_validation"
    assert result["loaded"] is True
    assert result["valid"] is True
    assert result["schema_title"] == "signals.csv research export row"
    assert result["publishing_boundary"] == "validated_locally_no_remote_publish"
    assert result["row"]["agent_hash"] == "open_stock_ai"
    assert result["row"]["message_type"] == "trading_signal"
    assert result["row"]["side"] == "buy"
    assert result["row"]["tags"] == "fingpt,tradingagents"
    assert set(result["row"].keys()) == set(result["row"].keys()).intersection(
        set(AITraderSource()._read_json(Path("external/AI-Trader/research/schemas/signals.schema.json"))["properties"].keys())
    )


def test_ai_trader_source_projects_signal_and_trade_interop_contracts():
    source = AITraderSource()
    signal_validation = source.validate_signal(
        {
            "symbol": "2330.TW",
            "market": "TW",
            "horizon": "swing",
            "action": "buy",
            "reason": "unit signal",
            "entry_price": 100.0,
            "target_price": 110.0,
            "position_size_pct": 5.0,
            "source_modules": ["fingpt", "finrobot", "tradingagents"],
        },
        created_at="2026-07-03T00:00:00+00:00",
    )
    trade_validation = source.validate_paper_order(
        {
            "order_id": "PAPER-UNIT",
            "created_at": "2026-07-03T00:00:00+00:00",
            "market": "TW",
            "symbol": "2330.TW",
            "horizon": "swing",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 5.0,
        }
    )

    signal_projection = source.build_signal_interop_projection(signal_validation)
    trade_projection = source.build_trade_interop_projection(trade_validation)
    skill_route = source.build_skill_route_projection(
        {
            "symbol": "2330.TW",
            "market": "TW",
            "horizon": "swing",
            "action": "buy",
            "reason": "unit signal",
            "source_modules": ["fingpt", "finrobot", "tradingagents"],
        },
        validation=signal_validation,
    )

    assert signal_projection["schema_version"] == "open_stock_ai.ai_trader_interop_projection.v1"
    assert signal_projection["method"] == "local_signal_schema_interop_projection"
    assert signal_projection["artifact_type"] == "signal"
    assert signal_projection["valid"] is True
    assert signal_projection["skill_count"] >= 1
    assert signal_projection["execution_boundary"] == "validated_locally_no_remote_ai_trader_publish"
    assert signal_projection["source_lineage"]["schema_version"] == "open_stock_ai.external_evidence_lineage.v1"
    assert signal_projection["source_lineage"]["all_lock_verified"] is True
    assert trade_projection["schema_version"] == "open_stock_ai.ai_trader_interop_projection.v1"
    assert trade_projection["method"] == "local_trade_schema_interop_projection"
    assert trade_projection["artifact_type"] == "paper_order"
    assert trade_projection["valid"] is True
    assert trade_projection["publishing_boundary"] == "validated_locally_no_remote_publish"
    assert trade_projection["source_lineage"]["schema_version"] == "open_stock_ai.external_evidence_lineage.v1"
    assert trade_projection["source_lineage"]["all_lock_verified"] is True
    assert skill_route["schema_version"] == "open_stock_ai.ai_trader_skill_route.v1"
    assert skill_route["method"] == "local_skill_frontmatter_route_projection"
    assert skill_route["valid"] is True
    assert skill_route["route_ready"] is True
    assert "ai-trader" in skill_route["selected_skill_names"]
    assert "market-intel" in skill_route["selected_skill_names"]
    assert "publish_signal" in skill_route["disabled_remote_actions"]
    assert skill_route["execution_boundary"] == "read_only_skill_route_no_remote_ai_trader_publish"
    assert skill_route["source_lineage"]["schema_version"] == "open_stock_ai.external_evidence_lineage.v1"
    assert skill_route["source_lineage"]["all_lock_verified"] is True


def test_finrobot_source_parses_local_report_contracts():
    result = FinRobotSource().load_capability_contract()
    role_names = {item["name"] for item in result["agent_roles"]["roles"]}
    tool_names = {item["name"] for item in result["report_analysis_tools"]["tools"]}

    assert result["loaded"] is True
    assert result["method"] == "local_report_agent_contract"
    assert "Expert_Investor" in role_names
    assert "analyze_income_stmt" in tool_names
    assert "get_risk_assessment" in tool_names
    assert result["equity_report_modules"]["count"] >= 1
    assert result["report_structure_contract"]["standard_section_count"] >= 6
    assert "risk_factors" in result["report_structure_contract"]["required_sections"]
    assert result["sample_reports"]["count"] >= 1
    assert result["execution_boundary"] == "read_only_contract_no_finrobot_runtime_import"


def test_finrobot_source_projects_equity_report_contract_into_intelligence():
    request = StockRequest(symbol="2330.TW", market="TW", horizon="swing")
    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=120.0,
        news=[{"title": "Company reports strong growth with manageable risk"}],
        financials={"revenue": {"yoy_change_percent": 18.0, "mom_change_percent": 4.0}},
    )

    result = FinRobotSource().analyze_fundamentals(request, snapshot)
    projection = result["report_projection"]

    assert projection["schema_version"] == "open_stock_ai.finrobot_report_projection.v1"
    assert projection["method"] == "local_equity_report_contract_projection"
    assert projection["report_view"] == "positive"
    assert projection["risk_view"] in {"low", "medium"}
    assert projection["valuation_snapshot"]["method"] == "local_revenue_growth_valuation_band"
    assert projection["report_contract"]["section_count"] >= 6
    assert projection["agent_contract"]["primary_role"] == "Expert_Investor"
    assert projection["agent_contract"]["required_tools_present"]["analyze_income_stmt"] is True
    assert projection["agent_contract"]["required_tools_present"]["get_risk_assessment"] is True
    assert projection["execution_boundary"] == "read_only_contract_no_finrobot_runtime_import"
    assert projection["model_provenance"]["provenance_type"] == "local_baseline"
    assert projection["model_provenance"]["model_output"] is False
    assert result["capability_contract"]["runtime_capability"]["status"] == "disabled"
    assert result["adapter_result"]["loaded"] is False
    assert result["adapter_result"]["metrics"]["report_projection"] == projection


def test_fingpt_source_parses_read_only_sentiment_forecaster_contracts():
    result = FinGPTSource().load_capability_contract()
    benchmark = result["benchmark_contract"]
    forecaster = result["forecaster_contract"]
    rag = result["rag_contract"]
    trading = result["trading_contract"]

    assert result["loaded"] is True
    assert result["method"] == "fingpt_read_only_contract"
    assert benchmark["sentiment_template_count"] >= 1
    assert any("fpb.py" in item for item in benchmark["benchmarks"])
    assert "get_all_prompts" in forecaster["prompt_functions"]
    assert "PROMPT_END" in forecaster["prompt_constants"]
    assert rag["component_count"] >= 1
    assert trading["strategy_readme_count"] >= 1
    assert result["execution_boundary"] == "read_only_contract_no_fingpt_runtime_import"
    assert result["runtime_capability"]["enabled"] is False
    assert result["runtime_capability"]["status"] == "disabled"


def test_fingpt_source_projects_forecaster_prompt_contract_into_intelligence():
    request = StockRequest(symbol="2330.TW", market="TW", horizon="swing")
    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        ohlcv=[{"close": float(100 + day)} for day in range(10)],
        news=[{"title": "Company reports strong growth and record profit"}],
        financials={"revenue": {"yoy_change_percent": 18.0}},
    )

    result = FinGPTSource().analyze_news(request, snapshot)
    projection = result["forecast_projection"]

    assert projection["schema_version"] == "open_stock_ai.fingpt_forecast_projection.v1"
    assert projection["method"] == "deterministic_forecaster_contract_projection"
    assert projection["direction"] in {"up", "down", "flat"}
    assert projection["prompt_contract"]["path"] == "fingpt/FinGPT_Forecaster/prompt.py"
    assert projection["prompt_contract"]["has_get_all_prompts"] is True
    assert projection["prompt_contract"]["has_prompt_end"] is True
    assert projection["execution_boundary"] == "read_only_contract_no_fingpt_runtime_import"
    assert projection["model_provenance"]["provenance_type"] == "local_baseline"
    assert projection["model_provenance"]["model_output"] is False
    assert result["status"] == "disabled"
    assert result["adapter_result"]["loaded"] is False
    assert result["adapter_result"]["metrics"]["forecast_projection"] == projection


def test_finrl_source_parses_local_backtest_contracts():
    result = FinRLSource().load_capability_contract()
    finrl_contract = result["finrl_contract"]
    finrl_trading_contract = result["finrl_trading_contract"]

    assert result["loaded"] is True
    assert result["method"] == "local_backtest_paper_trading_contract"
    assert finrl_contract["paper_example_count"] >= 1
    assert finrl_contract["environment_count"] >= 1
    assert any(module["path"] == "finrl/train.py" for module in finrl_contract["core_modules"])
    assert "BacktestEngine" in finrl_trading_contract["backtest_engine"]["classes"]
    assert finrl_trading_contract["adaptive_rotation_file_count"] >= 1
    assert result["execution_boundary"] == "read_only_contract_no_finrl_runtime_import"


def test_qlib_source_parses_local_workflow_contracts():
    result = QlibSource().load_capability_contract()
    workflow = result["workflow_contract"]
    docs = result["documentation_contract"]["documents"]

    assert result["loaded"] is True
    assert result["method"] == "local_workflow_factor_contract"
    assert workflow["workflow_count"] >= 1
    assert "LightGBM" in workflow["families"]
    assert any(item["model"] for item in workflow["sample_workflows"])
    assert any(item["path"] == "docs/advanced/alpha.rst" and item["exists"] for item in docs)
    assert result["execution_boundary"] == "read_only_contract_no_qlib_runtime_import"


def test_qlib_source_projects_workflow_summary_into_research_result():
    request = StockRequest(symbol="2330.TW", market="TW", horizon="swing")
    signal = TradingSignal(
        symbol="2330.TW",
        market="TW",
        action="buy",
        confidence=0.8,
        horizon="swing",
        reason="test",
    )
    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        ohlcv=[{"close": float(100 + day)} for day in range(30)],
    )

    result = QlibSource().validate_factor(request, signal, snapshot)
    summary = result["workflow_summary"]
    projection = result["factor_projection"]

    assert summary["schema_version"] == "open_stock_ai.qlib_workflow_summary.v1"
    assert summary["method"] == "local_qlib_workflow_projection"
    assert summary["workflow_count"] >= 1
    assert summary["selected_workflow"]["model"]
    assert summary["execution_boundary"] == "read_only_contract_no_qlib_runtime_import"
    assert projection["schema_version"] == "open_stock_ai.qlib_factor_projection.v1"
    assert projection["method"] == "local_qlib_factor_projection"
    assert projection["passed"] is True
    assert projection["model_score"] is not None
    assert projection["rank_ic_proxy"] is not None
    assert projection["rank_ic_method"] == "single_symbol_forward_return_proxy"
    assert projection["selected_model"] == summary["selected_workflow"]["model"]
    assert projection["workflow_summary_schema_version"] == "open_stock_ai.qlib_workflow_summary.v1"
    assert projection["research_report"]["schema_version"] == "open_stock_ai.research_artifacts.v1"
    assert projection["research_report"]["path"] is None
    assert projection["execution_boundary"] == "read_only_contract_no_qlib_runtime_import"
    assert result["adapter_result"]["metrics"]["factor_projection"] == projection


def test_tradingagents_source_parses_local_multi_agent_contracts():
    result = TradingAgentsSource().load_capability_contract()
    schema = result["schema_contract"]
    graph = result["graph_contract"]
    risk_memory = result["risk_memory_contract"]
    class_names = {item["name"] for item in schema["classes"]}

    assert result["loaded"] is True
    assert result["method"] == "local_multi_agent_schema_graph_contract"
    assert {"PortfolioRating", "TraderAction", "TraderProposal", "PortfolioDecision"}.issubset(class_names)
    assert "render_trader_proposal" in schema["render_functions"]
    assert graph["selected_analysts"] == ["market", "social", "news", "fundamentals"]
    assert "Trader" in graph["nodes"]
    assert "Portfolio Manager" in graph["nodes"]
    assert risk_memory["rating_scale"] == ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    assert risk_memory["risk_debator_count"] == 3
    assert result["execution_boundary"] == "read_only_contract_no_tradingagents_runtime_import"


def test_tradingagents_source_projects_risk_debate_summary():
    request = StockRequest(symbol="2330.TW", market="TW", horizon="swing")
    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        ohlcv=[{"close": float(100 + day)} for day in range(30)],
    )

    result = TradingAgentsSource().analyze_context(request, snapshot)
    summary = result["risk_debate_summary"]

    assert summary["schema_version"] == "open_stock_ai.tradingagents_risk_debate.v1"
    assert summary["method"] == "local_tradingagents_risk_debate_projection"
    assert summary["risk_debator_count"] == 3
    assert summary["rating_scale"] == ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    assert summary["execution_boundary"] == "read_only_contract_no_tradingagents_runtime_import"
