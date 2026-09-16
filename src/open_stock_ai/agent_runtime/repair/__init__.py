"""Branch-local, provider-neutral repair primitives."""

from .contracts import ErrorReceipt, FailureFingerprint, ModelPatch, PatchOperation
from .deterministic import DeterministicRepair, RepairResult
from .outcomes import BranchOutcome, CompletionGate, PartialCompletionReport
from .patching import (
    HostModelRepairPipeline,
    ModelPatchValidator,
    ModelRepairOutcome,
    PatchValidationError,
)
from .strategy import (
    ChaosFault,
    IdenticalRetryGuard,
    RecoveryDecision,
    RecoveryLevel,
    RecoveryStrategyLadder,
    error_receipt_for_fault,
)

__all__ = [
    "BranchOutcome",
    "ChaosFault",
    "CompletionGate",
    "DeterministicRepair",
    "ErrorReceipt",
    "FailureFingerprint",
    "HostModelRepairPipeline",
    "IdenticalRetryGuard",
    "ModelPatch",
    "ModelPatchValidator",
    "ModelRepairOutcome",
    "PartialCompletionReport",
    "PatchOperation",
    "PatchValidationError",
    "RecoveryDecision",
    "RecoveryLevel",
    "RecoveryStrategyLadder",
    "RepairResult",
    "error_receipt_for_fault",
]
