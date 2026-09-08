from __future__ import annotations

from typing import Any, Callable

from .candidate_ranker import rank_candidates
from .feature_store import load_all_taiwan_features
from .market_regime import classify_market_regime
from .portfolio_fit import current_paper_positions
from .scanner_factor_enrichment import enrich_features


class BroadScanner:
    """Deterministic whole-market scanner; no language model is called."""

    def __init__(
        self,
        *,
        feature_loader: Callable[[], list[dict[str, Any]]] = load_all_taiwan_features,
        feature_enricher: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] = enrich_features,
        position_loader: Callable[[], dict[str, dict[str, Any]]] = current_paper_positions,
    ) -> None:
        self.feature_loader = feature_loader
        self.feature_enricher = feature_enricher
        self.position_loader = position_loader

    def scan(
        self,
        *,
        previous_ranks: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        all_features = self.feature_enricher(self.feature_loader())
        investable_features = [
            item for item in all_features
            if not item.get("is_warrant")
            and not item.get("is_managed_stock")
            and not item.get("is_special_security")
            and not item.get("is_etf")
        ]
        positions = self.position_loader()
        # ETFs are not new-stock candidates, but an already held ETF still
        # needs the same visible portfolio risk / add / reduce / exit review.
        # Keep the two universes distinct instead of silently dropping a held
        # product from the portfolio workspace.
        held_symbols = {str(symbol).upper() for symbol in positions}
        held_non_candidate_features = [
            item for item in all_features
            if str(item.get("symbol") or "").upper() in held_symbols
            and item not in investable_features
            and not item.get("is_warrant")
            and not item.get("is_managed_stock")
            and not item.get("is_special_security")
        ]
        features = [*investable_features, *held_non_candidate_features]
        market_regime = classify_market_regime(features)
        details, rankings, portfolio_actions, statistics = rank_candidates(
            features,
            positions=positions,
            previous_ranks=previous_ranks,
            market_regime=market_regime.label,
        )
        return {
            "features": features,
            "all_features": all_features,
            "universe_breakdown": {
                "security_master_count": len(all_features),
                "ordinary_stock_count": sum(1 for item in all_features if not item.get("is_etf") and not item.get("is_warrant")),
                "etf_count": sum(1 for item in all_features if item.get("is_etf")),
                "excluded_product_count": len(all_features) - len(investable_features),
                "investable_count": len(investable_features),
            },
            "market_regime": market_regime,
            "candidate_details": details,
            "rankings": rankings,
            "portfolio_actions": portfolio_actions,
            "statistics": statistics,
        }
