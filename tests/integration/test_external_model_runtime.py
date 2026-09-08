from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from open_stock_ai.external_sources.tradingagents_runtime import TradingAgentsRuntimeAdapter
from open_stock_ai.types import MarketSnapshot, StockRequest
from stock_ai.external_project_tools import ExternalProjectToolProvider


_RUNTIME_FINGERPRINT_SCHEMA = "open_stock_ai.external_worker_runtime_fingerprint.v2"


@pytest.mark.skipif(
    "finrl" not in os.getenv("STOCK_AI_RUN_EXTERNAL_MODEL_E2E", "").split(","),
    reason="release smoke only: bootstrap the FinRL runtime and opt in explicitly",
)
def test_real_finrl_policy_trains_persists_reloads_and_predicts_actions():
    provider = ExternalProjectToolProvider()
    context = AgentRunContext(
        run_id="AR-finrl-release-smoke",
        autonomy="external_execute",
        symbols=("TEST",),
        allow_external_actions=True,
    )
    prices = [[100.0 + index * 0.3] for index in range(24)]
    training = asyncio.run(
        provider.execute(
            "external.finrl.train_policy",
            {
                "prices": prices,
                "algorithm": "ppo",
                "total_timesteps": 64,
                "seed": 7,
                "artifact_name": "finrl-release-smoke",
                "initial_capital": 10_000,
                "max_stock": 5,
                "model_kwargs": {
                    "n_steps": 64,
                    "batch_size": 32,
                    "n_epochs": 1,
                    "learning_rate": 0.001,
                },
                "timeout_seconds": 600,
            },
            context,
        )
    )
    prediction = asyncio.run(
        provider.execute(
            "external.finrl.predict_actions",
            {
                "artifact_name": "finrl-release-smoke",
                "algorithm": "ppo",
                "prices": prices[:12],
                "initial_capital": 10_000,
                "max_stock": 5,
                "timeout_seconds": 600,
            },
            context,
        )
    )

    assert training["executed_function"] == "DRLAgent.get_model/train_model"
    assert training["model_provenance"]["training_executed"] is True
    assert training["runtime_fingerprint"]["schema_version"] == _RUNTIME_FINGERPRINT_SCHEMA
    assert training["runtime_fingerprint"]["python_build"]
    assert training["runtime_fingerprint"]["hardware"]
    assert len(training["runtime_fingerprint"]["package_lock_sha256"]) == 64
    assert len(training["result"]["policy_sha256"]) == 64
    assert prediction["executed_function"] == "MODELS.load/model.predict"
    assert prediction["result"]["policy_sha256"] == training["result"]["policy_sha256"]
    assert prediction["model_provenance"]["policy_loaded"] is True
    assert prediction["runtime_fingerprint"]["package_lock_sha256"] == training["runtime_fingerprint"]["package_lock_sha256"]
    assert prediction["result"]["steps_executed"] == 11
    assets = [float(prediction["result"]["initial_total_asset"])] + [
        float(step["total_asset"]) for step in prediction["result"]["steps"]
    ]
    returns = [current / previous - 1.0 for previous, current in zip(assets, assets[1:])]
    qlib_reference = asyncio.run(
        provider.execute(
            "external.qlib.reference_risk_metrics",
            {"returns": returns, "periods_per_year": 252},
            context,
        )
    )
    assert qlib_reference["executed_function"] == "qlib.contrib.evaluate.risk_analysis"
    assert qlib_reference["result"]["sample_count"] == len(returns)


@pytest.mark.skipif(
    "qlib" not in os.getenv("STOCK_AI_RUN_EXTERNAL_MODEL_E2E", "").split(","),
    reason="release smoke only: bootstrap the Qlib runtime and opt in explicitly",
)
def test_real_qlib_workflow_records_replayable_dataset_model_signal_and_portfolio_artifacts():
    provider = ExternalProjectToolProvider()
    provider_uri = provider.workflow_root / "qlib-data" / "cn_data"
    assert provider_uri.is_dir()
    context = AgentRunContext(
        run_id="AR-qlib-release-smoke",
        autonomy="external_execute",
        symbols=("SH000300",),
        allow_external_actions=True,
    )
    receipt = asyncio.run(
        provider.execute(
            "external.qlib.backtest_model",
            {
                "config_path": "config/qlib/workflow_smoke_linear_Alpha158.yaml",
                "provider_uri": str(provider_uri),
                "experiment_name": "qlib-release-smoke",
                "timeout_seconds": 900,
            },
            context,
        )
    )

    assert receipt["schema_version"] == "open_stock_ai.external_full_workflow.v1"
    assert receipt["project"] == "qlib"
    assert receipt["action"] == "backtest_model"
    assert receipt["executed_function"] == "qlib.cli.run.workflow"
    assert receipt["result"]["workflow_completed"] is True
    assert len(receipt["result"]["config_sha256"]) == 64
    return_path = receipt["result"]["portfolio_return_path"]
    assert return_path["aggregation"] == "return_minus_benchmark_minus_cost"
    assert return_path["sample_count"] == len(return_path["returns"])
    assert len(return_path["return_path_sha256"]) == 64
    reference = asyncio.run(
        provider.execute(
            "external.qlib.reference_risk_metrics",
            {"returns": return_path["returns"], "periods_per_year": return_path["periods_per_year"]},
            context,
        )
    )
    assert reference["executed_function"] == "qlib.contrib.evaluate.risk_analysis"
    assert reference["result"]["return_path_sha256"] == return_path["return_path_sha256"]
    assert set(reference["result"]["metrics"]) >= {"annualized_return", "max_drawdown"}
    provenance = receipt["model_provenance"]
    assert provenance["qlib_workflow_executed"] is True
    assert provenance["training_executed"] is True
    assert provenance["model_loaded"] is True
    assert provenance["inference_executed"] is True
    assert provenance["portfolio_analysis_required"] is True
    assert provenance["provider_uri_configured"] is True
    paths = {item["path"] for item in receipt["artifacts"]}
    assert any(path.endswith("/artifacts/dataset") for path in paths)
    assert any(path.endswith("/artifacts/params.pkl") for path in paths)
    assert any(path.endswith("/artifacts/pred.pkl") for path in paths)
    assert any("/artifacts/sig_analysis/" in path for path in paths)
    assert any("/artifacts/portfolio_analysis/" in path for path in paths)
    assert all(len(item["sha256"]) == 64 and item["size"] >= 0 for item in receipt["artifacts"])
    assert receipt["runtime_fingerprint"]["schema_version"] == _RUNTIME_FINGERPRINT_SCHEMA
    assert receipt["runtime_fingerprint"]["python_build"]
    assert receipt["runtime_fingerprint"]["hardware"]
    assert len(receipt["runtime_fingerprint"]["package_lock_sha256"]) == 64


@pytest.mark.skipif(
    "tradingagents" not in os.getenv("STOCK_AI_RUN_EXTERNAL_MODEL_E2E", "").split(","),
    reason="release smoke only: opt in to the vendored TradingAgents graph and remote model",
)
def test_real_tradingagents_graph_runs_with_remote_openai_compatible_model():
    base_url = os.getenv("STOCK_AI_TRADINGAGENTS_BASE_URL")
    model = os.getenv("STOCK_AI_TRADINGAGENTS_MODEL", "gpt-oss:20b")
    if not base_url:
        pytest.fail("STOCK_AI_TRADINGAGENTS_BASE_URL is required for the TradingAgents release smoke")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.fail("OPENAI_API_KEY is required for the OpenAI-compatible TradingAgents release smoke")

    snapshot = MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=100.0,
        ohlcv=[{"timestamp": "2026-08-20T05:30:00+00:00", "close": 100.0}],
        raw={
            "tradingagents_runtime": {
                "enabled": True,
                "base_url": base_url,
                "model": model,
                "selected_analysts": ["market"],
                "max_debate_rounds": 0,
                "max_risk_discuss_rounds": 0,
            }
        },
    )
    receipt = TradingAgentsRuntimeAdapter(
        project_root=Path.cwd(), timeout_seconds=180
    ).run(StockRequest(symbol="2330.TW", market="TW"), snapshot)

    assert receipt["status"] == "executed"
    assert receipt["model_output"] is True
    assert receipt["provider"] == {
        "kind": "openai_compatible",
        "base_url": base_url.rstrip("/"),
        "model": model,
    }
    assert receipt["execution_authority"] == "none"
    assert receipt["execution_boundary"] == "research_evidence_only_no_order_authority"
    assert receipt["result"]["decision"]
    assert len(receipt["result_sha256"]) == 64
