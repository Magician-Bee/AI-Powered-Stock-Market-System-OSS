from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import urljoin, urlparse
import re

import yaml

from .contracts import CachePolicy, SourceDatasetDefinition, SourceDefinition
from .availability import get_data_availability_registry


SCHEMA_VERSION = "stock_ai.source_registry.v2"
DEFAULT_SOURCE_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "market_data_sources.yaml"
)


class SourceRegistry:
    """Validated source, dataset, endpoint and failure-policy registry."""

    def __init__(self, path: str | Path = DEFAULT_SOURCE_REGISTRY_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Invalid market data source registry schema; expected {SCHEMA_VERSION}")

        source_items = [
            SourceDefinition.model_validate(item) for item in payload.get("sources") or []
        ]
        dataset_items = [
            SourceDatasetDefinition.model_validate(item)
            for item in payload.get("datasets") or []
        ]
        policy_items = [
            CachePolicy.model_validate(item)
            for item in payload.get("cache_policies") or []
        ]
        if not source_items:
            raise ValueError("Market data source registry cannot be empty")
        if not dataset_items:
            raise ValueError("Market data dataset registry cannot be empty")
        self.sources = self._unique(source_items, "source_id")
        self.datasets = self._unique(dataset_items, "dataset_id")
        self.cache_policies = self._unique(policy_items, "dataset")
        self._validate_references()
        availability_registry = get_data_availability_registry()
        availability_registry.validate_source_coverage(set(self.sources))
        availability_registry.validate_endpoint_coverage(self.datasets.values())
        self.availability_audit = availability_registry.audit_receipt(self.datasets.values())

    @staticmethod
    def _unique(items: list[Any], attribute: str) -> dict[str, Any]:
        result = {str(getattr(item, attribute)): item for item in items}
        if len(result) != len(items):
            raise ValueError(f"Market data registry {attribute} values must be unique")
        return result

    def _validate_references(self) -> None:
        source_ids = set(self.sources)
        dataset_ids = set(self.datasets)
        for source in self.sources.values():
            unknown = set(source.failover_source_ids) - source_ids
            if unknown:
                raise ValueError(
                    f"{source.source_id} references unknown failover sources: {sorted(unknown)}"
                )
            if not source.base_url and any(
                item.source_id == source.source_id and item.transport != "import_only"
                for item in self.datasets.values()
            ):
                raise ValueError(f"{source.source_id} requires base_url for network datasets")
        for dataset in self.datasets.values():
            if dataset.source_id not in source_ids:
                raise ValueError(
                    f"{dataset.dataset_id} references unknown source: {dataset.source_id}"
                )
            unknown = (
                set(dataset.failure_strategy.failover_dataset_ids) - dataset_ids
            )
            if unknown:
                raise ValueError(
                    f"{dataset.dataset_id} references unknown failover datasets: {sorted(unknown)}"
                )
            strategy = dataset.failure_strategy
            if strategy.on_exhausted == "use_failover" and not strategy.failover_dataset_ids:
                raise ValueError(
                    f"{dataset.dataset_id} requires failover_dataset_ids for use_failover"
                )
            for failover_dataset_id in strategy.failover_dataset_ids:
                failover_source_id = self.datasets[failover_dataset_id].source_id
                primary_source = self.sources[dataset.source_id]
                if failover_source_id not in primary_source.failover_source_ids:
                    raise ValueError(
                        f"{dataset.dataset_id} failover source {failover_source_id} "
                        f"is not reviewed by {primary_source.source_id}"
                    )
        for dataset_id in self.datasets:
            self.failover_chain(dataset_id)

    def source(self, source_id: str) -> SourceDefinition:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise KeyError(f"Unknown source_id: {source_id}") from exc

    def dataset(self, dataset_id: str) -> SourceDatasetDefinition:
        try:
            return self.datasets[dataset_id]
        except KeyError as exc:
            raise KeyError(f"Unknown source dataset_id: {dataset_id}") from exc

    def failover_chain(self, dataset_id: str) -> list[SourceDatasetDefinition]:
        """Return the reviewed, deterministic primary-to-failover traversal."""

        ordered: list[SourceDatasetDefinition] = []
        visited: set[str] = set()
        active_path: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in active_path:
                raise ValueError(f"Source failover cycle detected at {current_id}")
            if current_id in visited:
                return
            active_path.add(current_id)
            current = self.dataset(current_id)
            ordered.append(current)
            visited.add(current_id)
            if current.failure_strategy.on_exhausted == "use_failover":
                for fallback_id in current.failure_strategy.failover_dataset_ids:
                    visit(fallback_id)
            active_path.remove(current_id)

        visit(dataset_id)
        return ordered

    def endpoint(self, dataset_id: str, **parameters: Any) -> str:
        dataset = self.dataset(dataset_id)
        source = self.source(dataset.source_id)
        if dataset.transport == "import_only":
            return dataset.endpoint_path
        values = {**dataset.query_defaults, **parameters}
        required = {
            field_name
            for _, field_name, _, _ in Formatter().parse(dataset.endpoint_path)
            if field_name
        }
        missing = sorted(required - set(values))
        if missing:
            raise ValueError(
                f"{dataset_id} endpoint requires parameters: {missing}"
            )
        relative = dataset.endpoint_path.format_map(
            {key: str(value) for key, value in values.items()}
        )
        base_url = str(source.base_url or "").rstrip("/") + "/"
        return urljoin(base_url, relative.lstrip("/"))

    def dataset_contract(self, dataset_id: str) -> dict[str, Any]:
        dataset = self.dataset(dataset_id)
        source = self.source(dataset.source_id)
        payload = dataset.model_dump(mode="json")
        payload.update(
            {
                "source_display_name": source.display_name,
                "authority": source.authority,
                "license_status": source.license_status,
                "effective_update_frequency_seconds": (
                    dataset.update_frequency_seconds
                    or source.update_frequency_seconds
                ),
                "effective_reliability_tier": (
                    dataset.reliability_tier or source.reliability_tier
                ),
                "endpoint_template": self._endpoint_template(dataset_id),
            }
        )
        return payload

    def _endpoint_template(self, dataset_id: str) -> str:
        dataset = self.dataset(dataset_id)
        placeholders = {
            field_name: f"{{{field_name}}}"
            for _, field_name, _, _ in Formatter().parse(dataset.endpoint_path)
            if field_name
        }
        return self.endpoint(dataset_id, **placeholders)

    def identify_url(self, url: str) -> SourceDefinition | None:
        host = (urlparse(str(url or "")).hostname or "").casefold()
        if not host:
            return None
        candidates: list[SourceDefinition] = []
        for source in self.sources.values():
            registered_hosts = {
                (urlparse(source.base_url or "").hostname or "").casefold(),
                *(alias.casefold() for alias in source.host_aliases),
            }
            if any(
                registered and (host == registered or host.endswith(f".{registered}"))
                for registered in registered_hosts
            ):
                candidates.append(source)
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (item.priority, item.source_id))[0]

    def identify_dataset_url(self, url: str) -> SourceDatasetDefinition | None:
        """Resolve a concrete request URL back to its reviewed dataset contract."""

        matches: list[tuple[int, SourceDatasetDefinition]] = []
        for dataset_id, dataset in self.datasets.items():
            if dataset.transport == "import_only":
                continue
            template = self._endpoint_template(dataset_id)
            pattern_parts: list[str] = []
            placeholder_count = 0
            for literal, field_name, _, _ in Formatter().parse(template):
                pattern_parts.append(re.escape(literal))
                if field_name:
                    placeholder_count += 1
                    pattern_parts.append(r".+?")
            if re.fullmatch("".join(pattern_parts), str(url)):
                matches.append((placeholder_count, dataset))
        if not matches:
            return None
        return sorted(matches, key=lambda item: (item[0], item[1].dataset_id))[0][1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "catalog_path": str(self.path),
            "count": len(self.sources),
            "dataset_count": len(self.datasets),
            "items": [
                source.model_dump(mode="json")
                for source in sorted(
                    self.sources.values(), key=lambda item: (item.priority, item.source_id)
                )
            ],
            "datasets": [
                self.dataset_contract(dataset_id)
                for dataset_id in sorted(self.datasets)
            ],
            "cache_policies": [
                policy.model_dump(mode="json")
                for policy in sorted(
                    self.cache_policies.values(), key=lambda item: item.dataset
                )
            ],
            "availability_audit": self.availability_audit,
        }


@lru_cache(maxsize=8)
def get_source_registry(path: str | Path = DEFAULT_SOURCE_REGISTRY_PATH) -> SourceRegistry:
    return SourceRegistry(path)


def source_endpoint(dataset_id: str, **parameters: Any) -> str:
    return get_source_registry().endpoint(dataset_id, **parameters)
