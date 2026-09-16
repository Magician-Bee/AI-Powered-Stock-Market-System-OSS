from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from open_stock_ai.types import ExecutionResult, RiskDecision, StockRequest, TradingSignal


@dataclass
class ExecutionEngine:
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    active_change_id: str | None = None

    def execute_paper(self, request: StockRequest, signal: TradingSignal, risk: RiskDecision) -> ExecutionResult:
        if not risk.approved:
            return ExecutionResult(executed=False, mode="paper", reason=f"Not executed: {risk.reason}")
        if self.trading_mode != "paper" or self.live_trading_enabled:
            return ExecutionResult(executed=False, mode="live_disabled", reason="Only paper trading is allowed.")
        order_id = f"PAPER-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
        # Execution truth belongs to PaperOMS.  This object is only a durable
        # order intent; callers must replace it with the OMS receipt before
        # they report a fill or persist execution=True.
        return ExecutionResult(
            executed=False,
            mode="paper_pending_oms",
            order_id=order_id,
            change_id=self.active_change_id,
            reason="Paper order intent created; awaiting authoritative PaperOMS receipt.",
            ledger={"execution_state": "requested", "oms_authoritative": True},
        )
