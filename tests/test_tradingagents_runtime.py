from __future__ import annotations

from pathlib import Path

from open_stock_ai.external_sources.tradingagents_runtime import TradingAgentsRuntimeAdapter
from open_stock_ai.external_sources.tradingagents_source import TradingAgentsSource
from open_stock_ai.types import MarketSnapshot, StockRequest


def _snapshot(runtime: dict | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="2330.TW", market="TW", price=100.0,
        ohlcv=[{"timestamp": "2026-08-20T05:30:00+00:00", "close": 100.0}],
        raw={"tradingagents_runtime": runtime} if runtime is not None else {},
    )


def _configuration() -> dict:
    return {
        "enabled": True,
        "base_url": "http://research-ollama.example/v1",
        "model": "gpt-oss:20b",
        "selected_analysts": ["market", "news"],
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    }


def test_tradingagents_runtime_is_disabled_without_explicit_opt_in() -> None:
    receipt = TradingAgentsRuntimeAdapter(project_root=Path.cwd()).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot()
    )

    assert receipt["status"] == "disabled"
    assert receipt["reason"] == "tradingagents_runtime_not_enabled"
    assert receipt["model_output"] is False
    assert receipt["execution_authority"] == "none"


def test_tradingagents_runtime_validates_provider_receipt_and_stays_research_only() -> None:
    received: list[dict] = []

    def runner(payload: dict) -> dict:
        received.append(payload)
        return {"decision": "Hold until evidence improves", "state_keys": ["messages", "risk_debate"]}

    receipt = TradingAgentsRuntimeAdapter(project_root=Path.cwd(), runner=runner).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_configuration())
    )

    assert receipt["status"] == "executed"
    assert receipt["model_output"] is True
    assert receipt["provider"] == {"kind": "openai_compatible", "base_url": "http://research-ollama.example/v1", "model": "gpt-oss:20b"}
    assert receipt["result"]["state_keys"] == ["messages", "risk_debate"]
    assert len(receipt["result_sha256"]) == 64
    assert receipt["execution_authority"] == "none"
    assert received[0]["trade_date"] == "2026-08-20"


def test_tradingagents_runtime_rejects_an_invalid_provider_result() -> None:
    adapter = TradingAgentsRuntimeAdapter(project_root=Path.cwd(), runner=lambda _: {"decision": "", "state_keys": []})
    receipt = adapter.run(StockRequest(symbol="2330.TW", market="TW"), _snapshot(_configuration()))

    assert receipt["status"] == "failed"
    assert receipt["reason"] == "tradingagents_runtime_failed:ValueError"
    assert receipt["model_output"] is False


def test_tradingagents_source_keeps_real_runtime_as_research_evidence_only() -> None:
    runtime = TradingAgentsRuntimeAdapter(
        project_root=Path.cwd(),
        runner=lambda _: {"decision": "Research evidence favors Hold", "state_keys": ["final_trade_decision"]},
    )
    result = TradingAgentsSource(runtime=runtime).analyze_context(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_configuration())
    )

    assert result["status"] == "executed"
    assert result["method"] == "tradingagents_graph_runtime_research_evidence"
    assert result["model_provenance"]["model_output"] is True
    assert result["model_provenance"]["execution_authority"] == "none"
    assert result["adapter_result"]["loaded"] is True
