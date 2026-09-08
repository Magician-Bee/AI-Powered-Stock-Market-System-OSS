from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .intent import CostPolicy


@dataclass(frozen=True, slots=True)
class ModelAssignment:
    role: str
    tier: str
    model: str | None


class ModelRoleRouter:
    """Route semantic model work by cost/risk; deterministic work has no model role."""

    _DEFAULT_TIERS = {
        "planner": "standard",
        "researcher": "economy",
        "critic": "strong",
        "formatter": "economy",
        "repair": "economy",
        "reanalysis": "standard",
        "high_risk_review": "strong",
    }

    def route(self, role: str, policy: CostPolicy, *, high_risk: bool = False) -> ModelAssignment:
        resolved_role = "high_risk_review" if high_risk else role
        tier = self._DEFAULT_TIERS.get(resolved_role, "standard")
        if policy.budget_class == "economy" and tier == "standard":
            tier = "economy"
        if policy.budget_class == "quality" and resolved_role in {"planner", "reanalysis"}:
            tier = "strong"
        overrides: Mapping[str, str] = policy.role_models
        return ModelAssignment(resolved_role, tier, overrides.get(resolved_role) or overrides.get(role))

    def workflow_routes(self, policy: CostPolicy, *, high_risk: bool = False) -> dict[str, dict[str, str | None]]:
        roles = ("planner", "researcher", "critic", "formatter", "repair", "reanalysis")
        return {
            role: {
                "tier": assignment.tier,
                "model": assignment.model,
            }
            for role in roles
            for assignment in (self.route(role, policy, high_risk=high_risk and role == "critic"),)
        }
