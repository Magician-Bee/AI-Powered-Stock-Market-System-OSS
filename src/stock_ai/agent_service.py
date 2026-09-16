from __future__ import annotations

from threading import RLock

from open_stock_ai.agent_runtime import AgentOrchestrator
from open_stock_ai.agent_runtime.approval_manager import ApprovalManager
from open_stock_ai.agent_runtime.artifact_store import ArtifactStore
from open_stock_ai.agent_runtime.checkpoint_manager import CheckpointManager
from open_stock_ai.agent_runtime.checkpoint_store import CheckpointStore
from open_stock_ai.agent_runtime.environment_snapshot import EnvironmentSnapshotBuilder
from open_stock_ai.agent_runtime.memory import MemoryManager, MemoryStore
from open_stock_ai.agent_runtime.plan_manager import PlanManager
from open_stock_ai.agent_runtime.policy_engine import PolicyEngine
from open_stock_ai.agent_runtime.rollback_manager import RollbackManager
from open_stock_ai.agent_runtime.runtime_paths import AgentRuntimePaths
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.storage_health import AgentDatabaseMaintenance
from open_stock_ai.agent_runtime.operational_alert_runtime import OperationalAlertRuntime
from open_stock_ai.agent_runtime.validators import ValidatorEngine
from open_stock_ai.agent_runtime.workflow_store import WorkflowStore
from open_stock_ai.agent_runtime.workers import WorkerSupervisor

from .agent_drivers import (
    build_agent_drivers,
    build_provider_registry,
    load_agent_driver_settings,
)
from .agent_event_bus import agent_event_bus
from .agent_ui_bridge import agent_ui_bridge
from .agent_run_store import AgentRunStore
from .agent_tools import StockAgentToolRegistry
from .automation_notifications import build_automation_notification_senders
from .config import get_settings
from .durable_agent_runtime import DurableAgentRuntime
from .n8n_automation import N8nGatewayConfig, build_n8n_gateway_adapter
from .market_calendar import default_taiwan_market_calendar
from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from .paper_training_api import paper_training_account


_SERVICE: AgentOrchestrator | None = None
_RUN_RUNTIME: DurableAgentRuntime | None = None
_COMPONENTS: dict[str, object] | None = None
_SERVICE_LOCK = RLock()


def get_agent_service() -> AgentOrchestrator:
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            settings = load_agent_driver_settings()
            components = _runtime_components()
            provider_registry = build_provider_registry(settings)
            _SERVICE = AgentOrchestrator(
                drivers=build_agent_drivers(
                    settings,
                    provider_registry=provider_registry,
                ),
                tools=components["tools"],
                default_driver=settings.default_driver,
                plan_manager=components["plan_manager"],
                checkpoint_manager=components["checkpoint_manager"],
                approval_manager=components["approval_manager"],
                memory_manager=components["memory_manager"],
                snapshot_builder=components["snapshot_builder"],
                worker_supervisor=components["worker_supervisor"],
                validator=components["validator"],
                policy_engine=components["policy_engine"],
                rollback_manager=components["rollback_manager"],
                provider_registry=provider_registry,
                control_provider=components["run_store"].consume_control_messages,
            )
    return _SERVICE


def clear_agent_service() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None


def get_agent_database_maintenance() -> AgentDatabaseMaintenance:
    """Return the runtime-owned maintenance boundary without rebuilding it."""

    maintenance = _runtime_components()["database_maintenance"]
    if not isinstance(maintenance, AgentDatabaseMaintenance):
        raise RuntimeError("Agent database maintenance is unavailable")
    return maintenance


def get_operational_alert_runtime() -> OperationalAlertRuntime:
    """Return the runtime-owned operational-alert evaluator for diagnostics."""

    runtime = _runtime_components()["operational_alert_runtime"]
    if not isinstance(runtime, OperationalAlertRuntime):
        raise RuntimeError("Operational alert runtime is unavailable")
    return runtime


def get_agent_run_runtime() -> DurableAgentRuntime:
    global _RUN_RUNTIME
    if _RUN_RUNTIME is not None:
        return _RUN_RUNTIME
    with _SERVICE_LOCK:
        if _RUN_RUNTIME is None:
            components = _runtime_components()
            settings = get_settings()
            _RUN_RUNTIME = DurableAgentRuntime(
                service_provider=get_agent_service,
                store=components["run_store"],
                session_store=components["session_store"],
                plan_manager=components["plan_manager"],
                checkpoint_manager=components["checkpoint_manager"],
                approval_manager=components["approval_manager"],
                artifact_store=components["artifact_store"],
                workflow_store=components["workflow_store"],
                worker_supervisor=components["worker_supervisor"],
                final_runtime=FinalAgentRuntime(
                    components["run_store"].path,
                    artifact_store=components["artifact_store"],
                    market_calendar=default_taiwan_market_calendar(),
                    n8n_backend=build_n8n_gateway_adapter(
                        N8nGatewayConfig(
                            gateway_url=settings.n8n_automation_gateway_url,
                            gateway_token=settings.n8n_automation_gateway_token,
                            callback_secret=settings.n8n_automation_callback_secret,
                            timeout_seconds=settings.n8n_automation_gateway_timeout_seconds,
                            deployment_mode=settings.n8n_automation_deployment_mode,
                            remote_host_allowlist=tuple(
                                item.strip()
                                for item in settings.n8n_automation_remote_host_allowlist.split(",")
                                if item.strip()
                            ),
                            remote_tls_certificate_sha256=(
                                settings.n8n_automation_remote_tls_certificate_sha256
                            ),
                            remote_mtls_certificate_path=(
                                settings.n8n_automation_remote_mtls_certificate_path
                            ),
                            remote_mtls_key_path=settings.n8n_automation_remote_mtls_key_path,
                            remote_tls_ca_bundle_path=(
                                settings.n8n_automation_remote_tls_ca_bundle_path
                            ),
                        )
                    ),
                    notification_senders=build_automation_notification_senders(),
                ),
                namespace=components["memory_manager"].namespace,
            )
            _RUN_RUNTIME.start()
    return _RUN_RUNTIME


def set_agent_run_runtime(runtime: DurableAgentRuntime | None) -> None:
    """Replace the process runtime for isolated tests; production uses the singleton."""
    global _RUN_RUNTIME
    with _SERVICE_LOCK:
        _RUN_RUNTIME = runtime


def _runtime_components() -> dict[str, object]:
    global _COMPONENTS
    if _COMPONENTS is not None:
        return _COMPONENTS
    paths = AgentRuntimePaths.discover()
    tools = StockAgentToolRegistry()
    run_store = AgentRunStore(paths.database)
    agent_event_bus.configure(run_store)
    agent_ui_bridge.configure(
        paths.database,
        event_publisher=agent_event_bus.publish,
    )
    database_maintenance = AgentDatabaseMaintenance(paths.database, paths.root / "backups")
    database_health = database_maintenance.startup_check()
    if not database_health["healthy"]:
        raise RuntimeError(f"Agent Runtime database startup check failed: {database_health['checks']}")
    if not any((paths.root / "backups").glob("agent-runtime-*.sqlite")):
        database_maintenance.backup()
    # Import lazily to keep the Agent service from constructing broker workers
    # during module import. This only reads the Host-owned runtime metrics.
    from .broker_api import broker_runtime_snapshot

    operational_alert_runtime = OperationalAlertRuntime(
        paths.database,
        storage_report=database_maintenance.storage_report,
        broker_snapshot=broker_runtime_snapshot,
    )
    session_store = AgentSessionStore(paths.database)
    plan_manager = PlanManager(paths.database)
    checkpoint_manager = CheckpointManager(CheckpointStore(paths.database))
    approval_manager = ApprovalManager(paths.database)
    policy_engine = PolicyEngine()
    rollback_manager = RollbackManager()
    memory_manager = MemoryManager(MemoryStore(paths.database), project_root=tools.general_tools.project_root)
    worker_supervisor = WorkerSupervisor(
        paths.database,
        socket_path=paths.socket,
        project_root=tools.general_tools.project_root,
    )
    snapshot_builder = EnvironmentSnapshotBuilder(
        project_root=tools.general_tools.project_root,
        capability_manifest=tools.manifest,
        ui_snapshot=agent_ui_bridge.snapshot,
        account_snapshot=lambda: paper_training_account(refresh_prices=False),
        external_snapshot=tools.external_tools.describe,
    )
    _COMPONENTS = {
        "paths": paths,
        "tools": tools,
        "run_store": run_store,
        "database_maintenance": database_maintenance,
        "database_health": database_health,
        "operational_alert_runtime": operational_alert_runtime,
        "session_store": session_store,
        "plan_manager": plan_manager,
        "checkpoint_manager": checkpoint_manager,
        "approval_manager": approval_manager,
        "policy_engine": policy_engine,
        "rollback_manager": rollback_manager,
        "artifact_store": ArtifactStore(paths.database, paths.artifacts),
        "workflow_store": WorkflowStore(paths.database),
        "memory_manager": memory_manager,
        "worker_supervisor": worker_supervisor,
        "snapshot_builder": snapshot_builder,
        "validator": ValidatorEngine(),
    }
    return _COMPONENTS
