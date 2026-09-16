"""Authoritative production-governance contracts."""

from .capability_status import (
    ExecutionStageResolution,
    capability_status,
    render_readme_capability_status,
    resolve_execution_stage,
)
from .authoritative_stores import (
    authoritative_store_for,
    authoritative_store_matrix,
    is_authoritative_store,
)
from .artifact_rollback import ApprovedArtifactRollbackRegistry, ArtifactRollbackError, RollbackReceipt, verify_rollback_receipt
from .durable_store import SQLiteGovernanceStore, SQLiteRetentionStore
from .runtime import RuntimeGovernance, build_runtime_governance
from .content_retention import ContentAddressedRetentionLedger, RetentionError, RetentionMaintenanceScheduler, RetentionReceipt, verify_retention_receipt
from .change_management import ChangeManagementError, ChangeManagementRegistry, ChangeSet, verify_order_version_binding
from .performance_regression import (
    PerformanceBaselineArtifact,
    PerformanceBaselineStore,
    PerformanceBudget,
    PerformanceReport,
    evaluate_performance_regression,
    verify_performance_report,
)
from .load_test_contract import LoadTestProfile, LoadTestReport, evaluate_load_test, verify_load_test_report
from .load_test_runner import RequestSample, collect_load_metrics, run_load_test, summarize_samples, write_report
from .hosted_load_gate import (
    HOSTED_LOAD_GATE_SCHEMA,
    run_hosted_load_gate,
    verify_hosted_load_gate_receipt,
)
from .hosted_performance_gate import (
    DEFERRED_MODEL_SURFACE,
    HOSTED_PERFORMANCE_GATE_SCHEMA,
    NON_MODEL_SURFACES,
    PerformanceSurfaceProfile,
    collect_surface_metrics,
    run_hosted_performance_gate,
    verify_hosted_performance_gate_receipt,
)
from .hosted_chaos_gate import (
    HOSTED_CHAOS_GATE_SCHEMA,
    build_hosted_chaos_gate_receipt,
    verify_hosted_chaos_gate_receipt,
)
from .chaos_recovery import (
    CHAOS_SCENARIOS,
    ChaosRecoveryCatalog,
    ChaosRecoveryReceipt,
    DurableChaosRecoveryStore,
)
from .ha_leader_lease import HA_SERVICES, LeaseNotAcquired, LeaseReceipt, SQLiteLeaderLease, ServiceTopology
from .hosted_ha_failover import (
    HOSTED_HA_FAILOVER_SCHEMA,
    REQUIRED_FAILOVER_INVARIANTS,
    build_hosted_ha_failover_receipt,
    verify_hosted_ha_failover_receipt,
)
from .hosted_circuit_gate import (
    HOSTED_CIRCUIT_GATE_SCHEMA,
    HOSTED_CIRCUIT_SCOPES,
    build_hosted_circuit_gate_receipt,
    verify_hosted_circuit_gate_receipt,
)
from .hosted_rate_limit_gate import (
    HOSTED_RATE_LIMIT_GATE_SCHEMA,
    HOSTED_RATE_SCOPES,
    RATE_POLICY_SCHEMA,
    build_hosted_rate_limit_gate_receipt,
    build_rate_policy_receipt,
    verify_hosted_rate_limit_gate_receipt,
)
from .hosted_rollback_gate import (
    HOSTED_ROLLBACK_GATE_SCHEMA,
    ROLLBACK_SCOPES,
    build_hosted_rollback_gate_receipt,
    verify_hosted_rollback_gate_receipt,
)
from .retention_archive import (
    ARCHIVE_SCHEMA,
    RESTORE_SCHEMA,
    build_critical_retention_archive,
    restore_critical_retention_archive,
    verify_critical_retention_archive,
    verify_critical_retention_restore,
)
from .hosted_retention_gate import (
    HOSTED_RETENTION_GATE_SCHEMA,
    REQUIRED_CRITICAL_SCHEMAS,
    build_hosted_retention_gate_receipt,
    verify_hosted_retention_gate_receipt,
)
from .release_signing import RELEASE_PROVENANCE_SCHEMA, build_provenance, verify_provenance_payload
from .security_controls import (
    SECURITY_CONTROL_SCHEMA,
    SECURITY_CONTROLS,
    SecurityControl,
    build_security_control_receipt,
    verify_security_control_receipt,
)
from .production_requirements import production_requirement_status
from .promotion_ladder import (
    CapabilityPromotionLadder,
    PromotionReceipt,
    SQLitePromotionReceiptStore,
    ordered_levels,
)
from .release_gate import ReleaseGateFailure, assert_release_gate, evaluate_release_gate

__all__ = [
    "ExecutionStageResolution",
    "ApprovedArtifactRollbackRegistry",
    "SQLiteGovernanceStore",
    "SQLiteRetentionStore",
    "RuntimeGovernance",
    "build_runtime_governance",
    "ArtifactRollbackError",
    "RollbackReceipt",
    "ContentAddressedRetentionLedger",
    "RetentionError",
    "RetentionMaintenanceScheduler",
    "RetentionReceipt",
    "ChangeManagementError",
    "ChangeManagementRegistry",
    "ChangeSet",
    "PerformanceBudget",
    "PerformanceReport",
    "PerformanceBaselineArtifact",
    "PerformanceBaselineStore",
    "capability_status",
    "authoritative_store_for",
    "authoritative_store_matrix",
    "is_authoritative_store",
    "render_readme_capability_status",
    "production_requirement_status",
    "CapabilityPromotionLadder",
    "PromotionReceipt",
    "SQLitePromotionReceiptStore",
    "ordered_levels",
    "ReleaseGateFailure",
    "assert_release_gate",
    "evaluate_release_gate",
    "verify_rollback_receipt",
    "verify_retention_receipt",
    "verify_order_version_binding",
    "evaluate_performance_regression",
    "verify_performance_report",
    "LoadTestProfile",
    "LoadTestReport",
    "evaluate_load_test",
    "verify_load_test_report",
    "RequestSample",
    "collect_load_metrics",
    "run_load_test",
    "summarize_samples",
    "write_report",
    "HOSTED_LOAD_GATE_SCHEMA",
    "run_hosted_load_gate",
    "verify_hosted_load_gate_receipt",
    "DEFERRED_MODEL_SURFACE",
    "HOSTED_PERFORMANCE_GATE_SCHEMA",
    "NON_MODEL_SURFACES",
    "PerformanceSurfaceProfile",
    "collect_surface_metrics",
    "run_hosted_performance_gate",
    "verify_hosted_performance_gate_receipt",
    "HOSTED_CHAOS_GATE_SCHEMA",
    "build_hosted_chaos_gate_receipt",
    "verify_hosted_chaos_gate_receipt",
    "CHAOS_SCENARIOS",
    "ChaosRecoveryCatalog",
    "ChaosRecoveryReceipt",
    "DurableChaosRecoveryStore",
    "HA_SERVICES",
    "LeaseNotAcquired",
    "LeaseReceipt",
    "SQLiteLeaderLease",
    "ServiceTopology",
    "HOSTED_HA_FAILOVER_SCHEMA",
    "REQUIRED_FAILOVER_INVARIANTS",
    "build_hosted_ha_failover_receipt",
    "verify_hosted_ha_failover_receipt",
    "HOSTED_CIRCUIT_GATE_SCHEMA",
    "HOSTED_CIRCUIT_SCOPES",
    "build_hosted_circuit_gate_receipt",
    "verify_hosted_circuit_gate_receipt",
    "HOSTED_RATE_LIMIT_GATE_SCHEMA",
    "HOSTED_RATE_SCOPES",
    "RATE_POLICY_SCHEMA",
    "build_hosted_rate_limit_gate_receipt",
    "build_rate_policy_receipt",
    "verify_hosted_rate_limit_gate_receipt",
    "HOSTED_ROLLBACK_GATE_SCHEMA",
    "ROLLBACK_SCOPES",
    "build_hosted_rollback_gate_receipt",
    "verify_hosted_rollback_gate_receipt",
    "ARCHIVE_SCHEMA",
    "RESTORE_SCHEMA",
    "build_critical_retention_archive",
    "restore_critical_retention_archive",
    "verify_critical_retention_archive",
    "verify_critical_retention_restore",
    "HOSTED_RETENTION_GATE_SCHEMA",
    "REQUIRED_CRITICAL_SCHEMAS",
    "build_hosted_retention_gate_receipt",
    "verify_hosted_retention_gate_receipt",
    "RELEASE_PROVENANCE_SCHEMA",
    "build_provenance",
    "verify_provenance_payload",
    "SECURITY_CONTROL_SCHEMA",
    "SECURITY_CONTROLS",
    "SecurityControl",
    "build_security_control_receipt",
    "verify_security_control_receipt",
    "resolve_execution_stage",
]
