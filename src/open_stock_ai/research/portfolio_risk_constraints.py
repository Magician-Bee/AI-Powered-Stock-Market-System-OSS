from __future__ import annotations

"""Portfolio-level risk constraints for paper research targets.

The single-name risk sizer and covariance optimizer are necessary but not
sufficient for a portfolio decision.  This engine independently verifies the
portfolio's concentration, liquidity, tail loss and stress limits against one
point-in-time account context.  It is deliberately fail-closed: missing
metadata produces a withheld receipt rather than a permissive default.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, sqrt
from statistics import NormalDist
from typing import Any

from open_stock_ai.research.portfolio_risk_context import (
    verify_pit_portfolio_risk_context,
)


_SCHEMA = "open_stock_ai.portfolio_risk_constraints_receipt.v1"
_CONCENTRATION_KEYS = ("issuer", "industry", "factor", "currency", "broker", "account")


@dataclass(frozen=True, slots=True)
class PortfolioRiskConstraintEngine:
    """Verify independent portfolio concentration and tail-risk constraints."""

    minimum_return_observations: int = 20

    def evaluate(
        self,
        positions: list[dict[str, Any]],
        target_weights_pct: dict[str, Any],
        context: Any,
        *,
        optimizer_status: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(context) if isinstance(context, dict) else {}
        symbols = [str(item.get("symbol") or "").strip().upper() for item in positions]
        weights = {symbol: self._finite(target_weights_pct.get(symbol)) or 0.0 for symbol in symbols}
        blockers = self._validate_context(symbols, payload, optimizer_status)
        if blockers:
            return self._receipt(
                status="withheld",
                symbols=symbols,
                weights={symbol: 0.0 for symbol in symbols},
                context=payload,
                blockers=blockers,
            )

        metadata = payload["position_risk_metadata"]
        limits = payload["concentration_limits"]
        account_equity = float(payload["account_equity"])
        factors = payload["factor_exposures"]
        factor_limits = payload["factor_limits"]
        concentration = self._concentration_metrics(symbols, weights, metadata, factors)
        liquidity = self._liquidity_metrics(
            symbols,
            weights,
            metadata,
            account_equity=account_equity,
            max_participation_rate=float(payload["max_participation_rate"]),
            max_days_to_liquidate=float(payload["max_days_to_liquidate"]),
        )
        cvar = self._cvar_metrics(
            symbols,
            weights,
            payload["historical_returns"],
            confidence=float(payload["cvar_confidence"]),
        )
        stress = self._stress_metrics(symbols, weights, payload["stress_scenarios"])

        for dimension, exposure in concentration["exposures"].items():
            limit = float(limits[dimension])
            for group, value in exposure.items():
                if abs(value) > limit + 1e-12:
                    blockers.append(f"portfolio_{dimension}_concentration_limit_breached:{group}")
        for item in liquidity["symbols"]:
            if item["participation_rate"] > liquidity["max_participation_rate"] + 1e-12:
                blockers.append(f"portfolio_participation_limit_breached:{item['symbol']}")
            if item["days_to_liquidate"] > liquidity["max_days_to_liquidate"] + 1e-12:
                blockers.append(f"portfolio_days_to_liquidate_limit_breached:{item['symbol']}")
        max_cvar = float(payload["max_cvar_pct"])
        if cvar["historical_cvar_pct"] > max_cvar + 1e-12:
            blockers.append("portfolio_historical_cvar_limit_breached")
        if cvar["parametric_cvar_pct"] > max_cvar + 1e-12:
            blockers.append("portfolio_parametric_cvar_limit_breached")
        max_stress_loss = float(payload["max_stress_loss_pct"])
        if stress["worst_loss_pct"] > max_stress_loss + 1e-12:
            blockers.append("portfolio_stress_loss_limit_breached")
        return self._receipt(
            status="verified" if not blockers else "withheld",
            symbols=symbols,
            weights=weights if not blockers else {symbol: 0.0 for symbol in symbols},
            context=payload,
            blockers=sorted(set(blockers)),
            metrics={
                "concentration": concentration,
                "liquidity": liquidity,
                "cvar": cvar,
                "stress": stress,
                "limits": {
                    "concentration": {key: float(limits[key]) for key in _CONCENTRATION_KEYS},
                    "max_cvar_pct": max_cvar,
                    "max_stress_loss_pct": max_stress_loss,
                },
            },
        )

    def _validate_context(
        self,
        symbols: list[str],
        context: dict[str, Any],
        optimizer_status: str | None,
    ) -> list[str]:
        blockers: list[str] = []
        if optimizer_status != "verified":
            blockers.append("portfolio_optimizer_not_verified")
        if not symbols or len(set(symbols)) != len(symbols):
            blockers.append("portfolio_symbols_missing_or_duplicate")
        as_of = str(context.get("as_of") or "")
        try:
            datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            blockers.append("portfolio_risk_context_as_of_missing_or_invalid")
        for name in (
            "dataset_manifest_hash",
            "risk_context_receipt_sha256",
            "account_risk_receipt_sha256",
        ):
            value = str(context.get(name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                blockers.append(f"portfolio_risk_context_hash_missing:{name}")
        if context.get("risk_context_status") != "verified":
            blockers.append("portfolio_risk_context_not_verified")
        if context.get("account_risk_status") != "verified":
            blockers.append("portfolio_account_risk_not_verified")
        if context.get("schema_version") == "open_stock_ai.pit_portfolio_risk_context.v1" and not verify_pit_portfolio_risk_context(context):
            blockers.append("portfolio_risk_context_receipt_invalid")

        metadata = context.get("position_risk_metadata")
        if not isinstance(metadata, dict):
            blockers.append("portfolio_position_risk_metadata_missing")
        else:
            for symbol in symbols:
                item = metadata.get(symbol)
                if not isinstance(item, dict):
                    blockers.append(f"portfolio_position_risk_metadata_missing:{symbol}")
                    continue
                for key in ("issuer_id", "industry", "currency", "broker_id", "account_alias"):
                    if not str(item.get(key) or "").strip():
                        blockers.append(f"portfolio_position_metadata_missing:{symbol}:{key}")
                if self._positive(item.get("adv_value")) is None:
                    blockers.append(f"portfolio_adv_missing_or_invalid:{symbol}")

        limits = context.get("concentration_limits")
        if not isinstance(limits, dict):
            blockers.append("portfolio_concentration_limits_missing")
        else:
            for key in _CONCENTRATION_KEYS:
                if self._positive(limits.get(key)) is None:
                    blockers.append(f"portfolio_concentration_limit_missing:{key}")
        for key in ("account_equity", "max_participation_rate", "max_days_to_liquidate", "max_cvar_pct", "max_stress_loss_pct"):
            if self._positive(context.get(key)) is None:
                blockers.append(f"portfolio_risk_constraint_input_missing:{key}")
        participation = self._positive(context.get("max_participation_rate"))
        if participation is not None and participation > 1.0:
            blockers.append("portfolio_max_participation_rate_invalid")
        confidence = self._finite(context.get("cvar_confidence"))
        if confidence is None or not 0.5 < confidence < 1.0:
            blockers.append("portfolio_cvar_confidence_invalid")

        factors = context.get("factor_exposures")
        factor_limits = context.get("factor_limits")
        if not isinstance(factors, dict) or not isinstance(factor_limits, dict) or not factor_limits:
            blockers.append("portfolio_factor_risk_context_missing")
        else:
            for symbol in symbols:
                row = factors.get(symbol)
                if not isinstance(row, dict):
                    blockers.append(f"portfolio_factor_exposure_missing:{symbol}")
                    continue
                for factor in factor_limits:
                    if self._finite(row.get(factor)) is None:
                        blockers.append(f"portfolio_factor_exposure_missing:{symbol}:{factor}")

        returns = context.get("historical_returns")
        if not isinstance(returns, dict):
            blockers.append("portfolio_historical_returns_missing")
        else:
            lengths: set[int] = set()
            for symbol in symbols:
                values = returns.get(symbol)
                if not isinstance(values, list) or len(values) < self.minimum_return_observations:
                    blockers.append(f"portfolio_historical_returns_insufficient:{symbol}")
                    continue
                lengths.add(len(values))
                if any(self._finite(value) is None for value in values):
                    blockers.append(f"portfolio_historical_returns_invalid:{symbol}")
            if len(lengths) > 1:
                blockers.append("portfolio_historical_returns_length_mismatch")
        scenarios = context.get("stress_scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            blockers.append("portfolio_stress_scenarios_missing")
        elif any(not isinstance(item, dict) or not str(item.get("scenario_id") or "").strip() for item in scenarios):
            blockers.append("portfolio_stress_scenario_invalid")
        return sorted(set(blockers))

    def _concentration_metrics(
        self,
        symbols: list[str],
        weights: dict[str, float],
        metadata: dict[str, Any],
        factors: dict[str, Any],
    ) -> dict[str, Any]:
        exposures: dict[str, dict[str, float]] = {key: {} for key in _CONCENTRATION_KEYS}
        for symbol in symbols:
            item = metadata[symbol]
            weight = weights[symbol]
            for dimension, field in (
                ("issuer", "issuer_id"),
                ("industry", "industry"),
                ("currency", "currency"),
                ("broker", "broker_id"),
                ("account", "account_alias"),
            ):
                group = str(item[field])
                exposures[dimension][group] = exposures[dimension].get(group, 0.0) + weight
            for factor, value in factors[symbol].items():
                if self._finite(value) is not None:
                    exposures["factor"][str(factor)] = exposures["factor"].get(str(factor), 0.0) + weight / 100.0 * float(value)
        return {"exposures": self._round_nested(exposures)}

    def _liquidity_metrics(
        self,
        symbols: list[str],
        weights: dict[str, float],
        metadata: dict[str, Any],
        *,
        account_equity: float,
        max_participation_rate: float,
        max_days_to_liquidate: float,
    ) -> dict[str, Any]:
        rows = []
        for symbol in symbols:
            adv = float(metadata[symbol]["adv_value"])
            order_notional = max(0.0, weights[symbol] / 100.0 * account_equity)
            rows.append(
                {
                    "symbol": symbol,
                    "adv_value": round(adv, 6),
                    "order_notional": round(order_notional, 6),
                    "participation_rate": round(order_notional / adv, 8),
                    "days_to_liquidate": round(order_notional / adv, 8),
                }
            )
        return {
            "max_participation_rate": max_participation_rate,
            "max_days_to_liquidate": max_days_to_liquidate,
            "symbols": rows,
        }

    def _cvar_metrics(
        self,
        symbols: list[str],
        weights: dict[str, float],
        returns: dict[str, list[Any]],
        *,
        confidence: float,
    ) -> dict[str, Any]:
        portfolio_returns = [
            sum(weights[symbol] / 100.0 * float(returns[symbol][index]) for symbol in symbols)
            for index in range(len(returns[symbols[0]]))
        ]
        sorted_returns = sorted(portfolio_returns)
        tail_count = max(1, int((1.0 - confidence) * len(sorted_returns) + 0.999999))
        historical_loss = -sum(sorted_returns[:tail_count]) / tail_count * 100.0
        mean = sum(portfolio_returns) / len(portfolio_returns)
        variance = sum((value - mean) ** 2 for value in portfolio_returns) / max(1, len(portfolio_returns) - 1)
        sigma = sqrt(max(0.0, variance))
        z = NormalDist().inv_cdf(1.0 - confidence)
        tail_mean = NormalDist().pdf(z) / (1.0 - confidence)
        parametric_loss = -(mean - sigma * tail_mean) * 100.0
        return {
            "confidence": confidence,
            "observation_count": len(portfolio_returns),
            "historical_cvar_pct": round(max(0.0, historical_loss), 8),
            "parametric_cvar_pct": round(max(0.0, parametric_loss), 8),
            "portfolio_mean_return": round(mean, 8),
            "portfolio_return_stddev": round(sigma, 8),
        }

    def _stress_metrics(
        self,
        symbols: list[str],
        weights: dict[str, float],
        scenarios: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows = []
        for scenario in scenarios:
            shocks = scenario.get("shocks") if isinstance(scenario.get("shocks"), dict) else {}
            portfolio_return = sum(
                weights[symbol] / 100.0 * (self._finite(shocks.get(symbol)) or 0.0)
                for symbol in symbols
            )
            rows.append(
                {
                    "scenario_id": str(scenario["scenario_id"]),
                    "portfolio_return": round(portfolio_return, 8),
                    "loss_pct": round(max(0.0, -portfolio_return * 100.0), 8),
                }
            )
        worst = max((row["loss_pct"] for row in rows), default=0.0)
        return {"worst_loss_pct": worst, "scenarios": rows}

    def _receipt(
        self,
        *,
        status: str,
        symbols: list[str],
        weights: dict[str, float],
        context: dict[str, Any],
        blockers: list[str],
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt = {
            "schema_version": _SCHEMA,
            "status": status,
            "symbols": symbols,
            "target_weights_pct": {symbol: round(float(weights.get(symbol, 0.0)), 6) for symbol in symbols},
            "as_of": str(context.get("as_of") or ""),
            "dataset_manifest_hash": str(context.get("dataset_manifest_hash") or ""),
            "metrics": self._round_nested(metrics or {}),
            "blockers": sorted(set(blockers)),
            "execution_authority": "none",
            "execution_boundary": "paper_research_portfolio_risk_only_no_order_authority",
        }
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        receipt["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return receipt

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) else None

    @classmethod
    def _positive(cls, value: Any) -> float | None:
        number = cls._finite(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _round_nested(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): PortfolioRiskConstraintEngine._round_nested(item) for key, item in value.items()}
        if isinstance(value, list):
            return [PortfolioRiskConstraintEngine._round_nested(item) for item in value]
        if isinstance(value, float):
            return round(value, 8)
        return value
