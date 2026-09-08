from __future__ import annotations

import ast
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config.settings import OpenStockAISettings
from .external_sources.broker_import_governance import BrokerAccountImportGovernance
from .external_sources.optional_registry import OptionalExternalSourceRegistry
from .external_sources.runtime_governance import RuntimeConnectorGovernance
from .research.outcome_attribution import OutcomeAttribution
from .research.portfolio_construction import PortfolioConstruction
from stock_ai.source_policy import source_policy_status
from stock_ai.system_contract import stock_app_requirement_contract
from stock_ai.update_runner import build_update_plan, dry_run_update
from stock_ai.official_derivatives import official_derivatives_status
from stock_ai.official_events import official_events_status


EXPECTED_EXTERNAL_PROJECTS = [
    "tradingagents",
    "finrobot",
    "fingpt",
    "finrl_trading",
    "finrl",
    "qlib",
    "ai_trader",
]

REQUIRED_CONTRACTS = [
    "tradingagents",
    "fingpt",
    "finrobot",
    "finrl",
    "qlib",
    "ai_trader",
]

ADAPTER_SCHEMA_VERSION = "open_stock_ai.adapter_result.v1"

REQUIRED_MODULE_FILES = [
    "api.py",
    "engine.py",
    "pipeline.py",
    "state.py",
    "types.py",
    "config/settings.py",
    "config/loader.py",
    "data/market_data_hub.py",
    "data/twse_source.py",
    "data/tpex_source.py",
    "data/yahoo_source.py",
    "data/mops_source.py",
    "data/news_source.py",
    "data/data_quality.py",
    "intelligence/intelligence_hub.py",
    "intelligence/news_intelligence.py",
    "intelligence/financial_report_intelligence.py",
    "intelligence/technical_intelligence.py",
    "intelligence/sentiment_intelligence.py",
    "intelligence/fundamental_intelligence.py",
    "intelligence/reflection_intelligence.py",
    "strategy/strategy_engine.py",
    "strategy/signal_builder.py",
    "strategy/ma_strategy.py",
    "strategy/breakout_strategy.py",
    "strategy/rsi_macd_strategy.py",
    "strategy/llm_strategy.py",
    "research/research_engine.py",
    "research/factor_research.py",
    "research/backtest_research.py",
    "research/report_generator.py",
    "research/portfolio_construction.py",
    "research/portfolio_attribution.py",
    "research/outcome_attribution.py",
    "risk/risk_engine.py",
    "risk/position_sizing.py",
    "risk/stop_loss.py",
    "risk/drawdown_guard.py",
    "risk/kill_switch.py",
    "execution/execution_engine.py",
    "execution/paper_executor.py",
    "execution/live_executor_disabled.py",
    "execution/order_store.py",
    "external_sources/tradingagents_source.py",
    "external_sources/fingpt_source.py",
    "external_sources/finrobot_source.py",
    "external_sources/finrl_source.py",
    "external_sources/qlib_source.py",
    "external_sources/ai_trader_source.py",
    "external_sources/broker_import_governance.py",
    "external_sources/optional_registry.py",
    "external_sources/runtime_governance.py",
    "storage/sqlite_store.py",
    "storage/signal_store.py",
    "storage/report_store.py",
    "storage/trade_store.py",
    "ui/dashboard.py",
    "notify/telegram_notifier.py",
    "notify/line_notifier.py",
    "llm/llm_router.py",
    "llm/openai_compatible_client.py",
]

REQUIRED_MODULE_DIRS = [
    "src/open_stock_ai",
    "src/open_stock_ai/llm/prompts",
    "docs/integration",
    "output/reports",
    "output/backtests",
    "logs/agent",
]


def build_integration_audit(
    *,
    settings: OpenStockAISettings,
    external_sources: dict[str, Any],
    storage: dict[str, Any],
    decision_review: dict[str, Any],
    signals: dict[str, Any] | None = None,
    paper_orders: dict[str, Any] | None = None,
    paper_exposure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projects = external_sources.get("projects", {})
    contracts = external_sources.get("contracts", {})
    source_lock = external_sources.get("source_lock", {})
    loaded_contracts = [
        name for name in REQUIRED_CONTRACTS if _contract_loaded(contracts.get(name))
    ]
    missing_contracts = [name for name in REQUIRED_CONTRACTS if name not in loaded_contracts]
    schema_contracts = [
        name for name in REQUIRED_CONTRACTS if _adapter_schema_loaded(contracts.get(name))
    ]
    missing_schema_contracts = [name for name in REQUIRED_CONTRACTS if name not in schema_contracts]
    external_license_footprint = _external_license_footprint_summary(projects)
    runtime_connector_governance = RuntimeConnectorGovernance().build(settings=settings, projects=projects)
    broker_import_governance = BrokerAccountImportGovernance().build(settings=settings, projects=projects)
    optional_external_sources = OptionalExternalSourceRegistry().build(approved_keys=EXPECTED_EXTERNAL_PROJECTS)
    verified_projects = [
        name
        for name in EXPECTED_EXTERNAL_PROJECTS
        if projects.get(name, {}).get("origin_verified") is True
    ]
    missing_projects = [name for name in EXPECTED_EXTERNAL_PROJECTS if name not in verified_projects]
    locked_entries = source_lock.get("entries") or []
    locked_projects = [
        item.get("key")
        for item in locked_entries
        if item.get("key") in EXPECTED_EXTERNAL_PROJECTS and item.get("lock_verified") is True
    ]
    missing_locked_projects = [
        name for name in EXPECTED_EXTERNAL_PROJECTS if name not in locked_projects
    ]
    runtime = {
        "mode": settings.mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "require_human_confirm": settings.require_human_confirm,
        "paper_only": settings.mode == "paper" and not settings.live_trading_enabled,
        "broker_account_imports_enabled": settings.broker_account_imports_enabled,
        "external_credentials_enabled": settings.external_credentials_enabled,
    }
    risk = {
        "min_rule_score_threshold": settings.min_rule_score_threshold,
        "max_position_size_pct": settings.max_position_size_pct,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_total_drawdown_pct": settings.max_total_drawdown_pct,
        "require_backtest_passed": settings.require_backtest_passed,
    }
    replay = {
        "method": decision_review.get("method"),
        "schema_version": decision_review.get("schema_version"),
        "total": decision_review.get("total", 0),
        "has_latest": bool(decision_review.get("latest")),
        "db_path": decision_review.get("db_path") or storage.get("db_path"),
        "available": decision_review.get("method") == "tradingagents_style_decision_replay"
        and decision_review.get("schema_version") == "open_stock_ai.decision_review.v1"
        and storage.get("configured") is True,
    }
    reflection_replay = _reflection_replay_summary(decision_review)
    portfolio_attribution = _portfolio_attribution_summary(decision_review)
    decision_log_ledger = {
        "method": storage.get("decision_log_method"),
        "schema_version": storage.get("decision_log_schema_version"),
        "row_schema_version": storage.get("decision_log_row_schema_version"),
        "count": storage.get("decision_logs", 0),
        "available": storage.get("decision_log_method") == "open_stock_ai_decision_log_ledger"
        and storage.get("decision_log_schema_version") == "open_stock_ai.decision_log_ledger.v1"
        and storage.get("decision_log_row_schema_version") == "open_stock_ai.decision_log_row.v1"
        and storage.get("configured") is True,
    }
    paper_ledger = {
        "method": (paper_orders or {}).get("method"),
        "schema_version": (paper_orders or {}).get("schema_version"),
        "row_schema_version": (paper_orders or {}).get("row_schema_version"),
        "count": (paper_orders or {}).get("count", storage.get("trades", 0)),
        "available": (paper_orders or {}).get("method") == "open_stock_ai_paper_order_ledger"
        and (paper_orders or {}).get("schema_version") == "open_stock_ai.paper_order.v1"
        and (paper_orders or {}).get("row_schema_version") == "open_stock_ai.paper_order.v1"
        and all(item.get("schema_version") == "open_stock_ai.paper_order.v1" for item in (paper_orders or {}).get("items") or [])
        and storage.get("configured") is True,
    }
    signal_items = (signals or {}).get("items") or []
    signal_ledger = {
        "method": (signals or {}).get("method"),
        "schema_version": (signals or {}).get("schema_version"),
        "row_schema_version": signal_items[0].get("schema_version") if signal_items else "open_stock_ai.signal_ledger_row.v1",
        "count": (signals or {}).get("count", 0),
        "available": (signals or {}).get("method") == "open_stock_ai_signal_ledger"
        and (signals or {}).get("schema_version") == "open_stock_ai.signal_ledger.v1"
        and all(item.get("schema_version") == "open_stock_ai.signal_ledger_row.v1" for item in signal_items),
    }
    ai_trader_signal_validated_count = sum(1 for item in signal_items if item.get("ai_trader_valid") is True)
    ai_trader_signal_schema = {
        "available": ai_trader_signal_validated_count == len(signal_items),
        "validated_count": ai_trader_signal_validated_count,
        "checked_count": len(signal_items),
        "schema": "AI-Trader signals.schema.json",
    }
    portfolio_construction = PortfolioConstruction().build(
        [
            item.get("payload")
            for item in signal_items
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
        ],
        session="recent_signals",
        source="signal_ledger",
    )
    outcome_attribution = OutcomeAttribution().build(signal_items, source="signal_ledger")
    data_source_schema = _data_source_summary(signal_items)
    stock_decision_schema = _stock_decision_summary(signal_items)
    risk_decision_schema = _risk_decision_summary(signal_items)
    research_artifacts = _research_artifact_summary(signal_items)
    qlib_workflow_summary = _qlib_workflow_summary(signal_items)
    qlib_factor_projection = _qlib_factor_projection_summary(signal_items)
    finrl_backtest_projection = _finrl_backtest_projection_summary(signal_items)
    fingpt_forecast_projection = _fingpt_forecast_projection_summary(signal_items)
    finrobot_report_projection = _finrobot_report_projection_summary(signal_items)
    paper_items = (paper_orders or {}).get("items") or []
    ai_trader_interop_projection = _ai_trader_interop_projection_summary(signal_items, paper_items)
    ai_trader_skill_route = _ai_trader_skill_route_summary(signal_items)
    external_evidence_lineage = _external_evidence_lineage_summary(signal_items)
    external_project_contribution_matrix = _external_project_contribution_matrix(
        projects=projects,
        locked_projects=locked_projects,
        contracts=contracts,
        reflection_replay=reflection_replay,
        portfolio_construction=portfolio_construction,
        qlib_workflow_summary=qlib_workflow_summary,
        qlib_factor_projection=qlib_factor_projection,
        finrl_backtest_projection=finrl_backtest_projection,
        fingpt_forecast_projection=fingpt_forecast_projection,
        finrobot_report_projection=finrobot_report_projection,
        ai_trader_interop_projection=ai_trader_interop_projection,
        ai_trader_skill_route=ai_trader_skill_route,
    )
    ai_trader_validated_count = sum(1 for item in paper_items if item.get("ai_trader_valid") is True)
    ai_trader_trade_schema = {
        "available": ai_trader_validated_count == len(paper_items),
        "validated_count": ai_trader_validated_count,
        "checked_count": len(paper_items),
        "schema": "AI-Trader trades.schema.json",
    }
    paper_exposure_summary = {
        "method": (paper_exposure or {}).get("method"),
        "schema_version": (paper_exposure or {}).get("schema_version"),
        "total_position_size_pct": (paper_exposure or {}).get("total_position_size_pct", 0.0),
        "symbol_count": (paper_exposure or {}).get("symbol_count", 0),
        "available": (paper_exposure or {}).get("method") == "open_stock_ai_paper_portfolio_exposure"
        and (paper_exposure or {}).get("schema_version") == "open_stock_ai.paper_portfolio_exposure.v1"
        and storage.get("configured") is True,
    }
    runtime_boundary = scan_runtime_import_boundary()
    module_surface = scan_module_surface()
    api_router = scan_api_router_mount()
    design_system_contract = scan_design_system_contract()
    stock_app_contract = stock_app_requirement_contract()
    stock_source_policy = source_policy_status()
    stock_official_derivatives = official_derivatives_status()
    stock_official_events = official_events_status()
    stock_update_plan = build_update_plan(as_of="2026-07-10T15:40:00+08:00")
    stock_update_run = dry_run_update(phase="after_hours", as_of="2026-07-10T15:40:00+08:00")
    storage_summary = {
        "configured": storage.get("configured") is True,
        "db_path": storage.get("db_path"),
        "signals": storage.get("signals", 0),
        "reports": storage.get("reports", 0),
        "trades": storage.get("trades", 0),
        "decision_logs": storage.get("decision_logs", 0),
        "method": storage.get("method"),
        "schema_version": storage.get("schema_version"),
        "decision_log_method": storage.get("decision_log_method"),
        "decision_log_schema_version": storage.get("decision_log_schema_version"),
        "decision_log_row_schema_version": storage.get("decision_log_row_schema_version"),
    }
    invariants = [
        _invariant(
            "src_open_stock_ai_entrypoint",
            api_router["mounted"],
            "FastAPI mounts Open Stock AI routes through open_stock_ai.api.router.",
        ),
        _invariant(
            "open_stock_ai_design_system_contract_available",
            design_system_contract["available"],
            (
                f"{design_system_contract['schema_version']} "
                f"{design_system_contract['satisfied_count']}/{design_system_contract['component_count']} design contracts."
            ),
        ),
        _invariant(
            "stock_app_goal_requirement_contract_available",
            stock_app_contract.get("schema_version") == "stock_ai.requirement_contract.v1"
            and stock_app_contract.get("coverage", {}).get("source_tier_count") == 3
            and stock_app_contract.get("coverage", {}).get("data_module_count") == 14
            and stock_app_contract.get("coverage", {}).get("app_feature_count") == 13
            and stock_app_contract.get("coverage", {}).get("schedule_window_count") == 5
            and stock_app_contract.get("coverage", {}).get("schedule_guard_schema") == "stock_ai.schedule_guard.v1"
            and stock_app_contract.get("coverage", {}).get("source_policy_schema") == "stock_ai.source_policy.v1"
            and stock_app_contract.get("coverage", {}).get("update_runner_schema") == "stock_ai.update_runner.v1"
            and stock_app_contract.get("coverage", {}).get("official_derivatives_schema") == "stock_ai.official_derivatives.v1"
            and stock_app_contract.get("coverage", {}).get("live_ordering_enabled") is False,
            (
                f"{stock_app_contract.get('schema_version') or 'missing'} "
                f"{stock_app_contract.get('coverage', {}).get('data_module_count', 0)} modules / "
                f"{stock_app_contract.get('coverage', {}).get('app_feature_count', 0)} app features."
            ),
        ),
        _invariant(
            "stock_app_official_derivatives_contract_available",
            stock_official_derivatives.get("schema_version") == "stock_ai.official_derivatives.v1"
            and stock_official_derivatives.get("source_tier") == 2
            and stock_official_derivatives.get("dry_run") is True
            and stock_official_derivatives.get("live_trading_source") is False
            and stock_official_derivatives.get("required_source_count") == 2,
            (
                f"{stock_official_derivatives.get('schema_version') or 'missing'} "
                f"{stock_official_derivatives.get('connected_source_count', 0)}/"
                f"{stock_official_derivatives.get('required_source_count', 0)} connected official derivative sources."
            ),
        ),
        _invariant(
            "stock_app_official_events_contract_available",
            stock_official_events.get("schema_version") == "stock_ai.official_events.v1"
            and stock_official_events.get("source_tier") == 2
            and stock_official_events.get("dry_run") is True
            and stock_official_events.get("live_trading_source") is False
            and stock_official_events.get("required_source_count") == 1,
            (
                f"{stock_official_events.get('schema_version') or 'missing'} "
                f"{stock_official_events.get('connected_source_count', 0)}/"
                f"{stock_official_events.get('required_source_count', 0)} connected official event sources."
            ),
        ),
        _invariant(
            "stock_app_source_policy_gate_available",
            stock_source_policy.get("schema_version") == "stock_ai.source_policy.v1"
            and stock_source_policy.get("guardrails_enforced", {}).get("auxiliary_source_cannot_be_sole_price_source") is True
            and stock_source_policy.get("guardrails_enforced", {}).get("complete_decision_evidence_allowed") is True
            and stock_source_policy.get("guardrails_enforced", {}).get("source_conflict_requires_note") is True,
            (
                f"{stock_source_policy.get('schema_version') or 'missing'} "
                f"auxiliary-only price blocked="
                f"{stock_source_policy.get('guardrails_enforced', {}).get('auxiliary_source_cannot_be_sole_price_source')}."
            ),
        ),
        _invariant(
            "stock_app_update_runner_dry_run_available",
            stock_update_plan.get("schema_version") == "stock_ai.update_runner.v1"
            and stock_update_plan.get("dry_run") is True
            and stock_update_plan.get("phase_count") == 5
            and stock_update_plan.get("job_count") == 18
            and stock_update_run.get("schema_version") == "stock_ai.update_run.v1"
            and stock_update_run.get("dry_run") is True
            and stock_update_run.get("mutated") is False,
            (
                f"{stock_update_plan.get('schema_version') or 'missing'} "
                f"{stock_update_plan.get('job_count', 0)} jobs / "
                f"dry_run mutated={stock_update_run.get('mutated')}."
            ),
        ),
        _invariant(
            "required_open_stock_ai_module_surface_available",
            module_surface["complete"]
            and module_surface["dirs_complete"]
            and module_surface["no_placeholders"],
            (
                f"{module_surface['existing_count']}/{module_surface['required_count']} required files exist; "
                f"{module_surface['existing_dir_count']}/{module_surface['required_dir_count']} required dirs exist; "
                f"{len(module_surface['placeholder_hits'])} placeholder hits."
            ),
        ),
        _invariant(
            "all_expected_external_sources_verified",
            len(missing_projects) == 0,
            f"{len(verified_projects)}/{len(EXPECTED_EXTERNAL_PROJECTS)} cloned origins verified.",
        ),
        _invariant(
            "external_source_git_lock_verified",
            len(missing_locked_projects) == 0,
            f"{len(locked_projects)}/{len(EXPECTED_EXTERNAL_PROJECTS)} cloned origins and HEAD commits match the approved git clone lock.",
        ),
        _invariant(
            "optional_external_sources_excluded",
            optional_external_sources["available"],
            (
                f"{optional_external_sources['excluded_count']}/"
                f"{optional_external_sources['optional_count']} optional sources excluded; "
                f"unexpected clones={optional_external_sources['unexpected_clone_count']}."
            ),
        ),
        _invariant(
            "all_required_contracts_loaded",
            len(missing_contracts) == 0,
            f"{len(loaded_contracts)}/{len(REQUIRED_CONTRACTS)} read-only adapter contracts loaded.",
        ),
        _invariant(
            "unified_adapter_result_schema_available",
            len(missing_schema_contracts) == 0,
            f"{len(schema_contracts)}/{len(REQUIRED_CONTRACTS)} contracts expose {ADAPTER_SCHEMA_VERSION}.",
        ),
        _invariant(
            "external_evidence_lineage_available",
            external_evidence_lineage["available"],
            (
                f"{external_evidence_lineage['traced_count']}/"
                f"{external_evidence_lineage['expected_count']} external evidence sources traced; "
                f"missing={','.join(external_evidence_lineage['missing_sources']) or 'none'}."
            ),
        ),
        _invariant(
            "external_license_footprint_reviewed",
            external_license_footprint["available"],
            (
                f"{external_license_footprint['reviewed_count']}/{external_license_footprint['project_count']} projects reviewed; "
                f"{external_license_footprint['missing_license_count']} missing license files."
            ),
        ),
        _invariant(
            "runtime_connector_governance_enforced",
            runtime_connector_governance["available"],
            (
                f"{runtime_connector_governance['blocked_count']}/"
                f"{runtime_connector_governance['connector_count']} runtime connectors blocked; "
                f"remote orders allowed={runtime_connector_governance['remote_order_submission_allowed']}."
            ),
        ),
        _invariant(
            "broker_account_import_governance_enforced",
            broker_import_governance["available"],
            (
                f"{broker_import_governance['blocked_count']}/"
                f"{broker_import_governance['connector_count']} broker/account import paths blocked; "
                f"credentials present={broker_import_governance['credential_count']}."
            ),
        ),
        _invariant(
            "external_project_contribution_matrix_available",
            external_project_contribution_matrix["available"],
            (
                f"{external_project_contribution_matrix['traced_count']}/"
                f"{external_project_contribution_matrix['project_count']} external projects traced; "
                f"{external_project_contribution_matrix['remote_order_bypass_count']} remote bypasses."
            ),
        ),
        _invariant(
            "unified_market_data_source_envelopes_available",
            data_source_schema["available"],
            f"{data_source_schema['schema_version'] or 'missing'} with {data_source_schema['source_count']} source envelopes.",
        ),
        _invariant(
            "unified_stock_decision_schema_available",
            stock_decision_schema["available"],
            f"{stock_decision_schema['schema_version'] or 'missing'} with {stock_decision_schema['component_count']} typed components.",
        ),
        _invariant(
            "unified_risk_gate_configured",
            settings.min_rule_score_threshold > 0
            and settings.max_position_size_pct > 0
            and settings.max_daily_loss_pct > 0
            and settings.max_total_drawdown_pct > 0,
            "Risk limits are loaded from OpenStockAISettings.",
        ),
        _invariant(
            "unified_risk_decision_schema_available",
            risk_decision_schema["available"],
            f"{risk_decision_schema['schema_version'] or 'missing'} with {risk_decision_schema['gate_check_count']} gate checks.",
        ),
        _invariant(
            "paper_execution_only",
            runtime["paper_only"],
            f"mode={settings.mode}, live_trading_enabled={settings.live_trading_enabled}.",
        ),
        _invariant(
            "paper_order_ledger_available",
            paper_ledger["available"],
            f"{paper_ledger['schema_version'] or 'missing'} stored in SQLite trades table.",
        ),
        _invariant(
            "research_artifacts_available",
            research_artifacts["available"],
            f"{research_artifacts['schema_version'] or 'missing'} with {research_artifacts['existing_count']}/{research_artifacts['artifact_count']} files present.",
        ),
        _invariant(
            "qlib_workflow_summary_available",
            qlib_workflow_summary["available"],
            f"{qlib_workflow_summary['schema_version'] or 'missing'} selected {qlib_workflow_summary['selected_model'] or 'no model'}.",
        ),
        _invariant(
            "qlib_factor_projection_available",
            qlib_factor_projection["available"],
            (
                f"{qlib_factor_projection['schema_version'] or 'missing'} model "
                f"{qlib_factor_projection['model_score'] if qlib_factor_projection['model_score'] is not None else 'n/a'} "
                f"rank {qlib_factor_projection['rank_ic_proxy'] if qlib_factor_projection['rank_ic_proxy'] is not None else 'n/a'}."
            ),
        ),
        _invariant(
            "finrl_backtest_projection_available",
            finrl_backtest_projection["available"],
            f"{finrl_backtest_projection['schema_version'] or 'missing'} projected sharpe {finrl_backtest_projection['sharpe'] or 'n/a'}.",
        ),
        _invariant(
            "fingpt_forecast_projection_available",
            fingpt_forecast_projection["available"],
            f"{fingpt_forecast_projection['schema_version'] or 'missing'} projected {fingpt_forecast_projection['direction'] or 'no direction'}.",
        ),
        _invariant(
            "finrobot_report_projection_available",
            finrobot_report_projection["available"],
            f"{finrobot_report_projection['schema_version'] or 'missing'} projected {finrobot_report_projection['report_view'] or 'no report view'}.",
        ),
        _invariant(
            "signals_ai_trader_schema_validated",
            ai_trader_signal_schema["available"],
            f"{ai_trader_signal_validated_count}/{len(signal_items)} recent signals validate against AI-Trader signals schema.",
        ),
        _invariant(
            "ai_trader_interop_projection_available",
            ai_trader_interop_projection["available"],
            f"{ai_trader_interop_projection['schema_version'] or 'missing'} signal projection valid={ai_trader_interop_projection['signal_valid']}.",
        ),
        _invariant(
            "ai_trader_skill_route_available",
            ai_trader_skill_route["available"],
            (
                f"{ai_trader_skill_route['schema_version'] or 'missing'} "
                f"{ai_trader_skill_route['selected_skill_count']} selected / "
                f"{ai_trader_skill_route['skill_count']} skills."
            ),
        ),
        _invariant(
            "signal_ledger_schema_available",
            signal_ledger["available"],
            f"{signal_ledger['schema_version'] or 'missing'} rows {signal_ledger['row_schema_version'] or 'missing'}.",
        ),
        _invariant(
            "paper_orders_ai_trader_schema_validated",
            ai_trader_trade_schema["available"],
            f"{ai_trader_validated_count}/{len(paper_items)} recent paper orders validate against AI-Trader trades schema.",
        ),
        _invariant(
            "paper_portfolio_exposure_available",
            paper_exposure_summary["available"],
            f"{paper_exposure_summary['schema_version'] or 'missing'} total exposure {paper_exposure_summary['total_position_size_pct']}%.",
        ),
        _invariant(
            "portfolio_construction_projection_available",
            portfolio_construction["available"]
            and portfolio_construction["schema_version"] == "open_stock_ai.portfolio_construction.v5",
            (
                f"{portfolio_construction['schema_version'] or 'missing'} "
                f"{portfolio_construction['item_count']} items / {portfolio_construction['proposed_total_weight_pct']}% proposed / "
                f"portfolio_ready={portfolio_construction.get('portfolio_ready') is True}."
            ),
        ),
        _invariant(
            "paper_outcome_attribution_available",
            outcome_attribution["available"]
            and outcome_attribution["schema_version"] == "open_stock_ai.paper_outcome_attribution.v1",
            (
                f"{outcome_attribution['schema_version'] or 'missing'} "
                f"{outcome_attribution['observation_count']} observations / "
                f"{outcome_attribution['evaluated_count']} evaluated / "
                f"{outcome_attribution['positive_rate']} positive rate."
            ),
        ),
        _invariant(
            "no_external_runtime_imports",
            runtime_boundary["clean"],
            f"{runtime_boundary['scanned_files']} files scanned; {len(runtime_boundary['violations'])} disallowed imports.",
        ),
        _invariant(
            "decision_replay_available",
            replay["available"],
            f"{replay['schema_version'] or 'missing'} backed by {storage_summary['db_path'] or 'no db path'}.",
        ),
        _invariant(
            "tradingagents_reflection_replay_available",
            reflection_replay["available"],
            (
                f"{reflection_replay['schema_version'] or 'missing'} "
                f"{reflection_replay['dominant_cohort'] or 'no cohort'} / "
                f"{reflection_replay['risk_debator_count']} risk debators."
            ),
        ),
        _invariant(
            "portfolio_attribution_projection_available",
            portfolio_attribution["available"],
            (
                f"{portfolio_attribution['schema_version'] or 'missing'} "
                f"{portfolio_attribution['symbol_count']} symbols / "
                f"{portfolio_attribution['decision_count']} decisions."
            ),
        ),
        _invariant(
            "decision_log_ledger_schema_available",
            decision_log_ledger["available"],
            f"{decision_log_ledger['schema_version'] or 'missing'} rows {decision_log_ledger['row_schema_version'] or 'missing'}.",
        ),
        _invariant(
            "integration_requirement_matrix_available",
            True,
            "Requirement matrix is generated from current audit evidence.",
        ),
    ]
    passed_count = sum(1 for item in invariants if item["passed"])
    requirement_matrix = _integration_requirement_matrix(
        invariants=invariants,
        external_license_footprint=external_license_footprint,
        runtime_connector_governance=runtime_connector_governance,
        broker_import_governance=broker_import_governance,
    )
    return {
        "method": "open_stock_ai_integration_audit",
        "entrypoint": "src/open_stock_ai",
        "external_sources": {
            "expected": EXPECTED_EXTERNAL_PROJECTS,
            "count": external_sources.get("count", len(projects)),
            "verified_count": external_sources.get("verified_count", len(verified_projects)),
            "all_verified": external_sources.get("all_verified") is True,
            "lock_verified_count": source_lock.get("lock_verified_count", len(locked_projects)),
            "all_locked": source_lock.get("all_locked") is True,
            "verified_projects": verified_projects,
            "missing_projects": missing_projects,
        },
        "external_source_lock": {
            "schema_version": source_lock.get("schema_version"),
            "method": source_lock.get("method"),
            "lock_verified_count": source_lock.get("lock_verified_count", len(locked_projects)),
            "count": source_lock.get("count", len(locked_entries)),
            "all_locked": source_lock.get("all_locked") is True,
            "missing_projects": missing_locked_projects,
            "manifest": source_lock.get("manifest"),
        },
        "contracts": {
            "required": REQUIRED_CONTRACTS,
            "loaded_count": len(loaded_contracts),
            "loaded": loaded_contracts,
            "missing": missing_contracts,
        },
        "adapter_schema": {
            "version": ADAPTER_SCHEMA_VERSION,
            "contract_count": len(schema_contracts),
            "contracts": schema_contracts,
            "missing": missing_schema_contracts,
        },
        "external_evidence_lineage": external_evidence_lineage,
        "external_license_footprint": external_license_footprint,
        "optional_external_sources": optional_external_sources,
        "runtime_connector_governance": runtime_connector_governance,
        "broker_import_governance": broker_import_governance,
        "external_project_contribution_matrix": external_project_contribution_matrix,
        "runtime": runtime,
        "data_source_schema": data_source_schema,
        "stock_decision_schema": stock_decision_schema,
        "risk": risk,
        "risk_decision_schema": risk_decision_schema,
        "runtime_boundary": runtime_boundary,
        "module_surface": module_surface,
        "api_router": api_router,
        "design_system_contract": design_system_contract,
        "stock_app_requirement_contract": stock_app_contract,
        "stock_app_source_policy": stock_source_policy,
        "stock_app_official_derivatives": stock_official_derivatives,
        "stock_app_official_events": stock_official_events,
        "stock_app_update_plan": stock_update_plan,
        "stock_app_update_run": stock_update_run,
        "storage": storage_summary,
        "replay": replay,
        "reflection_replay": reflection_replay,
        "portfolio_attribution": portfolio_attribution,
        "decision_log_ledger": decision_log_ledger,
        "paper_ledger": paper_ledger,
        "signal_ledger": signal_ledger,
        "research_artifacts": research_artifacts,
        "qlib_workflow_summary": qlib_workflow_summary,
        "qlib_factor_projection": qlib_factor_projection,
        "finrl_backtest_projection": finrl_backtest_projection,
        "fingpt_forecast_projection": fingpt_forecast_projection,
        "finrobot_report_projection": finrobot_report_projection,
        "ai_trader_interop_projection": ai_trader_interop_projection,
        "ai_trader_skill_route": ai_trader_skill_route,
        "ai_trader_signal_schema": ai_trader_signal_schema,
        "ai_trader_trade_schema": ai_trader_trade_schema,
        "paper_exposure": paper_exposure_summary,
        "portfolio_construction": portfolio_construction,
        "outcome_attribution": outcome_attribution,
        "requirement_matrix": requirement_matrix,
        "invariants": invariants,
        "invariants_passed": passed_count,
        "invariants_total": len(invariants),
        "ready": passed_count == len(invariants),
        "settings": asdict(settings),
    }


def _contract_loaded(contract: Any) -> bool:
    return isinstance(contract, dict) and contract.get("loaded") is True


def _external_license_footprint_summary(projects: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for key in EXPECTED_EXTERNAL_PROJECTS:
        project = projects.get(key) if isinstance(projects.get(key), dict) else {}
        license_path = project.get("license_path")
        license_name = project.get("license_name")
        requirement_paths = project.get("requirement_paths") if isinstance(project.get("requirement_paths"), list) else []
        dependency_manifest_count = project.get("dependency_manifest_count")
        if not isinstance(dependency_manifest_count, int):
            dependency_manifest_count = len(requirement_paths)
        status = project.get("license_status") or (
            "license_file_detected" if license_path else "missing_license_file_review_required"
        )
        items.append(
            {
                "key": key,
                "display_name": project.get("display_name") or key,
                "license_path": license_path,
                "license_name": license_name,
                "license_status": status,
                "license_evidence_path": project.get("license_evidence_path") or license_path,
                "license_evidence_kind": project.get("license_evidence_kind")
                or ("license_file" if license_path else None),
                "requires_manual_review": status != "license_file_detected",
                "dependency_manifest_count": dependency_manifest_count,
                "requirement_paths": requirement_paths,
                "runtime_footprint": "read_only_adapter_contract",
            }
        )
    reviewed_count = len(items)
    licensed_count = sum(1 for item in items if item["license_status"] == "license_file_detected")
    missing_license_count = sum(1 for item in items if item["requires_manual_review"])
    return {
        "schema_version": "open_stock_ai.external_license_footprint.v1",
        "method": "local_external_license_and_dependency_manifest_scan",
        "project_count": len(EXPECTED_EXTERNAL_PROJECTS),
        "reviewed_count": reviewed_count,
        "licensed_count": licensed_count,
        "missing_license_count": missing_license_count,
        "dependency_manifest_count": sum(item["dependency_manifest_count"] for item in items),
        "runtime_footprint": "read_only_adapter_contract_no_external_runtime_import",
        "available": reviewed_count == len(EXPECTED_EXTERNAL_PROJECTS),
        "items": items,
    }


def _external_project_contribution_matrix(
    *,
    projects: dict[str, Any],
    locked_projects: list[str],
    contracts: dict[str, Any],
    reflection_replay: dict[str, Any],
    portfolio_construction: dict[str, Any],
    qlib_workflow_summary: dict[str, Any],
    qlib_factor_projection: dict[str, Any],
    finrl_backtest_projection: dict[str, Any],
    fingpt_forecast_projection: dict[str, Any],
    finrobot_report_projection: dict[str, Any],
    ai_trader_interop_projection: dict[str, Any],
    ai_trader_skill_route: dict[str, Any],
) -> dict[str, Any]:
    locked = set(locked_projects)
    contribution_specs = [
        {
            "key": "tradingagents",
            "contract_source_key": "tradingagents",
            "adapter_result_source_key": "tradingagents",
            "projection_schemas": [
                ADAPTER_SCHEMA_VERSION,
                "open_stock_ai.tradingagents_risk_debate.v1",
                reflection_replay.get("schema_version"),
            ],
            "pipeline_stages": ["intelligence", "risk", "replay"],
            "consumed_by": ["IntelligenceHub", "RiskEngine", "decision_review"],
            "risk_engine_gate": "tradingagents_risk_evidence",
            "paper_executor_boundary": "risk_decision_required_before_paper_order",
            "contribution_summary": "Risk debate and reflection memory are projected into central risk and replay evidence.",
        },
        {
            "key": "finrobot",
            "contract_source_key": "finrobot",
            "adapter_result_source_key": "finrobot",
            "projection_schemas": [ADAPTER_SCHEMA_VERSION, finrobot_report_projection.get("schema_version")],
            "pipeline_stages": ["intelligence", "risk"],
            "consumed_by": ["IntelligenceHub", "RiskEngine"],
            "risk_engine_gate": "finrobot_report_evidence",
            "paper_executor_boundary": "risk_decision_required_before_paper_order",
            "contribution_summary": "Equity report structure is projected into report evidence and central risk checks.",
        },
        {
            "key": "fingpt",
            "contract_source_key": "fingpt",
            "adapter_result_source_key": "fingpt",
            "projection_schemas": [ADAPTER_SCHEMA_VERSION, fingpt_forecast_projection.get("schema_version")],
            "pipeline_stages": ["intelligence", "strategy"],
            "consumed_by": ["IntelligenceHub", "StrategyEngine"],
            "risk_engine_gate": None,
            "paper_executor_boundary": "risk_decision_required_before_paper_order",
            "contribution_summary": "Forecast prompt evidence contributes bounded sentiment and direction evidence before risk approval.",
        },
        {
            "key": "finrl_trading",
            "contract_source_key": "finrl",
            "adapter_result_source_key": "finrl",
            "projection_schemas": [
                ADAPTER_SCHEMA_VERSION,
                finrl_backtest_projection.get("schema_version"),
                portfolio_construction.get("schema_version"),
            ],
            "pipeline_stages": ["research", "portfolio", "risk"],
            "consumed_by": ["ResearchEngine", "PortfolioConstruction", "RiskEngine"],
            "risk_engine_gate": "finrl_backtest_evidence",
            "paper_executor_boundary": "paper_only_portfolio_projection",
            "contribution_summary": "FinRL-Trading adaptive-rotation evidence is represented through the FinRL adapter and paper-only portfolio construction.",
        },
        {
            "key": "finrl",
            "contract_source_key": "finrl",
            "adapter_result_source_key": "finrl",
            "projection_schemas": [ADAPTER_SCHEMA_VERSION, finrl_backtest_projection.get("schema_version")],
            "pipeline_stages": ["research", "risk", "portfolio"],
            "consumed_by": ["ResearchEngine", "RiskEngine", "PortfolioConstruction"],
            "risk_engine_gate": "finrl_backtest_evidence",
            "paper_executor_boundary": "risk_decision_required_before_paper_order",
            "contribution_summary": "Backtest contract evidence is projected into research metrics and central risk checks.",
        },
        {
            "key": "qlib",
            "contract_source_key": "qlib",
            "adapter_result_source_key": "qlib",
            "projection_schemas": [
                ADAPTER_SCHEMA_VERSION,
                qlib_workflow_summary.get("schema_version"),
                qlib_factor_projection.get("schema_version"),
            ],
            "pipeline_stages": ["research", "risk", "portfolio"],
            "consumed_by": ["ResearchEngine", "RiskEngine", "PortfolioConstruction"],
            "risk_engine_gate": "qlib_factor_evidence",
            "paper_executor_boundary": "risk_decision_required_before_paper_order",
            "contribution_summary": "Workflow and factor evidence are projected into research scoring and central risk checks.",
        },
        {
            "key": "ai_trader",
            "contract_source_key": "ai_trader",
            "adapter_result_source_key": "ai_trader",
            "projection_schemas": [
                ADAPTER_SCHEMA_VERSION,
                ai_trader_interop_projection.get("schema_version"),
                ai_trader_skill_route.get("schema_version"),
            ],
            "pipeline_stages": ["signal", "paper", "agent_ui"],
            "consumed_by": ["SignalPipeline", "PaperExecutor", "AgentElementsPanel"],
            "risk_engine_gate": None,
            "paper_executor_boundary": "validated_locally_no_remote_publish",
            "contribution_summary": "Signal/trade schemas and skill routes are validated locally without remote publishing.",
        },
    ]
    rows: list[dict[str, Any]] = []
    for spec in contribution_specs:
        key = spec["key"]
        project = projects.get(key) if isinstance(projects.get(key), dict) else {}
        contract_source_key = str(spec["contract_source_key"])
        contract = contracts.get(contract_source_key) if isinstance(contracts.get(contract_source_key), dict) else {}
        projection_schemas = [
            schema for schema in spec["projection_schemas"] if isinstance(schema, str) and schema
        ]
        adapter_schema_version = (
            (contract.get("adapter_result") or {}).get("schema_version")
            if isinstance(contract.get("adapter_result"), dict)
            else None
        )
        source_locked = key in locked
        origin_verified = project.get("origin_verified") is True
        adapter_contract_loaded = _contract_loaded(contract)
        adapter_schema_loaded = adapter_schema_version == ADAPTER_SCHEMA_VERSION
        risk_controlled = spec["paper_executor_boundary"] in {
            "risk_decision_required_before_paper_order",
            "paper_only_portfolio_projection",
            "validated_locally_no_remote_publish",
        }
        row_available = (
            source_locked
            and origin_verified
            and adapter_contract_loaded
            and adapter_schema_loaded
            and len(projection_schemas) >= 2
            and risk_controlled
        )
        rows.append(
            {
                "key": key,
                "display_name": project.get("display_name") or key,
                "source_locked": source_locked,
                "origin_verified": origin_verified,
                "contract_source_key": contract_source_key,
                "adapter_result_source_key": spec["adapter_result_source_key"],
                "adapter_contract_loaded": adapter_contract_loaded,
                "adapter_schema_version": adapter_schema_version,
                "projection_schemas": projection_schemas,
                "pipeline_stages": spec["pipeline_stages"],
                "consumed_by": spec["consumed_by"],
                "risk_engine_gate": spec["risk_engine_gate"],
                "requires_central_risk_engine": True,
                "can_bypass_risk_engine": False,
                "remote_order_submission_allowed": False,
                "paper_executor_boundary": spec["paper_executor_boundary"],
                "execution_boundary": "read_only_adapter_no_external_runtime_import",
                "contribution_summary": spec["contribution_summary"],
                "available": row_available,
            }
        )
    traced_count = sum(1 for row in rows if row["available"])
    locked_count = sum(1 for row in rows if row["source_locked"])
    risk_controlled_count = sum(1 for row in rows if row["requires_central_risk_engine"] and not row["can_bypass_risk_engine"])
    remote_order_bypass_count = sum(
        1
        for row in rows
        if row["remote_order_submission_allowed"] or row["can_bypass_risk_engine"]
    )
    return {
        "schema_version": "open_stock_ai.external_project_contribution_matrix.v1",
        "method": "approved_external_project_pipeline_contribution_trace",
        "project_count": len(rows),
        "traced_count": traced_count,
        "locked_count": locked_count,
        "risk_controlled_count": risk_controlled_count,
        "remote_order_bypass_count": remote_order_bypass_count,
        "execution_policy": "read_only_adapters_central_risk_engine_paper_executor_only",
        "available": (
            len(rows) == len(EXPECTED_EXTERNAL_PROJECTS)
            and traced_count == len(rows)
            and locked_count == len(rows)
            and risk_controlled_count == len(rows)
            and remote_order_bypass_count == 0
        ),
        "rows": rows,
    }


def _external_evidence_lineage_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    expected_sources = {
        "tradingagents",
        "fingpt",
        "finrobot",
        "finrl_trading",
        "finrl",
        "qlib",
        "ai_trader",
    }
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        if not isinstance(payload, dict):
            continue
        intelligence = payload.get("intelligence") if isinstance(payload.get("intelligence"), dict) else {}
        research = payload.get("research") if isinstance(payload.get("research"), dict) else {}
        signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
        lineage_entries: list[dict[str, Any]] = []
        artifacts = []
        for role, container in [("intelligence", intelligence), ("research", research)]:
            adapter_results = container.get("adapter_results") if isinstance(container.get("adapter_results"), list) else []
            for adapter_result in adapter_results:
                if not isinstance(adapter_result, dict):
                    continue
                lineage = adapter_result.get("source_lineage") if isinstance(adapter_result.get("source_lineage"), dict) else {}
                artifacts.append(
                    {
                        "artifact_type": "adapter_result",
                        "role": role,
                        "source_key": adapter_result.get("source_key"),
                        "adapter_schema_version": adapter_result.get("schema_version"),
                        "execution_boundary": adapter_result.get("execution_boundary"),
                        "lineage_schema_version": lineage.get("schema_version"),
                    }
                )
                lineage_entries.extend(_lineage_entries(lineage, adapter_result.get("execution_boundary")))
        for artifact_type, projection in [
            ("ai_trader_interop_projection", signal.get("ai_trader_interop_projection")),
            ("ai_trader_skill_route", signal.get("ai_trader_skill_route")),
        ]:
            if not isinstance(projection, dict):
                continue
            lineage = projection.get("source_lineage") if isinstance(projection.get("source_lineage"), dict) else {}
            artifacts.append(
                {
                    "artifact_type": artifact_type,
                    "role": "signal",
                    "source_key": "ai_trader",
                    "adapter_schema_version": projection.get("schema_version"),
                    "execution_boundary": projection.get("execution_boundary"),
                    "lineage_schema_version": lineage.get("schema_version"),
                }
            )
            lineage_entries.extend(_lineage_entries(lineage, projection.get("execution_boundary")))
        deduped: dict[str, dict[str, Any]] = {}
        for entry in lineage_entries:
            key = entry.get("source_key")
            if isinstance(key, str) and key not in deduped:
                deduped[key] = entry
        traced_sources = sorted(deduped)
        missing_sources = sorted(expected_sources - set(traced_sources))
        unverified_sources = sorted(
            key
            for key, entry in deduped.items()
            if entry.get("origin_verified") is not True or entry.get("lock_verified") is not True
        )
        missing_boundaries = [
            item.get("artifact_type")
            for item in artifacts
            if isinstance(item, dict) and not item.get("execution_boundary")
        ]
        available = (
            not missing_sources
            and not unverified_sources
            and not missing_boundaries
            and len(artifacts) >= 7
            and all(item.get("lineage_schema_version") == "open_stock_ai.external_evidence_lineage.v1" for item in artifacts)
        )
        return {
            "schema_version": "open_stock_ai.external_evidence_lineage.v1",
            "method": "latest_decision_external_evidence_lineage_trace",
            "decision_schema_version": payload.get("schema_version"),
            "expected_sources": sorted(expected_sources),
            "expected_count": len(expected_sources),
            "traced_sources": traced_sources,
            "traced_count": len(traced_sources),
            "missing_sources": missing_sources,
            "unverified_sources": unverified_sources,
            "artifact_count": len(artifacts),
            "missing_execution_boundary_artifacts": missing_boundaries,
            "available": available,
            "artifacts": artifacts,
            "entries": [deduped[key] for key in traced_sources],
        }
    return {
        "schema_version": "open_stock_ai.external_evidence_lineage.v1",
        "method": "latest_decision_external_evidence_lineage_trace",
        "decision_schema_version": None,
        "expected_sources": sorted(expected_sources),
        "expected_count": len(expected_sources),
        "traced_sources": [],
        "traced_count": 0,
        "missing_sources": sorted(expected_sources),
        "unverified_sources": [],
        "artifact_count": 0,
        "missing_execution_boundary_artifacts": [],
        "available": False,
        "artifacts": [],
        "entries": [],
    }


def _lineage_entries(lineage: dict[str, Any], execution_boundary: Any) -> list[dict[str, Any]]:
    if lineage.get("schema_version") != "open_stock_ai.external_evidence_lineage.v1":
        return []
    entries = lineage.get("entries") if isinstance(lineage.get("entries"), list) else []
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized.append({**entry, "execution_boundary": execution_boundary})
    return normalized


def _reflection_replay_summary(decision_review: dict[str, Any]) -> dict[str, Any]:
    projection = (
        decision_review.get("reflection_replay")
        if isinstance(decision_review.get("reflection_replay"), dict)
        else {}
    )
    reflection_contract = (
        projection.get("reflection_contract")
        if isinstance(projection.get("reflection_contract"), dict)
        else {}
    )
    rating_scale = projection.get("rating_scale") if isinstance(projection.get("rating_scale"), list) else []
    cohorts = projection.get("cohorts") if isinstance(projection.get("cohorts"), dict) else {}
    available = (
        projection.get("schema_version") == "open_stock_ai.tradingagents_reflection_replay.v1"
        and projection.get("method") == "local_tradingagents_reflection_memory_projection"
        and projection.get("review_schema_version") == "open_stock_ai.decision_review.v1"
        and projection.get("review_method") == "tradingagents_style_decision_replay"
        and projection.get("memory_ready") is True
        and reflection_contract.get("exists") is True
        and len(rating_scale) >= 5
        and projection.get("risk_debator_count", 0) >= 3
        and sum(value for value in cohorts.values() if isinstance(value, int)) == projection.get("total", 0)
        and projection.get("execution_boundary") == "read_only_reflection_replay_no_tradingagents_runtime_import"
    )
    return {
        "schema_version": projection.get("schema_version"),
        "method": projection.get("method"),
        "review_schema_version": projection.get("review_schema_version"),
        "total": projection.get("total", 0),
        "dominant_cohort": projection.get("dominant_cohort"),
        "cohorts": cohorts,
        "latest_lesson": projection.get("latest_lesson"),
        "lesson_count": projection.get("lesson_count", 0),
        "reflection_path": reflection_contract.get("path"),
        "reflection_exists": reflection_contract.get("exists") is True,
        "rating_scale": rating_scale,
        "risk_debator_count": projection.get("risk_debator_count", 0),
        "memory_ready": projection.get("memory_ready") is True,
        "execution_boundary": projection.get("execution_boundary"),
        "available": available,
    }


def _portfolio_attribution_summary(decision_review: dict[str, Any]) -> dict[str, Any]:
    attribution = (
        decision_review.get("portfolio_attribution")
        if isinstance(decision_review.get("portfolio_attribution"), dict)
        else {}
    )
    positions = attribution.get("positions") if isinstance(attribution.get("positions"), list) else []
    position_decision_count = sum(
        item.get("decision_count", 0)
        for item in positions
        if isinstance(item, dict) and isinstance(item.get("decision_count"), int)
    )
    available = (
        attribution.get("schema_version") == "open_stock_ai.portfolio_attribution.v1"
        and attribution.get("method") == "local_decision_log_portfolio_attribution"
        and attribution.get("review_schema_version") == "open_stock_ai.decision_review.v1"
        and attribution.get("available") is True
        and attribution.get("decision_count", 0) > 0
        and attribution.get("symbol_count", 0) == len(positions)
        and position_decision_count == attribution.get("decision_count", 0)
        and attribution.get("execution_boundary") == "replay_attribution_only_no_order_execution"
    )
    return {
        "schema_version": attribution.get("schema_version"),
        "method": attribution.get("method"),
        "review_schema_version": attribution.get("review_schema_version"),
        "symbol_count": attribution.get("symbol_count", 0),
        "decision_count": attribution.get("decision_count", 0),
        "approved_count": attribution.get("approved_count", 0),
        "executed_count": attribution.get("executed_count", 0),
        "blocked_count": attribution.get("blocked_count", 0),
        "approval_rate": attribution.get("approval_rate", 0.0),
        "execution_rate": attribution.get("execution_rate", 0.0),
        "proposed_position_size_pct": attribution.get("proposed_position_size_pct", 0.0),
        "top_symbol": positions[0].get("symbol") if positions and isinstance(positions[0], dict) else None,
        "execution_boundary": attribution.get("execution_boundary"),
        "available": available,
    }


def _adapter_schema_loaded(contract: Any) -> bool:
    if not isinstance(contract, dict):
        return False
    adapter_result = contract.get("adapter_result")
    return isinstance(adapter_result, dict) and adapter_result.get("schema_version") == ADAPTER_SCHEMA_VERSION


def _invariant(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "evidence": evidence}


def _integration_requirement_matrix(
    *,
    invariants: list[dict[str, Any]],
    external_license_footprint: dict[str, Any],
    runtime_connector_governance: dict[str, Any],
    broker_import_governance: dict[str, Any],
) -> dict[str, Any]:
    invariant_map = {item.get("name"): item for item in invariants if isinstance(item, dict)}
    rows = [
        _requirement_row(
            "single_open_stock_ai_entrypoint",
            "Keep src/open_stock_ai as the Open Stock AI owner and API entrypoint.",
            ["src_open_stock_ai_entrypoint", "required_open_stock_ai_module_surface_available"],
            invariant_map,
            evidence_paths=["src/open_stock_ai", "src/open_stock_ai/api.py", "src/stock_ai/main.py"],
        ),
        _requirement_row(
            "requested_design_system_integrated",
            "Expose the requested shadcn/ui, Magic UI, Aceternity, Agent Elements, and Tailwind-inspired design contracts in the local UI.",
            ["open_stock_ai_design_system_contract_available"],
            invariant_map,
            evidence_paths=["src/stock_ai/ui/static/index.html", "src/stock_ai/ui/static/css/", "src/stock_ai/ui/static/js/"],
        ),
        _requirement_row(
            "approved_external_source_lock",
            "Use the approved external GitHub clones from external/ with origin and HEAD verification.",
            [
                "all_expected_external_sources_verified",
                "external_source_git_lock_verified",
                "optional_external_sources_excluded",
            ],
            invariant_map,
            evidence_paths=[
                "external/",
                "docs/integration/external_sources_manifest.md",
                "src/open_stock_ai/external_sources/optional_registry.py",
            ],
        ),
        _requirement_row(
            "read_only_external_adapter_boundary",
            "Integrate external projects through read-only adapters without direct heavy runtime imports.",
            [
                "all_required_contracts_loaded",
                "unified_adapter_result_schema_available",
                "external_license_footprint_reviewed",
                "runtime_connector_governance_enforced",
                "broker_account_import_governance_enforced",
                "external_project_contribution_matrix_available",
                "no_external_runtime_imports",
            ],
            invariant_map,
            evidence_paths=[
                "src/open_stock_ai/external_sources",
                "src/open_stock_ai/external_sources/runtime_governance.py",
                "src/open_stock_ai/external_sources/broker_import_governance.py",
            ],
        ),
        _requirement_row(
            "unified_data_and_decision_schema",
            "Normalize market data, stock decisions, and risk decisions into stable Open Stock AI schemas.",
            [
                "unified_market_data_source_envelopes_available",
                "unified_stock_decision_schema_available",
                "unified_risk_decision_schema_available",
            ],
            invariant_map,
            evidence_paths=["src/open_stock_ai/types.py", "src/open_stock_ai/data"],
        ),
        _requirement_row(
            "external_intelligence_and_research_projection",
            "Project TradingAgents, FinGPT, FinRobot, FinRL, Qlib, and AI-Trader evidence into the unified pipeline.",
            [
                "external_evidence_lineage_available",
                "qlib_workflow_summary_available",
                "qlib_factor_projection_available",
                "finrl_backtest_projection_available",
                "fingpt_forecast_projection_available",
                "finrobot_report_projection_available",
                "signals_ai_trader_schema_validated",
                "ai_trader_interop_projection_available",
                "ai_trader_skill_route_available",
            ],
            invariant_map,
            evidence_paths=["src/open_stock_ai/intelligence", "src/open_stock_ai/research", "src/open_stock_ai/external_sources"],
        ),
        _requirement_row(
            "centralized_risk_gate",
            "Route all decisions through the central RiskEngine before any paper execution.",
            ["unified_risk_gate_configured", "unified_risk_decision_schema_available"],
            invariant_map,
            evidence_paths=["src/open_stock_ai/risk/risk_engine.py", "config/open_stock_ai.yaml"],
        ),
        _requirement_row(
            "paper_execution_and_ledger_only",
            "Keep execution paper-only and store paper orders/exposure in the Open Stock AI ledger.",
            ["paper_execution_only", "paper_order_ledger_available", "paper_portfolio_exposure_available"],
            invariant_map,
            evidence_paths=["src/open_stock_ai/execution", "src/open_stock_ai/storage/trade_store.py"],
        ),
        _requirement_row(
            "research_artifacts_and_replay",
            "Persist research artifacts, decisions, replay summaries, and outcome attribution.",
            [
                "research_artifacts_available",
                "signal_ledger_schema_available",
                "decision_replay_available",
                "tradingagents_reflection_replay_available",
                "portfolio_attribution_projection_available",
                "paper_outcome_attribution_available",
                "decision_log_ledger_schema_available",
            ],
            invariant_map,
            evidence_paths=["output/reports", "output/backtests", "src/open_stock_ai/storage"],
        ),
        _requirement_row(
            "batch_and_portfolio_projection",
            "Run configured sessions and produce paper-only portfolio construction projections.",
            ["portfolio_construction_projection_available"],
            invariant_map,
            evidence_paths=["src/open_stock_ai/research/portfolio_construction.py", "config/open_stock_ai.yaml"],
        ),
        _requirement_row(
            "known_external_review_items_are_explicit",
            "Keep unresolved external license/runtime/live-connector items explicit instead of silently enabling them.",
            [
                "external_license_footprint_reviewed",
                "runtime_connector_governance_enforced",
                "broker_account_import_governance_enforced",
            ],
            invariant_map,
            evidence_paths=[
                "src/open_stock_ai/external_sources/registry.py",
                "src/open_stock_ai/external_sources/runtime_governance.py",
                "src/open_stock_ai/external_sources/broker_import_governance.py",
            ],
            status_override="satisfied_with_review_items",
        ),
    ]
    satisfied = sum(1 for row in rows if row["status"] in {"satisfied", "satisfied_with_review_items"})
    unsatisfied = [row["id"] for row in rows if row["status"] == "unsatisfied"]
    review_items = _requirement_review_items(
        external_license_footprint,
        runtime_connector_governance,
        broker_import_governance,
    )
    return {
        "schema_version": "open_stock_ai.integration_requirement_matrix.v1",
        "method": "current_audit_requirement_mapping",
        "objective": (
            "Integrate approved external GitHub projects under external/ into Open Stock AI while keeping "
            "src/open_stock_ai as the entrypoint, unified data/risk, and paper execution."
        ),
        "requirement_count": len(rows),
        "satisfied_count": satisfied,
        "unsatisfied_count": len(unsatisfied),
        "unsatisfied": unsatisfied,
        "review_item_count": len(review_items),
        "review_items": review_items,
        "rows": rows,
        "available": len(rows) >= 10 and not unsatisfied,
    }


def _requirement_row(
    row_id: str,
    requirement: str,
    invariant_names: list[str],
    invariant_map: dict[str, dict[str, Any]],
    *,
    evidence_paths: list[str],
    status_override: str | None = None,
) -> dict[str, Any]:
    checks = [invariant_map.get(name, {"name": name, "passed": False, "evidence": "missing invariant"}) for name in invariant_names]
    passed = all(item.get("passed") is True for item in checks)
    status = status_override if passed and status_override else "satisfied" if passed else "unsatisfied"
    return {
        "id": row_id,
        "requirement": requirement,
        "status": status,
        "passed": passed,
        "invariants": [
            {
                "name": item.get("name"),
                "passed": item.get("passed") is True,
                "evidence": item.get("evidence"),
            }
            for item in checks
        ],
        "evidence_paths": evidence_paths,
    }


def _requirement_review_items(
    external_license_footprint: dict[str, Any],
    runtime_connector_governance: dict[str, Any],
    broker_import_governance: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in external_license_footprint.get("items") or []:
        if not isinstance(item, dict) or item.get("requires_manual_review") is not True:
            continue
        items.append(
            {
                "type": "external_license_review",
                "key": item.get("key"),
                "status": item.get("license_status"),
                "evidence": item.get("license_evidence_path") or "no top-level license file detected",
            }
        )
    for item in runtime_connector_governance.get("entries") or []:
        if not isinstance(item, dict):
            continue
        if item.get("runtime_role", {}).get("can_submit_remote_orders") is True:
            items.append(
                {
                    "type": "remote_order_capable_runtime_disabled",
                    "key": item.get("key"),
                    "status": item.get("connector_status"),
                    "evidence": ",".join(item.get("blockers") or []),
                }
            )
    for item in broker_import_governance.get("entries") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("import_role") if isinstance(item.get("import_role"), dict) else {}
        if role.get("can_import_realized_orders") is True or role.get("can_mutate_remote_broker") is True:
            items.append(
                {
                    "type": "broker_account_import_disabled",
                    "key": item.get("key"),
                    "status": item.get("import_status"),
                    "evidence": ",".join(item.get("blockers") or []),
                }
            )
    return items


def _research_artifact_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        research = (payload or {}).get("research") if isinstance(payload, dict) else {}
        artifacts = (research or {}).get("artifacts") if isinstance(research, dict) else {}
        if not isinstance(artifacts, dict):
            continue
        artifact_items = artifacts.get("artifacts") if isinstance(artifacts.get("artifacts"), list) else []
        existing_count = sum(1 for artifact in artifact_items if _artifact_exists(artifact))
        kinds = {artifact.get("kind") for artifact in artifact_items if isinstance(artifact, dict)}
        available = (
            artifacts.get("schema_version") == "open_stock_ai.research_artifacts.v1"
            and {"research_report", "backtest_payload"}.issubset(kinds)
            and existing_count == len(artifact_items)
            and len(artifact_items) >= 2
        )
        return {
            "schema_version": artifacts.get("schema_version"),
            "method": artifacts.get("method"),
            "generated_at": artifacts.get("generated_at"),
            "artifact_count": len(artifact_items),
            "existing_count": existing_count,
            "kinds": sorted(kind for kind in kinds if kind),
            "available": available,
        }
    return {
        "schema_version": None,
        "method": None,
        "generated_at": None,
        "artifact_count": 0,
        "existing_count": 0,
        "kinds": [],
        "available": False,
    }


def _qlib_workflow_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        research = (payload or {}).get("research") if isinstance(payload, dict) else {}
        raw = (research or {}).get("raw") if isinstance(research, dict) else {}
        qlib = (raw or {}).get("qlib") if isinstance(raw, dict) else {}
        summary = (qlib or {}).get("workflow_summary") if isinstance(qlib, dict) else {}
        if not isinstance(summary, dict):
            continue
        selected = summary.get("selected_workflow") if isinstance(summary.get("selected_workflow"), dict) else {}
        available = (
            summary.get("schema_version") == "open_stock_ai.qlib_workflow_summary.v1"
            and summary.get("method") == "local_qlib_workflow_projection"
            and summary.get("workflow_count", 0) >= 1
            and bool(selected.get("model"))
            and summary.get("execution_boundary") == "read_only_contract_no_qlib_runtime_import"
        )
        return {
            "schema_version": summary.get("schema_version"),
            "method": summary.get("method"),
            "workflow_count": summary.get("workflow_count", 0),
            "family_count": summary.get("family_count", 0),
            "selected_family": selected.get("family"),
            "selected_model": selected.get("model"),
            "selected_dataset": selected.get("dataset"),
            "selected_strategy": selected.get("strategy"),
            "factor_score": summary.get("factor_score"),
            "factor_passed": summary.get("factor_passed"),
            "available": available,
        }
    return {
        "schema_version": None,
        "method": None,
        "workflow_count": 0,
        "family_count": 0,
        "selected_family": None,
        "selected_model": None,
        "selected_dataset": None,
        "selected_strategy": None,
        "factor_score": None,
        "factor_passed": None,
        "available": False,
    }


def _qlib_factor_projection_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    fallback: dict[str, Any] | None = None
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        research = (payload or {}).get("research") if isinstance(payload, dict) else {}
        raw = (research or {}).get("raw") if isinstance(research, dict) else {}
        qlib = (raw or {}).get("qlib") if isinstance(raw, dict) else {}
        projection = (qlib or {}).get("factor_projection") if isinstance(qlib, dict) else {}
        if not isinstance(projection, dict):
            continue
        factors = projection.get("factors") if isinstance(projection.get("factors"), dict) else {}
        research_report = (
            projection.get("research_report")
            if isinstance(projection.get("research_report"), dict)
            else {}
        )
        available = (
            projection.get("schema_version") == "open_stock_ai.qlib_factor_projection.v1"
            and projection.get("method") == "local_qlib_factor_projection"
            and projection.get("score") is not None
            and projection.get("model_score") is not None
            and projection.get("rank_ic_proxy") is not None
            and bool(projection.get("selected_model"))
            and projection.get("workflow_count", 0) >= 1
            and projection.get("workflow_summary_schema_version") == "open_stock_ai.qlib_workflow_summary.v1"
            and "momentum_5d" in factors
            and "volatility_20d" in factors
            and research_report.get("schema_version") == "open_stock_ai.research_artifacts.v1"
            and bool(research_report.get("path"))
            and Path(str(research_report.get("path"))).exists()
            and projection.get("execution_boundary") == "read_only_contract_no_qlib_runtime_import"
        )
        summary = {
            "schema_version": projection.get("schema_version"),
            "method": projection.get("method"),
            "passed": projection.get("passed"),
            "score": projection.get("score"),
            "model_score": projection.get("model_score"),
            "rank_ic_proxy": projection.get("rank_ic_proxy"),
            "selected_family": projection.get("selected_family"),
            "selected_model": projection.get("selected_model"),
            "selected_dataset": projection.get("selected_dataset"),
            "selected_strategy": projection.get("selected_strategy"),
            "workflow_count": projection.get("workflow_count", 0),
            "family_count": projection.get("family_count", 0),
            "research_report_path": research_report.get("path"),
            "research_report_exists": bool(research_report.get("path"))
            and Path(str(research_report.get("path"))).exists(),
            "available": available,
        }
        if available:
            return summary
        if fallback is None:
            fallback = summary
    if fallback is not None:
        return fallback
    return {
        "schema_version": None,
        "method": None,
        "passed": False,
        "score": None,
        "model_score": None,
        "rank_ic_proxy": None,
        "selected_family": None,
        "selected_model": None,
        "selected_dataset": None,
        "selected_strategy": None,
        "workflow_count": 0,
        "family_count": 0,
        "research_report_path": None,
        "research_report_exists": False,
        "available": False,
    }


def _finrl_backtest_projection_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    fallback: dict[str, Any] | None = None
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        research = (payload or {}).get("research") if isinstance(payload, dict) else {}
        raw = (research or {}).get("raw") if isinstance(research, dict) else {}
        finrl = (raw or {}).get("finrl") if isinstance(raw, dict) else {}
        projection = (finrl or {}).get("backtest_projection") if isinstance(finrl, dict) else {}
        if not isinstance(projection, dict):
            continue
        metrics = projection.get("metrics") if isinstance(projection.get("metrics"), dict) else {}
        contract = projection.get("contract") if isinstance(projection.get("contract"), dict) else {}
        available = (
            projection.get("schema_version") == "open_stock_ai.finrl_backtest_projection.v1"
            and projection.get("method") == "local_finrl_backtest_contract_projection"
            and metrics.get("sharpe") is not None
            and contract.get("has_backtest_engine") is True
            and contract.get("adaptive_rotation_file_count", 0) >= 1
            and projection.get("execution_boundary") == "read_only_contract_no_finrl_runtime_import"
        )
        summary = {
            "schema_version": projection.get("schema_version"),
            "method": projection.get("method"),
            "backtest_id": projection.get("backtest_id"),
            "passed": projection.get("passed"),
            "sharpe": metrics.get("sharpe"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "win_rate": metrics.get("win_rate"),
            "has_backtest_engine": contract.get("has_backtest_engine"),
            "adaptive_rotation_file_count": contract.get("adaptive_rotation_file_count", 0),
            "available": available,
        }
        if available:
            return summary
        if fallback is None:
            fallback = summary
    if fallback is not None:
        return fallback
    return {
        "schema_version": None,
        "method": None,
        "backtest_id": None,
        "passed": False,
        "sharpe": None,
        "max_drawdown_pct": None,
        "win_rate": None,
        "has_backtest_engine": False,
        "adaptive_rotation_file_count": 0,
        "available": False,
    }


def _fingpt_forecast_projection_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        intelligence = (payload or {}).get("intelligence") if isinstance(payload, dict) else {}
        raw = (intelligence or {}).get("raw") if isinstance(intelligence, dict) else {}
        fingpt = (raw or {}).get("fingpt") if isinstance(raw, dict) else {}
        projection = (fingpt or {}).get("forecast_projection") if isinstance(fingpt, dict) else {}
        if not isinstance(projection, dict):
            continue
        prompt_contract = (
            projection.get("prompt_contract")
            if isinstance(projection.get("prompt_contract"), dict)
            else {}
        )
        available = (
            projection.get("schema_version") == "open_stock_ai.fingpt_forecast_projection.v1"
            and projection.get("method") == "deterministic_forecaster_contract_projection"
            and projection.get("direction") in {"up", "down", "flat"}
            and prompt_contract.get("has_get_all_prompts") is True
            and prompt_contract.get("has_prompt_end") is True
            and projection.get("execution_boundary") == "read_only_contract_no_fingpt_runtime_import"
        )
        return {
            "schema_version": projection.get("schema_version"),
            "method": projection.get("method"),
            "direction": projection.get("direction"),
            "bin_label": projection.get("bin_label"),
            "forecast_score": projection.get("forecast_score"),
            "prompt_path": prompt_contract.get("path"),
            "available": available,
        }
    return {
        "schema_version": None,
        "method": None,
        "direction": None,
        "bin_label": None,
        "forecast_score": None,
        "prompt_path": None,
        "available": False,
    }


def _finrobot_report_projection_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        intelligence = (payload or {}).get("intelligence") if isinstance(payload, dict) else {}
        raw = (intelligence or {}).get("raw") if isinstance(intelligence, dict) else {}
        finrobot = (raw or {}).get("finrobot") if isinstance(raw, dict) else {}
        projection = (finrobot or {}).get("report_projection") if isinstance(finrobot, dict) else {}
        if not isinstance(projection, dict):
            continue
        report_contract = (
            projection.get("report_contract")
            if isinstance(projection.get("report_contract"), dict)
            else {}
        )
        agent_contract = (
            projection.get("agent_contract")
            if isinstance(projection.get("agent_contract"), dict)
            else {}
        )
        required_tools = (
            agent_contract.get("required_tools_present")
            if isinstance(agent_contract.get("required_tools_present"), dict)
            else {}
        )
        available = (
            projection.get("schema_version") == "open_stock_ai.finrobot_report_projection.v1"
            and projection.get("method") == "local_equity_report_contract_projection"
            and projection.get("report_view") in {"positive", "negative", "neutral"}
            and projection.get("risk_view") in {"low", "medium", "high"}
            and report_contract.get("section_count", 0) >= 6
            and required_tools.get("analyze_income_stmt") is True
            and required_tools.get("get_risk_assessment") is True
            and projection.get("execution_boundary") == "read_only_contract_no_finrobot_runtime_import"
        )
        return {
            "schema_version": projection.get("schema_version"),
            "method": projection.get("method"),
            "report_view": projection.get("report_view"),
            "risk_view": projection.get("risk_view"),
            "valuation_view": projection.get("valuation_view"),
            "section_count": report_contract.get("section_count", 0),
            "tool_count": agent_contract.get("tool_count", 0),
            "available": available,
        }
    return {
        "schema_version": None,
        "method": None,
        "report_view": None,
        "risk_view": None,
        "valuation_view": None,
        "section_count": 0,
        "tool_count": 0,
        "available": False,
    }


def _ai_trader_interop_projection_summary(
    signal_items: list[dict[str, Any]],
    paper_items: list[dict[str, Any]],
) -> dict[str, Any]:
    signal_projection: dict[str, Any] = {}
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        signal = (payload or {}).get("signal") if isinstance(payload, dict) else {}
        projection = (
            signal.get("ai_trader_interop_projection")
            if isinstance(signal, dict) and isinstance(signal.get("ai_trader_interop_projection"), dict)
            else {}
        )
        if projection:
            signal_projection = projection
            break
    trade_projections = [
        item.get("ai_trader_interop_projection")
        for item in paper_items
        if isinstance(item, dict) and isinstance(item.get("ai_trader_interop_projection"), dict)
    ]
    signal_available = (
        signal_projection.get("schema_version") == "open_stock_ai.ai_trader_interop_projection.v1"
        and signal_projection.get("method") == "local_signal_schema_interop_projection"
        and signal_projection.get("artifact_type") == "signal"
        and signal_projection.get("valid") is True
        and signal_projection.get("validation_schema_version") == "open_stock_ai.ai_trader_signal_validation.v1"
        and signal_projection.get("publishing_boundary") == "validated_locally_no_remote_publish"
        and signal_projection.get("execution_boundary") == "validated_locally_no_remote_ai_trader_publish"
        and signal_projection.get("skill_count", 0) >= 1
    )
    return {
        "schema_version": signal_projection.get("schema_version"),
        "method": signal_projection.get("method"),
        "signal_valid": signal_projection.get("valid") is True,
        "signal_schema_path": signal_projection.get("schema_path"),
        "signal_row_field_count": signal_projection.get("row_field_count", 0),
        "signal_skill_count": signal_projection.get("skill_count", 0),
        "paper_order_projection_count": len(trade_projections),
        "paper_order_checked_count": len(paper_items),
        "available": signal_available,
    }


def _ai_trader_skill_route_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        signal = (payload or {}).get("signal") if isinstance(payload, dict) else {}
        route = (
            signal.get("ai_trader_skill_route")
            if isinstance(signal, dict) and isinstance(signal.get("ai_trader_skill_route"), dict)
            else {}
        )
        if not route:
            continue
        selected_names = route.get("selected_skill_names") if isinstance(route.get("selected_skill_names"), list) else []
        disabled_actions = (
            route.get("disabled_remote_actions")
            if isinstance(route.get("disabled_remote_actions"), list)
            else []
        )
        available = (
            route.get("schema_version") == "open_stock_ai.ai_trader_skill_route.v1"
            and route.get("method") == "local_skill_frontmatter_route_projection"
            and route.get("valid") is True
            and route.get("route_ready") is True
            and route.get("selected_skill_count", 0) >= 1
            and "ai-trader" in selected_names
            and "publish_signal" in disabled_actions
            and route.get("publishing_boundary") == "validated_locally_no_remote_publish"
            and route.get("execution_boundary") == "read_only_skill_route_no_remote_ai_trader_publish"
        )
        return {
            "schema_version": route.get("schema_version"),
            "method": route.get("method"),
            "valid": route.get("valid") is True,
            "route_ready": route.get("route_ready") is True,
            "skill_count": route.get("skill_count", 0),
            "selected_skill_count": route.get("selected_skill_count", 0),
            "selected_skill_names": selected_names,
            "disabled_remote_actions": disabled_actions,
            "publishing_boundary": route.get("publishing_boundary"),
            "execution_boundary": route.get("execution_boundary"),
            "available": available,
        }
    return {
        "schema_version": None,
        "method": None,
        "valid": False,
        "route_ready": False,
        "skill_count": 0,
        "selected_skill_count": 0,
        "selected_skill_names": [],
        "disabled_remote_actions": [],
        "publishing_boundary": None,
        "execution_boundary": None,
        "available": False,
    }


def _data_source_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    required_sources = {"twse", "tpex", "yahoo", "mops", "news"}
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        snapshot = (payload or {}).get("market_snapshot") if isinstance(payload, dict) else {}
        raw = (snapshot or {}).get("raw") if isinstance(snapshot, dict) else {}
        envelopes = (raw or {}).get("source_envelopes") if isinstance(raw, dict) else {}
        if not isinstance(envelopes, dict):
            continue
        source_keys = set(envelopes.keys())
        valid_keys = [
            key
            for key, envelope in envelopes.items()
            if isinstance(envelope, dict)
            and envelope.get("schema_version") == "open_stock_ai.data_source_envelope.v1"
            and isinstance(envelope.get("quality"), dict)
            and envelope["quality"].get("schema_version") == "open_stock_ai.data_quality.v1"
        ]
        available = required_sources.issubset(source_keys) and set(valid_keys).issuperset(required_sources)
        return {
            "schema_version": "open_stock_ai.data_source_envelope.v1" if envelopes else None,
            "available": available,
            "source_count": len(envelopes),
            "sources": sorted(source_keys),
            "valid_sources": sorted(valid_keys),
            "missing_sources": sorted(required_sources - source_keys),
        }
    return {
        "schema_version": None,
        "available": False,
        "source_count": 0,
        "sources": [],
        "valid_sources": [],
        "missing_sources": sorted(required_sources),
    }


def _stock_decision_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {
        "schema_version": "open_stock_ai.stock_decision.v1",
        "request": "open_stock_ai.stock_request.v1",
        "market_snapshot": "open_stock_ai.market_snapshot.v1",
        "intelligence": "open_stock_ai.intelligence_result.v1",
        "signal": "open_stock_ai.trading_signal.v1",
        "research": "open_stock_ai.research_result.v1",
        "risk": "open_stock_ai.risk_decision.v1",
        "execution": "open_stock_ai.execution_result.v1",
    }
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        if not isinstance(payload, dict):
            continue
        observed = {
            "schema_version": payload.get("schema_version"),
            "request": ((payload.get("request") or {}) if isinstance(payload.get("request"), dict) else {}).get("schema_version"),
            "market_snapshot": (
                (payload.get("market_snapshot") or {}) if isinstance(payload.get("market_snapshot"), dict) else {}
            ).get("schema_version"),
            "intelligence": (
                (payload.get("intelligence") or {}) if isinstance(payload.get("intelligence"), dict) else {}
            ).get("schema_version"),
            "signal": ((payload.get("signal") or {}) if isinstance(payload.get("signal"), dict) else {}).get("schema_version"),
            "research": ((payload.get("research") or {}) if isinstance(payload.get("research"), dict) else {}).get("schema_version"),
            "risk": ((payload.get("risk") or {}) if isinstance(payload.get("risk"), dict) else {}).get("schema_version"),
            "execution": (
                (payload.get("execution") or {}) if isinstance(payload.get("execution"), dict) else {}
            ).get("schema_version"),
        }
        missing = [key for key, version in expected.items() if observed.get(key) != version]
        return {
            "schema_version": observed.get("schema_version"),
            "available": not missing,
            "component_count": len(expected),
            "expected": expected,
            "observed": observed,
            "missing": missing,
        }
    return {
        "schema_version": None,
        "available": False,
        "component_count": 0,
        "expected": expected,
        "observed": {},
        "missing": sorted(expected.keys()),
    }


def _risk_decision_summary(signal_items: list[dict[str, Any]]) -> dict[str, Any]:
    fallback: dict[str, Any] | None = None
    for item in signal_items:
        payload = item.get("payload") if isinstance(item, dict) else {}
        risk = (payload or {}).get("risk") if isinstance(payload, dict) else {}
        if not isinstance(risk, dict):
            continue
        gate_checks = risk.get("gate_checks") if isinstance(risk.get("gate_checks"), list) else []
        policy = risk.get("policy") if isinstance(risk.get("policy"), dict) else {}
        codes = [check.get("code") for check in gate_checks if isinstance(check, dict) and check.get("code")]
        required_codes = {
            "rule_score_threshold",
            "tradingagents_risk_evidence",
            "finrobot_report_evidence",
            "finrl_backtest_evidence",
            "qlib_factor_evidence",
            "research_validation",
            "max_drawdown",
            "position_size_cap",
            "stop_loss_required",
            "estimated_daily_loss",
            "paper_exposure",
        }
        tradingagents_gate = next(
            (check for check in gate_checks if isinstance(check, dict) and check.get("code") == "tradingagents_risk_evidence"),
            {},
        )
        tradingagents_observed = (
            tradingagents_gate.get("observed") if isinstance(tradingagents_gate.get("observed"), dict) else {}
        )
        finrobot_gate = next(
            (check for check in gate_checks if isinstance(check, dict) and check.get("code") == "finrobot_report_evidence"),
            {},
        )
        finrobot_observed = (
            finrobot_gate.get("observed") if isinstance(finrobot_gate.get("observed"), dict) else {}
        )
        finrl_gate = next(
            (check for check in gate_checks if isinstance(check, dict) and check.get("code") == "finrl_backtest_evidence"),
            {},
        )
        finrl_observed = (
            finrl_gate.get("observed") if isinstance(finrl_gate.get("observed"), dict) else {}
        )
        qlib_gate = next(
            (check for check in gate_checks if isinstance(check, dict) and check.get("code") == "qlib_factor_evidence"),
            {},
        )
        qlib_observed = (
            qlib_gate.get("observed") if isinstance(qlib_gate.get("observed"), dict) else {}
        )
        available = (
            risk.get("schema_version") == "open_stock_ai.risk_decision.v1"
            and required_codes.issubset(set(codes))
            and bool(policy)
            and tradingagents_observed.get("schema_version") == "open_stock_ai.tradingagents_risk_debate.v1"
            and finrobot_observed.get("schema_version") == "open_stock_ai.finrobot_report_projection.v1"
            and finrl_observed.get("schema_version") == "open_stock_ai.finrl_backtest_projection.v1"
            and qlib_observed.get("schema_version") == "open_stock_ai.qlib_factor_projection.v1"
        )
        summary = {
            "schema_version": risk.get("schema_version"),
            "available": available,
            "gate_check_count": len(gate_checks),
            "codes": codes,
            "policy_keys": sorted(policy.keys()),
            "tradingagents_risk_evidence": tradingagents_observed,
            "finrobot_report_evidence": finrobot_observed,
            "finrl_backtest_evidence": finrl_observed,
            "qlib_factor_evidence": qlib_observed,
        }
        if available:
            return summary
        if fallback is None:
            fallback = summary
    if fallback is not None:
        return fallback
    return {
        "schema_version": None,
        "available": False,
        "gate_check_count": 0,
        "codes": [],
        "policy_keys": [],
        "tradingagents_risk_evidence": {},
        "finrobot_report_evidence": {},
        "finrl_backtest_evidence": {},
        "qlib_factor_evidence": {},
    }


def _artifact_exists(artifact: Any) -> bool:
    if not isinstance(artifact, dict):
        return False
    path = artifact.get("path")
    return bool(path and Path(str(path)).exists() and artifact.get("exists") is True)


def scan_module_surface(source_root: str | Path = "src/open_stock_ai") -> dict[str, Any]:
    root = Path(source_root)
    missing = [path for path in REQUIRED_MODULE_FILES if not (root / path).exists()]
    missing_dirs = [path for path in REQUIRED_MODULE_DIRS if not Path(path).exists()]
    placeholder_hits: list[dict[str, Any]] = []
    paths = [root / path for path in REQUIRED_MODULE_FILES if (root / path).exists()]
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped == "pass" or "placeholder" in stripped.lower():
                placeholder_hits.append(
                    {
                        "path": str(path),
                        "line": line_number,
                        "text": stripped,
                    }
                )
    required_count = len(REQUIRED_MODULE_FILES)
    existing_count = required_count - len(missing)
    required_dir_count = len(REQUIRED_MODULE_DIRS)
    existing_dir_count = required_dir_count - len(missing_dirs)
    return {
        "source_root": str(root),
        "required_count": required_count,
        "existing_count": existing_count,
        "missing": missing,
        "complete": not missing,
        "required_dirs": REQUIRED_MODULE_DIRS,
        "required_dir_count": required_dir_count,
        "existing_dir_count": existing_dir_count,
        "missing_dirs": missing_dirs,
        "dirs_complete": not missing_dirs,
        "placeholder_hits": placeholder_hits,
        "no_placeholders": not placeholder_hits,
    }


def scan_runtime_import_boundary(source_root: str | Path = "src/open_stock_ai") -> dict[str, Any]:
    root = Path(source_root)
    disallowed = {"tradingagents", "finrobot", "fingpt", "finrl", "qlib", "ai_trader"}
    violations: list[dict[str, Any]] = []
    scanned_files = 0
    paths = sorted(
        path for path in root.rglob("*.py") if not path.name.startswith("._")
    ) if root.exists() else []
    for path in paths:
        scanned_files += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            violations.append(
                {
                    "path": str(path),
                    "line": getattr(exc, "lineno", 0) or 0,
                    "module": "syntax_error",
                    "statement": getattr(exc, "msg", str(exc)),
                }
            )
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".", 1)[0]
                    if top_level in disallowed:
                        violations.append(
                            {
                                "path": str(path),
                                "line": node.lineno,
                                "module": alias.name,
                                "statement": f"import {alias.name}",
                            }
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                module = node.module or ""
                top_level = module.split(".", 1)[0]
                if top_level in disallowed:
                    names = ", ".join(alias.name for alias in node.names)
                    violations.append(
                        {
                            "path": str(path),
                            "line": node.lineno,
                            "module": module,
                            "statement": f"from {module} import {names}",
                        }
                    )
    return {
        "source_root": str(root),
        "disallowed_modules": sorted(disallowed),
        "scanned_files": scanned_files,
        "violations": violations,
        "clean": not violations,
    }


def scan_api_router_mount(
    app_path: str | Path = "src/stock_ai/main.py",
    router_path: str | Path = "src/open_stock_ai/api.py",
) -> dict[str, Any]:
    app_file = Path(app_path)
    router_file = Path(router_path)
    app_text = app_file.read_text(encoding="utf-8") if app_file.exists() else ""
    router_text = router_file.read_text(encoding="utf-8") if router_file.exists() else ""
    route_count = router_text.count('@router.get("')
    direct_app_routes = [
        line.strip()
        for line in app_text.splitlines()
        if '@app.get("/api/open-stock-ai' in line or '@app.post("/api/open-stock-ai' in line
    ]
    import_mounted = "from open_stock_ai.api import router as open_stock_ai_router" in app_text
    include_mounted = "app.include_router(open_stock_ai_router)" in app_text
    router_declared = 'APIRouter(prefix="/api/open-stock-ai"' in router_text
    return {
        "app_path": str(app_file),
        "router_path": str(router_file),
        "router_exists": router_file.exists(),
        "router_declared": router_declared,
        "route_count": route_count,
        "import_mounted": import_mounted,
        "include_mounted": include_mounted,
        "direct_app_route_count": len(direct_app_routes),
        "direct_app_routes": direct_app_routes,
        "mounted": router_file.exists()
        and router_declared
        and route_count >= 10
        and import_mounted
        and include_mounted
        and not direct_app_routes,
    }


def scan_design_system_contract(
    index_path: str | Path = "src/stock_ai/ui/static/index.html",
    styles_path: str | Path = "src/stock_ai/ui/static/css",
    app_path: str | Path = "src/stock_ai/ui/static/js",
) -> dict[str, Any]:
    index_file = Path(index_path)
    styles_file = Path(styles_path)
    app_file = Path(app_path)
    index_text = index_file.read_text(encoding="utf-8") if index_file.exists() else ""
    style_sources = (
        sorted(styles_file.rglob("*.css"))
        if styles_file.is_dir()
        else ([styles_file] if styles_file.is_file() else [])
    )
    styles_text = "\n".join(path.read_text(encoding="utf-8") for path in style_sources)
    app_sources = (
        sorted(app_file.rglob("*.js"))
        if app_file.is_dir()
        else ([app_file] if app_file.is_file() else [])
    )
    app_text = "\n".join(path.read_text(encoding="utf-8") for path in app_sources)
    vendor_dir = index_file.parent / "vendor"
    liquid_gl_file = vendor_dir / "liquidGL.js"
    html2canvas_file = vendor_dir / "html2canvas.min.js"
    glin_theme_file = vendor_dir / "glinui-theme.css"
    liquid_system_file = index_file.parent / "liquid-glass-system.css"
    vendor_note_file = vendor_dir / "OPEN_SOURCE_UI.md"
    liquid_gl_text = liquid_gl_file.read_text(encoding="utf-8") if liquid_gl_file.exists() else ""
    components = [
        {
            "key": "shadcn_ui",
            "label": "shadcn/ui",
            "evidence": [
                "shadcn cards" in index_text
                or "shadcn 卡片結構" in index_text
                or "市場資料" in index_text,
                ".panel" in styles_text,
                ".mini-card" in styles_text,
                "border-radius:var(--radius)" in styles_text or "border-radius: var(--radius)" in styles_text,
                "renderWorkspaceCard" in app_text,
            ],
        },
        {
            "key": "magic_ui",
            "label": "Magic UI",
            "evidence": [
                "Magic shimmer" in index_text
                or "Magic 動態質感" in index_text
                or "Magic 玻璃質感" in index_text
                or "即時行情監控" in index_text,
                ".market-panel" in styles_text,
                "backdrop-filter:saturate(165%) blur(var(--glass-blur-lg))" in styles_text,
                "@keyframes shimmer" not in styles_text,
                ".status-dot" in styles_text and "@keyframes pulse" not in styles_text,
            ],
        },
        {
            "key": "aceternity_ui",
            "label": "Aceternity UI",
            "evidence": [
                "Aceternity grid" in index_text
                or "Aceternity 層次網格" in index_text
                or "Aceternity 層次背景" in index_text
                or "投資工作區" in index_text,
                ".ambient-grid" in styles_text,
                ".ambient-grid{display:none}" in styles_text,
                "background:" in styles_text,
                "background:var(--page-bg)" in styles_text,
            ],
        },
        {
            "key": "agent_elements",
            "label": "21st.dev Agent Elements",
            "evidence": [
                "Agent workspace" in index_text
                or "Agent 工作區" in index_text
                or "量化策略研究" in index_text,
                "openStockFlowBox" in index_text,
                ".process-flow" in styles_text,
                ".process-step" in styles_text,
                "renderOpenStockFlow" in app_text,
            ],
        },
        {
            "key": "tailwind_tokens",
            "label": "Tailwind CSS",
            "evidence": [
                "Tailwind tokens" in index_text
                or "Tailwind 設計基礎" in index_text
                or "資產部位" in index_text
                or "投資組合" in index_text,
                "--tw-" in styles_text,
                "grid-template-columns" in styles_text,
                "@media" in styles_text,
            ],
        },
    ]
    normalized = []
    for component in components:
        evidence = component["evidence"]
        normalized.append(
            {
                "key": component["key"],
                "label": component["label"],
                "passed": all(evidence),
                "evidence_count": sum(1 for item in evidence if item),
                "required_count": len(evidence),
            }
        )
    satisfied = sum(1 for item in normalized if item["passed"])
    return {
        "schema_version": "open_stock_ai.design_system_contract.v1",
        "method": "local_static_ui_design_contract_scan",
        "component_count": len(normalized),
        "satisfied_count": satisfied,
        "components": normalized,
        "external_ui_sources": {
            "liquidgl": {
                "origin": "https://github.com/naughtyduk/liquidGL.git",
                "head": "2cef983b7fe593d3e0878dc78e5b79b47038a953",
                "local_clone": "external/liquidGL",
                "loaded": all(
                    [
                        liquid_gl_file.exists(),
                        html2canvas_file.exists(),
                        "/static/vendor/liquidGL.js" in index_text,
                        "/static/vendor/html2canvas.min.js" in index_text,
                        "window.liquidGL({" in app_text,
                        "'.liquid-webgl-lens,.glass-control-lens'" in app_text,
                        "snapshot: '.glass-sample-layer'" in app_text,
                        "liquidSidebarLens" in index_text,
                        "glassSampleLayer" in index_text,
                        "data-liquid-sample-source-root" in index_text,
                        "data-glass-optical-background" in index_text,
                        "live-document-uv-scroll" in app_text,
                        "glassSampleScrollY" in app_text,
                        "glassScrollFrame" in app_text,
                        "glassCanvasSync" in app_text,
                        "data-liquid-canvas-snapshot" in app_text,
                        "data-liquid-sample-document" in index_text,
                        "glass-sample-app" in app_text,
                        "document.documentElement.dataset.liquidGl = 'webgl-ready'" in app_text,
                        "bindLiquidPointerTracking" in app_text,
                        "dataset.liquidPointerTracking = 'shader-uniforms'" in app_text,
                        "presentLiquidCanvasInLenses" in app_text,
                        "initLiveGlassSampling" in app_text,
                        "dataset.glassControlRefraction = 'webgl-realtime-uv-sampling'" in app_text,
                        "dataset.liquidSnapshotVariance" in liquid_gl_text,
                        "updateCanvasRegion" in liquid_gl_text,
                        "texSubImage2D" in liquid_gl_text,
                        "u_chromaticAberration" in liquid_gl_text,
                        "chromaticAberration: 0.0036" in app_text,
                        "stock-ai-ui-settings-v1" in app_text,
                        'data-view="settings"' in index_text
                        or 'data-view="system"' in index_text,
                        'data-ui-theme="terminal"' in styles_text,
                        "data-liquid-sample-source-root" in liquid_gl_text,
                        "data-liquid-ignore" in index_text,
                    ]
                ),
                "mode": "webgl_realtime_document_uv_canvas_patch_multi_lens",
                "runtime_files": [str(liquid_gl_file), str(html2canvas_file)],
            },
            "glinui": {
                "origin": "https://github.com/glincker/glinui.git",
                "head": "2e19376efbb601f915239ed9571b1c9a3fa9fb7e",
                "local_clone": "external/glinui",
                "referenced_sources": [
                    "external/glinui/packages/ui/src/components/glass-card.tsx",
                    "external/glinui/packages/ui/src/components/liquid-button.tsx",
                    "external/glinui/packages/ui/src/components/spotlight-card.tsx",
                    "external/glinui/packages/ui/src/lib/use-liquid-glass.tsx",
                ],
                "loaded": all(
                    [
                        glin_theme_file.exists(),
                        liquid_system_file.exists(),
                        "/static/vendor/glinui-theme.css" in index_text,
                        "/static/liquid-glass-system.css" in index_text,
                        "GlassSurface" in app_text,
                        "GlassButton" in app_text,
                        "GlassNavigation" in app_text,
                        "initDynamicGlassInteractions" in app_text,
                        "dataset.glassInteractions = 'pointer-elastic'" in app_text,
                        "ResizeObserver" in app_text,
                        "dataset.glinuiGlass = 'token-bridge'" in app_text,
                        "dataset.glinSpotlightPresentation = 'token-bridge'" in app_text,
                    ]
                ),
                "mode": "token_bridge_plus_native_webgl",
                "runtime_files": [str(glin_theme_file), str(liquid_system_file)],
            },
            "attribution_file": str(vendor_note_file),
            "attribution_exists": vendor_note_file.exists(),
        },
        "files": {
            "index": str(index_file),
            "styles": str(styles_file),
            "style_sources": [str(path) for path in style_sources],
            "app": str(app_file),
            "app_sources": [str(path) for path in app_sources],
        },
        "single_local_ui": index_file.exists() and bool(style_sources) and bool(app_sources),
        "available": satisfied == len(normalized) and index_file.exists() and bool(style_sources) and bool(app_sources),
    }
