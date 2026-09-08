from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from .contracts import DataQuery
from .service import get_market_data_platform
from .ui_api import unified_data_api_contract


router = APIRouter(prefix="/api/data", tags=["unified-market-data"])
logger = logging.getLogger(__name__)


def _source_freshness_evidence(dashboard: dict[str, Any]) -> tuple[bool, datetime | None, dict[str, Any]]:
    """Derive one fail-closed SLO sample from actual source timestamps.

    ``generated_at`` only says when the dashboard was evaluated; it must never
    be substituted for the timestamp of market data.  The oldest usable source
    timestamp is the conservative freshness bound for this evaluation.
    """

    sources = list(dashboard.get("sources") or [])
    timestamps: list[datetime] = []
    unavailable: list[str] = []
    for source in sources:
        source_key = f"{source.get('source_id') or '-'}:{source.get('dataset') or '-'}"
        raw_timestamp = source.get("latest_success_at")
        if not raw_timestamp or str(source.get("lag_status") or "") != "passed":
            unavailable.append(source_key)
            continue
        try:
            timestamps.append(datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00")))
        except ValueError:
            unavailable.append(source_key)

    data_at = min(timestamps) if timestamps and not unavailable else None
    success = bool(sources) and dashboard.get("status") == "passed" and not unavailable
    return success, data_at, {
        "source_count": len(sources),
        "dashboard_status": str(dashboard.get("status") or "unknown"),
        "fresh_source_count": len(timestamps),
        "unavailable_sources": unavailable,
        "data_at": data_at.isoformat() if data_at else None,
    }


def _record_source_freshness_slo(
    dashboard: dict[str, Any],
    *,
    runtime: Any,
    latency_ms: float,
) -> dict[str, Any]:
    """Record the dashboard evaluation without allowing observability failure to rewrite it."""

    success, data_at, evidence = _source_freshness_evidence(dashboard)
    try:
        observation = runtime.record_slo_observation(
            "data.freshness",
            latency_ms=latency_ms,
            success=success,
            data_at=data_at,
        )
    except Exception:
        logger.exception("Unable to persist source freshness SLO observation")
        return {"status": "recording_failed", "evidence": evidence}
    return {"status": "recorded", "observation": observation, "evidence": evidence}


@router.get("/status")
def data_platform_status(
    summary: bool = Query(default=False),
) -> dict[str, Any]:
    return get_market_data_platform().status(operations_summary=summary)


@router.get("/ui/v1/contract")
def unified_ui_data_api_contract() -> dict[str, Any]:
    return unified_data_api_contract()


@router.get("/sources")
def data_sources() -> dict[str, Any]:
    return get_market_data_platform().source_registry()


@router.get("/cache")
def cache_status(
    dataset: str | None = Query(default=None, min_length=1, max_length=200),
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return get_market_data_platform().cache_status(
            dataset=dataset,
            as_of=as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/cache/{dataset}")
def dataset_cache_status(
    dataset: str,
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return get_market_data_platform().cache_status(
            dataset=dataset,
            as_of=as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cache/{dataset}/invalidate")
def invalidate_dataset_cache(
    dataset: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    try:
        return get_market_data_platform().invalidate_cache(
            dataset=dataset,
            reason=str(payload.get("reason") or "manual_refresh"),
            source_id=(
                str(payload["source_id"]) if payload.get("source_id") else None
            ),
            partition_key=(
                str(payload["partition_key"])
                if payload.get("partition_key")
                else None
            ),
            invalidated_at=(
                str(payload["invalidated_at"])
                if payload.get("invalidated_at")
                else None
            ),
            metadata=(
                dict(payload["metadata"])
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/failover")
def source_failover_status() -> dict[str, Any]:
    return get_market_data_platform().source_failover_status()


@router.get("/failover/runs")
def source_failover_runs(
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    return get_market_data_platform().source_failover_runs(limit=limit)


@router.get("/failover/runs/{run_id}")
def source_failover_run(run_id: str) -> dict[str, Any]:
    item = get_market_data_platform().source_failover_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Source failover run not found")
    return {
        "schema_version": "stock_ai.source_failover_run.v1",
        "item": item,
    }


@router.get("/observability")
def source_observability(
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return get_market_data_platform().source_observability(as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/observability/notify")
def notify_source_observability(
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        started = time.monotonic()
        result = get_market_data_platform().notify_source_observability(as_of=as_of)
        # Keep the data platform usable on its own; the durable Agent runtime
        # is an additional audit sink, not a prerequisite for source alerts.
        from ..agent_service import get_agent_run_runtime

        return {
            **result,
            "slo_observation": _record_source_freshness_slo(
                result,
                runtime=get_agent_run_runtime(),
                latency_ms=(time.monotonic() - started) * 1000.0,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/entity-registry")
def entity_registry_status() -> dict[str, Any]:
    return get_market_data_platform().entity_registry.status()


@router.get("/entity-registry/resolve")
def resolve_entity_identifier(
    identifier: str = Query(min_length=1, max_length=500),
    source_id: str | None = Query(default=None, min_length=1, max_length=100),
    identifier_type: str | None = Query(default=None, min_length=1, max_length=100),
    as_of: str | None = None,
) -> dict[str, Any]:
    try:
        return get_market_data_platform().resolve_entity(
            identifier,
            source_id=source_id,
            identifier_type=identifier_type,
            as_of=as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/entities")
def data_entities(
    q: str = "",
    market: str = "all",
    limit: int = Query(default=200, ge=1, le=10000),
) -> dict[str, Any]:
    items = get_market_data_platform().securities(query=q, market=market, limit=limit)
    return {
        "schema_version": "stock_ai.entity_registry_query.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/security-lifecycle")
def security_lifecycle_summary() -> dict[str, Any]:
    warehouse = get_market_data_platform().warehouse
    return {
        **warehouse.lifecycle_summary(),
        "quality": warehouse.security_lifecycle_quality(),
    }


@router.get("/entities/{entity_id}/lifecycle")
def entity_lifecycle(entity_id: str) -> dict[str, Any]:
    payload = get_market_data_platform().warehouse.entity_lifecycle(entity_id)
    if payload["entity"] is None:
        raise HTTPException(status_code=404, detail="Market entity not found")
    return payload


@router.get("/entities/{entity_id}")
def entity_profile(entity_id: str) -> dict[str, Any]:
    payload = get_market_data_platform().warehouse.entity_profile(entity_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Market entity not found")
    return {
        "schema_version": "stock_ai.entity_profile.v1",
        "entity": payload,
    }


@router.post("/query")
def query_data(request: DataQuery) -> dict[str, Any]:
    items = get_market_data_platform().warehouse.query(request)
    return {
        "schema_version": "stock_ai.unified_data_query.v1",
        "query": request.model_dump(mode="json"),
        "count": len(items),
        "items": [item.model_dump(mode="json") for item in items],
    }


@router.get("/warehouse/{domain}")
def query_standard_warehouse(
    domain: str,
    dataset: str | None = None,
    entity_id: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = Query(default=100, ge=1, le=10000),
) -> dict[str, Any]:
    try:
        items = get_market_data_platform().warehouse.standard_records(
            domain,
            dataset=dataset,
            entity_id=entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "schema_version": "stock_ai.standard_warehouse_query.v1",
        "domain": domain,
        "dataset": dataset,
        "count": len(items),
        "items": items,
    }


@router.get("/ingestion/runs")
def ingestion_runs(
    limit: int = Query(default=50, ge=1, le=1000),
) -> dict[str, Any]:
    items = get_market_data_platform().warehouse.list_ingestion_runs(limit=limit)
    return {
        "schema_version": "stock_ai.incremental_ingestion_runs.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/ingestion/runs/{run_id}")
def ingestion_run(run_id: str) -> dict[str, Any]:
    item = get_market_data_platform().warehouse.ingestion_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found")
    return {
        "schema_version": "stock_ai.incremental_ingestion_run.v1",
        "item": item,
    }


@router.get("/revisions/history")
def revision_history(
    dataset: str = Query(min_length=1, max_length=200),
    entity_id: str = Query(min_length=1, max_length=200),
    observation_key: str = Query(min_length=1, max_length=500),
    source_id: str | None = Query(default=None, min_length=1, max_length=200),
) -> dict[str, Any]:
    return get_market_data_platform().warehouse.revision_history(
        dataset=dataset,
        entity_id=entity_id,
        observation_key=observation_key,
        source_id=source_id,
    )


@router.post("/snapshots")
def create_revision_snapshot(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not payload.get("dataset"):
        raise HTTPException(status_code=422, detail="Missing field: dataset")
    try:
        snapshot = get_market_data_platform().warehouse.create_revision_snapshot(
            dataset=str(payload["dataset"]),
            knowledge_at=(
                str(payload["knowledge_at"]) if payload.get("knowledge_at") else None
            ),
            effective_at=(
                str(payload["effective_at"]) if payload.get("effective_at") else None
            ),
            entity_id=str(payload["entity_id"]) if payload.get("entity_id") else None,
            source_id=str(payload["source_id"]) if payload.get("source_id") else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "schema_version": "stock_ai.revision_snapshot_create.v1",
        "snapshot": snapshot,
    }


@router.get("/snapshots/{snapshot_id}")
def revision_snapshot(
    snapshot_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=10000),
) -> dict[str, Any]:
    snapshot = get_market_data_platform().warehouse.revision_snapshot(
        snapshot_id,
        offset=offset,
        limit=limit,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Revision snapshot not found")
    return snapshot


@router.get("/revisions/{revision_id}/lineage")
def data_lineage(revision_id: str) -> dict[str, Any]:
    payload = get_market_data_platform().warehouse.lineage(revision_id)
    if payload["revision"] is None:
        raise HTTPException(status_code=404, detail="Data revision not found")
    return payload


@router.get("/lineage/artifacts")
def data_lineage_artifacts(
    artifact_type: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    items = get_market_data_platform().warehouse.list_lineage_artifacts(
        artifact_type=artifact_type,
        entity_id=entity_id,
        limit=limit,
    )
    return {
        "schema_version": "stock_ai.data_lineage_artifact_list.v1",
        "count": len(items),
        "items": items,
    }


@router.post("/lineage/artifacts")
def create_data_lineage_artifact(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    try:
        artifact = get_market_data_platform().warehouse.record_lineage_artifact(
            artifact_type=str(payload.get("artifact_type") or ""),
            name=str(payload.get("name") or ""),
            value=payload.get("value"),
            inputs=(
                payload["inputs"]
                if isinstance(payload.get("inputs"), list)
                else []
            ),
            transformation_id=str(payload.get("transformation_id") or ""),
            code_version=get_market_data_platform().code_version,
            entity_id=(
                str(payload["entity_id"]) if payload.get("entity_id") else None
            ),
            observation_key=(
                str(payload["observation_key"])
                if payload.get("observation_key")
                else None
            ),
            quality_status=str(payload.get("quality_status") or "valid"),
            parameters=(
                dict(payload["parameters"])
                if isinstance(payload.get("parameters"), dict)
                else {}
            ),
            metadata=(
                dict(payload["metadata"])
                if isinstance(payload.get("metadata"), dict)
                else {}
            ),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Lineage input not found: {exc.args[0]}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    graph = get_market_data_platform().warehouse.lineage_graph(
        artifact["artifact_id"]
    )
    return {
        "schema_version": "stock_ai.data_lineage_artifact_create.v1",
        "artifact": artifact,
        "graph": graph,
    }


@router.get("/lineage/{target_id}")
def complete_data_lineage(target_id: str) -> dict[str, Any]:
    graph = get_market_data_platform().warehouse.lineage_graph(target_id)
    if graph["target"] is None:
        raise HTTPException(status_code=404, detail="Lineage target not found")
    return graph


@router.get("/raw")
def raw_data_objects(
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    items = get_market_data_platform().warehouse.list_raw_payloads(limit=limit)
    return {
        "schema_version": "stock_ai.raw_data_lake_query.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/raw/{raw_payload_id}")
def raw_data_object(raw_payload_id: str) -> dict[str, Any]:
    payload = get_market_data_platform().warehouse.raw_payload(raw_payload_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Raw payload not found")
    return payload


@router.post("/raw/{raw_payload_id}/reprocess")
def reprocess_raw_data(
    raw_payload_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    try:
        return get_market_data_platform().warehouse.reprocess_raw_payload(
            raw_payload_id,
            dataset=str(payload["dataset"]) if payload.get("dataset") else None,
            transformation_id="stock_ai.raw_cleaning_replay.v1",
            code_version=get_market_data_platform().code_version,
            parameters=(
                dict(payload["parameters"])
                if isinstance(payload.get("parameters"), dict)
                else {}
            ),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Raw payload not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/quality/daily")
def daily_data_quality(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    datasets = payload.get("datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise HTTPException(status_code=422, detail="datasets must be a list")
    try:
        return get_market_data_platform().run_daily_quality_reports(
            datasets=[str(item) for item in datasets] if datasets is not None else None,
            report_date=(
                str(payload["report_date"]) if payload.get("report_date") else None
            ),
            knowledge_at=(
                str(payload["knowledge_at"]) if payload.get("knowledge_at") else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/quality/reports")
def quality_reports(
    dataset: str | None = Query(default=None, min_length=1, max_length=200),
    report_date: str | None = Query(default=None, min_length=10, max_length=10),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    return get_market_data_platform().quality_reports(
        dataset=dataset,
        report_date=report_date,
        limit=limit,
    )


@router.get("/quality/reports/{report_id}")
def quality_report_detail(report_id: str) -> dict[str, Any]:
    report = get_market_data_platform().quality_report_detail(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Quality report not found")
    return report


@router.post("/quality/{dataset}")
def data_quality(dataset: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    try:
        return get_market_data_platform().daily_quality_report(
            dataset=dataset,
            partition_key=str(payload.get("partition_key") or "all"),
            report_date=(
                str(payload["report_date"]) if payload.get("report_date") else None
            ),
            knowledge_at=(
                str(payload["knowledge_at"]) if payload.get("knowledge_at") else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/reconcile")
def reconcile_data(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    required = {"dataset", "entity_id", "observation_key", "field_names"}
    missing = required - set(payload)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {sorted(missing)}")
    return get_market_data_platform().warehouse.reconcile(
        dataset=str(payload["dataset"]),
        entity_id=str(payload["entity_id"]),
        observation_key=str(payload["observation_key"]),
        field_names=[str(item) for item in payload["field_names"]],
        tolerance=float(payload.get("tolerance") or 0),
        as_of=str(payload["as_of"]) if payload.get("as_of") else None,
    )


@router.get("/reconciliation")
def reconciliation_status() -> dict[str, Any]:
    return get_market_data_platform().reconciliation_status()


@router.post("/reconciliation/runs")
def run_reconciliation(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not payload.get("dataset"):
        raise HTTPException(status_code=422, detail="Missing field: dataset")
    try:
        return get_market_data_platform().reconcile_sources(
            dataset=str(payload["dataset"]),
            entity_id=(
                str(payload["entity_id"]) if payload.get("entity_id") else None
            ),
            observation_key=(
                str(payload["observation_key"])
                if payload.get("observation_key")
                else None
            ),
            knowledge_at=(
                str(payload["knowledge_at"])
                if payload.get("knowledge_at")
                else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/reconciliation/runs")
def reconciliation_runs(
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    return get_market_data_platform().reconciliation_runs(limit=limit)


@router.get("/reconciliation/runs/{run_id}")
def reconciliation_run(run_id: str) -> dict[str, Any]:
    item = get_market_data_platform().reconciliation_run(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")
    return {
        "schema_version": "stock_ai.reconciliation_run.v1",
        "item": item,
    }


@router.get("/reconciliation/conflicts")
def reconciliation_conflicts(
    dataset: str | None = Query(default=None, min_length=1, max_length=200),
    status: str | None = Query(default="open", pattern="^(open|resolved)$"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    return get_market_data_platform().reconciliation_conflicts(
        dataset=dataset,
        status=status,
        limit=limit,
    )
