"""Evidence-first external research planning and reconciliation."""

from .evidence_graph import ConflictResolution, ConflictResolver, EvidenceGraph
from .models import Evidence, ResearchPlan, ResearchQuestion, SourceRequirement
from .planner import ResearchPlanner
from .recovery import BranchSourceRecovery, SourceRecoveryPlan

__all__ = [
    "BranchSourceRecovery",
    "ConflictResolution",
    "ConflictResolver",
    "Evidence",
    "EvidenceGraph",
    "ResearchPlan",
    "ResearchPlanner",
    "ResearchQuestion",
    "SourceRecoveryPlan",
    "SourceRequirement",
]
