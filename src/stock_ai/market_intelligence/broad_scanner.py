from __future__ import annotations

from typing import Any, Callable

from .candidate_ranker import rank_candidates
from .feature_store import load_all_taiwan_features
from .market_regime import classify_market_regime
from .portfolio_fit import current_paper_positions
from .scanner_factor_enrichment import enrich_features
from .product_projection import product_counts


class BroadScanner:
    """Deterministic scanner over the loaded market batch; no LLM is called."""

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
        positions = self.position_loader()
        # Product admission governs new exposure, not research visibility or
        # existing-position management. Unknown and non-stock rows stay here.
        features = all_features
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
                **product_counts(all_features),
            },
            "market_regime": market_regime,
            "candidate_details": details,
            "rankings": rankings,
            "portfolio_actions": portfolio_actions,
            "statistics": statistics,
        }
