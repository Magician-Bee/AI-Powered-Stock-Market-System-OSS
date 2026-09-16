"""Provider-neutral Automation domain for the durable Agent runtime."""

from .backends import HeadlessN8nAdapter, InternalSchedulerBackend
from .controller import AutomationController, AutomationProposal, TriggerOutcome
from .dedup import DedupDecision, SemanticDeduplicator, semantic_fingerprint
from .intent import (
    AutomationIntent,
    AutomationKind,
    CostPolicy,
    NotificationPolicy,
)
from .lifecycle import AutomationState, Lifecycle
from .model_routing import ModelRoleRouter
from .notifications import NotificationManager, NotificationReceipt
from .opportunity_detector import AutomationOpportunity, OpportunityDetector
from .policy_engine import AutomationPolicyDecision, AutomationPolicyEngine
from .store import AutomationStore
from .time_poller import ProductTimePoller, TimePollResult
from .workflow_compiler import CompiledWorkflow, WorkflowCompiler

__all__ = [
    "AutomationController",
    "AutomationIntent",
    "AutomationKind",
    "AutomationOpportunity",
    "AutomationPolicyDecision",
    "AutomationPolicyEngine",
    "AutomationProposal",
    "AutomationState",
    "AutomationStore",
    "CompiledWorkflow",
    "CostPolicy",
    "DedupDecision",
    "HeadlessN8nAdapter",
    "InternalSchedulerBackend",
    "Lifecycle",
    "ModelRoleRouter",
    "NotificationManager",
    "NotificationPolicy",
    "NotificationReceipt",
    "OpportunityDetector",
    "ProductTimePoller",
    "SemanticDeduplicator",
    "TriggerOutcome",
    "TimePollResult",
    "WorkflowCompiler",
    "semantic_fingerprint",
]
