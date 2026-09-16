from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PortfolioAttribution:
    def build(
        self,
        review: dict[str, Any],
        *,
        source: str = "decision_review",
    ) -> dict[str, Any]:
        items = review.get("items") if isinstance(review.get("items"), list) else []
        symbols: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "UNKNOWN")
            bucket = symbols.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": item.get("market"),
                    "decision_count": 0,
                    "approved_count": 0,
                    "executed_count": 0,
                    "blocked_count": 0,
                    "confidence_sum": 0.0,
                    "confidence_count": 0,
                    "proposed_position_size_pct": 0.0,
                    "ratings": {},
                    "trader_actions": {},
                    "signal_actions": {},
                    "latest_lesson": None,
                },
            )
            bucket["decision_count"] += 1
            if item.get("risk_approved") is True:
                bucket["approved_count"] += 1
            else:
                bucket["blocked_count"] += 1
            if item.get("executed") is True:
                bucket["executed_count"] += 1
            confidence = self._number(item.get("confidence"))
            if confidence is not None:
                bucket["confidence_sum"] += confidence
                bucket["confidence_count"] += 1
            size = self._number(item.get("position_size_pct"))
            if size is not None and item.get("risk_approved") is True:
                bucket["proposed_position_size_pct"] += size
            self._count(bucket["ratings"], item.get("rating"))
            self._count(bucket["trader_actions"], item.get("trader_action"))
            self._count(bucket["signal_actions"], item.get("signal_action"))
            if item.get("lesson") and bucket["latest_lesson"] is None:
                bucket["latest_lesson"] = item.get("lesson")

        positions = [self._finalize_symbol(bucket) for bucket in symbols.values()]
        positions.sort(key=lambda item: (item["decision_count"], item["average_confidence"] or 0.0), reverse=True)
        total = sum(item["decision_count"] for item in positions)
        approved = sum(item["approved_count"] for item in positions)
        executed = sum(item["executed_count"] for item in positions)
        blocked = sum(item["blocked_count"] for item in positions)
        proposed_size = round(sum(item["proposed_position_size_pct"] for item in positions), 4)
        return {
            "schema_version": "open_stock_ai.portfolio_attribution.v1",
            "method": "local_decision_log_portfolio_attribution",
            "source": source,
            "review_schema_version": review.get("schema_version"),
            "symbol_filter": review.get("symbol"),
            "symbol_count": len(positions),
            "decision_count": total,
            "approved_count": approved,
            "executed_count": executed,
            "blocked_count": blocked,
            "approval_rate": round(approved / total, 4) if total else 0.0,
            "execution_rate": round(executed / total, 4) if total else 0.0,
            "proposed_position_size_pct": proposed_size,
            "positions": positions,
            "available": total > 0 and review.get("schema_version") == "open_stock_ai.decision_review.v1",
            "execution_boundary": "replay_attribution_only_no_order_execution",
        }

    def _finalize_symbol(self, bucket: dict[str, Any]) -> dict[str, Any]:
        confidence_count = bucket.pop("confidence_count")
        confidence_sum = bucket.pop("confidence_sum")
        decision_count = bucket["decision_count"]
        approved_count = bucket["approved_count"]
        executed_count = bucket["executed_count"]
        bucket["average_confidence"] = round(confidence_sum / confidence_count, 4) if confidence_count else None
        bucket["confidence_type"] = "legacy_decision_log_value"
        bucket["confidence_calibrated"] = False
        bucket["approval_rate"] = round(approved_count / decision_count, 4) if decision_count else 0.0
        bucket["execution_rate"] = round(executed_count / decision_count, 4) if decision_count else 0.0
        bucket["proposed_position_size_pct"] = round(bucket["proposed_position_size_pct"], 4)
        bucket["dominant_rating"] = self._dominant(bucket["ratings"])
        bucket["dominant_trader_action"] = self._dominant(bucket["trader_actions"])
        bucket["dominant_signal_action"] = self._dominant(bucket["signal_actions"])
        return bucket

    def _count(self, counts: dict[str, int], value: Any) -> None:
        if value is None or value == "":
            return
        key = str(value)
        counts[key] = counts.get(key, 0) + 1

    def _dominant(self, counts: dict[str, int]) -> str | None:
        if not counts:
            return None
        return max(counts, key=counts.get)

    def _number(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
