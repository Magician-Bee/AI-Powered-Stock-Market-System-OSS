from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentOrchestrator, AgentRunContext
from stock_ai.agent_tools import StockAgentToolRegistry
from stock_ai.external_project_tools import ExternalProjectToolProvider


EXTERNAL_TOOL_NAMES = {
    "external.tradingagents.model_capabilities",
    "external.tradingagents.investment_decision",
    "external.fingpt.sentiment_consensus",
    "external.finrobot.report_quality",
    "external.finrobot.market_data",
    "external.finrl.walk_forward_windows",
    "external.finrl.simulate_environment",
    "external.finrl_trading.information_ratio",
    "external.finrl_trading.regime_signals",
    "external.qlib.align_signals",
    "external.qlib.factor_dataset",
    "external.ai_trader.variant_metrics",
    "external.ai_trader.score_signal",
}


def _context() -> AgentRunContext:
    return AgentRunContext(run_id="AR-external", autonomy="advisory", symbols=("2330.TW",))


def test_agent_manifest_exposes_one_real_executor_per_finance_external_project():
    manifest = StockAgentToolRegistry().manifest()
    by_name = {item["name"]: item for item in manifest}

    assert EXTERNAL_TOOL_NAMES.issubset(by_name)
    for name in EXTERNAL_TOOL_NAMES:
        assert by_name[name]["category"] == "external_runtime"
        assert by_name[name]["skills"]
        assert by_name[name]["packages"][0].startswith("external/")
        assert by_name[name]["mutating"] is False


def test_all_external_agent_tools_execute_vendored_source_without_fallback():
    registry = StockAgentToolRegistry()
    cases = {
        "external.tradingagents.model_capabilities": {"model": "deepseek-reasoner"},
        "external.tradingagents.investment_decision": {
            "analysis": "Evidence supports a measured allocation. Rating: Overweight",
        },
        "external.fingpt.sentiment_consensus": {
            "signals": [
                {"source": "news", "average_sentiment_score": 0.4, "total_mentions": 12},
                {"source": "reddit", "average_sentiment_score": 0.2, "total_mentions": 4},
            ]
        },
        "external.finrobot.report_quality": {
            "text": "one two three four",
            "min_words": 3,
            "max_words": 5,
        },
        "external.finrl.walk_forward_windows": {
            "train_dates": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "trade_dates": ["2026-01-04", "2026-01-05", "2026-01-06"],
            "rolling_window_length": 2,
        },
        "external.finrl.simulate_environment": {
            "prices": [[100.0], [101.0], [102.0], [99.0]],
            "actions": [[0.5], [0.0], [-0.5]],
            "initial_capital": 10_000,
            "max_stock": 10,
        },
        "external.finrl_trading.information_ratio": {
            "returns": [0.01, 0.02, -0.01, 0.03],
            "benchmark_returns": [0.005, 0.01, 0.0, 0.015],
            "lookback": 4,
        },
        "external.finrl_trading.regime_signals": {
            "dates": ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23"],
            "spx_prices": [100, 102, 98, 90],
            "vix_prices": [15, 16, 20, 40],
            "trend_ma_weeks": 3,
            "drawdown_weeks": 3,
            "drawdown_threshold": 0.05,
        },
        "external.qlib.align_signals": {
            "left": {"AAPL": 0.7, "MSFT": 0.3},
            "right": {"MSFT": 0.2, "NVDA": 0.8},
        },
        "external.qlib.factor_dataset": {
            "features": {"2026-01-01": 1.0, "2026-01-02": 2.0, "2026-01-03": 3.0},
            "labels": {"2026-01-02": 2.0, "2026-01-03": 4.0, "2026-01-04": 8.0},
        },
        "external.ai_trader.variant_metrics": {
            "metric": "return_pct",
            "rows": [
                {"variant_key": "a", "return_pct": 1.0},
                {"variant_key": "a", "return_pct": 3.0},
                {"variant_key": "b", "return_pct": 2.0},
            ],
        },
        "external.ai_trader.score_signal": {
            "symbol": "2330.TW",
            "content": "Bull breakout because data supports upside. Target price 120, confidence 72%. Risk below 90.",
            "tags": ["technical", "risk"],
        },
    }

    async def execute_all():
        return await asyncio.gather(
            *(registry.execute(name, arguments, _context()) for name, arguments in cases.items())
        )

    results = dict(zip(cases, asyncio.run(execute_all())))

    for name, result in results.items():
        assert result["schema_version"] == "open_stock_ai.external_execution.v1"
        assert result["external_module_loaded"] is True
        assert result["module_under_locked_path"] is True
        assert result["fallback_used"] is False
        assert result["executed_module"].startswith("external/")
        assert len(result["module_sha256"]) == 64
        assert len(result["source_lock"]["head"]) == 40
        assert result["result"] is not None, name

    assert results["external.fingpt.sentiment_consensus"]["result"]["total_mentions"] == 16
    assert results["external.tradingagents.investment_decision"]["result"]["rating"] == "Overweight"
    assert results["external.finrobot.report_quality"]["result"]["within_range"] is True
    assert results["external.finrl.walk_forward_windows"]["result"]["trade_starts"] == [
        "2026-01-04",
        "2026-01-06",
    ]
    assert results["external.finrl.simulate_environment"]["result"]["terminated"] is True
    assert results["external.finrl.simulate_environment"]["result"]["steps_executed"] == 3
    assert results["external.finrl_trading.regime_signals"]["result"]["regime"] == "risk_off"
    assert results["external.qlib.align_signals"]["result"]["aligned"] == {
        "AAPL": 0.7,
        "MSFT": 0.5,
        "NVDA": 0.8,
    }
    assert results["external.qlib.factor_dataset"]["result"]["valid_pair_count"] == 2
    assert results["external.ai_trader.score_signal"]["result"]["prediction"]["direction"] == "up"
    assert results["external.ai_trader.score_signal"]["result"]["quality"]["overall_score"] > 0


def test_finrobot_market_data_is_registered_as_online_core_executor_without_network_test():
    item = next(
        tool for tool in StockAgentToolRegistry().manifest()
        if tool["name"] == "external.finrobot.market_data"
    )

    assert item["skills"] == ["finrobot-market-data"]
    assert item["packages"] == ["external/FinRobot", "yfinance", "pandas"]
    assert item["input_schema"]["required"] == ["symbol", "start_date", "end_date"]


def test_high_level_external_workflow_runs_real_capability_steps_without_claiming_models(monkeypatch):
    provider = ExternalProjectToolProvider()
    called = []

    async def fake_worker(project, action, payload):
        called.append((project, action, payload))
        result = {"rating": "Hold"} if project == "tradingagents" else {"ok": True}
        return {"project": project, "action": action, "result": result}

    monkeypatch.setattr(provider, "_run_worker", fake_worker)
    result = asyncio.run(
        provider.execute(
            "external.workflow.analyze_verified_pack",
            {
                "symbol": "2330.TW",
                "analysis": "Verified observations support Hold.",
                "sentiment_signals": [
                    {"source": "official-news", "average_sentiment_score": 0.1, "total_mentions": 2}
                ],
            },
            _context(),
        )
    )

    assert [(project, action) for project, action, _ in called] == [
        ("tradingagents", "investment_decision"),
        ("fingpt", "sentiment_consensus"),
        ("ai_trader", "score_signal"),
    ]
    assert result["workflow_completed"] is True
    assert result["model_health"]["available_vendored_functions_executed"] is True
    assert result["model_health"]["fingpt_foundation_model_loaded"] is False


def test_external_runtime_health_is_capability_level_truth():
    result = asyncio.run(ExternalProjectToolProvider().execute("external.runtime.health", {}, _context()))

    assert result["truth_model"] == "capability_level_not_project_name"
    assert result["projects"]["FinRL"]["environment_ready"] is True
    finrl = result["projects"]["FinRL"]
    assert isinstance(finrl["policy_loaded"], bool)
    if finrl["policy_loaded"]:
        assert finrl["last_workflow_execution"]["action"] == "predict_actions"
        assert finrl["last_workflow_execution"]["source_lock_head"] == (
            "220f9e490996a6e5c84cfad914ff14f2e0c42d22"
        )
    elif finrl["last_workflow_execution"] is not None:
        assert finrl["last_workflow_execution"]["action"] == "train_policy"


class _ExternalToolDriver:
    driver_id = "external-test"

    def __init__(self) -> None:
        self.turn = 0

    def describe(self):
        return {"id": self.driver_id, "configured": True}

    async def decide(self, turn):
        self.turn += 1
        if self.turn == 1:
            return {
                "state": "continue",
                "summary": "Run FinGPT sentiment consensus",
                "tool_calls": [
                    {
                        "id": "fingpt-1",
                        "name": "external.fingpt.sentiment_consensus",
                        "arguments": {
                            "signals": [
                                {"source": "news", "average_sentiment_score": 0.35, "total_mentions": 8}
                            ]
                        },
                    }
                ],
                "decision": None,
            }
        return {
            "state": "complete",
            "summary": "External evidence collected",
            "tool_calls": [],
            "decision": {
                "action": "watch",
                "symbol": "2330.TW",
                "confidence": 60,
                "rationale": "FinGPT evidence observed",
                "next_check": "next refresh",
            },
        }


def test_agent_run_stream_identifies_external_tool_skill_package_and_result():
    events = []
    driver = _ExternalToolDriver()
    runtime = AgentOrchestrator(
        drivers={driver.driver_id: driver},
        tools=StockAgentToolRegistry(),
        default_driver=driver.driver_id,
    )

    result = asyncio.run(
        runtime.run(objective="Use FinGPT evidence", symbols=["2330.TW"], event_sink=events.append)
    )

    started = next(event for event in events if event["type"] == "tool.started")
    completed = next(event for event in events if event["type"] == "tool.completed")
    assert result["status"] == "completed"
    assert started["tool"] == "external.fingpt.sentiment_consensus"
    assert started["skills"] == ["fingpt-sentiment"]
    assert started["packages"] == ["external/FinGPT", "pandas"]
    assert completed["tool"] == "external.fingpt.sentiment_consensus"
