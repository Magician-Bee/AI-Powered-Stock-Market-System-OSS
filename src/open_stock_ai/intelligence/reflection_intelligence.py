from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ReflectionIntelligence:
    def review(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(decisions)
        approved = sum(1 for item in decisions if (item.get("risk") or {}).get("approved") is True)
        executed = sum(1 for item in decisions if (item.get("execution") or {}).get("executed") is True)
        blocked_reasons = [
            str((item.get("risk") or {}).get("reason"))
            for item in decisions
            if (item.get("risk") or {}).get("approved") is not True and (item.get("risk") or {}).get("reason")
        ]
        return {
            "method": "tradingagents_style_reflection_summary",
            "total": total,
            "approved": approved,
            "executed": executed,
            "approval_rate": round(approved / total, 4) if total else 0.0,
            "execution_rate": round(executed / total, 4) if total else 0.0,
            "blocked_reasons": blocked_reasons[:10],
            "lesson": self._lesson(total, approved, executed, blocked_reasons),
        }

    def replay_projection(
        self,
        review: dict[str, Any],
        tradingagents_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        items = review.get("items") if isinstance(review.get("items"), list) else []
        contract = tradingagents_contract or {}
        risk_memory = contract.get("risk_memory_contract") if isinstance(contract.get("risk_memory_contract"), dict) else {}
        files = risk_memory.get("files") if isinstance(risk_memory.get("files"), list) else []
        reflection_file = next(
            (
                item
                for item in files
                if isinstance(item, dict)
                and item.get("path") == "tradingagents/graph/reflection.py"
            ),
            {},
        )
        rating_scale = risk_memory.get("rating_scale") if isinstance(risk_memory.get("rating_scale"), list) else []
        cohorts = self._decision_cohorts(items)
        lessons = review.get("lessons") if isinstance(review.get("lessons"), list) else []
        return {
            "schema_version": "open_stock_ai.tradingagents_reflection_replay.v1",
            "method": "local_tradingagents_reflection_memory_projection",
            "source": "TradingAgents",
            "review_schema_version": review.get("schema_version"),
            "review_method": review.get("method"),
            "symbol": review.get("symbol"),
            "total": review.get("total", 0),
            "approval_rate": review.get("approval_rate", 0.0),
            "execution_rate": review.get("execution_rate", 0.0),
            "average_confidence": review.get("average_confidence"),
            "rating_scale": rating_scale,
            "risk_debator_count": risk_memory.get("risk_debator_count", 0),
            "reflection_contract": {
                "path": reflection_file.get("path"),
                "exists": reflection_file.get("exists") is True,
                "classes": reflection_file.get("classes") if isinstance(reflection_file.get("classes"), list) else [],
                "functions": reflection_file.get("functions") if isinstance(reflection_file.get("functions"), list) else [],
            },
            "cohorts": cohorts,
            "dominant_cohort": max(cohorts, key=cohorts.get) if cohorts else None,
            "latest_lesson": lessons[0] if lessons else None,
            "lesson_count": len(lessons),
            "memory_ready": bool(rating_scale)
            and risk_memory.get("risk_debator_count", 0) >= 3
            and reflection_file.get("exists") is True,
            "execution_boundary": "read_only_reflection_replay_no_tradingagents_runtime_import",
        }

    def _lesson(self, total: int, approved: int, executed: int, blocked_reasons: list[str]) -> str:
        if total == 0:
            return "No decisions available for reflection."
        if executed:
            return "Paper execution occurred; review sizing, stop loss, and follow-through."
        if approved:
            return "Risk approved but no paper execution occurred; inspect execution mode and paper-only controls."
        if blocked_reasons:
            return f"Risk gates blocked all decisions; most recent reason: {blocked_reasons[0]}"
        return "Decisions stayed in review mode."

    def _decision_cohorts(self, items: list[Any]) -> dict[str, int]:
        cohorts = {
            "approved_executed": 0,
            "approved_not_executed": 0,
            "blocked_by_risk": 0,
            "review_only": 0,
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            approved = item.get("risk_approved") is True
            executed = item.get("executed") is True
            if approved and executed:
                cohorts["approved_executed"] += 1
            elif approved:
                cohorts["approved_not_executed"] += 1
            elif item.get("lesson") or item.get("risk_approved") is False:
                cohorts["blocked_by_risk"] += 1
            else:
                cohorts["review_only"] += 1
        return cohorts
