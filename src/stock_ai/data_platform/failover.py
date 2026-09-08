from __future__ import annotations

from collections.abc import Callable, Iterable
from time import sleep
from typing import Any

from .contracts import (
    FailoverObservation,
    SourceDatasetDefinition,
    SourceFetchPayload,
    normalize_timestamp,
    utc_now,
)


FetchSourceDataset = Callable[
    [SourceDatasetDefinition, str, int],
    SourceFetchPayload | dict[str, Any],
]
NormalizeSourceDataset = Callable[
    [Any, dict[str, Any]],
    Iterable[FailoverObservation | dict[str, Any]],
]


class SourceFetchError(RuntimeError):
    """Transport/upstream failure with optional evidence safe for raw capture."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_payload: Any | None = None,
        content_type: str = "application/json",
        raw_body: bytes | str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_payload = response_payload
        self.content_type = content_type
        self.raw_body = raw_body


class SourceNormalizationError(RuntimeError):
    """A source returned data that its reviewed normalizer could not consume."""


class SourceFailoverExhausted(RuntimeError):
    """Every reviewed source-dataset candidate failed."""

    def __init__(self, run_id: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"Source failover exhausted for run {run_id}")
        self.run_id = run_id
        self.errors = errors


class SourceFailoverService:
    """Execute reviewed retries/failover while retaining actual source identity."""

    def __init__(
        self,
        platform: Any,
        *,
        sleeper: Callable[[float], None] = sleep,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.platform = platform
        self.registry = platform.source_registry_service
        self.warehouse = platform.warehouse
        self.sleeper = sleeper
        self.clock = clock

    def ingest(
        self,
        *,
        dataset_id: str,
        normalized_dataset: str,
        fetch: FetchSourceDataset,
        normalize: NormalizeSourceDataset,
        endpoint_parameters: dict[str, Any] | None = None,
        partition_key: str = "all",
        acquired_at: str | None = None,
    ) -> dict[str, Any]:
        requested = self.registry.dataset(dataset_id)
        chain = self.registry.failover_chain(dataset_id)
        acquired = normalize_timestamp(acquired_at or self.clock(), required=True)
        assert acquired is not None
        policy = {
            "schema_version": "stock_ai.source_failover_policy.v1",
            "requested_dataset_id": dataset_id,
            "requested_source_id": requested.source_id,
            "candidates": [
                {
                    "dataset_id": item.dataset_id,
                    "source_id": item.source_id,
                    "failure_strategy": item.failure_strategy.model_dump(mode="json"),
                }
                for item in chain
            ],
        }
        run_id = self.warehouse.start_source_failover_run(
            requested_dataset_id=dataset_id,
            requested_source_id=requested.source_id,
            normalized_dataset=normalized_dataset,
            policy=policy,
            started_at=self.clock(),
        )
        parameters = dict(endpoint_parameters or {})
        sequence = 0
        errors: list[dict[str, Any]] = []

        for failover_depth, candidate in enumerate(chain):
            strategy = candidate.failure_strategy
            for attempt_number in range(1, strategy.max_attempts + 1):
                sequence += 1
                attempt_started = self.clock()
                request_url: str | None = None
                raw_payload_id: str | None = None
                response: SourceFetchPayload | None = None
                phase = "endpoint"
                try:
                    request_url = self.registry.endpoint(
                        candidate.dataset_id,
                        **parameters,
                    )
                    phase = "fetch"
                    response = SourceFetchPayload.model_validate(
                        fetch(candidate, request_url, attempt_number)
                    )
                    if response.http_status >= 400:
                        raise SourceFetchError(
                            f"Upstream returned HTTP {response.http_status}",
                            http_status=response.http_status,
                            response_payload=response.payload,
                            content_type=response.content_type,
                            raw_body=response.raw_body,
                        )
                    source_provenance = {
                        "schema_version": "stock_ai.source_failover_provenance.v1",
                        "run_id": run_id,
                        "requested_dataset_id": dataset_id,
                        "requested_source_id": requested.source_id,
                        "resolved_dataset_id": candidate.dataset_id,
                        "resolved_source_id": candidate.source_id,
                        "is_failover": failover_depth > 0,
                        "failover_depth": failover_depth,
                        "attempt_number": attempt_number,
                        "acquired_at": acquired,
                        "source_difference_preserved": True,
                    }
                    raw_payload_id = self.warehouse.record_raw_payload(
                        source_id=candidate.source_id,
                        payload=response.payload,
                        request_url=request_url,
                        requested_at=attempt_started,
                        received_at=self.clock(),
                        http_status=response.http_status,
                        content_type=response.content_type,
                        content_encoding=response.content_encoding,
                        raw_body=response.raw_body,
                        metadata={
                            **response.metadata,
                            "dataset": normalized_dataset,
                            "source_dataset": candidate.dataset_id,
                            "source_failover": source_provenance,
                        },
                    )
                    phase = "normalize"
                    try:
                        observations = [
                            FailoverObservation.model_validate(item)
                            for item in normalize(response.payload, source_provenance)
                        ]
                    except Exception as exc:
                        raise SourceNormalizationError(str(exc)) from exc
                    phase = "persist"
                    revisions = [
                        self.warehouse.write_revision(
                            dataset=normalized_dataset,
                            entity_id=item.entity_id,
                            observation_key=item.observation_key,
                            source_id=candidate.source_id,
                            temporal=item.temporal,
                            payload=item.payload,
                            raw_payload_id=raw_payload_id,
                            quality_status=item.quality_status,
                            quality_flags=item.quality_flags,
                            is_fallback=failover_depth > 0,
                            transformation_id=item.transformation_id,
                            code_version=self.platform.code_version,
                            parameters={
                                **item.parameters,
                                "source_failover": source_provenance,
                            },
                        )
                        for item in observations
                    ]
                    revision_ids = [item.revision_id for item in revisions]
                    cache_entry = self.platform.cache_policy_service.mark_refreshed(
                        source_id=candidate.source_id,
                        dataset=normalized_dataset,
                        partition_key=partition_key,
                        refreshed_at=acquired,
                    )
                    completed = self.clock()
                    attempt_id = self.warehouse.record_source_failover_attempt(
                        run_id=run_id,
                        sequence=sequence,
                        dataset_id=candidate.dataset_id,
                        source_id=candidate.source_id,
                        failover_depth=failover_depth,
                        attempt_number=attempt_number,
                        request_url=request_url,
                        status="succeeded",
                        http_status=response.http_status,
                        raw_payload_id=raw_payload_id,
                        revision_ids=revision_ids,
                        started_at=attempt_started,
                        completed_at=completed,
                    )
                    self.warehouse.finish_source_failover_run(
                        run_id,
                        status="succeeded",
                        selected_dataset_id=candidate.dataset_id,
                        selected_source_id=candidate.source_id,
                        is_failover=failover_depth > 0,
                        attempt_count=sequence,
                        revision_ids=revision_ids,
                        completed_at=completed,
                    )
                    return {
                        "schema_version": "stock_ai.source_failover_result.v1",
                        "status": "succeeded",
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "requested_dataset_id": dataset_id,
                        "requested_source_id": requested.source_id,
                        "selected_dataset_id": candidate.dataset_id,
                        "selected_source_id": candidate.source_id,
                        "is_failover": failover_depth > 0,
                        "failover_depth": failover_depth,
                        "attempt_count": sequence,
                        "raw_payload_id": raw_payload_id,
                        "revision_ids": revision_ids,
                        "source_difference_preserved": True,
                        "cache": cache_entry,
                    }
                except Exception as exc:
                    completed = self.clock()
                    http_status = (
                        exc.http_status if isinstance(exc, SourceFetchError) else None
                    )
                    if (
                        isinstance(exc, SourceFetchError)
                        and strategy.preserve_error_payload
                        and exc.response_payload is not None
                    ):
                        raw_payload_id = self.warehouse.record_raw_payload(
                            source_id=candidate.source_id,
                            payload=exc.response_payload,
                            request_url=request_url,
                            requested_at=attempt_started,
                            received_at=completed,
                            http_status=http_status,
                            content_type=exc.content_type,
                            raw_body=exc.raw_body,
                            metadata={
                                "dataset": normalized_dataset,
                                "source_dataset": candidate.dataset_id,
                                "source_failover_run_id": run_id,
                                "failed_attempt": True,
                                "phase": phase,
                            },
                        )
                    error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "phase": phase,
                        "dataset_id": candidate.dataset_id,
                        "source_id": candidate.source_id,
                        "http_status": http_status,
                    }
                    errors.append(error)
                    self.warehouse.record_source_failover_attempt(
                        run_id=run_id,
                        sequence=sequence,
                        dataset_id=candidate.dataset_id,
                        source_id=candidate.source_id,
                        failover_depth=failover_depth,
                        attempt_number=attempt_number,
                        request_url=request_url,
                        status="failed",
                        http_status=http_status,
                        raw_payload_id=raw_payload_id,
                        error=error,
                        started_at=attempt_started,
                        completed_at=completed,
                    )
                    retryable = (
                        phase == "fetch"
                        and (
                            http_status is None
                            or http_status in strategy.retry_http_statuses
                        )
                    )
                    if not retryable or attempt_number >= strategy.max_attempts:
                        break
                    backoff_index = attempt_number - 1
                    if backoff_index < len(strategy.backoff_seconds):
                        self.sleeper(float(strategy.backoff_seconds[backoff_index]))

        completed = self.clock()
        final_error = {
            "type": "SourceFailoverExhausted",
            "message": "Every reviewed source-dataset candidate failed",
            "attempt_errors": errors,
        }
        self.warehouse.finish_source_failover_run(
            run_id,
            status="failed",
            attempt_count=sequence,
            error=final_error,
            completed_at=completed,
        )
        raise SourceFailoverExhausted(run_id, errors)

    def status(self) -> dict[str, Any]:
        return self.warehouse.source_failover_status()

    def runs(self, *, limit: int = 100) -> dict[str, Any]:
        items = self.warehouse.list_source_failover_runs(limit=limit)
        return {
            "schema_version": "stock_ai.source_failover_runs.v1",
            "count": len(items),
            "items": items,
        }

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self.warehouse.source_failover_run(run_id)
