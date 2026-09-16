from __future__ import annotations

from dataclasses import dataclass

from open_stock_ai.external_sources.fingpt_source import FinGPTSource
from open_stock_ai.external_sources.finrobot_source import FinRobotSource
from open_stock_ai.external_sources.tradingagents_source import TradingAgentsSource
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest


@dataclass
class IntelligenceHub:
    fingpt: FinGPTSource
    finrobot: FinRobotSource
    tradingagents: TradingAgentsSource

    def analyze(self, request: StockRequest, market_snapshot: MarketSnapshot) -> IntelligenceResult:
        sentiment = self.fingpt.analyze_news(request, market_snapshot)
        report = self.finrobot.analyze_fundamentals(request, market_snapshot)
        agent_view = self.tradingagents.analyze_context(request, market_snapshot)
        summary = "\n\n".join(
            part for part in [sentiment.get("summary"), report.get("summary"), agent_view.get("summary")] if part
        ).strip()
        risks = sorted(set(sentiment.get("risks", []) + report.get("risks", []) + agent_view.get("risks", [])))
        evidence = sentiment.get("evidence", []) + report.get("evidence", []) + agent_view.get("evidence", [])
        adapter_results = [
            item["adapter_result"]
            for item in [sentiment, report, agent_view]
            if isinstance(item.get("adapter_result"), dict)
        ]
        return IntelligenceResult(
            symbol=request.symbol,
            market=request.market,
            summary=summary or "No integrated intelligence summary available.",
            sentiment_score=sentiment.get("sentiment_score"),
            sentiment_label=sentiment.get("sentiment_label"),
            fundamental_view=report.get("fundamental_view"),
            technical_view=agent_view.get("technical_view"),
            news_view=sentiment.get("summary"),
            risks=risks,
            evidence=evidence,
            adapter_results=adapter_results,
            raw={"fingpt": sentiment, "finrobot": report, "tradingagents": agent_view},
        )
