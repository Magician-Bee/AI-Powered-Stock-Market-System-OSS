from __future__ import annotations

import argparse
from dataclasses import asdict

from .config.settings import OpenStockAISettings, load_settings
from .data.market_data_hub import MarketDataHub
from .engine import OpenStockAIEngine
from .execution.execution_engine import ExecutionEngine
from .external_sources.broker_import_governance import BrokerAccountImportGovernance
from .external_sources.fingpt_source import FinGPTSource
from .external_sources.finrl_source import FinRLSource
from .external_sources.finrobot_source import FinRobotSource
from .external_sources.optional_registry import OptionalExternalSourceRegistry
from .external_sources.qlib_source import QlibSource
from .external_sources.registry import EXPECTED_REPOSITORY_LOCKS, ExternalProjectRegistry
from .external_sources.tradingagents_source import TradingAgentsSource
from .intelligence.reflection_intelligence import ReflectionIntelligence
from .integration_audit import build_integration_audit
from .external_sources.ai_trader_source import AITraderSource
from .intelligence.intelligence_hub import IntelligenceHub
from .pipeline import SignalPipeline
from .research.portfolio_attribution import PortfolioAttribution
from .research.portfolio_construction import PortfolioConstruction
from .research.portfolio_risk_context import (
    PortfolioRiskContextStore,
    materialize_session_portfolio_risk_context,
)
from .research.research_engine import ResearchEngine
from .research.model_registry import ResearchModelRegistry
from .strategy.strategy_registry import StrategyArtifactRegistry
from .risk.risk_engine import RiskEngine
from .risk.kill_switch import DurableRiskControlStore, LossLimitPolicy
from .storage.decision_log_store import DecisionLogStore
from .storage.signal_store import SignalStore
from .storage.sqlite_store import SQLiteStore
from .storage.trade_store import TradeStore
from .strategy.strategy_engine import StrategyEngine
from .types import MissingSymbolError, StockRequest
from .governance.runtime import build_runtime_governance


EXTERNAL_SOURCE_TERMS = {
    "tradingagents": ["trading_graph", "analyst", "signal_processing", "reflection", "risk", "agents"],
    "finrobot": ["annual_report", "report", "rag", "agent", "fundamental", "analyzer"],
    "fingpt": ["sentiment", "forecaster", "rag", "market_sentiment", "prompt"],
    "finrl_trading": ["backtest", "strategy", "adaptive_rotation", "paper_trading", "execution"],
    "finrl": ["train", "trade", "backtest", "portfolio", "papertrading", "env"],
    "qlib": ["workflow", "alpha", "factor", "backtest", "dataset", "LightGBM", "Transformer"],
    "ai_trader": ["skill", "market-intel", "signal", "agent", "routes", "research"],
}


def build_engine(settings: OpenStockAISettings | None = None) -> OpenStockAIEngine:
    settings = settings or load_settings()
    registry = build_registry(settings)
    sqlite_store = SQLiteStore(db_path=settings.sqlite_path)
    runtime_governance = build_runtime_governance(
        settings.sqlite_path,
        retention_maintenance_interval_seconds=settings.retention_maintenance_interval_seconds,
    )
    risk_control = DurableRiskControlStore(
        database_path=sqlite_store.path,
        policy=LossLimitPolicy(
            intraday_loss_pct=settings.max_intraday_loss_pct,
            daily_loss_pct=settings.max_daily_loss_pct,
            weekly_loss_pct=settings.max_weekly_loss_pct,
            monthly_loss_pct=settings.max_monthly_loss_pct,
            consecutive_loss_count=settings.max_consecutive_losses,
        ),
    )
    ai_trader = AITraderSource(registry=registry)
    pipeline = SignalPipeline(
        market_data=MarketDataHub(),
        intelligence=IntelligenceHub(
            fingpt=FinGPTSource(registry=registry),
            finrobot=FinRobotSource(registry=registry),
            tradingagents=TradingAgentsSource(registry=registry),
        ),
        strategy=StrategyEngine(artifact_registry=StrategyArtifactRegistry(store=sqlite_store)),
        research=ResearchEngine(
            finrl=FinRLSource(registry=registry),
            qlib=QlibSource(registry=registry),
            model_registry=ResearchModelRegistry(
                store=sqlite_store,
                artifact_rollback=runtime_governance.artifact_rollback,
            ),
        ),
        risk=RiskEngine(
            min_rule_score_threshold=settings.min_rule_score_threshold,
            max_position_size_pct=settings.max_position_size_pct,
            risk_control=risk_control,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_total_drawdown_pct=settings.max_total_drawdown_pct,
            max_symbol_exposure_pct=settings.max_symbol_exposure_pct,
            max_total_paper_exposure_pct=settings.max_total_paper_exposure_pct,
            max_industry_exposure_pct=settings.max_industry_exposure_pct,
            require_backtest_passed=settings.require_backtest_passed,
            live_trading_enabled=settings.live_trading_enabled,
        ),
        execution=ExecutionEngine(
            trading_mode=settings.mode,
            live_trading_enabled=settings.live_trading_enabled,
            active_change_id=settings.active_change_id,
        ),
        signal_store=SignalStore(store=sqlite_store),
        decision_log_store=DecisionLogStore(store=sqlite_store),
        trade_store=TradeStore(
            store=sqlite_store,
            ai_trader=ai_trader,
            risk_control=risk_control,
            retention_ledger=runtime_governance.retention,
            change_management=runtime_governance.change_management,
            require_change_binding=settings.require_change_binding,
        ),
        ai_trader=ai_trader,
        batch_requests=_batch_requests(settings),
    )
    return OpenStockAIEngine(pipeline=pipeline, governance=runtime_governance)


def build_registry(settings: OpenStockAISettings | None = None) -> ExternalProjectRegistry:
    settings = settings or load_settings()
    return ExternalProjectRegistry(project_paths=settings.external_projects or {})


def _batch_requests(settings: OpenStockAISettings) -> tuple[StockRequest, ...]:
    requests: list[StockRequest] = []
    for item in settings.watchlist:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        requests.append(
            StockRequest(
                symbol=symbol,
                market=str(item.get("market") or "TW"),  # type: ignore[arg-type]
                horizon="swing",
            )
        )
    return tuple(requests)


def analyze_to_dict(
    symbol: str | None = None,
    market: str = "TW",
    horizon: str = "swing",
) -> dict:
    if not str(symbol or "").strip():
        raise MissingSymbolError("analyze_to_dict requires an explicit symbol")
    request = StockRequest(symbol=symbol, market=market, horizon=horizon)  # type: ignore[arg-type]
    return asdict(build_engine().analyze_stock(request))


def session_to_dict(session: str = "pre_market") -> dict:
    settings = load_settings()
    engine = build_engine(settings=settings)
    runners = {
        "pre_market": engine.run_pre_market,
        "intraday": engine.run_intraday,
        "after_market": engine.run_after_market,
    }
    if session not in runners:
        raise ValueError(f"Unsupported Open Stock AI session: {session}")
    decisions = [asdict(item) for item in runners[session]()]
    risk_context_store = PortfolioRiskContextStore(settings.sqlite_path) if decisions else None
    portfolio_risk_context = (
        materialize_session_portfolio_risk_context(decisions, receipt_store=risk_context_store)
        if risk_context_store is not None
        else None
    )
    portfolio_risk_context_receipt = (
        str(portfolio_risk_context.get("risk_context_receipt_sha256") or "")
        if isinstance(portfolio_risk_context, dict)
        else None
    )
    portfolio_construction = PortfolioConstruction().build(
        decisions,
        session=session,
        portfolio_risk_context_store=risk_context_store if portfolio_risk_context_receipt else None,
        portfolio_risk_context_receipt=portfolio_risk_context_receipt,
    )
    return {
        "method": "open_stock_ai_batch_session",
        "schema_version": "open_stock_ai.batch_session.v1",
        "session": session,
        "count": len(decisions),
        "portfolio_risk_context": portfolio_risk_context,
        "portfolio_construction": portfolio_construction,
        "items": decisions,
    }


def external_sources_to_dict() -> dict:
    registry = build_registry()
    projects = {
        key: asdict(registry.profile(key, capability_terms=terms))
        for key, terms in EXTERNAL_SOURCE_TERMS.items()
    }
    contracts = {
        "tradingagents": TradingAgentsSource(registry=registry).load_capability_contract(),
        "ai_trader": AITraderSource(registry=registry).load_skill_schema(),
        "fingpt": FinGPTSource(registry=registry).load_capability_contract(),
        "finrobot": FinRobotSource(registry=registry).load_capability_contract(),
        "finrl": FinRLSource(registry=registry).load_capability_contract(),
        "qlib": QlibSource(registry=registry).load_capability_contract(),
    }
    verified_count = sum(1 for item in projects.values() if item["origin_verified"])
    source_lock = external_source_lock_to_dict(projects=projects)
    return {
        "count": len(projects),
        "verified_count": verified_count,
        "all_verified": verified_count == len(projects),
        "lock_verified_count": source_lock["lock_verified_count"],
        "all_locked": source_lock["all_locked"],
        "source_lock": source_lock,
        "projects": projects,
        "contracts": contracts,
    }


def external_source_lock_to_dict(projects: dict | None = None) -> dict:
    if projects is None:
        registry = build_registry()
        projects = {
            key: asdict(registry.profile(key, capability_terms=EXTERNAL_SOURCE_TERMS.get(key, ())))
            for key in EXPECTED_REPOSITORY_LOCKS
        }
    entries = []
    for key, spec in EXPECTED_REPOSITORY_LOCKS.items():
        project = projects.get(key, {})
        entries.append(
            {
                "key": key,
                "name": spec["name"],
                "clone_command": f"git clone {spec['origin']} {spec['path']}",
                "expected_origin": spec["origin"],
                "expected_branch": spec["branch"],
                "expected_head": spec["head"],
                "local_path": project.get("path") or spec["path"],
                "origin": project.get("origin"),
                "branch": project.get("branch"),
                "head": project.get("head"),
                "origin_verified": project.get("origin_verified") is True,
                "branch_verified": project.get("branch_verified") is True,
                "head_verified": project.get("head_verified") is True,
                "lock_verified": project.get("lock_verified") is True,
            }
        )
    lock_verified_count = sum(1 for item in entries if item["lock_verified"])
    return {
        "schema_version": "open_stock_ai.external_source_lock.v1",
        "method": "local_git_origin_head_lock",
        "count": len(entries),
        "lock_verified_count": lock_verified_count,
        "all_locked": lock_verified_count == len(entries),
        "entries": entries,
        "manifest": "docs/integration/external_sources_manifest.md",
    }


def broker_import_governance_to_dict(projects: dict | None = None) -> dict:
    settings = load_settings()
    if projects is None:
        registry = build_registry(settings)
        projects = {
            key: asdict(registry.profile(key, capability_terms=EXTERNAL_SOURCE_TERMS.get(key, ())))
            for key in EXPECTED_REPOSITORY_LOCKS
        }
    return BrokerAccountImportGovernance().build(settings=settings, projects=projects)


def optional_external_sources_to_dict() -> dict:
    return OptionalExternalSourceRegistry().build(approved_keys=list(EXPECTED_REPOSITORY_LOCKS))


def storage_stats_to_dict() -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    latest_decision_logs = store.recent_decision_logs(limit=1)
    decision_log_row_schema_version = (
        latest_decision_logs[0].get("schema_version") if latest_decision_logs else "open_stock_ai.decision_log_row.v1"
    )
    return {
        "method": "open_stock_ai_storage_stats",
        "schema_version": "open_stock_ai.storage_stats.v1",
        "configured": store.configured,
        "db_path": str(store.path),
        "signals": store.count("signals"),
        "reports": store.count("reports"),
        "trades": store.count("trades"),
        "decision_logs": store.count("decision_logs"),
        "decision_log_method": "open_stock_ai_decision_log_ledger",
        "decision_log_schema_version": "open_stock_ai.decision_log_ledger.v1",
        "decision_log_row_schema_version": decision_log_row_schema_version,
    }


def signals_to_dict(limit: int = 20) -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    items = store.recent_signals(limit=limit)
    validator = AITraderSource(registry=build_registry(settings))
    for item in items:
        payload = item.get("payload") or {}
        signal = payload.get("signal") or {}
        validation = signal.get("ai_trader_validation")
        if not isinstance(validation, dict):
            validated = validator.validate_signal(signal, created_at=item.get("created_at"))
            validation = {key: value for key, value in validated.items() if key != "row"}
            signal = {**signal, "ai_trader_validation": validation, "ai_trader_signal_row": validated.get("row", {})}
            payload = {**payload, "signal": signal}
            item["payload"] = payload
        if not isinstance(signal.get("ai_trader_interop_projection"), dict):
            validated = validator.validate_signal(signal, created_at=item.get("created_at"))
            signal = {
                **signal,
                "ai_trader_interop_projection": validator.build_signal_interop_projection(validated),
            }
            payload = {**payload, "signal": signal}
            item["payload"] = payload
        if not isinstance(signal.get("ai_trader_skill_route"), dict):
            validated = validator.validate_signal(signal, created_at=item.get("created_at"))
            signal = {
                **signal,
                "ai_trader_skill_route": validator.build_skill_route_projection(signal, validation=validated),
            }
            payload = {**payload, "signal": signal}
            item["payload"] = payload
        item["ai_trader_valid"] = validation.get("valid")
        item["ai_trader_schema"] = validation.get("schema_title")
    return {
        "method": "open_stock_ai_signal_ledger",
        "schema_version": "open_stock_ai.signal_ledger.v1",
        "count": len(items),
        "items": items,
        "db_path": str(store.path),
    }


def decision_logs_to_dict(limit: int = 20) -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    items = store.recent_decision_logs(limit=limit)
    return {
        "method": "open_stock_ai_decision_log_ledger",
        "schema_version": "open_stock_ai.decision_log_ledger.v1",
        "count": len(items),
        "items": items,
        "db_path": str(store.path),
    }


def decision_review_to_dict(symbol: str | None = None, limit: int = 100) -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    review = store.decision_review(symbol=symbol, limit=limit)
    registry = build_registry(settings)
    tradingagents_contract = TradingAgentsSource(registry=registry).load_capability_contract()
    return {
        **review,
        "reflection_replay": ReflectionIntelligence().replay_projection(
            review,
            tradingagents_contract=tradingagents_contract,
        ),
        "portfolio_attribution": PortfolioAttribution().build(review),
        "db_path": str(store.path),
    }


def paper_orders_to_dict(limit: int = 20) -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    items = store.recent_trades(limit=limit)
    validator = AITraderSource(registry=build_registry(settings))
    for item in items:
        payload = item.get("payload") or {}
        if payload.get("schema_version") != "open_stock_ai.paper_order.v1":
            continue
        validation = payload.get("ai_trader_validation")
        if not isinstance(validation, dict):
            order = {**payload, "created_at": payload.get("created_at") or item.get("created_at")}
            validated = validator.validate_paper_order(order)
            validation = {key: value for key, value in validated.items() if key != "row"}
            payload = {**payload, "ai_trader_validation": validation, "ai_trader_trade_row": validated.get("row", {})}
            item["payload"] = payload
        item["ai_trader_valid"] = validation.get("valid")
        item["ai_trader_schema"] = validation.get("schema_title")
    return {
        "method": "open_stock_ai_paper_order_ledger",
        "schema_version": "open_stock_ai.paper_order.v1",
        "row_schema_version": items[0].get("schema_version") if items else "open_stock_ai.paper_order.v1",
        "count": len(items),
        "items": items,
        "db_path": str(store.path),
    }


def paper_exposure_to_dict() -> dict:
    settings = load_settings()
    store = SQLiteStore(db_path=settings.sqlite_path)
    return store.paper_portfolio_exposure()


def integration_audit_to_dict(symbol: str | None = None, limit: int = 100) -> dict:
    settings = load_settings()
    return build_integration_audit(
        settings=settings,
        external_sources=external_sources_to_dict(),
        storage=storage_stats_to_dict(),
        signals=signals_to_dict(limit=limit),
        decision_review=decision_review_to_dict(symbol=symbol, limit=limit),
        paper_orders=paper_orders_to_dict(limit=limit),
        paper_exposure=paper_exposure_to_dict(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an explicit Open Stock AI symbol analysis.")
    parser.add_argument("symbol", help="Explicit stock symbol; no default symbol is selected")
    parser.add_argument("--market", default="TW", choices=("TW", "US", "CRYPTO"))
    parser.add_argument("--horizon", default="swing", choices=("intraday", "swing", "weekly", "monthly"))
    args = parser.parse_args()
    decision = analyze_to_dict(symbol=args.symbol, market=args.market, horizon=args.horizon)
    print("=== Open Stock AI Decision ===")
    print(decision)


if __name__ == "__main__":
    main()
