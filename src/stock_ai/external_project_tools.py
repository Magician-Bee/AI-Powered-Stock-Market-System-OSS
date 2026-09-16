from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import yaml

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec

from .codex_llm_bridge import CodexLLMBridge
from .codex_runtime import codex_runtime


_NUMBER_ARRAY = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2000,
    "items": {"type": "number"},
}

_PRICE_MATRIX = {
    "type": "array",
    "minItems": 3,
    "maxItems": 10000,
    "items": {
        "type": "array",
        "minItems": 1,
        "maxItems": 100,
        "items": {"type": "number", "exclusiveMinimum": 0},
    },
}

_MODEL_INPUT_PROPERTIES = {
    "prices": _PRICE_MATRIX,
    "technical_features": {
        "type": "array",
        "maxItems": 10000,
        "items": {"type": "array", "maxItems": 1000, "items": {"type": "number"}},
    },
    "turbulence": {"type": "array", "maxItems": 10000, "items": {"type": "number"}},
    "initial_capital": {"type": "number", "exclusiveMinimum": 0},
    "max_stock": {"type": "number", "exclusiveMinimum": 0},
    "buy_cost_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
    "sell_cost_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
}

_FULL_WORKFLOW_ACTIONS = {
    "external.tradingagents.analyze_symbol": ("tradingagents", "analyze_symbol"),
    "external.fingpt.run_sentiment_model": ("fingpt", "run_sentiment_model"),
    "external.finrl.train_policy": ("finrl", "train_policy"),
    "external.finrl.predict_actions": ("finrl", "predict_actions"),
    "external.qlib.train_factor_model": ("qlib", "train_factor_model"),
    "external.qlib.backtest_model": ("qlib", "backtest_model"),
    "external.qlib.reference_risk_metrics": ("qlib", "reference_risk_metrics"),
    "external.finrobot.generate_financial_report": ("finrobot", "generate_financial_report"),
}

_PROJECT_DISPLAY_NAMES = {
    "tradingagents": "TradingAgents",
    "fingpt": "FinGPT",
    "finrl": "FinRL",
    "qlib": "Qlib",
    "finrobot": "FinRobot",
}


class ExternalProjectToolProvider:
    """Execute selected, read-only capabilities from vendored finance projects.

    Every action runs in a child Python process. This keeps optional external
    packages isolated from the API server while still executing the vendored
    project's own source module and function.
    """

    provider_id = "external_finance"

    def __init__(self, project_root: str | Path | None = None) -> None:
        configured_root = project_root or os.environ.get("STOCK_AI_PROJECT_ROOT")
        self.project_root = Path(configured_root or Path(__file__).resolve().parents[2]).resolve()
        self.workflow_root = (self.project_root / ".runtime" / "external-workflows").resolve()
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="external.runtime.health",
                    description=(
                        "Return capability-level health for every vendored finance framework, including explicit "
                        "model-loaded, training-ready, inference-executed and full-workflow flags."
                    ),
                    category="external_runtime",
                    packages=("External source locks",),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
                AgentToolSpec(
                    name="external.tradingagents.analyze_symbol",
                    description=(
                        "Execute the complete vendored TradingAgents LangGraph analysis, debate, risk discussion and "
                        "portfolio decision for one symbol. Every model turn is driven by the authenticated Codex App "
                        "Server through a run-scoped Host bridge; it never requires an external LLM API key or falls "
                        "back to a local parser."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("tradingagents-full-workflow",),
                    packages=("external/TradingAgents", "LangGraph", "Codex App Server"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol", "trade_date"],
                        "properties": {
                            "symbol": {"type": "string", "minLength": 1, "maxLength": 40},
                            "trade_date": {"type": "string", "format": "date"},
                            "asset_type": {"type": "string", "enum": ["stock", "crypto"]},
                            "selected_analysts": {
                                "type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                                "items": {"type": "string", "enum": ["market", "social", "news", "fundamentals"]},
                            },
                            "llm_provider": {"type": "string", "maxLength": 40},
                            "deep_model": {"type": "string", "maxLength": 200},
                            "quick_model": {"type": "string", "maxLength": 200},
                            "backend_url": {"type": "string", "maxLength": 1000},
                            "output_language": {"type": "string", "maxLength": 80},
                            "max_debate_rounds": {"type": "integer", "minimum": 1, "maximum": 5},
                            "max_risk_rounds": {"type": "integer", "minimum": 1, "maximum": 5},
                            "checkpoint_enabled": {"type": "boolean"},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.fingpt.run_sentiment_model",
                    description=(
                        "Optional local FinGPT base-model plus LoRA inference. This installation keeps the source "
                        "and executor but disables local model loading by default because Codex is the primary model "
                        "driver. It can run only after an operator explicitly enables fingpt_local_model_enabled."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("fingpt-model-inference",),
                    packages=("external/FinGPT", "torch", "transformers", "peft"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["texts"],
                        "properties": {
                            "texts": {
                                "type": "array", "minItems": 1, "maxItems": 50,
                                "items": {"type": "string", "minLength": 1, "maxLength": 5000},
                            },
                            "base_model": {"type": "string", "maxLength": 500},
                            "lora_model": {"type": "string", "maxLength": 500},
                            "device": {"type": "string", "enum": ["auto", "cpu", "mps", "cuda"]},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl.train_policy",
                    description=(
                        "Train a real FinRL Stable-Baselines3 policy on supplied point-in-time arrays, save the policy "
                        "under .runtime, and run deterministic evaluation. This is compute-only and cannot place orders."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("finrl-policy-training",),
                    packages=("external/FinRL", "Stable-Baselines3", "torch"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["prices"],
                        "properties": {
                            **_MODEL_INPUT_PROPERTIES,
                            "algorithm": {"type": "string", "enum": ["a2c", "ddpg", "ppo", "sac", "td3"]},
                            "total_timesteps": {"type": "integer", "minimum": 64, "maximum": 2000000},
                            "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                            "artifact_name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"},
                            "model_kwargs": {"type": "object", "maxProperties": 20, "additionalProperties": {"type": "number"}},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl.predict_actions",
                    description=(
                        "Load a previously trained FinRL policy artifact and let the policy produce deterministic "
                        "actions in the real vendored StockTradingEnv. Results remain simulation-only."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("finrl-policy-inference",),
                    packages=("external/FinRL", "Stable-Baselines3", "torch"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["artifact_name", "prices"],
                        "properties": {
                            **_MODEL_INPUT_PROPERTIES,
                            "artifact_name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"},
                            "algorithm": {"type": "string", "enum": ["a2c", "ddpg", "ppo", "sac", "td3"]},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.qlib.train_factor_model",
                    description=(
                        "Execute qlib.cli.run.workflow using a locked project workflow YAML, train its configured "
                        "Dataset/Model, and persist the real Recorder/MLflow artifacts under .runtime."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("qlib-model-training",),
                    packages=("external/qlib", "pyqlib", "MLflow"),
                    input_schema={
                        "type": "object", "additionalProperties": False, "required": ["config_path"],
                        "properties": {
                            "config_path": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "experiment_name": {"type": "string", "maxLength": 200},
                            "provider_uri": {"type": "string", "maxLength": 2000},
                            "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.qlib.backtest_model",
                    description=(
                        "Execute a complete Qlib workflow configuration containing Signal/Portfolio analysis records; "
                        "returns only real Recorder artifacts and rejects configs without a backtest stage."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("qlib-model-backtest",),
                    packages=("external/qlib", "pyqlib", "MLflow"),
                    input_schema={
                        "type": "object", "additionalProperties": False, "required": ["config_path"],
                        "properties": {
                            "config_path": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "experiment_name": {"type": "string", "maxLength": 200},
                            "provider_uri": {"type": "string", "maxLength": 2000},
                            "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.qlib.reference_risk_metrics",
                    description=(
                        "Use Qlib's upstream product-return risk calculator as an isolated, read-only "
                        "reference for a supplied finite return path. This creates a differential-validation "
                        "receipt; it never places an order or changes a model artifact."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("qlib-reference-risk-metrics",),
                    packages=("external/qlib", "pyqlib"),
                    input_schema={
                        "type": "object", "additionalProperties": False, "required": ["returns"],
                        "properties": {
                            "returns": {
                                "type": "array", "minItems": 2, "maxItems": 10000,
                                "items": {"type": "number", "minimum": -0.999999999},
                            },
                            "periods_per_year": {"type": "integer", "minimum": 1, "maximum": 1000},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrobot.generate_financial_report",
                    description=(
                        "Run FinRobot's real AutoGen SingleAssistant financial-report workflow with the authenticated "
                        "Codex App Server through a run-scoped Host bridge. No external model API key is required and "
                        "code execution remains disabled."
                    ),
                    category="external_runtime",
                    mutating=True,
                    requires_external_execution=True,
                    skills=("finrobot-autogen-report",),
                    packages=("external/FinRobot", "AutoGen", "Codex App Server"),
                    input_schema={
                        "type": "object", "additionalProperties": False, "required": ["prompt"],
                        "properties": {
                            "prompt": {"type": "string", "minLength": 1, "maxLength": 50000},
                            "agent_config": {"type": "string", "enum": ["Expert_Investor", "Financial_Analyst", "Market_Analyst"]},
                            "model": {"type": "string", "maxLength": 200},
                            "base_url": {"type": "string", "maxLength": 1000},
                            "api_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{2,80}$"},
                            "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                            "max_turns": {"type": "integer", "minimum": 1, "maximum": 20},
                            "llm_timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 600},
                            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.workflow.analyze_verified_pack",
                    description=(
                        "Run one high-level isolated workflow over already verified observations using the real "
                        "TradingAgents rating parser, FinGPT sentiment consensus and AI-Trader signal scorer. "
                        "This does not claim unavailable foundation models were loaded."
                    ),
                    category="external_runtime",
                    skills=("multi-framework-market-workflow",),
                    packages=("external/TradingAgents", "external/FinGPT", "external/AI-Trader"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol", "analysis", "sentiment_signals"],
                        "properties": {
                            "symbol": {"type": "string", "minLength": 1, "maxLength": 40},
                            "analysis": {"type": "string", "minLength": 1, "maxLength": 50000},
                            "sentiment_signals": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["source", "average_sentiment_score", "total_mentions"],
                                    "properties": {
                                        "source": {"type": "string"},
                                        "available": {"type": "boolean"},
                                        "average_sentiment_score": {"type": "number", "minimum": -1, "maximum": 1},
                                        "total_mentions": {"type": "integer", "minimum": 0},
                                    },
                                },
                            },
                            "market": {"type": "string", "maxLength": 40},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.tradingagents.model_capabilities",
                    description="Execute TradingAgents' real model-capability resolver before selecting structured tool output.",
                    category="external_runtime",
                    skills=("tradingagents-runtime",),
                    packages=("external/TradingAgents",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["model"],
                        "properties": {"model": {"type": "string", "minLength": 1}},
                    },
                ),
                AgentToolSpec(
                    name="external.tradingagents.investment_decision",
                    description=(
                        "Run TradingAgents' portfolio-manager five-tier decision parser on an analysis produced "
                        "after reading market.research_pack; returns Buy, Overweight, Hold, Underweight, or Sell."
                    ),
                    category="external_runtime",
                    skills=("tradingagents-portfolio-manager",),
                    packages=("external/TradingAgents",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["analysis"],
                        "properties": {
                            "analysis": {"type": "string", "minLength": 1, "maxLength": 50000},
                            "default": {
                                "type": "string",
                                "enum": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
                            },
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.fingpt.sentiment_consensus",
                    description="Execute FinGPT's real multi-source market sentiment aggregation.",
                    category="external_runtime",
                    skills=("fingpt-sentiment",),
                    packages=("external/FinGPT", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["signals"],
                        "properties": {
                            "signals": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["source", "average_sentiment_score", "total_mentions"],
                                    "properties": {
                                        "source": {"type": "string"},
                                        "available": {"type": "boolean"},
                                        "average_sentiment_score": {"type": "number", "minimum": -1, "maximum": 1},
                                        "total_mentions": {"type": "integer", "minimum": 0},
                                    },
                                },
                            }
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrobot.report_quality",
                    description="Execute FinRobot's real report text-length quality check.",
                    category="external_runtime",
                    skills=("finrobot-reporting",),
                    packages=("external/FinRobot",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "min_words": {"type": "integer", "minimum": 0, "maximum": 100000},
                            "max_words": {"type": "integer", "minimum": 1, "maximum": 100000},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrobot.market_data",
                    description=(
                        "Run FinRobot's YFinanceUtils data-source implementation for a symbol and return "
                        "the fetched OHLCV rows plus a compact market summary."
                    ),
                    category="external_runtime",
                    skills=("finrobot-market-data",),
                    packages=("external/FinRobot", "yfinance", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol", "start_date", "end_date"],
                        "properties": {
                            "symbol": {"type": "string", "minLength": 1, "maxLength": 40},
                            "start_date": {"type": "string", "format": "date"},
                            "end_date": {"type": "string", "format": "date"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 250},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl.walk_forward_windows",
                    description="Execute FinRL's real rolling train/trade window generator.",
                    category="external_runtime",
                    skills=("finrl-walk-forward",),
                    packages=("external/FinRL", "numpy", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["train_dates", "trade_dates", "rolling_window_length"],
                        "properties": {
                            "train_dates": {"type": "array", "minItems": 2, "maxItems": 2000, "items": {"type": "string"}},
                            "trade_dates": {"type": "array", "minItems": 1, "maxItems": 2000, "items": {"type": "string"}},
                            "rolling_window_length": {"type": "integer", "minimum": 1, "maximum": 1000},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl.simulate_environment",
                    description=(
                        "Run FinRL's real StockTradingEnv reset/step portfolio-accounting loop on verified "
                        "price arrays and explicit actions; this is simulation only and cannot place orders."
                    ),
                    category="external_runtime",
                    skills=("finrl-environment-simulation",),
                    packages=("external/FinRL", "gymnasium", "numpy"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["prices", "actions"],
                        "properties": {
                            "prices": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 1000,
                                "items": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 20,
                                    "items": {"type": "number", "exclusiveMinimum": 0},
                                },
                            },
                            "actions": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 999,
                                "items": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 20,
                                    "items": {"type": "number", "minimum": -1, "maximum": 1},
                                },
                            },
                            "technical_features": {
                                "type": "array",
                                "maxItems": 1000,
                                "items": {
                                    "type": "array",
                                    "maxItems": 200,
                                    "items": {"type": "number"},
                                },
                            },
                            "turbulence": {
                                "type": "array",
                                "maxItems": 1000,
                                "items": {"type": "number"},
                            },
                            "initial_capital": {"type": "number", "exclusiveMinimum": 0},
                            "max_stock": {"type": "number", "exclusiveMinimum": 0},
                            "buy_cost_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                            "sell_cost_pct": {"type": "number", "minimum": 0, "maximum": 0.1},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl_trading.information_ratio",
                    description="Execute FinRL-Trading's real robust information-ratio implementation.",
                    category="external_runtime",
                    skills=("finrl-trading-risk",),
                    packages=("external/FinRL-Trading", "numpy", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["returns", "benchmark_returns"],
                        "properties": {
                            "returns": _NUMBER_ARRAY,
                            "benchmark_returns": _NUMBER_ARRAY,
                            "lookback": {"type": "integer", "minimum": 2, "maximum": 2000},
                            "robust": {"type": "boolean"},
                            "annualization_factor": {"type": "number", "exclusiveMinimum": 0},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.finrl_trading.regime_signals",
                    description=(
                        "Run FinRL-Trading's adaptive-rotation slow-regime signal engine over point-in-time "
                        "SPX and VIX weekly history."
                    ),
                    category="external_runtime",
                    skills=("finrl-trading-regime",),
                    packages=("external/FinRL-Trading", "numpy", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["dates", "spx_prices", "vix_prices"],
                        "properties": {
                            "dates": {"type": "array", "minItems": 2, "maxItems": 520, "items": {"type": "string"}},
                            "spx_prices": _NUMBER_ARRAY,
                            "vix_prices": _NUMBER_ARRAY,
                            "trend_ma_weeks": {"type": "integer", "minimum": 2, "maximum": 52},
                            "drawdown_weeks": {"type": "integer", "minimum": 2, "maximum": 52},
                            "drawdown_threshold": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                            "vix_lookback_years": {"type": "integer", "minimum": 1, "maximum": 10},
                            "vix_z_threshold": {"type": "number", "minimum": 0, "maximum": 20},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.qlib.align_signals",
                    description="Execute Qlib's real indexed signal alignment and addition data structure.",
                    category="external_runtime",
                    skills=("qlib-signal-alignment",),
                    packages=("external/qlib", "numpy", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["left", "right"],
                        "properties": {
                            "left": {"type": "object", "additionalProperties": {"type": "number"}},
                            "right": {"type": "object", "additionalProperties": {"type": "number"}},
                            "fill_value": {"type": "number"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.qlib.factor_dataset",
                    description=(
                        "Run Qlib's SepDataFrame feature/label dataset alignment and calculate the aligned "
                        "factor information coefficient."
                    ),
                    category="external_runtime",
                    skills=("qlib-factor-dataset",),
                    packages=("external/qlib", "pandas"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["features", "labels"],
                        "properties": {
                            "features": {"type": "object", "minProperties": 2, "additionalProperties": {"type": "number"}},
                            "labels": {"type": "object", "minProperties": 1, "additionalProperties": {"type": "number"}},
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.ai_trader.variant_metrics",
                    description="Execute AI-Trader's real deterministic experiment variant summary and confidence interval.",
                    category="external_runtime",
                    skills=("ai-trader-research",),
                    packages=("external/AI-Trader",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["rows", "metric"],
                        "properties": {
                            "metric": {"type": "string", "minLength": 1},
                            "rows": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 2000,
                                "items": {
                                    "type": "object",
                                    "required": ["variant_key"],
                                    "properties": {"variant_key": {"type": "string"}},
                                },
                            },
                        },
                    },
                ),
                AgentToolSpec(
                    name="external.ai_trader.score_signal",
                    description=(
                        "Run AI-Trader's real prediction extraction and signal-quality scorer in an isolated "
                        "temporary SQLite workspace; it does not touch the host paper account."
                    ),
                    category="external_runtime",
                    skills=("ai-trader-signal-quality",),
                    packages=("external/AI-Trader", "SQLite"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol", "content"],
                        "properties": {
                            "symbol": {"type": "string", "minLength": 1, "maxLength": 40},
                            "content": {"type": "string", "minLength": 1, "maxLength": 50000},
                            "title": {"type": "string", "maxLength": 1000},
                            "market": {"type": "string", "maxLength": 40},
                            "tags": {"type": "array", "maxItems": 30, "items": {"type": "string"}},
                        },
                    },
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        result = [spec.to_dict() for spec in self._specs.values()]
        enabled = self._fingpt_local_model_enabled()
        for item in result:
            if item["name"] == "external.fingpt.run_sentiment_model":
                item["available"] = enabled
                item["availability_reason"] = (
                    "explicitly_enabled" if enabled else "local_fingpt_disabled_codex_is_primary"
                )
        return result

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        projects = {
            "TradingAgents": {
                "source_present": (self.project_root / "external" / "TradingAgents").is_dir(),
                "runtime_ready": True,
                "full_workflow_ready": False,
                "model_loaded": False,
                "inference_executed": False,
                "available_capabilities": ["model_capabilities", "investment_decision_parser", "analyze_symbol"],
                "full_workflow_tool": "external.tradingagents.analyze_symbol",
                "workflow_runtime_probe_required": True,
            },
            "FinGPT": {
                "source_present": (self.project_root / "external" / "FinGPT").is_dir(),
                "runtime_ready": True,
                "full_workflow_ready": False,
                "model_loaded": False,
                "inference_executed": False,
                "available_capabilities": ["sentiment_consensus", "run_sentiment_model"],
                "full_workflow_tool": "external.fingpt.run_sentiment_model",
                "workflow_runtime_probe_required": True,
            },
            "FinRL": {
                "source_present": (self.project_root / "external" / "FinRL").is_dir(),
                "runtime_ready": True,
                "environment_ready": True,
                "policy_loaded": False,
                "training_ready": False,
                "inference_executed": False,
                "available_capabilities": [
                    "walk_forward_windows", "simulate_environment_with_explicit_actions",
                    "train_policy", "predict_actions",
                ],
                "full_workflow_tools": ["external.finrl.train_policy", "external.finrl.predict_actions"],
                "workflow_runtime_probe_required": True,
            },
            "Qlib": {
                "source_present": (self.project_root / "external" / "qlib").is_dir(),
                "runtime_ready": True,
                "full_experiment_ready": False,
                "model_loaded": False,
                "inference_executed": False,
                "available_capabilities": ["align_signals", "factor_dataset_ic", "train_factor_model", "backtest_model"],
                "full_workflow_tools": ["external.qlib.train_factor_model", "external.qlib.backtest_model"],
                "workflow_runtime_probe_required": True,
            },
            "FinRobot": {
                "source_present": (self.project_root / "external" / "FinRobot").is_dir(),
                "runtime_ready": True,
                "full_agent_report_ready": False,
                "model_loaded": False,
                "inference_executed": False,
                "available_capabilities": ["market_data", "report_quality", "generate_financial_report"],
                "full_workflow_tool": "external.finrobot.generate_financial_report",
                "workflow_runtime_probe_required": True,
            },
            "AI-Trader": {
                "source_present": (self.project_root / "external" / "AI-Trader").is_dir(),
                "runtime_ready": True,
                "full_agent_network_ready": False,
                "model_loaded": False,
                "inference_executed": False,
                "available_capabilities": ["variant_metrics", "score_signal"],
            },
        }
        return {
            "configured": any(item["source_present"] for item in projects.values()),
            "runtime_ready": True,
            "health": "partial",
            "projects": projects,
            "truth_model": "capability_level_not_project_name",
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown external project tool: {name}")
        if spec.requires_external_execution and not context.allow_external_actions:
            raise PermissionError(f"{name} requires autonomy=external_execute or full_execute")
        if name == "external.runtime.health":
            return await self._runtime_health()
        if name in _FULL_WORKFLOW_ACTIONS:
            project, action = _FULL_WORKFLOW_ACTIONS[name]
            if project == "fingpt" and not self._fingpt_local_model_enabled():
                raise PermissionError(
                    "Local FinGPT model execution is disabled; Codex is the configured primary model driver."
                )
            return await self._run_workflow_worker(project, action, arguments, context)
        if name == "external.workflow.analyze_verified_pack":
            return await self._run_verified_pack_workflow(arguments)
        project, action = name.split(".", 2)[1:]
        return await self._run_worker(project, action, arguments)

    async def _runtime_health(self) -> dict[str, Any]:
        base = self.describe()
        probes = await asyncio.gather(
            *(self._probe_workflow_runtime(project) for project in _PROJECT_DISPLAY_NAMES),
        )
        for project, probe in zip(_PROJECT_DISPLAY_NAMES, probes):
            display_name = _PROJECT_DISPLAY_NAMES[project]
            current = base["projects"][display_name]
            health = probe.get("health") if isinstance(probe, dict) else None
            if not isinstance(health, dict):
                current.update(
                    {
                        "workflow_worker_python": None,
                        "workflow_dependencies_ready": False,
                        "workflow_missing_dependencies": [],
                        "workflow_configuration": {},
                        "workflow_configuration_ready": False,
                        "workflow_runtime_ready": False,
                        "full_workflow_ready": False,
                        "full_workflow_executed": False,
                        "last_workflow_execution": None,
                        "inference_executed": False,
                        "training_executed": False,
                        "model_loaded": False,
                        "policy_loaded": False,
                        "workflow_probe_error": probe.get("error") if isinstance(probe, dict) else "probe_failed",
                    }
                )
                continue
            last = self._last_workflow_status(project)
            configured = health.get("configured") if isinstance(health.get("configured"), dict) else {}
            configuration_ready = True
            if project in {"tradingagents", "finrobot"}:
                configured = {
                    "llm_driver": "codex_app_server",
                    "authentication": "existing_chatgpt_codex_account",
                    "external_api_key_required": False,
                    "account_checked_at_execution": True,
                }
                configuration_ready = True
            elif project == "fingpt":
                local_enabled = self._fingpt_local_model_enabled()
                configured = {
                    "primary_model_driver": "codex_app_server",
                    "local_model_enabled": local_enabled,
                    "local_model_assets_requested": False,
                    "local_model_source_preserved": True,
                }
                configuration_ready = local_enabled
            current.update(
                {
                    "workflow_worker_python": health.get("worker_python"),
                    "workflow_dependencies_ready": bool(health.get("dependencies_ready")),
                    "workflow_missing_dependencies": list(health.get("missing_dependencies") or []),
                    "workflow_configuration": configured,
                    "workflow_configuration_ready": configuration_ready,
                    "workflow_runtime_ready": bool(health.get("dependencies_ready")) and configuration_ready,
                    "full_workflow_ready": bool(health.get("dependencies_ready")) and configuration_ready,
                    "full_workflow_executed": bool(last),
                    "last_workflow_execution": last,
                    "inference_executed": bool(last and last.get("inference_executed")),
                    "training_executed": bool(last and last.get("training_executed")),
                    "model_loaded": bool(last and last.get("model_loaded")),
                    "policy_loaded": bool(last and last.get("policy_loaded")),
                }
            )
            if project == "finrl":
                current["training_ready"] = bool(health.get("dependencies_ready"))
                current["policy_artifact_ready"] = bool(configured.get("policy_artifact_count"))
            elif project == "qlib":
                current["full_experiment_ready"] = bool(health.get("dependencies_ready")) and configuration_ready
            elif project == "finrobot":
                current["full_agent_report_ready"] = bool(health.get("dependencies_ready")) and configuration_ready
            elif project == "fingpt" and not configuration_ready:
                current.update(
                    {
                        "full_workflow_ready": False,
                        "model_loaded": False,
                        "inference_executed": False,
                        "execution_policy": "disabled_codex_is_primary",
                    }
                )
        return {
            "schema_version": "open_stock_ai.external_runtime_health.v2",
            **base,
            "source_lock": str(self.project_root / "config" / "external_sources.lock.yaml"),
            # The macOS launcher can place durable runtime state outside a
            # Desktop-protected project folder.  Keep the public health
            # contract stable and avoid leaking a machine-specific Library
            # path, even when .runtime is a symlink to that managed root.
            "workflow_artifact_root": ".runtime/external-workflows",
            "live_trading": False,
            "fallback_models": False,
        }

    async def _probe_workflow_runtime(self, project: str) -> dict[str, Any]:
        try:
            return await self._invoke_workflow_worker(project, "health", {}, timeout=15)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _fingpt_local_model_enabled(self) -> bool:
        override = os.getenv("STOCK_AI_FINGPT_LOCAL_MODEL_ENABLED")
        if override is not None:
            return override.strip().casefold() in {"1", "true", "yes", "on"}
        path = self.project_root / "config" / "agent_runtime.yaml"
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, yaml.YAMLError):
            payload = {}
        models = payload.get("models") if isinstance(payload, dict) else {}
        return bool(models.get("fingpt_local_model_enabled", False)) if isinstance(models, dict) else False

    async def _run_workflow_worker(
        self,
        project: str,
        action: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        payload = dict(arguments)
        timeout = max(30, min(int(payload.pop("timeout_seconds", 900)), 3600))
        payload["_run_id"] = context.run_id
        bridge_evidence: dict[str, Any] | None = None
        if project in {"tradingagents", "finrobot"}:
            bridge_sink = self._bridge_event_sink(context)
            async with CodexLLMBridge(
                codex_runtime,
                run_id=context.run_id,
                event_sink=bridge_sink,
            ) as bridge:
                environment_overrides: dict[str, str]
                if project == "tradingagents":
                    payload.update(
                        {
                            "llm_provider": "openai_compatible",
                            # LangChain 1.3 routes any wire-level model name
                            # containing "codex" to /responses, even for a
                            # custom OpenAI-compatible endpoint.  This neutral
                            # alias keeps TradingAgents on Chat Completions;
                            # the Host evidence and bridge still identify the
                            # effective model driver as Codex.
                            "deep_model": "stock-ai-agent",
                            "quick_model": "stock-ai-agent",
                            "backend_url": bridge.base_url,
                        }
                    )
                    environment_overrides = {"OPENAI_COMPATIBLE_API_KEY": bridge.token}
                else:
                    payload.update(
                        {
                            "model": "codex",
                            "base_url": bridge.base_url,
                            "api_key_env": "STOCK_AI_CODEX_BRIDGE_TOKEN",
                        }
                    )
                    environment_overrides = {"STOCK_AI_CODEX_BRIDGE_TOKEN": bridge.token}
                worker_result = await self._invoke_workflow_worker(
                    project,
                    action,
                    payload,
                    timeout=timeout,
                    environment_overrides=environment_overrides,
                )
                bridge_evidence = bridge.evidence()
        else:
            worker_result = await self._invoke_workflow_worker(project, action, payload, timeout=timeout)
        if not worker_result.get("ok"):
            missing = worker_result.get("missing_prerequisites") or []
            suffix = f" Missing prerequisites: {', '.join(map(str, missing))}." if missing else ""
            raise RuntimeError(
                f"External full workflow failed ({project}.{action}): {worker_result.get('error') or 'unknown error'}.{suffix}"
            )
        upstream_modules = worker_result.get("upstream_modules")
        if not isinstance(upstream_modules, list) or not upstream_modules:
            raise RuntimeError(f"External full workflow returned no upstream module evidence: {project}.{action}")
        source_lock = self._source_lock(project)
        locked_root = (self.project_root / source_lock["path"]).resolve()
        module_evidence = []
        for relative in upstream_modules:
            module_path = (self.project_root / str(relative)).resolve()
            if not module_path.is_file() or not module_path.is_relative_to(locked_root):
                raise RuntimeError(f"External workflow module is outside its source lock: {module_path}")
            module_evidence.append(
                {
                    "path": str(module_path.relative_to(self.project_root)),
                    "sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
                }
            )
        runtime_module_evidence = []
        # Resolve the launcher-managed *virtualenv directory*, not the Python
        # executable: the executable is often itself a symlink to the base
        # interpreter and resolving it would lose the venv boundary.
        worker_python = Path(str(worker_result.get("worker_python") or "")).expanduser().absolute()
        worker_environment = worker_python.parent.parent.resolve()
        for runtime_module in worker_result.get("runtime_modules") or []:
            runtime_path = Path(str(runtime_module.get("path") or "")).expanduser().resolve()
            if not runtime_path.is_file() or not runtime_path.is_relative_to(worker_environment):
                raise RuntimeError(f"External runtime module escaped its isolated environment: {runtime_path}")
            runtime_sha = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
            if runtime_sha != runtime_module.get("sha256"):
                raise RuntimeError(f"External runtime module hash mismatch: {runtime_path}")
            runtime_module_evidence.append(
                {
                    "module": str(runtime_module.get("module") or ""),
                    "path": str(runtime_path),
                    "size": runtime_path.stat().st_size,
                    "sha256": runtime_sha,
                    "inside_isolated_environment": True,
                }
            )
        runtime_fingerprint = worker_result.get("runtime_fingerprint")
        if not _valid_runtime_fingerprint(runtime_fingerprint):
            raise RuntimeError(f"External workflow returned no valid runtime fingerprint: {project}.{action}")
        artifacts = worker_result.get("artifacts") or []
        for artifact in artifacts:
            path = (self.project_root / str(artifact.get("path") or "")).resolve()
            if not path.is_file() or not path.is_relative_to(self.workflow_root):
                raise RuntimeError(f"External workflow artifact escaped the runtime root: {path}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get("sha256"):
                raise RuntimeError(f"External workflow artifact hash mismatch: {path}")
        model_provenance = dict(worker_result.get("model_provenance") or {})
        if bridge_evidence is not None:
            model_provenance.update(
                {
                    "llm_driver": "codex_app_server",
                    "effective_model": "codex",
                    "framework_model_alias": (
                        "stock-ai-agent" if project == "tradingagents" else "codex"
                    ),
                    "external_llm_api_key_used": False,
                    "codex_hidden_session_used": True,
                }
            )
        evidence = {
            "schema_version": "open_stock_ai.external_full_workflow.v1",
            "project": project,
            "action": action,
            "execution_mode": "isolated_framework_python",
            "worker_python": worker_result.get("worker_python"),
            "executed_function": worker_result.get("executed_function"),
            "upstream_modules": module_evidence,
            "runtime_modules": runtime_module_evidence,
            "runtime_fingerprint": runtime_fingerprint,
            "source_lock": source_lock,
            "module_under_locked_path": True,
            "fallback_used": False,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "result": worker_result.get("result"),
            "artifacts": artifacts,
            "model_provenance": model_provenance,
            "llm_bridge": bridge_evidence,
            "execution_boundary": "external_compute_and_services_no_live_brokerage",
        }
        self._write_workflow_status(project, action, evidence)
        return evidence

    async def _invoke_workflow_worker(
        self,
        project: str,
        action: str,
        payload: dict[str, Any],
        *,
        timeout: int,
        environment_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        python = _workflow_python(project)
        worker_environment = _workflow_env(self.project_root, self.workflow_root, project, payload)
        safe_override_names = {"OPENAI_COMPATIBLE_API_KEY", "STOCK_AI_CODEX_BRIDGE_TOKEN"}
        for key, value in (environment_overrides or {}).items():
            if key not in safe_override_names:
                raise PermissionError(f"Unsafe external workflow environment override: {key}")
            worker_environment[key] = value
        process = await asyncio.create_subprocess_exec(
            python,
            "-m",
            "stock_ai.external_workflow_worker",
            project,
            action,
            cwd=str(self.project_root),
            env=worker_environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"External workflow timed out: {project}.{action}") from None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-4000:]
            raise RuntimeError(f"External workflow worker crashed ({project}.{action}): {detail}")
        lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"External workflow returned no JSON: {project}.{action}")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"External workflow returned invalid JSON: {project}.{action}") from exc

    @staticmethod
    def _bridge_event_sink(context: AgentRunContext):
        recorder = context.state.get("record_event")
        if not callable(recorder):
            return None

        async def emit(event: dict[str, Any]) -> None:
            payload = dict(event)
            event_type = str(payload.pop("type", "model.bridge.event"))
            emitted = recorder(event_type, **payload)
            if asyncio.iscoroutine(emitted):
                await emitted

        return emit

    def _write_workflow_status(self, project: str, action: str, evidence: dict[str, Any]) -> None:
        status_root = self.workflow_root / "status"
        status_root.mkdir(parents=True, exist_ok=True)
        provenance = evidence.get("model_provenance") or {}
        path = status_root / f"{project}.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        execution = {
            "completed_at": time.time(),
            "executed_function": evidence.get("executed_function"),
            "source_lock_head": (evidence.get("source_lock") or {}).get("head"),
            "artifact_count": len(evidence.get("artifacts") or []),
        }
        executions = dict(existing.get("executions") or {})
        executions[action] = execution
        inference_executed = bool(
            provenance.get("inference_executed")
            or provenance.get("policy_inference_executed")
            or provenance.get("autogen_workflow_executed")
            or provenance.get("full_graph_executed")
            or provenance.get("qlib_workflow_executed")
        )
        payload = {
            "project": project,
            "action": action,
            "completed_at": execution["completed_at"],
            "executed_function": evidence.get("executed_function"),
            "model_loaded": bool(existing.get("model_loaded") or provenance.get("model_loaded")),
            "inference_executed": bool(existing.get("inference_executed") or inference_executed),
            "training_executed": bool(existing.get("training_executed") or provenance.get("training_executed")),
            "policy_loaded": bool(existing.get("policy_loaded") or provenance.get("policy_loaded")),
            "source_lock_head": (evidence.get("source_lock") or {}).get("head"),
            "executions": executions,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def _last_workflow_status(self, project: str) -> dict[str, Any] | None:
        path = self.workflow_root / "status" / f"{project}.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _run_verified_pack_workflow(self, arguments: dict[str, Any]) -> dict[str, Any]:
        analysis = str(arguments["analysis"])
        symbol = str(arguments["symbol"])
        steps: list[dict[str, Any]] = []
        calls = (
            ("tradingagents", "investment_decision", {"analysis": analysis, "default": "Hold"}),
            ("fingpt", "sentiment_consensus", {"signals": arguments["sentiment_signals"]}),
            (
                "ai_trader",
                "score_signal",
                {
                    "symbol": symbol,
                    "market": str(arguments.get("market") or "stock"),
                    "title": "Verified multi-framework analysis",
                    "content": analysis,
                    "tags": ["verified-pack", "isolated-runtime"],
                },
            ),
        )
        for project, action, payload in calls:
            result = await self._run_worker(project, action, payload)
            steps.append(result)
        return {
            "schema_version": "open_stock_ai.external_verified_pack_workflow.v1",
            "symbol": symbol,
            "workflow_completed": True,
            "step_count": len(steps),
            "steps": steps,
            "outputs": {
                "tradingagents_rating": ((steps[0].get("result") or {}).get("rating")),
                "fingpt_sentiment": steps[1].get("result"),
                "ai_trader_signal_quality": steps[2].get("result"),
            },
            "model_health": {
                "tradingagents_full_graph_executed": False,
                "fingpt_foundation_model_loaded": False,
                "finrl_policy_inference_executed": False,
                "qlib_model_experiment_executed": False,
                "available_vendored_functions_executed": True,
            },
            "fallback_used": False,
            "execution_boundary": "read_only_high_level_workflow_no_live_trading",
        }

    async def _run_worker(self, project: str, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started_at = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "stock_ai.external_runtime_worker",
            project,
            action,
            cwd=str(self.project_root),
            env=_core_worker_env(self.project_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(arguments, ensure_ascii=False).encode("utf-8")),
                timeout=30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"External project tool timed out: {project}.{action}") from None
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-4000:]
            raise RuntimeError(f"External project tool failed ({project}.{action}): {detail}")
        output_lines = [line for line in stdout.decode("utf-8").splitlines() if line.strip()]
        if not output_lines:
            raise RuntimeError(f"External project tool returned no JSON: {project}.{action}")
        try:
            result = json.loads(output_lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"External project tool returned invalid JSON: {project}.{action}") from exc
        module_path = self.project_root / str(result["executed_module"])
        if not module_path.is_file():
            raise RuntimeError(f"External project evidence module is missing: {module_path}")
        source_lock = self._source_lock(project)
        locked_root = (self.project_root / source_lock["path"]).resolve()
        if not module_path.resolve().is_relative_to(locked_root):
            raise RuntimeError(f"External module is outside its source-lock path: {module_path}")
        return {
            "schema_version": "open_stock_ai.external_execution.v1",
            "project": project,
            "action": action,
            "execution_mode": "isolated_subprocess",
            "external_module_loaded": True,
            "executed_module": result["executed_module"],
            "executed_function": result["executed_function"],
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
            "source_lock": source_lock,
            "module_under_locked_path": True,
            "fallback_used": False,
            "result": result["result"],
            "execution_boundary": "read_only_external_project_runtime_no_live_trading",
        }

    def _source_lock(self, project: str) -> dict[str, str]:
        lock_path = self.project_root / "config" / "external_sources.lock.yaml"
        payload = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
        entry = (payload.get("projects") or {}).get(project)
        if not isinstance(entry, dict):
            raise RuntimeError(f"External project is missing from source lock: {project}")
        required = ("name", "path", "origin", "branch", "head")
        if any(not entry.get(key) for key in required):
            raise RuntimeError(f"External source-lock entry is incomplete: {project}")
        return {key: str(entry[key]) for key in required}


def _valid_runtime_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    package_lock = value.get("package_lock")
    digest = str(value.get("package_lock_sha256") or "")
    if not isinstance(package_lock, list) or len(digest) != 64:
        return False
    encoded = json.dumps(package_lock, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest() == digest and all(
        isinstance(item, dict) and str(item.get("name") or "") and str(item.get("version") or "")
        for item in package_lock
    )


def _pythonpath(project_root: Path) -> str:
    values = [str(project_root / "src")]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        values.append(existing)
    return os.pathsep.join(values)


def _workflow_python(project: str) -> str:
    env_names = {
        "tradingagents": "STOCK_AI_TRADINGAGENTS_PYTHON",
        "fingpt": "STOCK_AI_FINGPT_PYTHON",
        "finrl": "STOCK_AI_FINRL_PYTHON",
        "qlib": "STOCK_AI_QLIB_PYTHON",
        "finrobot": "STOCK_AI_FINROBOT_PYTHON",
    }
    configured = str(os.getenv(env_names[project]) or "").strip()
    if not configured:
        binding_path = (
            Path(os.getenv("STOCK_AI_PROJECT_ROOT") or Path(__file__).resolve().parents[2])
            / ".runtime" / "external-workflows" / "runtime-bindings.json"
        )
        try:
            bindings = json.loads(binding_path.read_text(encoding="utf-8")) if binding_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            bindings = {}
        configured = str(bindings.get(env_names[project]) or sys.executable).strip()
    resolved = shutil.which(configured) if os.sep not in configured else configured
    if not resolved or not Path(resolved).is_file():
        raise RuntimeError(f"Configured {project} workflow Python does not exist: {configured}")
    # Preserve virtual-environment launcher symlinks. Resolving them to the base
    # interpreter silently drops the venv site-packages.
    return str(Path(resolved).expanduser().absolute())


def _core_worker_env(project_root: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": _pythonpath(project_root),
        "STOCK_AI_PROJECT_ROOT": str(project_root),
        "PYTHONUNBUFFERED": "1",
    }
    for key in (
        "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _workflow_env(
    project_root: Path,
    workflow_root: Path,
    project: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    home = workflow_root / "home" / project
    temp = workflow_root / "tmp" / project
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    process_temp = temp
    if project == "qlib":
        # Python's multiprocessing resource_tracker encodes its temp resource
        # names as ASCII.  A perfectly valid Unicode project path would make
        # joblib fail before Qlib can load data, so keep only disposable
        # memmap files in an ASCII-only, per-project private temp directory.
        project_key = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:16]
        process_temp = Path("/tmp") / "open-stock-ai-external" / project_key / project
        process_temp.mkdir(parents=True, exist_ok=True)
        process_temp.chmod(0o700)
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": _pythonpath(project_root),
        "STOCK_AI_PROJECT_ROOT": str(project_root),
        "STOCK_AI_EXTERNAL_WORKFLOW_ROOT": str(workflow_root),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(process_temp),
        "PYTHONUNBUFFERED": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    if project == "qlib":
        # Qlib intentionally writes its MLflow Recorder into this run's
        # isolated .runtime artifact directory. MLflow 3.14+ requires an
        # explicit opt-in for that local file-store backend.
        env["MLFLOW_ALLOW_FILE_STORE"] = "true"
        env["JOBLIB_TEMP_FOLDER"] = str(process_temp)
    safe_transport = {
        "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    }
    allowed_exact = {
        "tradingagents": {
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "FRED_API_KEY",
            "ALPHA_VANTAGE_API_KEY", "FINNHUB_API_KEY",
        },
        "fingpt": {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"},
        "finrl": set(),
        "qlib": set(),
        "finrobot": {"FINROBOT_API_KEY", "FMP_API_KEY", "FINNHUB_API_KEY"},
    }[project]
    allowed_prefixes = {
        "tradingagents": ("TRADINGAGENTS_",),
        "fingpt": ("STOCK_AI_FINGPT_", "HF_"),
        "finrl": ("STOCK_AI_FINRL_",),
        "qlib": ("STOCK_AI_QLIB_", "QLIB_"),
        "finrobot": ("STOCK_AI_FINROBOT_", "FINROBOT_"),
    }[project]
    dynamic_key = str(payload.get("api_key_env") or "")
    if dynamic_key and dynamic_key.isupper() and dynamic_key.replace("_", "").isalnum():
        allowed_exact.add(dynamic_key)
    for key, value in os.environ.items():
        if key in safe_transport or key in allowed_exact or key.startswith(allowed_prefixes):
            env[key] = value
    return env
