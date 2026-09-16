from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .contracts import AgentRunContext
from .autonomy_contract import CAMPAIGN_MUTATIONS, campaign_execution_authorized


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: str
    reason: str
    risk_class: str
    required_approval: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.policy_decision.v1",
            "action": self.action,
            "reason": self.reason,
            "risk_class": self.risk_class,
            "required_approval": self.required_approval,
        }


class PolicyEngine:
    """Apply action-risk policy before a capability reaches a worker.

    The decision deliberately depends only on the requested capability, resource
    scope and current run permissions. It never uses provider/model identity.
    """

    _APPROVAL_RISKS = {
        "local_reversible",
        "local_destructive",
        "external_side_effect",
        "financial_paper",
        "credential_sensitive",
        "system_sensitive",
    }

    def evaluate(
        self,
        *,
        tool: dict[str, Any],
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> PolicyDecision:
        risk_class = str(tool.get("risk_class") or "read_only")
        name = str(tool.get("name") or "")
        if campaign_execution_authorized(context) and name.startswith("paper.") and (
            tool.get("mutating") or risk_class == "financial_paper"
        ):
            return PolicyDecision("deny", "The campaign must use its isolated account through autonomy tools; legacy paper mutations target another account.", risk_class, False)
        if (
            risk_class == "financial_real_action"
            or name.startswith("live.")
            or name in {"broker.order.submit", "broker.order.place"}
        ):
            return PolicyDecision(
                "deny",
                "Real brokerage actions are permanently unavailable in Stock AI.",
                risk_class,
                False,
            )
        argument_decision = _argument_boundary(name, arguments)
        if argument_decision is not None:
            action, reason, elevated_risk = argument_decision
            if action == "deny":
                return PolicyDecision(action, reason, elevated_risk, False)
            risk_class = elevated_risk
        if bool(tool.get("requires_project_execution")) and not context.allow_project_actions:
            return PolicyDecision("deny", "Project execution mode is not enabled.", risk_class, False)
        if bool(tool.get("requires_paper_execution")) and not context.allow_paper_orders:
            return PolicyDecision("deny", "Paper execution mode is not enabled.", risk_class, False)
        if bool(tool.get("requires_external_execution")) and not context.allow_external_actions:
            return PolicyDecision("deny", "External execution mode is not enabled.", risk_class, False)
        if bool(tool.get("requires_full_execution")) and context.autonomy != "full_execute":
            return PolicyDecision("deny", "Full execution mode is not enabled.", risk_class, False)
        if name in CAMPAIGN_MUTATIONS:
            if not campaign_execution_authorized(context):
                return PolicyDecision("deny", "This Host objective does not authorize an autonomous paper campaign.", risk_class, False)
            return PolicyDecision("allow", "The Host objective explicitly authorizes this bounded autonomous paper campaign.", risk_class, False)
        if (
            name == "paper.submit_order"
            and context.allow_paper_orders
            and context.state.get("explicit_paper_order_authorized") is True
        ):
            return PolicyDecision(
                "allow",
                "The user explicitly authorized this local paper-training order in paper execution mode.",
                risk_class,
                False,
            )
        approval = bool(tool.get("mutating")) or risk_class in self._APPROVAL_RISKS
        return PolicyDecision(
            "require_approval" if approval else "allow",
            "Mutation requires a user-bound approval." if approval else "Read-only capability is permitted.",
            risk_class,
            approval,
        )


def _argument_boundary(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, str, str] | None:
    """Apply deterministic argument-level boundaries before worker dispatch."""
    paths = [
        str(value).replace("\\", "/").casefold()
        for key, value in arguments.items()
        if key in {"path", "source", "destination", "cwd"} and value is not None
    ]
    if tool_name.startswith("project.") and any(_sensitive_path(path) for path in paths):
        return (
            "deny",
            "Secret, credential and private-key paths cannot enter model or tool context.",
            "credential_sensitive",
        )
    if tool_name == "browser.fill":
        selector = str(arguments.get("selector") or "").casefold()
        if any(token in selector for token in ("password", "passwd", "secret", "token", "api-key")):
            return (
                "deny",
                "Agent browser fill cannot target password or credential controls.",
                "credential_sensitive",
            )
    if tool_name == "terminal.run":
        command = str(arguments.get("command") or "")
        if re.search(r"(^|\\s)(sudo|su|ssh|scp|curl|wget|nc|netcat)(\\s|$)", command):
            return (
                "deny",
                "Privileged, remote-login and network shell commands are outside the terminal sandbox.",
                "system_sensitive",
            )
        return (
            "require_approval",
            "Terminal command arguments require a user-bound approval.",
            "system_sensitive",
        )
    credential_keys = {
        str(key).casefold()
        for key in arguments
        if any(token in str(key).casefold() for token in ("password", "secret", "api_key", "token"))
    }
    if credential_keys:
        return (
            "require_approval",
            "Credential-bearing arguments require explicit user approval.",
            "credential_sensitive",
        )
    # Force JSON serialization here so unusual provider-controlled values
    # cannot bypass later approval argument binding.
    json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return None


def _sensitive_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519"}
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".token", ".credentials"))
    )
