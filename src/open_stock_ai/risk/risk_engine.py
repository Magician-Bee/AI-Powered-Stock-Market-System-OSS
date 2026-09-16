from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from open_stock_ai.risk.drawdown_guard import drawdown_allowed
from open_stock_ai.risk.kill_switch import DurableRiskControlStore
from open_stock_ai.risk.position_sizing import cap_position_size
from open_stock_ai.types import ResearchResult, RiskDecision, StockRequest, TradingSignal


@dataclass
class RiskEngine:
    min_rule_score_threshold: float = 0.70
    max_position_size_pct: float = 10.0
    max_daily_loss_pct: float = 3.0
    max_total_drawdown_pct: float = 15.0
    max_symbol_exposure_pct: float = 20.0
    max_total_paper_exposure_pct: float = 100.0
    max_industry_exposure_pct: float = 45.0
    require_backtest_passed: bool = True
    live_trading_enabled: bool = False
    risk_control: DurableRiskControlStore | None = None

    def evaluate(
        self,
        request: StockRequest,
        signal: TradingSignal,
        research: ResearchResult,
        paper_exposure: dict[str, Any] | None = None,
    ) -> RiskDecision:
        checks: list[dict[str, Any]] = []
        if self.risk_control is not None:
            risk_scopes: dict[str, str] = {"global": "global", "symbol": request.symbol}
            if isinstance(paper_exposure, dict):
                configured_scopes = paper_exposure.get("risk_scopes")
                if isinstance(configured_scopes, dict):
                    for scope_type, scope_key in configured_scopes.items():
                        if scope_key is not None and str(scope_key).strip():
                            risk_scopes[str(scope_type)] = str(scope_key)
                for scope_type, keys in {
                    "account": ("account_id", "account_alias"),
                    "broker": ("broker_id", "broker"),
                    "strategy": ("strategy_id", "strategy"),
                }.items():
                    for key in keys:
                        value = paper_exposure.get(key)
                        if value is not None and str(value).strip():
                            risk_scopes[scope_type] = str(value)
                            break
            order_gate = self.risk_control.order_gate(risk_scopes)
            gate_passed = order_gate["allowed"] is True
            checks.append(
                self._gate(
                    code="durable_risk_control",
                    passed=gate_passed,
                    observed=order_gate["active_switches"],
                    limit="no_active_switch",
                    message=(
                        "No matching durable risk-control switch is active."
                        if gate_passed
                        else "A matching durable risk-control switch blocks new orders."
                    ),
                )
            )
            if not gate_passed:
                self._append_not_evaluated(checks, after="durable_risk_control")
                return self._decision(
                    approved=False,
                    reason="Durable risk control switch is active.",
                    adjusted_position_size_pct=0.0,
                    risk_notes=[
                        f"Blocked by {len(order_gate['active_switches'])} durable risk-control switch(es)."
                    ],
                    gate_checks=checks,
                )
        calibrated_probability = (
            float(signal.confidence)
            if signal.confidence_calibrated is True
            and signal.confidence_type == "calibrated_probability"
            and signal.confidence is not None
            else None
        )
        rule_score_magnitude = (
            calibrated_probability
            if calibrated_probability is not None
            else abs(float(signal.rule_score))
            if signal.rule_score is not None
            else float(signal.confidence)
            if signal.confidence is not None
            else None
        )
        score_label = "Calibrated success probability" if calibrated_probability is not None else "Uncalibrated rule-score magnitude"
        score_passed = (
            rule_score_magnitude is not None
            and isfinite(rule_score_magnitude)
            and rule_score_magnitude >= self.min_rule_score_threshold
            and signal.decision_status == "ready"
        )
        score_gate = self._gate(
            code="rule_score_threshold",
            passed=score_passed,
            observed=rule_score_magnitude,
            limit=self.min_rule_score_threshold,
            message=(
                f"{score_label} {rule_score_magnitude:.3f} "
                f">= policy threshold {self.min_rule_score_threshold:.3f}."
                if rule_score_magnitude is not None
                else "No decision-ready rule score is available."
            ),
        )
        checks.append(score_gate)
        checks.extend(
            [
                self._external_risk_evidence_gate(signal.external_risk_evidence),
                self._external_report_evidence_gate(signal.external_report_evidence),
                self._finrl_backtest_evidence_gate(research),
                self._qlib_factor_evidence_gate(research),
            ]
        )
        if not score_passed:
            score_gate["message"] = (
                "Signal is not decision-ready."
                if signal.decision_status != "ready"
                else "Rule score must be a finite number."
                if rule_score_magnitude is not None and not isfinite(rule_score_magnitude)
                else f"{score_label} {rule_score_magnitude:.3f} "
                f"< policy threshold {self.min_rule_score_threshold:.3f}."
                if rule_score_magnitude is not None
                else "No decision-ready rule score is available."
            )
            self._append_not_evaluated(checks, after="qlib_factor_evidence")
            return self._decision(
                approved=False,
                reason="Rule-score policy threshold was not met.",
                adjusted_position_size_pct=0.0,
                risk_notes=[
                    "Rejected by calibrated probability policy filter."
                    if calibrated_probability is not None
                    else "Rejected by uncalibrated rule-score policy filter."
                ],
                gate_checks=checks,
            )

        research_passed = (not self.require_backtest_passed) or research.passed
        checks.append(
            self._gate(
                code="research_validation",
                passed=research_passed,
                observed=research.passed,
                limit=True if self.require_backtest_passed else None,
                message="Research validation passed." if research_passed else "Research validation failed.",
            )
        )
        if not research_passed:
            self._append_not_evaluated(checks, after="research_validation")
            return self._decision(
                approved=False,
                reason="Research or backtest validation failed.",
                adjusted_position_size_pct=0.0,
                risk_notes=["Rejected by research validation."],
                gate_checks=checks,
            )

        drawdown_passed = research.max_drawdown_pct is None or drawdown_allowed(
            research.max_drawdown_pct,
            self.max_total_drawdown_pct,
        )
        checks.append(
            self._gate(
                code="max_drawdown",
                passed=drawdown_passed,
                observed=research.max_drawdown_pct,
                limit=self.max_total_drawdown_pct,
                message=(
                    "Backtest drawdown is within limit."
                    if drawdown_passed
                    else f"Drawdown {research.max_drawdown_pct:.2f}% > limit {self.max_total_drawdown_pct:.2f}%."
                ),
            )
        )
        if not drawdown_passed:
            self._append_not_evaluated(checks, after="max_drawdown")
            return self._decision(
                approved=False,
                reason="Backtest drawdown exceeds configured risk limit.",
                adjusted_position_size_pct=0.0,
                risk_notes=[
                    f"Drawdown {research.max_drawdown_pct:.2f}% > limit {self.max_total_drawdown_pct:.2f}%."
                ],
                gate_checks=checks,
            )

        # A position size is an execution instruction, never a convenient
        # default.  In particular, ``0`` is how the Strategy layer expresses
        # "do not size this idea".  Falling back to the policy maximum turns
        # an unknown/zero quantity into an unexpectedly large order.
        size_required = signal.action in {"buy", "add", "sell", "reduce"}
        requested_position_size = signal.position_size_pct
        try:
            normalized_position_size = (
                float(requested_position_size) if requested_position_size is not None else None
            )
        except (TypeError, ValueError):
            normalized_position_size = None
        sizing_passed = (
            not size_required
            or (
                normalized_position_size is not None
                and isfinite(normalized_position_size)
                and normalized_position_size > 0.0
            )
        )
        checks.append(
            self._gate(
                code="position_size_required",
                passed=sizing_passed,
                observed=requested_position_size,
                limit="> 0 for an executable action",
                message=(
                    "An explicit positive position size is present."
                    if sizing_passed
                    else "Executable actions require an explicit positive position size; no fallback is permitted."
                ),
            )
        )
        if not sizing_passed:
            self._append_not_evaluated(checks, after="position_size_required")
            return self._decision(
                approved=False,
                reason="Executable signal is missing an explicit positive position size.",
                adjusted_position_size_pct=0.0,
                risk_notes=["Rejected because zero or missing sizing must never default to the policy maximum."],
                gate_checks=checks,
            )

        requested_position_size = float(normalized_position_size or 0.0)
        position_size = cap_position_size(requested_position_size, self.max_position_size_pct)
        checks.append(
            self._gate(
                code="position_size_cap",
                passed=True,
                observed=requested_position_size,
                limit=self.max_position_size_pct,
                message=f"Adjusted position size is {position_size:.2f}%.",
                adjusted=position_size,
            )
        )

        stop_loss_passed = signal.action not in {"buy", "add"} or signal.stop_loss is not None
        checks.append(
            self._gate(
                code="stop_loss_required",
                passed=stop_loss_passed,
                observed=signal.stop_loss,
                limit="required for buy/add",
                message="Stop loss is present." if stop_loss_passed else "Buy/Add signal is missing stop loss.",
            )
        )
        if not stop_loss_passed:
            self._append_not_evaluated(checks, after="stop_loss_required")
            return self._decision(
                approved=False,
                reason="Buy/Add signal requires stop loss.",
                adjusted_position_size_pct=0.0,
                risk_notes=["Rejected because stop loss is missing."],
                gate_checks=checks,
            )

        estimated_loss_pct = self._estimated_loss_pct(signal, position_size)
        daily_loss_passed = estimated_loss_pct is None or estimated_loss_pct <= self.max_daily_loss_pct
        checks.append(
            self._gate(
                code="estimated_daily_loss",
                passed=daily_loss_passed,
                observed=estimated_loss_pct,
                limit=self.max_daily_loss_pct,
                message=(
                    "Estimated stop-loss exposure is within daily loss limit."
                    if daily_loss_passed
                    else f"Estimated loss {estimated_loss_pct:.2f}% > daily limit {self.max_daily_loss_pct:.2f}%."
                ),
            )
        )
        if not daily_loss_passed:
            self._append_not_evaluated(checks, after="estimated_daily_loss")
            return self._decision(
                approved=False,
                reason="Estimated stop-loss exposure exceeds configured daily loss limit.",
                adjusted_position_size_pct=0.0,
                risk_notes=[
                    f"Estimated loss {estimated_loss_pct:.2f}% > daily limit {self.max_daily_loss_pct:.2f}%."
                ],
                gate_checks=checks,
            )

        exposure_gate = self._portfolio_exposure_gate(
            request=request,
            signal=signal,
            position_size_pct=position_size,
            paper_exposure=paper_exposure,
        )
        checks.append(exposure_gate["check"])
        if not exposure_gate["check"]["passed"]:
            return self._decision(
                approved=False,
                reason=exposure_gate["reason"],
                adjusted_position_size_pct=0.0,
                risk_notes=exposure_gate["risk_notes"],
                gate_checks=checks,
            )

        exposure_notes = self._portfolio_exposure_notes(
            request=request,
            signal=signal,
            position_size_pct=position_size,
            paper_exposure=paper_exposure,
        )
        return self._decision(
            approved=True,
            reason="Approved for paper trading.",
            adjusted_position_size_pct=position_size,
            risk_notes=[
                "Paper trading only.",
                f"Configured max drawdown {self.max_total_drawdown_pct:.2f}%; daily loss {self.max_daily_loss_pct:.2f}%.",
                *exposure_notes,
            ],
            gate_checks=checks,
        )

    def evaluate_order_intent(
        self, *, intent: dict[str, Any], account_summary: dict[str, Any], evidence: dict[str, Any],
        mode: str = "paper", eligibility: str = "bounded_experiment", now: Any = None,
    ) -> dict[str, Any]:
        from .order_policy import evaluate_order_intent

        return evaluate_order_intent(self, intent=intent, account_summary=account_summary, evidence=evidence,
                                     mode=mode, eligibility=eligibility, now=now)

    def evaluate_order_preview(
        self,
        *,
        symbol: str,
        side: str,
        quantity_shares: float,
        reference_price: float,
        industry: str | None,
        account_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate a UI/Paper OMS order with the same central exposure policy.

        This preview does not bypass research approval or submit an order. It only
        answers whether the proposed notional, cash and portfolio exposure fit the
        configured RiskEngine limits.
        """
        normalized_side = str(side or "").lower()
        total_equity = max(self._number(account_summary.get("total_equity")) or 0.0, 0.0)
        cash_balance = max(self._number(account_summary.get("cash_balance")) or 0.0, 0.0)
        today_pnl = self._number(account_summary.get("today_pnl"))
        positions = account_summary.get("positions") if isinstance(account_summary.get("positions"), list) else []
        trade_value = max(0.0, float(quantity_shares or 0.0) * float(reference_price or 0.0))
        order_notional_pct = trade_value / total_equity * 100.0 if total_equity else 0.0
        current_total_value = sum(self._number(item.get("market_value")) or 0.0 for item in positions if isinstance(item, dict))
        current_symbol_value = sum(
            self._number(item.get("market_value")) or 0.0
            for item in positions
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol
        )
        current_industry_value = sum(
            self._number(item.get("market_value")) or 0.0
            for item in positions
            if isinstance(item, dict) and str(item.get("industry") or "") == str(industry or "")
        )
        current_symbol_quantity = sum(
            self._number(item.get("quantity")) or 0.0
            for item in positions
            if isinstance(item, dict) and str(item.get("symbol") or "") == symbol
        )
        is_buy = normalized_side == "buy"
        is_sell = normalized_side == "sell"
        delta_value = trade_value if is_buy else -min(current_symbol_value, trade_value) if is_sell else 0.0
        proposed_symbol_value = max(0.0, current_symbol_value + delta_value)
        proposed_total_value = max(0.0, current_total_value + delta_value)
        proposed_industry_value = max(0.0, current_industry_value + delta_value)
        proposed_symbol_pct = proposed_symbol_value / total_equity * 100.0 if total_equity else 0.0
        proposed_total_pct = proposed_total_value / total_equity * 100.0 if total_equity else 0.0
        proposed_industry_pct = proposed_industry_value / total_equity * 100.0 if total_equity else 0.0
        daily_loss_pct = abs(min(today_pnl or 0.0, 0.0)) / total_equity * 100.0 if total_equity else 0.0

        checks = [
            self._gate(
                code="paper_mode",
                passed=True,
                observed="paper",
                limit="paper_only",
                message="Central RiskEngine preview is paper-only.",
                severity="info",
            ),
            self._gate(
                code="cash_available",
                passed=(not is_buy) or trade_value <= cash_balance,
                observed=trade_value if is_buy else None,
                limit=cash_balance if is_buy else None,
                message="Paper cash is sufficient." if (not is_buy) or trade_value <= cash_balance else "Paper cash is insufficient.",
                applies=is_buy,
            ),
            self._gate(
                code="position_available",
                passed=(not is_sell) or current_symbol_quantity >= float(quantity_shares or 0.0),
                observed=current_symbol_quantity if is_sell else None,
                limit=float(quantity_shares or 0.0) if is_sell else None,
                message="Paper position is sufficient." if (not is_sell) or current_symbol_quantity >= float(quantity_shares or 0.0) else "Paper position is insufficient.",
                applies=is_sell,
            ),
            self._gate(
                code="order_notional",
                passed=order_notional_pct <= self.max_position_size_pct,
                observed=round(order_notional_pct, 4),
                limit=self.max_position_size_pct,
                message="Order notional is within the central position-size limit." if order_notional_pct <= self.max_position_size_pct else "Order notional exceeds the central position-size limit.",
            ),
            self._gate(
                code="symbol_exposure",
                passed=proposed_symbol_pct <= self.max_symbol_exposure_pct,
                observed=round(proposed_symbol_pct, 4),
                limit=self.max_symbol_exposure_pct,
                message="Proposed symbol exposure is within limit." if proposed_symbol_pct <= self.max_symbol_exposure_pct else "Proposed symbol exposure exceeds limit.",
            ),
            self._gate(
                code="total_exposure",
                passed=proposed_total_pct <= self.max_total_paper_exposure_pct,
                observed=round(proposed_total_pct, 4),
                limit=self.max_total_paper_exposure_pct,
                message="Proposed total paper exposure is within limit." if proposed_total_pct <= self.max_total_paper_exposure_pct else "Proposed total paper exposure exceeds limit.",
            ),
            self._gate(
                code="industry_exposure",
                passed=proposed_industry_pct <= self.max_industry_exposure_pct,
                observed=round(proposed_industry_pct, 4),
                limit=self.max_industry_exposure_pct,
                message="Proposed industry exposure is within the advisory limit." if proposed_industry_pct <= self.max_industry_exposure_pct else "Proposed industry exposure exceeds the advisory limit.",
                severity="warning",
            ),
            self._gate(
                code="daily_loss",
                passed=daily_loss_pct <= self.max_daily_loss_pct,
                observed=round(daily_loss_pct, 4),
                limit=self.max_daily_loss_pct,
                message="Observed daily paper loss is within limit." if daily_loss_pct <= self.max_daily_loss_pct else "Observed daily paper loss exceeds limit.",
                applies=today_pnl is not None,
            ),
        ]
        blocking_failures = [
            check for check in checks
            if check.get("applies") is not False and check.get("severity") == "block" and check.get("passed") is not True
        ]
        return {
            "schema_version": "open_stock_ai.order_risk_preview.v1",
            "method": "central_risk_engine_order_preview",
            "approved": not blocking_failures and normalized_side in {"buy", "sell"} and trade_value > 0,
            "symbol": symbol,
            "side": normalized_side,
            "metrics": {
                "trade_value": round(trade_value, 2),
                "order_notional_pct": round(order_notional_pct, 4),
                "current_daily_loss_pct": round(daily_loss_pct, 4),
                "proposed_symbol_exposure_pct": round(proposed_symbol_pct, 4),
                "proposed_total_exposure_pct": round(proposed_total_pct, 4),
                "proposed_industry_exposure_pct": round(proposed_industry_pct, 4),
            },
            "limits": {
                "max_position_size_pct": self.max_position_size_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_symbol_exposure_pct": self.max_symbol_exposure_pct,
                "max_total_paper_exposure_pct": self.max_total_paper_exposure_pct,
                "max_industry_exposure_pct": self.max_industry_exposure_pct,
            },
            "gate_checks": checks,
            "execution_boundary": "read_only_preview_requires_research_and_execution_pipeline_for_order",
        }

    def _estimated_loss_pct(self, signal: TradingSignal, position_size_pct: float) -> float | None:
        if position_size_pct <= 0 or signal.action == "hold":
            return 0.0
        if signal.entry_price is None or signal.stop_loss is None or signal.entry_price <= 0:
            return None
        stop_distance_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price * 100
        return position_size_pct * stop_distance_pct / 100

    def _portfolio_exposure_gate(
        self,
        *,
        request: StockRequest,
        signal: TradingSignal,
        position_size_pct: float,
        paper_exposure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not paper_exposure or signal.action not in {"buy", "add"} or position_size_pct <= 0:
            return {
                "check": self._gate(
                    code="paper_exposure",
                    passed=True,
                    observed=None,
                    limit={
                        "symbol": self.max_symbol_exposure_pct,
                        "total": self.max_total_paper_exposure_pct,
                    },
                    message="Paper exposure check not applicable.",
                    applies=False,
                ),
                "reason": "",
                "risk_notes": [],
            }
        current_symbol = self._symbol_exposure(request.symbol, paper_exposure)
        current_total = self._total_exposure(paper_exposure)
        proposed_symbol = current_symbol + position_size_pct
        proposed_total = current_total + position_size_pct
        if proposed_symbol > self.max_symbol_exposure_pct:
            return {
                "check": self._gate(
                    code="paper_exposure",
                    passed=False,
                    observed={"symbol": proposed_symbol, "total": proposed_total},
                    limit={
                        "symbol": self.max_symbol_exposure_pct,
                        "total": self.max_total_paper_exposure_pct,
                    },
                    message=f"Symbol exposure {proposed_symbol:.2f}% > limit {self.max_symbol_exposure_pct:.2f}%.",
                ),
                "reason": "Paper portfolio symbol exposure exceeds configured limit.",
                "risk_notes": [
                    f"Symbol exposure {proposed_symbol:.2f}% > limit {self.max_symbol_exposure_pct:.2f}%.",
                    f"Current {request.symbol} paper exposure is {current_symbol:.2f}%.",
                ],
            }
        if proposed_total > self.max_total_paper_exposure_pct:
            return {
                "check": self._gate(
                    code="paper_exposure",
                    passed=False,
                    observed={"symbol": proposed_symbol, "total": proposed_total},
                    limit={
                        "symbol": self.max_symbol_exposure_pct,
                        "total": self.max_total_paper_exposure_pct,
                    },
                    message=f"Total paper exposure {proposed_total:.2f}% > limit {self.max_total_paper_exposure_pct:.2f}%.",
                ),
                "reason": "Paper portfolio total exposure exceeds configured limit.",
                "risk_notes": [
                    f"Total paper exposure {proposed_total:.2f}% > limit {self.max_total_paper_exposure_pct:.2f}%.",
                    f"Current total paper exposure is {current_total:.2f}%.",
                ],
            }
        return {
            "check": self._gate(
                code="paper_exposure",
                passed=True,
                observed={"symbol": proposed_symbol, "total": proposed_total},
                limit={
                    "symbol": self.max_symbol_exposure_pct,
                    "total": self.max_total_paper_exposure_pct,
                },
                message="Paper exposure remains within configured limits.",
            ),
            "reason": "",
            "risk_notes": [],
        }

    def _portfolio_exposure_notes(
        self,
        *,
        request: StockRequest,
        signal: TradingSignal,
        position_size_pct: float,
        paper_exposure: dict[str, Any] | None,
    ) -> list[str]:
        if not paper_exposure or signal.action not in {"buy", "add"} or position_size_pct <= 0:
            return []
        current_symbol = self._symbol_exposure(request.symbol, paper_exposure)
        current_total = self._total_exposure(paper_exposure)
        return [
            (
                f"Paper exposure after order: {request.symbol} "
                f"{current_symbol + position_size_pct:.2f}%/{self.max_symbol_exposure_pct:.2f}%, "
                f"total {current_total + position_size_pct:.2f}%/{self.max_total_paper_exposure_pct:.2f}%."
            )
        ]

    def _symbol_exposure(self, symbol: str, paper_exposure: dict[str, Any]) -> float:
        symbols = paper_exposure.get("symbols") if isinstance(paper_exposure, dict) else {}
        item = symbols.get(symbol) if isinstance(symbols, dict) else {}
        try:
            return float((item or {}).get("position_size_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _total_exposure(self, paper_exposure: dict[str, Any]) -> float:
        try:
            return float(paper_exposure.get("total_position_size_pct") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _gate(
        self,
        *,
        code: str,
        passed: bool,
        observed: Any,
        limit: Any,
        message: str,
        severity: str = "block",
        applies: bool = True,
        adjusted: Any = None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "passed": passed,
            "applies": applies,
            "severity": severity,
            "observed": observed,
            "limit": limit,
            "adjusted": adjusted,
            "message": message,
        }

    def _append_not_evaluated(self, checks: list[dict[str, Any]], after: str) -> None:
        order = [
            "durable_risk_control",
            "rule_score_threshold",
            "tradingagents_risk_evidence",
            "finrobot_report_evidence",
            "finrl_backtest_evidence",
            "qlib_factor_evidence",
            "research_validation",
            "max_drawdown",
            "position_size_required",
            "position_size_cap",
            "stop_loss_required",
            "estimated_daily_loss",
            "paper_exposure",
        ]
        existing = {check.get("code") for check in checks}
        if after not in order:
            return
        for code in order[order.index(after) + 1 :]:
            if code in existing:
                continue
            checks.append(
                self._gate(
                    code=code,
                    passed=False,
                    observed=None,
                    limit=None,
                    message="Not evaluated because an earlier risk gate failed.",
                    applies=False,
                    severity="info",
                )
            )

    def _external_risk_evidence_gate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        schema_version = evidence.get("schema_version") if isinstance(evidence, dict) else None
        debator_count = evidence.get("risk_debator_count") if isinstance(evidence, dict) else None
        rating_scale = evidence.get("rating_scale") if isinstance(evidence, dict) else []
        passed = (
            schema_version == "open_stock_ai.tradingagents_risk_debate.v1"
            and isinstance(debator_count, int)
            and debator_count >= 3
            and isinstance(rating_scale, list)
            and len(rating_scale) >= 5
        )
        return self._gate(
            code="tradingagents_risk_evidence",
            passed=passed,
            observed={
                "schema_version": schema_version,
                "stance": evidence.get("stance") if isinstance(evidence, dict) else None,
                "risk_debator_count": debator_count,
                "rating_count": len(rating_scale) if isinstance(rating_scale, list) else 0,
            },
            limit={
                "schema_version": "open_stock_ai.tradingagents_risk_debate.v1",
                "risk_debator_count": 3,
                "rating_count": 5,
            },
            message=(
                "TradingAgents risk debate evidence is available."
                if passed
                else "TradingAgents risk debate evidence is missing or incomplete."
            ),
            severity="info",
        )

    def _external_report_evidence_gate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        schema_version = evidence.get("schema_version") if isinstance(evidence, dict) else None
        report_contract = evidence.get("report_contract") if isinstance(evidence, dict) else {}
        agent_contract = evidence.get("agent_contract") if isinstance(evidence, dict) else {}
        required_tools = agent_contract.get("required_tools_present") if isinstance(agent_contract, dict) else {}
        section_count = report_contract.get("section_count") if isinstance(report_contract, dict) else 0
        required_sections = report_contract.get("required_sections") if isinstance(report_contract, dict) else []
        passed = (
            schema_version == "open_stock_ai.finrobot_report_projection.v1"
            and isinstance(section_count, int)
            and section_count >= 6
            and isinstance(required_sections, list)
            and len(required_sections) >= 6
            and isinstance(required_tools, dict)
            and required_tools.get("analyze_income_stmt") is True
            and required_tools.get("get_risk_assessment") is True
        )
        return self._gate(
            code="finrobot_report_evidence",
            passed=passed,
            observed={
                "schema_version": schema_version,
                "report_view": evidence.get("report_view") if isinstance(evidence, dict) else None,
                "risk_view": evidence.get("risk_view") if isinstance(evidence, dict) else None,
                "valuation_view": evidence.get("valuation_view") if isinstance(evidence, dict) else None,
                "section_count": section_count,
                "tool_count": agent_contract.get("tool_count") if isinstance(agent_contract, dict) else 0,
            },
            limit={
                "schema_version": "open_stock_ai.finrobot_report_projection.v1",
                "section_count": 6,
                "required_tools": ["analyze_income_stmt", "get_risk_assessment"],
            },
            message=(
                "FinRobot report projection evidence is available."
                if passed
                else "FinRobot report projection evidence is missing or incomplete."
            ),
            severity="info",
        )

    def _finrl_backtest_evidence_gate(self, research: ResearchResult) -> dict[str, Any]:
        raw = research.raw if isinstance(research.raw, dict) else {}
        finrl = raw.get("finrl") if isinstance(raw.get("finrl"), dict) else {}
        evidence = finrl.get("backtest_projection") if isinstance(finrl.get("backtest_projection"), dict) else {}
        schema_version = evidence.get("schema_version") if isinstance(evidence, dict) else None
        metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else {}
        contract = evidence.get("contract") if isinstance(evidence.get("contract"), dict) else {}
        sharpe = self._number(metrics.get("sharpe"))
        drawdown = self._number(metrics.get("max_drawdown_pct"))
        projection_passed = (
            schema_version == "open_stock_ai.finrl_backtest_projection.v1"
            and evidence.get("method") == "local_finrl_backtest_contract_projection"
            and evidence.get("passed") is True
            and sharpe is not None
            and sharpe >= 1.0
            and (drawdown is None or drawdown <= self.max_total_drawdown_pct)
            and contract.get("has_backtest_engine") is True
            and contract.get("adaptive_rotation_file_count", 0) >= 1
            and evidence.get("execution_boundary") == "read_only_contract_no_finrl_runtime_import"
        )
        runtime = raw.get("model_runtime") if isinstance(raw.get("model_runtime"), dict) else {}
        finrl_runtime = runtime.get("finrl") if isinstance(runtime.get("finrl"), dict) else {}
        inference = finrl_runtime.get("inference") if isinstance(finrl_runtime.get("inference"), dict) else {}
        immutable = finrl_runtime.get("immutable_artifact") if isinstance(finrl_runtime.get("immutable_artifact"), dict) else {}
        inference_result = inference.get("result") if isinstance(inference.get("result"), dict) else {}
        inference_provenance = inference.get("model_provenance") if isinstance(inference.get("model_provenance"), dict) else {}
        policy_hash = str(immutable.get("policy_sha256") or "")
        runtime_passed = (
            runtime.get("schema_version") == "open_stock_ai.research_model_runtime.v1"
            and runtime.get("status") == "executed"
            and inference.get("schema_version") == "open_stock_ai.external_full_workflow.v1"
            and inference.get("project") == "finrl"
            and inference.get("executed_function") == "MODELS.load/model.predict"
            and inference.get("execution_boundary") == "external_compute_and_services_no_live_brokerage"
            and inference_provenance.get("policy_loaded") is True
            and inference_provenance.get("policy_inference_executed") is True
            and len(policy_hash) == 64
            and policy_hash == inference_result.get("policy_sha256")
            and immutable.get("refit_allowed") is False
        )
        passed = projection_passed or runtime_passed
        return self._gate(
            code="finrl_backtest_evidence",
            passed=passed,
            observed={
                "schema_version": schema_version,
                "passed": evidence.get("passed") if isinstance(evidence, dict) else None,
                "sharpe": sharpe,
                "max_drawdown_pct": drawdown,
                "has_backtest_engine": contract.get("has_backtest_engine"),
                "adaptive_rotation_file_count": contract.get("adaptive_rotation_file_count", 0),
                "runtime_executed": runtime_passed,
                "policy_sha256": policy_hash or None,
            },
            limit={
                "accepted_evidence": [
                    "open_stock_ai.finrl_backtest_projection.v1",
                    "open_stock_ai.research_model_runtime.v1",
                ],
                "min_sharpe": 1.0,
                "max_drawdown_pct": self.max_total_drawdown_pct,
                "has_backtest_engine": True,
            },
            message=(
                "FinRL projection or immutable real-model inference evidence is available."
                if passed
                else "FinRL backtest projection evidence is missing or below threshold."
            ),
            severity="info",
        )

    def _qlib_factor_evidence_gate(self, research: ResearchResult) -> dict[str, Any]:
        raw = research.raw if isinstance(research.raw, dict) else {}
        qlib = raw.get("qlib") if isinstance(raw.get("qlib"), dict) else {}
        evidence = qlib.get("factor_projection") if isinstance(qlib.get("factor_projection"), dict) else {}
        schema_version = evidence.get("schema_version") if isinstance(evidence, dict) else None
        score = self._number(evidence.get("score"))
        model_score = self._number(evidence.get("model_score"))
        rank_ic_proxy = self._number(evidence.get("rank_ic_proxy"))
        factors = evidence.get("factors") if isinstance(evidence.get("factors"), dict) else {}
        projection_passed = (
            schema_version == "open_stock_ai.qlib_factor_projection.v1"
            and evidence.get("method") == "local_qlib_factor_projection"
            and evidence.get("passed") is True
            and score is not None
            and model_score is not None
            and model_score >= 0.325
            and rank_ic_proxy is not None
            and rank_ic_proxy >= -0.20
            and isinstance(factors, dict)
            and "momentum_5d" in factors
            and "volatility_20d" in factors
            and bool(evidence.get("selected_model"))
            and evidence.get("workflow_count", 0) >= 1
            and evidence.get("workflow_summary_schema_version") == "open_stock_ai.qlib_workflow_summary.v1"
            and evidence.get("execution_boundary") == "read_only_contract_no_qlib_runtime_import"
        )
        runtime = raw.get("model_runtime") if isinstance(raw.get("model_runtime"), dict) else {}
        qlib_runtime = runtime.get("qlib") if isinstance(runtime.get("qlib"), dict) else {}
        workflow = qlib_runtime.get("workflow") if isinstance(qlib_runtime.get("workflow"), dict) else {}
        workflow_result = workflow.get("result") if isinstance(workflow.get("result"), dict) else {}
        workflow_provenance = workflow.get("model_provenance") if isinstance(workflow.get("model_provenance"), dict) else {}
        artifacts = workflow.get("artifacts") if isinstance(workflow.get("artifacts"), list) else []
        runtime_passed = (
            runtime.get("schema_version") == "open_stock_ai.research_model_runtime.v1"
            and runtime.get("status") == "executed"
            and qlib_runtime.get("dataset_manifest_hash") == (runtime.get("dataset") or {}).get("manifest_hash")
            and len(str(qlib_runtime.get("config_sha256") or "")) == 64
            and workflow.get("schema_version") == "open_stock_ai.external_full_workflow.v1"
            and workflow.get("project") == "qlib"
            and workflow.get("action") == "backtest_model"
            and workflow.get("executed_function") == "qlib.cli.run.workflow"
            and workflow.get("execution_boundary") == "external_compute_and_services_no_live_brokerage"
            and workflow_result.get("workflow_completed") is True
            and workflow_result.get("config_sha256") == qlib_runtime.get("config_sha256")
            and workflow_provenance.get("training_executed") is True
            and workflow_provenance.get("inference_executed") is True
            and len(artifacts) > 0
        )
        passed = projection_passed or runtime_passed
        return self._gate(
            code="qlib_factor_evidence",
            passed=passed,
            observed={
                "schema_version": schema_version,
                "passed": evidence.get("passed") if isinstance(evidence, dict) else None,
                "score": score,
                "model_score": model_score,
                "rank_ic_proxy": rank_ic_proxy,
                "selected_model": evidence.get("selected_model") if isinstance(evidence, dict) else None,
                "selected_dataset": evidence.get("selected_dataset") if isinstance(evidence, dict) else None,
                "workflow_count": evidence.get("workflow_count", 0) if isinstance(evidence, dict) else 0,
                "runtime_executed": runtime_passed,
                "runtime_artifact_count": len(artifacts),
            },
            limit={
                "accepted_evidence": [
                    "open_stock_ai.qlib_factor_projection.v1",
                    "open_stock_ai.research_model_runtime.v1",
                ],
                "method": "local_qlib_factor_projection",
                "min_model_score": 0.325,
                "min_rank_ic_proxy": -0.20,
                "workflow_summary_schema_version": "open_stock_ai.qlib_workflow_summary.v1",
            },
            message=(
                "Qlib projection or real Recorder workflow evidence is available."
                if passed
                else "Qlib factor projection evidence is missing or below threshold."
            ),
            severity="info",
        )

    def _decision(
        self,
        *,
        approved: bool,
        reason: str,
        adjusted_position_size_pct: float,
        risk_notes: list[str],
        gate_checks: list[dict[str, Any]],
    ) -> RiskDecision:
        return RiskDecision(
            approved=approved,
            reason=reason,
            max_position_size_pct=self.max_position_size_pct,
            adjusted_position_size_pct=adjusted_position_size_pct,
            risk_notes=risk_notes,
            gate_checks=gate_checks,
            policy={
                "min_rule_score_threshold": self.min_rule_score_threshold,
                "max_position_size_pct": self.max_position_size_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_total_drawdown_pct": self.max_total_drawdown_pct,
                "max_symbol_exposure_pct": self.max_symbol_exposure_pct,
                "max_total_paper_exposure_pct": self.max_total_paper_exposure_pct,
                "require_backtest_passed": self.require_backtest_passed,
                "live_trading_enabled": self.live_trading_enabled,
            },
        )

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
