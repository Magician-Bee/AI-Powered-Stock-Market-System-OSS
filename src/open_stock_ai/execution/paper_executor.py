from __future__ import annotations

from open_stock_ai.types import ExecutionResult, RiskDecision, StockRequest, TradingSignal

from .execution_engine import ExecutionEngine


class PaperExecutor(ExecutionEngine):
    def submit(self, request: StockRequest, signal: TradingSignal, risk: RiskDecision) -> ExecutionResult:
        return self.execute_paper(request=request, signal=signal, risk=risk)
