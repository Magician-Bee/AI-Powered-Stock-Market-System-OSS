from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .data.market_data_hub import MarketDataHub
from .execution.execution_engine import ExecutionEngine
from .intelligence.intelligence_hub import IntelligenceHub
from .research.research_engine import ResearchEngine
from .risk.risk_engine import RiskEngine
from .storage.decision_log_store import DecisionLogStore
from .storage.signal_store import SignalStore
from .storage.trade_store import TradeStore
from .strategy.strategy_engine import StrategyEngine
from .types import ExecutionResult, StockDecision, StockRequest

if TYPE_CHECKING:
    from .external_sources.ai_trader_source import AITraderSource


@dataclass
class SignalPipeline:
    market_data: MarketDataHub
    intelligence: IntelligenceHub
    strategy: StrategyEngine
    research: ResearchEngine
    risk: RiskEngine
    execution: ExecutionEngine
    signal_store: SignalStore | None = None
    decision_log_store: DecisionLogStore | None = None
    trade_store: TradeStore | None = None
    ai_trader: "AITraderSource | None" = None
    batch_requests: tuple[StockRequest, ...] = field(default_factory=tuple)

    def run(self, request: StockRequest, *, execute: bool = True) -> StockDecision:
        market_snapshot = self.market_data.load(request)
        intelligence_result = self.intelligence.analyze(request, market_snapshot)
        strategy_signal = self.strategy.generate_signal(
            request=request,
            market_snapshot=market_snapshot,
            intelligence=intelligence_result,
        )
        if self.ai_trader is not None:
            validated_signal = self.ai_trader.validate_signal(
                asdict(strategy_signal),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            strategy_signal.ai_trader_validation = {
                key: value for key, value in validated_signal.items() if key != "row"
            }
            strategy_signal.ai_trader_signal_row = validated_signal.get("row", {})
            strategy_signal.ai_trader_interop_projection = self.ai_trader.build_signal_interop_projection(
                validated_signal
            )
            strategy_signal.ai_trader_skill_route = self.ai_trader.build_skill_route_projection(
                asdict(strategy_signal),
                validation=validated_signal,
            )

        research_result = self.research.validate(
            request=request,
            signal=strategy_signal,
            market_snapshot=market_snapshot,
            intelligence=intelligence_result,
        )
        data_contract = (
            market_snapshot.raw.get("data_contract")
            if isinstance(market_snapshot.raw, dict) and isinstance(market_snapshot.raw.get("data_contract"), dict)
            else {}
        )
        if data_contract and data_contract.get("decision_ready") is not True:
            research_result.passed = False
            validation_status = research_result.raw.setdefault(
                "validation_status",
                {
                    "schema_version": "open_stock_ai.research_validation_status.v2",
                    "passed": False,
                    "blockers": [],
                },
            )
            validation_status["passed"] = False
            validation_status["execution_evidence_eligible"] = False
            blockers = validation_status.setdefault("blockers", [])
            for blocker in data_contract.get("blockers") or ["market_data_not_execution_eligible"]:
                if blocker not in blockers:
                    blockers.append(blocker)
            research_result.raw["market_data_contract"] = data_contract
            research_result.summary = (
                "Advisory analysis completed; execution is blocked because the market data contract is not decision-ready."
            )

        paper_exposure = self.trade_store.exposure() if self.trade_store is not None else None
        if paper_exposure is not None:
            research_result.raw["paper_exposure_before_risk"] = paper_exposure
        risk_decision = self.risk.evaluate(
            request=request,
            signal=strategy_signal,
            research=research_result,
            paper_exposure=paper_exposure,
        )

        if execute:
            execution_result = self.execution.execute_paper(
                request=request,
                signal=strategy_signal,
                risk=risk_decision,
            )
            if execution_result.executed:
                execution_result.executed = False
                execution_result.mode = "paper_pending_oms"
                execution_result.reason = "Execution claim ignored; awaiting an authoritative OMS fill receipt."
        else:
            execution_result = ExecutionResult(
                executed=False,
                mode="paper",
                reason="Agent analysis mode: no paper order was submitted.",
                ledger={"analysis_only": True, "execution_requested": False},
            )

        decision = StockDecision(
            request=request,
            market_snapshot=market_snapshot,
            intelligence=intelligence_result,
            signal=strategy_signal,
            research=research_result,
            risk=risk_decision,
            execution=execution_result,
        )

        # The execution engine creates a request only.  The OMS owns the
        # authoritative lifecycle and is the sole source allowed to mark the
        # decision executed.  This avoids the old two-truth sequence where a
        # provisional execution=True was later rewritten after an OMS reject.
        if (
            self.trade_store is not None
            and execute
            and risk_decision.approved
            and execution_result.mode == "paper_pending_oms"
        ):
            ledger = self.trade_store.save_paper_order(asdict(decision))
            decision.execution.ledger = ledger
            oms = ledger.get("oms") if isinstance(ledger, dict) else None
            if self._valid_oms_fill_receipt(ledger, execution_result.order_id):
                decision.execution.executed = True
                decision.execution.mode = "paper"
                decision.execution.reason = "PaperOMS accepted and filled the paper order."
            else:
                order = (
                    oms.get("order")
                    if isinstance(oms, dict) and isinstance(oms.get("order"), dict)
                    else {}
                )
                rejection_reason = order.get("rejection_reason") or "paper_oms_rejected"
                decision.execution.executed = False
                decision.execution.mode = "paper"
                decision.execution.reason = f"Paper OMS rejected the order: {rejection_reason}."

        if self.signal_store is not None:
            payload = asdict(decision)
            decision.research.raw["storage"] = self.signal_store.save(payload)
        if self.decision_log_store is not None:
            payload = asdict(decision)
            decision.research.raw["decision_log"] = self.decision_log_store.save(payload)
        return decision

    @staticmethod
    def _valid_oms_fill_receipt(ledger: object, expected_order_id: str | None) -> bool:
        if not isinstance(ledger, dict) or ledger.get("saved") is not True:
            return False
        oms = ledger.get("oms")
        if not isinstance(oms, dict) or oms.get("schema_version") != "open_stock_ai.paper_oms_result.v1":
            return False
        order = oms.get("order")
        fill = oms.get("fill")
        if not isinstance(order, dict) or not isinstance(fill, dict):
            return False
        order_id = str(order.get("order_id") or "")
        try:
            quantity = float(fill.get("quantity"))
        except (TypeError, ValueError):
            return False
        return (
            bool(expected_order_id)
            and order_id == expected_order_id
            and oms.get("filled") is True
            and order.get("status") == "filled"
            and fill.get("order_id") == order_id
            and bool(str(fill.get("fill_id") or "").strip())
            and quantity > 0
        )

    def run_pre_market(self) -> list[StockDecision]:
        return self._run_batch(horizon="swing")

    def run_intraday(self) -> list[StockDecision]:
        return self._run_batch(horizon="intraday")

    def run_after_market(self) -> list[StockDecision]:
        return self._run_batch(horizon="weekly")

    def _run_batch(self, horizon: str) -> list[StockDecision]:
        # Batch sessions are research/ranking operations. They must never create
        # orders merely because an Agent refreshed the watchlist.
        return [
            self.run(replace(request, horizon=horizon), execute=False)  # type: ignore[arg-type]
            for request in self.batch_requests
        ]
