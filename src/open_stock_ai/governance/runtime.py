"""Runtime-owned governance authorities.

The application has several governance ledgers (promotion, rollback, change
sets and retention).  Keeping their construction in one small factory makes it
possible to prove that production runtime components use the same migrated
SQLite database instead of silently falling back to process-local state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_rollback import ApprovedArtifactRollbackRegistry
from .change_management import ChangeManagementRegistry
from .content_retention import ContentAddressedRetentionLedger, RetentionMaintenanceScheduler
from .durable_store import SQLiteGovernanceStore, SQLiteRetentionStore


@dataclass(slots=True)
class RuntimeGovernance:
    """The four governance authorities owned by one runtime instance."""

    database_path: str
    durable: bool
    store: SQLiteGovernanceStore | None
    retention_store: SQLiteRetentionStore | None
    artifact_rollback: ApprovedArtifactRollbackRegistry
    change_management: ChangeManagementRegistry
    retention: ContentAddressedRetentionLedger
    retention_maintenance: RetentionMaintenanceScheduler

    def status(self) -> dict[str, Any]:
        """Return safe, non-secret wiring evidence for diagnostics and UI."""

        return {
            "schema_version": "open_stock_ai.runtime_governance.v1",
            "database_path": self.database_path,
            "durable": self.durable,
            "store": type(self.store).__name__ if self.store is not None else None,
            "authorities": {
                "artifact_rollback": {
                    "store_wired": self.artifact_rollback.store is self.store,
                    "approved_artifact_count": len(self.artifact_rollback._approved),
                    "activation_count": len(self.artifact_rollback._activation_history),
                    "rollback_receipt_count": len(self.artifact_rollback.receipts),
                },
                "change_management": {
                    "store_wired": self.change_management.store is self.store,
                    "change_set_count": len(self.change_management._changes),
                    "order_binding_count": len(self.change_management._orders),
                },
                "retention": {
                    "store_wired": (
                        self.retention_store is not None
                        and self.retention.store is self.retention_store
                        and self.retention_store.path == Path(self.database_path)
                    ),
                    "retained_counts": self.retention.retained_counts(),
                    "receipt_count": len(self.retention.receipts),
                    "max_signal_entries": self.retention.max_signal_entries,
                    "maintenance": self.retention_maintenance.status(),
                },
            },
        }


def build_runtime_governance(
    database_path: str | Path,
    *,
    retention_maintenance_interval_seconds: int = 3600,
) -> RuntimeGovernance:
    """Build all runtime governance registries against one authoritative DB.

    ``:memory:`` remains explicitly non-durable for isolated tests.  Any
    file-backed path is initialized through the normal SQLite migration path
    before the registries are restored, so first boot and restart have the same
    authority.
    """

    configured = str(database_path)
    if configured == ":memory:":
        retention = ContentAddressedRetentionLedger()
        return RuntimeGovernance(
            database_path=configured,
            durable=False,
            store=None,
            retention_store=None,
            artifact_rollback=ApprovedArtifactRollbackRegistry(),
            change_management=ChangeManagementRegistry(),
            retention=retention,
            retention_maintenance=RetentionMaintenanceScheduler(
                retention,
                interval_seconds=retention_maintenance_interval_seconds,
            ),
        )

    path = Path(configured).expanduser().resolve()
    store = SQLiteGovernanceStore(path)
    retention_store = SQLiteRetentionStore(path)
    retention = ContentAddressedRetentionLedger(store=retention_store)
    return RuntimeGovernance(
        database_path=str(path),
        durable=True,
        store=store,
        retention_store=retention_store,
        artifact_rollback=ApprovedArtifactRollbackRegistry(store),
        change_management=ChangeManagementRegistry(store),
        retention=retention,
        retention_maintenance=RetentionMaintenanceScheduler(
            retention,
            interval_seconds=retention_maintenance_interval_seconds,
        ),
    )
