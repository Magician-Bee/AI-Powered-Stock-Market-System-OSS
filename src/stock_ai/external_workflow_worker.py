from __future__ import annotations

"""Isolated entry points for complete vendored finance workflows.

This module intentionally contains no fallback models or local heuristics.  An
action either imports and executes the upstream framework, or fails with a
machine-readable prerequisite error.  The host provider validates every
reported upstream module against ``external_sources.lock.yaml``.
"""

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
from pathlib import Path
import random
import re
import sys
from types import ModuleType
from typing import Any


ROOT = Path(os.environ.get("STOCK_AI_PROJECT_ROOT") or Path(__file__).resolve().parents[2]).resolve()
RUNTIME_ROOT = Path(
    os.environ.get("STOCK_AI_EXTERNAL_WORKFLOW_ROOT")
    or ROOT / ".runtime" / "external-workflows"
).resolve()

_PROJECT_ROOTS = {
    "tradingagents": ROOT / "external" / "TradingAgents",
    "fingpt": ROOT / "external" / "FinGPT",
    "finrl": ROOT / "external" / "FinRL",
    "qlib": ROOT / "external" / "qlib",
    "finrobot": ROOT / "external" / "FinRobot",
}

_REQUIRED_MODULES = {
    "tradingagents": ("langgraph", "langchain_core", "yfinance"),
    "fingpt": ("torch", "transformers", "peft", "structlog"),
    "finrl": ("torch", "stable_baselines3", "gymnasium", "numpy", "pandas"),
    "qlib": ("fire", "jinja2", "ruamel.yaml", "mlflow"),
    "finrobot": ("autogen", "openai"),
}


class PrerequisiteError(RuntimeError):
    def __init__(self, project: str, missing: list[str], detail: str) -> None:
        super().__init__(detail)
        self.project = project
        self.missing = missing
        self.detail = detail


def _prepend_project(project: str) -> Path:
    root = _PROJECT_ROOTS[project]
    if not root.is_dir():
        raise PrerequisiteError(project, [str(root)], f"Vendored source is missing: {root}")
    value = str(root)
    if value not in sys.path:
        sys.path.insert(0, value)
    return root


def _missing_modules(project: str) -> list[str]:
    # Qlib is built as a wheel because its rolling/expanding operators are
    # Cython extensions.  Prepending the source snapshot here would shadow the
    # compiled wheel and make the real workflow import an incomplete package.
    # The host separately verifies both the locked source and the runtime
    # module hashes, so Qlib should load from its isolated venv.
    if project == "qlib":
        root = _PROJECT_ROOTS[project]
        if not root.is_dir():
            raise PrerequisiteError(project, [str(root)], f"Vendored source is missing: {root}")
    else:
        _prepend_project(project)
    missing = []
    for name in _REQUIRED_MODULES[project]:
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, AttributeError):
            available = False
        if not available:
            missing.append(name)
    return missing


def _require_runtime(project: str) -> None:
    missing = _missing_modules(project)
    if missing:
        raise PrerequisiteError(
            project,
            missing,
            f"{project} full workflow dependencies are missing: {', '.join(missing)}",
        )


def _package_shell(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    module = ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    module.__package__ = name
    sys.modules[name] = module
    return module


def _prepare_finrl_imports() -> None:
    root = _PROJECT_ROOTS["finrl"] / "finrl"
    _package_shell("finrl", root)
    # The upstream Stable-Baselines module imports data_split for its separate
    # DataFrame workflow. Array-based policy training below does not use it, so
    # avoid importing FinRL's unrelated downloader/stockstats dependency tree.
    preprocessors = ModuleType("finrl.meta.preprocessor.preprocessors")
    preprocessors.data_split = lambda frame, start, end, target_date_col="date": frame  # type: ignore[attr-defined]
    sys.modules.setdefault("finrl.meta.preprocessor.preprocessors", preprocessors)


def _run_id(payload: dict[str, Any]) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload.get("_run_id") or "manual"))
    return value[:100] or "manual"


def _artifact_dir(project: str, payload: dict[str, Any]) -> Path:
    path = (RUNTIME_ROOT / "runs" / _run_id(payload) / project).resolve()
    if not path.is_relative_to(RUNTIME_ROOT):
        raise PermissionError("External workflow artifact path escaped runtime root")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _source_module(project: str, relative: str) -> str:
    path = (_PROJECT_ROOTS[project] / relative).resolve()
    if not path.is_file() or not path.is_relative_to(_PROJECT_ROOTS[project].resolve()):
        raise FileNotFoundError(path)
    return str(path.relative_to(ROOT))


def _health(project: str) -> dict[str, Any]:
    source_present = _PROJECT_ROOTS[project].is_dir()
    missing = _missing_modules(project) if source_present else [str(_PROJECT_ROOTS[project])]
    configured: dict[str, Any] = {}
    if project == "tradingagents":
        provider = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai").strip().lower()
        credential_names = {
            "openai": ("OPENAI_API_KEY",),
            "anthropic": ("ANTHROPIC_API_KEY",),
            "google": ("GOOGLE_API_KEY",),
        }.get(provider, ())
        backend = os.getenv("TRADINGAGENTS_LLM_BACKEND_URL")
        configured = {
            "llm_provider": provider,
            "backend_url_configured": bool(backend),
            "credential_configured": bool(backend) or not credential_names or any(os.getenv(k) for k in credential_names),
            "required_credential_names": list(credential_names),
        }
    elif project == "fingpt":
        configured = {
            "base_model": os.getenv("STOCK_AI_FINGPT_BASE_MODEL", "NousResearch/Llama-2-13b-hf"),
            "lora_model": os.getenv("STOCK_AI_FINGPT_LORA_MODEL", "FinGPT/fingpt-sentiment_llama2-13b_lora"),
            "hf_token_configured": bool(os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")),
        }
    elif project == "finrl":
        policies = sorted((RUNTIME_ROOT / "policies" / "finrl").glob("*.zip"))
        configured = {"policy_artifact_count": len(policies)}
    elif project == "qlib":
        configured = {
            "provider_uri": os.getenv("QLIB_PROVIDER_URI") or None,
            "provider_uri_configured": bool(os.getenv("QLIB_PROVIDER_URI")),
        }
    elif project == "finrobot":
        configured = {
            "model": os.getenv("FINROBOT_MODEL") or None,
            "base_url": os.getenv("FINROBOT_BASE_URL") or None,
            "credential_configured": bool(os.getenv("FINROBOT_API_KEY")) or bool(os.getenv("FINROBOT_BASE_URL")),
        }
    return {
        "project": project,
        "source_present": source_present,
        "dependencies_ready": not missing,
        "missing_dependencies": missing,
        "configured": configured,
        "worker_python": sys.executable,
        "full_workflow_ready": source_present and not missing,
    }


def _tradingagents_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    project = "tradingagents"
    _require_runtime(project)
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    artifact_dir = _artifact_dir(project, payload)
    config = deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(artifact_dir / "results"),
            "data_cache_dir": str(artifact_dir / "cache"),
            "memory_log_path": str(artifact_dir / "memory" / "trading_memory.md"),
            "checkpoint_enabled": bool(payload.get("checkpoint_enabled", True)),
        }
    )
    for source_key, target_key in (
        ("llm_provider", "llm_provider"),
        ("deep_model", "deep_think_llm"),
        ("quick_model", "quick_think_llm"),
        ("backend_url", "backend_url"),
        ("max_debate_rounds", "max_debate_rounds"),
        ("max_risk_rounds", "max_risk_discuss_rounds"),
        ("output_language", "output_language"),
    ):
        if payload.get(source_key) not in (None, ""):
            config[target_key] = payload[source_key]
    analysts = tuple(payload.get("selected_analysts") or ("market", "social", "news", "fundamentals"))
    allowed = {"market", "social", "news", "fundamentals"}
    if not analysts or any(item not in allowed for item in analysts):
        raise ValueError(f"selected_analysts must be a non-empty subset of {sorted(allowed)}")
    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config)
    final_state, processed_signal = graph.propagate(
        str(payload["symbol"]),
        str(payload["trade_date"]),
        asset_type=str(payload.get("asset_type") or "stock"),
    )
    report_dir = graph.save_reports(final_state, str(payload["symbol"]), artifact_dir / "reports")
    reports = {
        key: _jsonable(final_state.get(key))
        for key in (
            "market_report",
            "sentiment_report",
            "news_report",
            "fundamentals_report",
            "investment_plan",
            "trader_investment_plan",
            "final_trade_decision",
        )
        if key in final_state
    }
    config_evidence = {
        "llm_provider": config.get("llm_provider"),
        "deep_think_llm": config.get("deep_think_llm"),
        "quick_think_llm": config.get("quick_think_llm"),
        "backend_url": config.get("backend_url"),
        "max_debate_rounds": config.get("max_debate_rounds"),
        "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds"),
        "selected_analysts": list(analysts),
    }
    return {
        "upstream_modules": [
            _source_module(project, "tradingagents/graph/trading_graph.py"),
            _source_module(project, "tradingagents/default_config.py"),
        ],
        "executed_function": "TradingAgentsGraph.propagate",
        "result": {
            "symbol": str(payload["symbol"]),
            "trade_date": str(payload["trade_date"]),
            "processed_signal": _jsonable(processed_signal),
            "reports": reports,
            "report_directory": _relative_runtime(report_dir),
        },
        "artifacts": _collect_artifacts(artifact_dir),
        "model_provenance": {
            **config_evidence,
            "config_sha256": _sha256_json(config_evidence),
            "full_graph_executed": True,
        },
    }


def _fingpt_sentiment(payload: dict[str, Any]) -> dict[str, Any]:
    project = "fingpt"
    _require_runtime(project)
    from finogrid.fingpt_integration.sentiment.crypto_sentiment import FinoGridSentimentAnalyzer

    base_model = str(payload.get("base_model") or os.getenv("STOCK_AI_FINGPT_BASE_MODEL") or "NousResearch/Llama-2-13b-hf")
    lora_model = str(payload.get("lora_model") or os.getenv("STOCK_AI_FINGPT_LORA_MODEL") or "FinGPT/fingpt-sentiment_llama2-13b_lora")
    analyzer = FinoGridSentimentAnalyzer(
        base_model=base_model,
        lora_model=lora_model,
        device=str(payload.get("device") or os.getenv("STOCK_AI_FINGPT_DEVICE") or "auto"),
    )
    analyzer.load()
    texts = [str(value) for value in payload["texts"]]
    scores = analyzer.score_batch(texts)
    return {
        "upstream_modules": [
            _source_module(project, "finogrid/fingpt_integration/sentiment/crypto_sentiment.py")
        ],
        "executed_function": "FinoGridSentimentAnalyzer.load/score_batch",
        "result": {
            "count": len(scores),
            "scores": scores,
            "average_score": sum(float(item["score"]) for item in scores) / len(scores),
        },
        "artifacts": [],
        "model_provenance": {
            "base_model": base_model,
            "lora_model": lora_model,
            "device": analyzer.device,
            "model_loaded": analyzer._model is not None,
            "inference_executed": True,
        },
    }


def _finrl_arrays(payload: dict[str, Any], *, train: bool):
    _prepare_finrl_imports()
    import numpy as np
    from finrl.meta.env_stock_trading.env_stocktrading_np import StockTradingEnv

    prices = np.asarray(payload["prices"], dtype=np.float32)
    if prices.ndim != 2 or prices.shape[0] < 3 or prices.shape[1] < 1 or not np.all(prices > 0):
        raise ValueError("prices must be a positive two-dimensional array with at least three rows")
    technical = payload.get("technical_features")
    tech = np.asarray(technical, dtype=np.float32) if technical is not None else np.zeros_like(prices)
    if tech.ndim != 2 or tech.shape[0] != prices.shape[0]:
        raise ValueError("technical_features must contain one row per price row")
    turbulence = np.asarray(payload.get("turbulence") or [0.0] * prices.shape[0], dtype=np.float32)
    if turbulence.shape != (prices.shape[0],):
        raise ValueError("turbulence must contain one value per price row")
    return StockTradingEnv(
        {
            "price_array": prices,
            "tech_array": tech,
            "turbulence_array": turbulence,
            "if_train": train,
        },
        initial_capital=float(payload.get("initial_capital") or 1_000_000.0),
        max_stock=float(payload.get("max_stock") or 100.0),
        buy_cost_pct=float(payload.get("buy_cost_pct") or 0.001),
        sell_cost_pct=float(payload.get("sell_cost_pct") or 0.001),
    )


def _finrl_rollout(model: Any, environment: Any) -> dict[str, Any]:
    state, _ = environment.reset(seed=0)
    steps: list[dict[str, Any]] = []
    done = False
    while not done:
        action, _ = model.predict(state, deterministic=True)
        state, reward, done, truncated, _ = environment.step(action)
        steps.append(
            {
                "day": int(environment.day),
                "action": [_finite(value) for value in action.tolist()],
                "reward": _finite(reward),
                "total_asset": _finite(environment.total_asset),
                "cash": _finite(environment.amount),
            }
        )
        done = bool(done or truncated)
    return {
        "steps": steps,
        "steps_executed": len(steps),
        "initial_total_asset": _finite(environment.initial_total_asset),
        "final_total_asset": _finite(environment.total_asset),
        "return_pct": _finite((environment.total_asset / environment.initial_total_asset - 1.0) * 100.0),
    }


def _finrl_train(payload: dict[str, Any]) -> dict[str, Any]:
    project = "finrl"
    _require_runtime(project)
    _prepare_finrl_imports()
    from finrl.agents.stablebaselines3.models import DRLAgent

    algorithm = str(payload.get("algorithm") or "ppo").lower()
    if algorithm not in {"a2c", "ddpg", "ppo", "sac", "td3"}:
        raise ValueError("Unsupported FinRL algorithm")
    train_env = _finrl_arrays(payload, train=True)
    model_kwargs = dict(payload.get("model_kwargs") or {}) or None
    agent = DRLAgent(train_env)
    model = agent.get_model(
        algorithm,
        model_kwargs=model_kwargs,
        verbose=0,
        seed=int(payload.get("seed") or 0),
    )
    timesteps = int(payload.get("total_timesteps") or 5_000)
    model = agent.train_model(model, tb_log_name=f"stock-ai-{_run_id(payload)}", total_timesteps=timesteps)
    policy_root = (RUNTIME_ROOT / "policies" / "finrl").resolve()
    policy_root.mkdir(parents=True, exist_ok=True)
    name = _artifact_name(str(payload.get("artifact_name") or f"{algorithm}-{_run_id(payload)}"))
    model.save(str(policy_root / name))
    policy_path = policy_root / f"{name}.zip"
    evaluation = _finrl_rollout(model, _finrl_arrays(payload, train=False))
    return {
        "upstream_modules": [
            _source_module(project, "finrl/agents/stablebaselines3/models.py"),
            _source_module(project, "finrl/meta/env_stock_trading/env_stocktrading_np.py"),
        ],
        "executed_function": "DRLAgent.get_model/train_model",
        "result": {
            "algorithm": algorithm,
            "total_timesteps": timesteps,
            "policy_artifact": _relative_runtime(policy_path),
            "policy_sha256": _sha256_file(policy_path),
            "evaluation": evaluation,
        },
        "artifacts": [_artifact_entry(policy_path)],
        "model_provenance": {
            "algorithm": algorithm,
            "seed": int(payload.get("seed") or 0),
            "model_kwargs": model_kwargs or {},
            "training_executed": True,
            "policy_inference_executed": True,
        },
    }


def _finrl_predict(payload: dict[str, Any]) -> dict[str, Any]:
    project = "finrl"
    _require_runtime(project)
    _prepare_finrl_imports()
    from finrl.agents.stablebaselines3 import models

    algorithm = str(payload.get("algorithm") or "ppo").lower()
    if algorithm not in models.MODELS:
        raise ValueError("Unsupported FinRL algorithm")
    name = _artifact_name(str(payload["artifact_name"]))
    policy_path = (RUNTIME_ROOT / "policies" / "finrl" / f"{name}.zip").resolve()
    if not policy_path.is_file() or not policy_path.is_relative_to(RUNTIME_ROOT):
        raise FileNotFoundError(f"FinRL policy artifact not found: {name}")
    model = models.MODELS[algorithm].load(str(policy_path))
    rollout = _finrl_rollout(model, _finrl_arrays(payload, train=False))
    return {
        "upstream_modules": [
            _source_module(project, "finrl/agents/stablebaselines3/models.py"),
            _source_module(project, "finrl/meta/env_stock_trading/env_stocktrading_np.py"),
        ],
        "executed_function": "MODELS.load/model.predict",
        "result": {
            "algorithm": algorithm,
            "policy_artifact": _relative_runtime(policy_path),
            "policy_sha256": _sha256_file(policy_path),
            **rollout,
        },
        "artifacts": [_artifact_entry(policy_path)],
        "model_provenance": {
            "algorithm": algorithm,
            "policy_loaded": True,
            "policy_inference_executed": True,
        },
    }


def _qlib_workflow(payload: dict[str, Any], *, require_backtest: bool) -> dict[str, Any]:
    project = "qlib"
    _require_runtime(project)
    import qlib
    import qlib.cli.run as qlib_run
    from qlib.cli.run import workflow

    config_path = _project_config_path(str(payload["config_path"]), project)
    seed = int(payload.get("seed") or 0)
    _seed_runtime(seed)
    config_text = config_path.read_text(encoding="utf-8")
    if require_backtest and not any(token in config_text for token in ("PortAnaRecord", "SignalRecord", "SigAnaRecord")):
        raise ValueError("Qlib backtest_model requires a workflow config with portfolio/signal analysis records")
    artifact_dir = _artifact_dir(project, payload)
    uri_folder = artifact_dir / "mlruns"
    provider_uri = payload.get("provider_uri") or os.getenv("QLIB_PROVIDER_URI")
    previous = os.environ.get("QLIB_PROVIDER_URI")
    if provider_uri:
        os.environ["QLIB_PROVIDER_URI"] = str(provider_uri)
    try:
        workflow(
            str(config_path),
            experiment_name=str(payload.get("experiment_name") or ("stock-ai-backtest" if require_backtest else "stock-ai-train")),
            uri_folder=str(uri_folder),
        )
    finally:
        if previous is None:
            os.environ.pop("QLIB_PROVIDER_URI", None)
        else:
            os.environ["QLIB_PROVIDER_URI"] = previous
    portfolio_return_path = _qlib_portfolio_return_path(artifact_dir) if require_backtest else None
    return {
        "upstream_modules": [_source_module(project, "qlib/cli/run.py")],
        "runtime_modules": [_runtime_module(qlib_run)],
        "executed_function": "qlib.cli.run.workflow",
        "result": {
            "stage": "backtest" if require_backtest else "train",
            "config_path": str(config_path.relative_to(ROOT)),
            "config_sha256": _sha256_file(config_path),
            "experiment_name": str(payload.get("experiment_name") or ("stock-ai-backtest" if require_backtest else "stock-ai-train")),
            "mlruns_path": _relative_runtime(uri_folder),
            "workflow_completed": True,
            "seed": seed,
            "portfolio_return_path": portfolio_return_path,
        },
        "artifacts": _collect_artifacts(artifact_dir),
        "model_provenance": {
            "qlib_workflow_executed": True,
            "qlib_version": str(qlib.__version__),
            "training_executed": True,
            "model_loaded": True,
            "inference_executed": True,
            "portfolio_analysis_required": require_backtest,
            "provider_uri_configured": bool(provider_uri),
            "seed": seed,
        },
    }


def _qlib_portfolio_return_path(artifact_dir: Path) -> dict[str, Any]:
    """Extract the Qlib portfolio return stream needed for a later replay.

    Qlib's ``report_normal`` is a real recorder artifact, so retaining the
    cost-adjusted excess-return path binds the differential check to the
    workflow that generated it rather than to a synthetic metric fixture.
    """
    import pandas as pd

    reports = list(artifact_dir.rglob("report_normal_*.pkl"))
    if not reports:
        raise ValueError("qlib_portfolio_report_artifact_missing")
    # A stable request/run id intentionally permits repeatable smoke runs.  Its
    # MLflow folder is append-only, so select the recorder report that Qlib
    # most recently wrote instead of treating historical receipts as a reason
    # to reject the fresh workflow.
    report_path = max(reports, key=lambda candidate: candidate.stat().st_mtime_ns)
    report = pd.read_pickle(report_path)
    required_columns = {"return", "bench", "cost"}
    if not required_columns.issubset(report.columns):
        raise ValueError("qlib_portfolio_report_columns_missing")
    values = [float(value) for value in (report["return"] - report["bench"] - report["cost"]).tolist()]
    if len(values) < 2 or any(not math.isfinite(value) or value <= -1.0 for value in values):
        raise ValueError("qlib_portfolio_return_path_invalid")
    return {
        "aggregation": "return_minus_benchmark_minus_cost",
        "periods_per_year": 238,
        "sample_count": len(values),
        "return_path_sha256": _sha256_json(values),
        "returns": values,
        "source_artifact": _artifact_entry(report_path),
    }


def _qlib_reference_risk_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """Run Qlib's risk implementation as an independent metric reference.

    This is deliberately a separate isolated-worker action rather than a host
    reimplementation.  The host can then compare only the metrics with shared
    definitions (product annualized return and drawdown) and retain Qlib's
    source/runtime fingerprints alongside the input-path hash.
    """
    project = "qlib"
    _require_runtime(project)
    import pandas as pd
    import qlib.contrib.evaluate as qlib_evaluate
    from qlib.contrib.evaluate import risk_analysis

    raw_returns = payload.get("returns")
    if not isinstance(raw_returns, list) or len(raw_returns) < 2 or len(raw_returns) > 10000:
        raise ValueError("Qlib reference metrics require 2..10000 return observations")
    returns = [_finite(value) for value in raw_returns]
    if any(value is None or value <= -1.0 for value in returns):
        raise ValueError("Qlib reference returns must be finite and greater than -1")
    values = [float(value) for value in returns if value is not None]
    periods_per_year = int(payload.get("periods_per_year") or 252)
    if not 1 <= periods_per_year <= 1000:
        raise ValueError("periods_per_year must be between 1 and 1000")
    frame = risk_analysis(pd.Series(values, dtype="float64"), N=periods_per_year, mode="product")
    risk = frame["risk"].to_dict()
    metrics = {
        name: float(value)
        for name, value in risk.items()
        if _finite(value) is not None
    }
    return {
        "upstream_modules": [_source_module(project, "qlib/contrib/evaluate.py")],
        "runtime_modules": [_runtime_module(qlib_evaluate)],
        "executed_function": "qlib.contrib.evaluate.risk_analysis",
        "result": {
            "mode": "product",
            "periods_per_year": periods_per_year,
            "sample_count": len(values),
            "return_path_sha256": _sha256_json(values),
            "metrics": metrics,
        },
        "artifacts": [],
        "model_provenance": {
            "reference_framework": "qlib",
            "reference_execution": True,
            "training_executed": False,
            "model_loaded": False,
            "inference_executed": False,
        },
    }


def _finrobot_report(payload: dict[str, Any]) -> dict[str, Any]:
    project = "finrobot"
    _require_runtime(project)
    from finrobot.agents.workflow import SingleAssistant

    artifact_dir = _artifact_dir(project, payload)
    os.chdir(artifact_dir)
    model = str(payload.get("model") or os.getenv("FINROBOT_MODEL") or "")
    base_url = str(payload.get("base_url") or os.getenv("FINROBOT_BASE_URL") or "")
    key_env = str(payload.get("api_key_env") or "FINROBOT_API_KEY")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", key_env):
        raise ValueError("api_key_env must be an uppercase environment-variable name")
    api_key = os.getenv(key_env)
    if not model:
        raise PrerequisiteError(project, ["FINROBOT_MODEL"], "FinRobot requires a configured model")
    if not api_key and not base_url:
        raise PrerequisiteError(project, [key_env, "FINROBOT_BASE_URL"], "FinRobot requires an API key or local base URL")
    config_item: dict[str, Any] = {"model": model, "api_key": api_key or "no-key"}
    if base_url:
        config_item["base_url"] = base_url
    llm_config = {
        "config_list": [config_item],
        "timeout": int(payload.get("llm_timeout_seconds") or 180),
        "temperature": float(payload.get("temperature") or 0.0),
    }
    agent_config = str(payload.get("agent_config") or "Expert_Investor")
    assistant = SingleAssistant(
        agent_config=agent_config,
        llm_config=llm_config,
        human_input_mode="NEVER",
        max_consecutive_auto_reply=int(payload.get("max_turns") or 8),
        code_execution_config=False,
    )
    chat_result = assistant.user_proxy.initiate_chat(
        assistant.assistant,
        message=str(payload["prompt"]),
        silent=True,
        max_turns=int(payload.get("max_turns") or 8),
    )
    history = list(getattr(chat_result, "chat_history", None) or [])
    final_text = ""
    for item in reversed(history):
        if item.get("name") != "User_Proxy" and item.get("content"):
            final_text = str(item["content"])
            break
    assistant.reset()
    return {
        "upstream_modules": [
            _source_module(project, "finrobot/agents/workflow.py"),
            _source_module(project, "finrobot/agents/agent_library.py"),
        ],
        "executed_function": "SingleAssistant/UserProxyAgent.initiate_chat",
        "result": {
            "agent_config": agent_config,
            "message_count": len(history),
            "report": final_text,
            "terminated": any(str(item.get("content") or "").rstrip().endswith("TERMINATE") for item in history),
        },
        "artifacts": _collect_artifacts(artifact_dir),
        "model_provenance": {
            "model": model,
            "base_url": base_url or None,
            "credential_env": key_env,
            "transport_credential_present": bool(api_key),
            "external_api_credential_present": bool(api_key) and key_env != "STOCK_AI_CODEX_BRIDGE_TOKEN",
            "credential_scope": (
                "run_scoped_loopback" if key_env == "STOCK_AI_CODEX_BRIDGE_TOKEN" else "configured_provider"
            ),
            "credential_value_exposed": False,
            "autogen_workflow_executed": True,
            "code_execution_enabled": False,
        },
    }


def _project_config_path(value: str, project: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError("Workflow config_path must be a project-relative path")
    path = (ROOT / relative).resolve()
    allowed = [_PROJECT_ROOTS[project].resolve(), (ROOT / "config" / project).resolve()]
    if not path.is_file() or not any(path.is_relative_to(root) for root in allowed):
        raise PermissionError(f"Workflow config must be inside external/{project} or config/{project}")
    return path


def _artifact_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("artifact_name may contain only letters, numbers, dot, dash and underscore")
    return value.removesuffix(".zip")


def _relative_runtime(path: str | Path) -> str:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(RUNTIME_ROOT):
        raise PermissionError(f"Artifact is outside runtime root: {resolved}")
    # The desktop launcher may bind ``.runtime`` to Application Support.  Keep
    # receipts project-relative through that managed symlink rather than
    # leaking an application-support absolute path or rejecting valid files.
    return str(Path(".runtime") / "external-workflows" / resolved.relative_to(RUNTIME_ROOT))


def _artifact_entry(path: Path) -> dict[str, Any]:
    return {
        "path": _relative_runtime(path),
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _runtime_module(module: ModuleType) -> dict[str, Any]:
    path = Path(str(getattr(module, "__file__", ""))).resolve()
    if not path.is_file():
        raise RuntimeError(f"Runtime module has no verifiable file: {module.__name__}")
    return {
        "module": module.__name__,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _collect_artifacts(root: Path, limit: int = 100) -> list[dict[str, Any]]:
    return [_artifact_entry(path) for path in sorted(root.rglob("*")) if path.is_file()][:limit]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _runtime_fingerprint() -> dict[str, Any]:
    """Return a non-secret, restorable fingerprint for this isolated worker."""

    packages = sorted(
        {
            (distribution.metadata["Name"] or distribution.metadata["name"], distribution.version)
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"] or distribution.metadata["name"]
        },
        key=lambda item: item[0].casefold(),
    )
    package_lock = [{"name": name, "version": version} for name, version in packages]
    return {
        "schema_version": "open_stock_ai.external_worker_runtime_fingerprint.v2",
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_build": list(platform.python_build()),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "hardware": {
            "architecture": platform.architecture()[0],
            "processor": platform.processor() or "unreported",
            "cpu_count": os.cpu_count(),
        },
        "package_lock": package_lock,
        "package_lock_sha256": _sha256_json(package_lock),
    }


def _seed_runtime(seed: int) -> None:
    """Set non-secret deterministic controls before a Qlib workflow starts."""

    random.seed(seed)
    try:
        import numpy as np
        np.random.seed(seed)
    except ImportError:
        pass
    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def _finite(value: Any) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        return _jsonable(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dispatch(project: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "health":
        return {"health": _health(project)}
    if (project, action) == ("tradingagents", "analyze_symbol"):
        return _tradingagents_analyze(payload)
    if (project, action) == ("fingpt", "run_sentiment_model"):
        return _fingpt_sentiment(payload)
    if (project, action) == ("finrl", "train_policy"):
        return _finrl_train(payload)
    if (project, action) == ("finrl", "predict_actions"):
        return _finrl_predict(payload)
    if (project, action) == ("qlib", "train_factor_model"):
        return _qlib_workflow(payload, require_backtest=False)
    if (project, action) == ("qlib", "backtest_model"):
        return _qlib_workflow(payload, require_backtest=True)
    if (project, action) == ("qlib", "reference_risk_metrics"):
        return _qlib_reference_risk_metrics(payload)
    if (project, action) == ("finrobot", "generate_financial_report"):
        return _finrobot_report(payload)
    raise ValueError(f"Unsupported external workflow action: {project}.{action}")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in _PROJECT_ROOTS:
        raise SystemExit("usage: external_workflow_worker PROJECT ACTION")
    project, action = sys.argv[1:]
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        output = _dispatch(project, action, payload)
        response = {
            "schema_version": "open_stock_ai.external_workflow_worker.v1",
            "ok": True,
            "project": project,
            "action": action,
            "worker_python": sys.executable,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "runtime_fingerprint": _runtime_fingerprint(),
            **output,
        }
    except PrerequisiteError as exc:
        response = {
            "schema_version": "open_stock_ai.external_workflow_worker.v1",
            "ok": False,
            "project": project,
            "action": action,
            "error_type": "prerequisite_missing",
            "missing_prerequisites": exc.missing,
            "error": exc.detail,
            "worker_python": sys.executable,
        }
    except Exception as exc:
        response = {
            "schema_version": "open_stock_ai.external_workflow_worker.v1",
            "ok": False,
            "project": project,
            "action": action,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "worker_python": sys.executable,
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
