from __future__ import annotations

"""Deterministic, evidence-bound sizing for paper portfolio proposals.

The old portfolio projection merely normalised independently approved weights.
That is useful as a display baseline, but it does not establish a portfolio risk
budget.  This module turns a supplied, point-in-time risk input into a compact
receipt.  It deliberately withholds a target when any input is missing instead
of manufacturing a weight from a score or an uncalibrated model projection.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from open_stock_ai.research.portfolio_risk_context import (
    verify_pit_portfolio_risk_context,
)


_SCHEMA = "open_stock_ai.portfolio_risk_sizing_receipt.v2"


@dataclass(frozen=True, slots=True)
class PortfolioRiskSizer:
    """Bound a proposed weight by volatility, loss budget and fractional Kelly."""

    target_portfolio_volatility_pct: float = 12.0
    default_risk_budget_pct: float = 1.0
    kelly_fraction_cap: float = 0.25

    def size(self, *, requested_weight_pct: Any, inputs: Any) -> dict[str, Any]:
        requested = self._positive(requested_weight_pct)
        payload = dict(inputs) if isinstance(inputs, dict) else {}
        volatility = self._positive(payload.get("annualized_volatility_pct"))
        win_probability = self._probability(payload.get("expected_win_probability"))
        expected_win = self._positive(payload.get("expected_win_pct"))
        expected_loss = self._positive(payload.get("expected_loss_pct"))
        stop_loss = self._positive(payload.get("max_loss_pct"))
        risk_budget = self._positive(payload.get("risk_budget_pct"))
        blockers: list[str] = []
        required = {
            "requested_weight_pct": requested,
            "annualized_volatility_pct": volatility,
            "expected_win_probability": win_probability,
            "expected_win_pct": expected_win,
            "expected_loss_pct": expected_loss,
            "max_loss_pct": stop_loss,
            "risk_budget_pct": risk_budget,
        }
        blockers.extend(f"portfolio_risk_input_missing:{key}" for key, value in required.items() if value is None)
        blockers.extend(self._pit_identity_blockers(payload))
        if blockers:
            return self._receipt(
                status="withheld",
                requested_weight_pct=requested or 0.0,
                target_weight_pct=0.0,
                inputs=self._clean_inputs(payload),
                blockers=blockers,
            )

        assert requested is not None and volatility is not None and win_probability is not None
        assert expected_win is not None and expected_loss is not None and stop_loss is not None
        payoff_ratio = expected_win / expected_loss
        raw_kelly_fraction = win_probability - ((1.0 - win_probability) / payoff_ratio)
        capped_kelly_fraction = min(max(raw_kelly_fraction, 0.0), self.kelly_fraction_cap)
        volatility_cap = self.target_portfolio_volatility_pct / volatility * 100.0
        loss_budget_cap = risk_budget / stop_loss * 100.0
        kelly_cap = capped_kelly_fraction * 100.0
        target = min(requested, volatility_cap, loss_budget_cap, kelly_cap)
        blockers = [] if target > 0 else ["portfolio_risk_sizing_nonpositive_after_caps"]
        return self._receipt(
            status="verified" if not blockers else "withheld",
            requested_weight_pct=requested,
            target_weight_pct=max(0.0, target),
            inputs={
                **self._clean_inputs(payload),
                "annualized_volatility_pct": volatility,
                "expected_win_probability": win_probability,
                "expected_win_pct": expected_win,
                "expected_loss_pct": expected_loss,
                "max_loss_pct": stop_loss,
                "risk_budget_pct": risk_budget,
            },
            caps={
                "volatility_target_cap_pct": volatility_cap,
                "max_loss_budget_cap_pct": loss_budget_cap,
                "fractional_kelly_cap_pct": kelly_cap,
                "raw_kelly_fraction": raw_kelly_fraction,
                "payoff_ratio": payoff_ratio,
            },
            blockers=blockers,
        )

    def _receipt(
        self,
        *,
        status: str,
        requested_weight_pct: float,
        target_weight_pct: float,
        inputs: dict[str, Any],
        blockers: list[str],
        caps: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        receipt = {
            "schema_version": _SCHEMA,
            "status": status,
            "requested_weight_pct": round(requested_weight_pct, 6),
            "target_weight_pct": round(target_weight_pct, 6),
            "target_portfolio_volatility_pct": self.target_portfolio_volatility_pct,
            "default_risk_budget_pct": self.default_risk_budget_pct,
            "kelly_fraction_cap": self.kelly_fraction_cap,
            "inputs": inputs,
            "caps": {key: round(value, 6) for key, value in (caps or {}).items()},
            "blockers": blockers,
            "execution_authority": "none",
            "execution_boundary": "paper_research_sizing_only_no_order_authority",
        }
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        receipt["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return receipt

    @staticmethod
    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) and number > 0 else None

    @staticmethod
    def _probability(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) and 0.0 < number < 1.0 else None

    @staticmethod
    def _clean_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "annualized_volatility_pct", "expected_win_probability", "expected_win_pct",
            "expected_loss_pct", "max_loss_pct", "risk_budget_pct", "as_of", "dataset_manifest_hash",
            "risk_context_status", "risk_context_receipt_sha256",
            "account_risk_status", "account_risk_receipt_sha256",
        }
        return {key: inputs[key] for key in sorted(allowed & set(inputs))}

    @staticmethod
    def _pit_identity_blockers(inputs: dict[str, Any]) -> list[str]:
        """Require the actual sizing inputs to be bound to a PIT risk context.

        A numerical volatility or loss budget without an as-of dataset and an
        account-risk receipt is not evidence that the value was available to
        the decision.  In particular, never silently use the class defaults
        as if they were a current portfolio limit.
        """

        blockers: list[str] = []
        full_context = inputs.get("_full_portfolio_risk_context")
        if isinstance(full_context, dict) and full_context.get("schema_version") == "open_stock_ai.pit_portfolio_risk_context.v1" and not verify_pit_portfolio_risk_context(full_context):
            blockers.append("portfolio_risk_context_receipt_invalid")
        as_of = str(inputs.get("as_of") or "")
        try:
            datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            blockers.append("portfolio_risk_context_as_of_missing_or_invalid")
        for field_name in (
            "dataset_manifest_hash",
            "risk_context_receipt_sha256",
            "account_risk_receipt_sha256",
        ):
            value = str(inputs.get(field_name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                blockers.append(f"portfolio_risk_context_hash_missing:{field_name}")
        if inputs.get("risk_context_status") != "verified":
            blockers.append("portfolio_risk_context_not_verified")
        if inputs.get("account_risk_status") != "verified":
            blockers.append("portfolio_account_risk_not_verified")
        return blockers
