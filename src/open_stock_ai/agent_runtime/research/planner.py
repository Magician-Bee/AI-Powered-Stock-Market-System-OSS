from __future__ import annotations

from .models import ResearchPlan, ResearchQuestion, SourceRequirement


class ResearchPlanner:
    """Create a bounded evidence-first plan before any web tool is selected."""

    def plan(
        self,
        objective: str,
        *,
        symbols: tuple[str, ...] = (),
        decision_horizon: str = "current",
        max_questions: int = 5,
    ) -> ResearchPlan:
        if not objective.strip():
            raise ValueError("Research objective is required")
        subject = ", ".join(symbols) if symbols else "目標標的"
        candidates = (
            ("material_events", f"{subject} 最近有哪些可能改變投資判斷的重大事件？", ("primary", "independent_news"), 24),
            ("fundamentals", f"{subject} 最新營運、財務與公司指引是否支持目前論點？", ("company_ir", "regulator"), 168),
            ("market_context", f"市場價格、成交與籌碼是否確認或反駁該論點？", ("exchange", "market_data"), 24),
            ("counter_evidence", "有哪些可信來源明確反駁主要投資論點？", ("independent_news", "industry"), 168),
            ("decision_impact", f"上述證據對 {decision_horizon} 決策的影響與不確定性為何？", ("synthesis",), 24),
        )
        bounded = max(1, min(int(max_questions), len(candidates)))
        questions = tuple(
            ResearchQuestion(
                question_id=f"RQ-{index + 1}",
                question=question,
                claim_scope=scope,
                source_roles=roles,
                freshness_hours=freshness,
                minimum_independent_sources=2 if scope != "decision_impact" else 1,
            )
            for index, (scope, question, roles, freshness) in enumerate(candidates[:bounded])
        )
        return ResearchPlan(
            objective=objective,
            questions=questions,
            source_requirements=(
                SourceRequirement("primary", ("company_ir", "exchange", "regulator"), True),
                SourceRequirement("independent_news", ("reputable_news", "industry")),
                SourceRequirement("market_data", ("exchange", "licensed_market_data")),
            ),
            conflict_rules=(
                "prefer_primary_for_company_disclosures",
                "prefer_newer_observation_for_time_sensitive_claims",
                "preserve_unresolved_material_conflicts",
                "never_average_incompatible_claims",
            ),
            stop_criteria=(
                "all_questions_meet_minimum_sources",
                "at_least_one_primary_source_for_material_company_claims",
                "material_conflicts_resolved_or_explicitly_reported",
                "additional_source_unlikely_to_change_decision",
            ),
        )
