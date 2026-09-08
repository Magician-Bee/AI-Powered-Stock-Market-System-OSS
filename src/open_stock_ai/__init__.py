"""Open Stock AI integration layer."""

from .types import StockDecision, StockRequest

__all__ = ["OpenStockAIEngine", "StockDecision", "StockRequest"]


def __getattr__(name: str):
    """Keep the public engine export without importing the whole pipeline eagerly."""
    if name == "OpenStockAIEngine":
        from .engine import OpenStockAIEngine

        return OpenStockAIEngine
    raise AttributeError(name)
