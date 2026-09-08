from __future__ import annotations

"""Small deterministic covariance-aware optimizer for paper portfolio research.

It intentionally receives a fully materialised point-in-time risk context.
Without a verified covariance matrix, factor exposure matrix and dataset
identity, it returns no targets.  This prevents the UI's portfolio card from
turning separately safe single-name caps into an unverified portfolio proposal.
"""

import hashlib
import json
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Any

from open_stock_ai.research.portfolio_risk_context import (
    verify_pit_portfolio_risk_context,
)


_SCHEMA = "open_stock_ai.covariance_portfolio_optimization_receipt.v1"


@dataclass(frozen=True, slots=True)
class CovariancePortfolioOptimizer:
    target_portfolio_volatility_pct: float = 12.0
    max_symbol_weight_pct: float = 20.0
    max_total_weight_pct: float = 100.0

    def optimize(self, positions: list[dict[str, Any]], context: Any) -> dict[str, Any]:
        symbols = [str(item.get("symbol") or "").strip().upper() for item in positions]
        candidates = {
            symbol: self._positive(item.get("risk_sized_weight_pct")) or 0.0
            for symbol, item in zip(symbols, positions)
            if symbol
        }
        raw = dict(context) if isinstance(context, dict) else {}
        blockers = self._validate_context(symbols, candidates, raw)
        if blockers:
            return self._receipt(
                status="withheld",
                symbols=symbols,
                weights={symbol: 0.0 for symbol in symbols},
                context=raw,
                blockers=blockers,
            )

        covariance = raw["covariance_matrix"]
        factor_exposures = raw["factor_exposures"]
        factor_limits = raw["factor_limits"]
        target_volatility = self._positive(raw.get("target_portfolio_volatility_pct")) or self.target_portfolio_volatility_pct
        symbol_cap = min(
            self._positive(raw.get("max_symbol_weight_pct")) or self.max_symbol_weight_pct,
            self.max_symbol_weight_pct,
        )
        total_cap = min(
            self._positive(raw.get("max_total_weight_pct")) or self.max_total_weight_pct,
            self.max_total_weight_pct,
        )
        weights = {symbol: min(candidates[symbol], symbol_cap) / 100.0 for symbol in symbols}
        total_weight = sum(weights.values())
        if total_weight > total_cap / 100.0:
            scale = (total_cap / 100.0) / total_weight
            weights = {symbol: value * scale for symbol, value in weights.items()}
        portfolio_volatility = self._portfolio_volatility(weights, symbols, covariance)
        if portfolio_volatility > target_volatility / 100.0:
            scale = (target_volatility / 100.0) / portfolio_volatility
            weights = {symbol: value * scale for symbol, value in weights.items()}

        # Exposure constraints are applied only to holdings that increase the
        # breached direction. Scaling those terms cannot increase any absolute
        # linear factor exposure and preserves every individual risk-sized cap.
        for factor, limit in sorted(factor_limits.items()):
            limit_value = self._positive(limit)
            if limit_value is None:
                continue
            exposure = sum(weights[symbol] * float(factor_exposures[symbol][factor]) for symbol in symbols)
            if abs(exposure) > limit_value:
                scale = limit_value / abs(exposure)
                for symbol in symbols:
                    contribution = float(factor_exposures[symbol][factor]) * exposure
                    if contribution > 0:
                        weights[symbol] *= scale

        final_volatility = self._portfolio_volatility(weights, symbols, covariance)
        final_factor_exposures = {
            factor: sum(weights[symbol] * float(factor_exposures[symbol][factor]) for symbol in symbols)
            for factor in sorted(factor_limits)
        }
        violations = [
            f"portfolio_factor_limit_breached:{factor}"
            for factor, value in final_factor_exposures.items()
            if abs(value) > float(factor_limits[factor]) + 1e-12
        ]
        if final_volatility > target_volatility / 100.0 + 1e-12:
            violations.append("portfolio_volatility_target_breached")
        return self._receipt(
            status="verified" if not violations else "withheld",
            symbols=symbols,
            weights={symbol: value * 100.0 if not violations else 0.0 for symbol, value in weights.items()},
            context=raw,
            blockers=violations,
            metrics={
                "target_portfolio_volatility_pct": target_volatility,
                "portfolio_volatility_pct": final_volatility * 100.0,
                "max_symbol_weight_pct": symbol_cap,
                "max_total_weight_pct": total_cap,
                "total_weight_pct": sum(weights.values()) * 100.0,
                "factor_exposures": final_factor_exposures,
                "factor_limits": {str(key): float(value) for key, value in factor_limits.items()},
            },
        )

    def _validate_context(self, symbols: list[str], candidates: dict[str, float], context: dict[str, Any]) -> list[str]:
        blockers: list[str] = []
        if not symbols or len(set(symbols)) != len(symbols):
            blockers.append("portfolio_symbols_missing_or_duplicate")
        if not any(candidates.values()):
            blockers.append("portfolio_no_risk_sized_candidates")
        if len(str(context.get("dataset_manifest_hash") or "")) != 64:
            blockers.append("portfolio_dataset_manifest_hash_missing")
        if not str(context.get("as_of") or "").strip():
            blockers.append("portfolio_as_of_missing")
        if context.get("risk_context_status") != "verified":
            blockers.append("portfolio_risk_context_not_verified")
        if context.get("account_risk_status") != "verified":
            blockers.append("portfolio_account_risk_not_verified")
        if context.get("schema_version") == "open_stock_ai.pit_portfolio_risk_context.v1" and not verify_pit_portfolio_risk_context(context):
            blockers.append("portfolio_risk_context_receipt_invalid")
        for field_name in ("risk_context_receipt_sha256", "account_risk_receipt_sha256"):
            value = str(context.get(field_name) or "")
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                blockers.append(f"portfolio_risk_context_hash_missing:{field_name}")
        covariance = context.get("covariance_matrix")
        factors = context.get("factor_exposures")
        limits = context.get("factor_limits")
        if not isinstance(covariance, dict):
            blockers.append("portfolio_covariance_matrix_missing")
        if not isinstance(factors, dict):
            blockers.append("portfolio_factor_exposures_missing")
        if not isinstance(limits, dict) or not limits:
            blockers.append("portfolio_factor_limits_missing")
        if blockers:
            return blockers
        for symbol in symbols:
            row = covariance.get(symbol)
            if not isinstance(row, dict):
                blockers.append(f"portfolio_covariance_row_missing:{symbol}")
                continue
            if self._positive(row.get(symbol)) is None:
                blockers.append(f"portfolio_covariance_diagonal_invalid:{symbol}")
            exposure = factors.get(symbol)
            if not isinstance(exposure, dict):
                blockers.append(f"portfolio_factor_exposure_row_missing:{symbol}")
                continue
            for factor in limits:
                if self._finite(exposure.get(factor)) is None:
                    blockers.append(f"portfolio_factor_exposure_missing:{symbol}:{factor}")
            for other in symbols:
                left = self._finite(row.get(other))
                right = self._finite((covariance.get(other) or {}).get(symbol))
                if left is None or right is None or abs(left - right) > 1e-12:
                    blockers.append(f"portfolio_covariance_not_symmetric:{symbol}:{other}")
        return sorted(set(blockers))

    @staticmethod
    def _portfolio_volatility(weights: dict[str, float], symbols: list[str], covariance: dict[str, Any]) -> float:
        variance = sum(
            weights[left] * weights[right] * float(covariance[left][right])
            for left in symbols
            for right in symbols
        )
        return sqrt(max(0.0, variance))

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
            "dataset_manifest_hash": str(context.get("dataset_manifest_hash") or ""),
            "as_of": str(context.get("as_of") or ""),
            "metrics": self._round_nested(metrics or {}),
            "blockers": blockers,
            "execution_authority": "none",
            "execution_boundary": "paper_research_optimization_only_no_order_authority",
        }
        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        receipt["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return receipt

    @staticmethod
    def _positive(value: Any) -> float | None:
        number = CovariancePortfolioOptimizer._finite(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) else None

    @staticmethod
    def _round_nested(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): CovariancePortfolioOptimizer._round_nested(item) for key, item in value.items()}
        if isinstance(value, float):
            return round(value, 6)
        return value
