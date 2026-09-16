from __future__ import annotations

from dataclasses import dataclass

from .pipeline import SignalPipeline
from .types import StockDecision, StockRequest
from .governance.runtime import RuntimeGovernance


@dataclass
class OpenStockAIEngine:
    pipeline: SignalPipeline
    governance: RuntimeGovernance | None = None

    def analyze_stock(self, request: StockRequest) -> StockDecision:
        return self.pipeline.run(request, execute=True)

    def analyze_for_agent(self, request: StockRequest) -> StockDecision:
        """Run the full analysis pipeline without creating any paper order.

        Embedded agents may inspect market data, intelligence, strategy, research,
        risk gates and portfolio exposure, while execution remains a separate,
        explicit action.
        """
        return self.pipeline.run(request, execute=False)

    def run_pre_market(self) -> list[StockDecision]:
        return self.pipeline.run_pre_market()

    def run_intraday(self) -> list[StockDecision]:
        return self.pipeline.run_intraday()

    def run_after_market(self) -> list[StockDecision]:
        return self.pipeline.run_after_market()
