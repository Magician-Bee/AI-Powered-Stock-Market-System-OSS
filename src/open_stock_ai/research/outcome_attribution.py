from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class OutcomeAttribution:
    def build(self, signal_items: list[dict[str, Any]], *, source: str = "signal_ledger") -> dict[str, Any]:
        observations = self._observations(signal_items)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in observations:
            grouped.setdefault(item["symbol"], []).append(item)
        outcomes: list[dict[str, Any]] = []
        for symbol_items in grouped.values():
            symbol_items.sort(key=lambda item: item["created_at"])
            for index, item in enumerate(symbol_items[:-1]):
                future = symbol_items[index + 1]
                outcome = self._outcome(item, future)
                if outcome:
                    outcomes.append(outcome)
        outcomes.sort(key=lambda item: item["observed_at"], reverse=True)
        evaluated = len(outcomes)
        positive = sum(1 for item in outcomes if item["directional_return_pct"] is not None and item["directional_return_pct"] > 0)
        target_hits = sum(1 for item in outcomes if item["target_hit"] is True)
        stop_hits = sum(1 for item in outcomes if item["stop_hit"] is True)
        average_return = (
            round(sum(item["directional_return_pct"] for item in outcomes) / evaluated, 4)
            if evaluated
            else None
        )
        return {
            "schema_version": "open_stock_ai.paper_outcome_attribution.v1",
            "method": "local_signal_ledger_forward_replay",
            "source": source,
            "input_count": len(signal_items),
            "observation_count": len(observations),
            "evaluated_count": evaluated,
            "positive_count": positive,
            "target_hit_count": target_hits,
            "stop_hit_count": stop_hits,
            "positive_rate": round(positive / evaluated, 4) if evaluated else 0.0,
            "target_hit_rate": round(target_hits / evaluated, 4) if evaluated else 0.0,
            "stop_hit_rate": round(stop_hits / evaluated, 4) if evaluated else 0.0,
            "average_directional_return_pct": average_return,
            "outcomes": outcomes[:20],
            "available": len(observations) > 0,
            "execution_boundary": "ledger_replay_only_no_order_execution",
        }

    def _observations(self, signal_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for item in signal_items:
            payload = item.get("payload") if isinstance(item, dict) else {}
            if not isinstance(payload, dict):
                continue
            signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
            snapshot = payload.get("market_snapshot") if isinstance(payload.get("market_snapshot"), dict) else {}
            request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
            created_at = item.get("created_at") or ((payload.get("research") or {}) if isinstance(payload.get("research"), dict) else {}).get("artifacts", {}).get("generated_at")
            symbol = signal.get("symbol") or request.get("symbol") or item.get("symbol")
            price = self._number(snapshot.get("price") if snapshot else None)
            entry = self._number(signal.get("entry_price"))
            if not symbol or not created_at or price is None:
                continue
            observations.append(
                {
                    "ledger_id": item.get("id"),
                    "created_at": self._parse_time(created_at),
                    "created_at_raw": created_at,
                    "symbol": str(symbol),
                    "market": signal.get("market") or request.get("market"),
                    "horizon": signal.get("horizon") or request.get("horizon"),
                    "action": signal.get("action"),
                    "confidence": self._number(signal.get("confidence")),
                    "entry_price": entry if entry is not None else price,
                    "target_price": self._number(signal.get("target_price")),
                    "stop_loss": self._number(signal.get("stop_loss")),
                    "observed_price": price,
                }
            )
        return observations

    def _outcome(self, item: dict[str, Any], future: dict[str, Any]) -> dict[str, Any] | None:
        entry = self._number(item.get("entry_price"))
        future_price = self._number(future.get("observed_price"))
        if entry is None or future_price is None or entry <= 0:
            return None
        action = str(item.get("action") or "").lower()
        raw_return = ((future_price - entry) / entry) * 100
        if action in {"sell", "reduce"}:
            directional_return = -raw_return
        elif action in {"buy", "add"}:
            directional_return = raw_return
        else:
            directional_return = 0.0
        target = self._number(item.get("target_price"))
        stop = self._number(item.get("stop_loss"))
        target_hit = None
        stop_hit = None
        if action in {"buy", "add"}:
            target_hit = future_price >= target if target is not None else None
            stop_hit = future_price <= stop if stop is not None else None
        elif action in {"sell", "reduce"}:
            target_hit = future_price <= target if target is not None else None
            stop_hit = future_price >= stop if stop is not None else None
        return {
            "symbol": item.get("symbol"),
            "market": item.get("market"),
            "horizon": item.get("horizon"),
            "action": action,
            "confidence": item.get("confidence"),
            "ledger_id": item.get("ledger_id"),
            "created_at": item.get("created_at_raw"),
            "observed_at": future.get("created_at_raw"),
            "entry_price": round(entry, 4),
            "future_price": round(future_price, 4),
            "raw_return_pct": round(raw_return, 4),
            "directional_return_pct": round(directional_return, 4),
            "target_price": target,
            "stop_loss": stop,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
            "observation_method": "next_signal_same_symbol_price",
        }

    def _parse_time(self, value: Any) -> datetime:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return datetime.min

    def _number(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
