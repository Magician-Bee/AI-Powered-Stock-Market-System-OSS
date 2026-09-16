from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_timestamp(value: str | datetime | None, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError("timestamp is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            if required:
                raise ValueError("timestamp is required")
            return None
        if len(text) == 10:
            text = f"{text}T00:00:00+00:00"
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_identifier_value(identifier_type: str, value: Any) -> str:
    """Return the comparison key while preserving the source value separately."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        raise ValueError("identifier value is required")
    kind = str(identifier_type or "").strip().casefold()
    if kind == "unified_business_no":
        normalized = re.sub(r"\D", "", text)
    elif kind in {"exchange_code", "display_symbol", "source_symbol", "isin", "figi", "lei"}:
        normalized = re.sub(r"\s+", "", text).upper()
    else:
        normalized = " ".join(text.casefold().split())
    if not normalized:
        raise ValueError("identifier value normalizes to an empty value")
    return normalized


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=300)
    authority: str = Field(min_length=1, max_length=100)
    base_url: str | None = None
    license_status: str = Field(min_length=1, max_length=100)
    update_frequency_seconds: int = Field(ge=1)
    reliability_tier: int = Field(ge=1, le=5)
    priority: int = Field(ge=1)
    domains: list[str] = Field(min_length=1)
    failover_source_ids: list[str] = Field(default_factory=list)
    host_aliases: list[str] = Field(default_factory=list)
    field_contract: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class SourceFailureStrategy(BaseModel):
    """Reviewed behavior for transport and upstream failures."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    backoff_seconds: list[float] = Field(default_factory=lambda: [0.15, 0.3])
    retry_http_statuses: list[int] = Field(
        default_factory=lambda: [408, 425, 429, 500, 502, 503, 504]
    )
    on_exhausted: Literal[
        "fail_closed",
        "serve_stale",
        "use_failover",
        "degrade_optional",
    ] = "fail_closed"
    failover_dataset_ids: list[str] = Field(default_factory=list)
    stale_if_error_seconds: int = Field(default=0, ge=0)
    checkpoint_required: bool = True
    preserve_error_payload: bool = True

    @field_validator("backoff_seconds")
    @classmethod
    def validate_backoff(cls, value: list[float]) -> list[float]:
        if any(item < 0 or item > 60 for item in value):
            raise ValueError("backoff_seconds entries must be between 0 and 60")
        return value

    @field_validator("retry_http_statuses")
    @classmethod
    def validate_http_statuses(cls, value: list[int]) -> list[int]:
        if any(item < 400 or item > 599 for item in value):
            raise ValueError("retry_http_statuses entries must be HTTP error statuses")
        return value


class SourceDatasetDefinition(BaseModel):
    """One source-owned dataset endpoint and its normalized field contract."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{1,99}$")
    source_id: str = Field(min_length=1, max_length=100)
    domain: str = Field(min_length=1, max_length=100)
    transport: Literal[
        "http_json",
        "http_xml",
        "http_html",
        "websocket",
        "import_only",
    ]
    method: Literal["GET", "POST"] = "GET"
    endpoint_path: str = Field(min_length=1)
    update_frequency_seconds: int | None = Field(default=None, ge=1)
    reliability_tier: int | None = Field(default=None, ge=1, le=5)
    fields: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)
    query_defaults: dict[str, str] = Field(default_factory=dict)
    failure_strategy: SourceFailureStrategy
    active: bool = True

    @model_validator(mode="after")
    def validate_field_contract(self) -> "SourceDatasetDefinition":
        declared = set(self.fields)
        unknown = (set(self.required_fields) | set(self.optional_fields)) - declared
        if unknown:
            raise ValueError(
                f"{self.dataset_id} classifies undeclared fields: {sorted(unknown)}"
            )
        if self.transport != "import_only" and "://" in self.endpoint_path:
            raise ValueError(
                f"{self.dataset_id} endpoint_path must be relative to its registered source"
            )
        return self


class CachePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(min_length=1, max_length=100)
    ttl_seconds: int = Field(ge=0)
    stale_while_revalidate_seconds: int = Field(default=0, ge=0)
    refresh_lease_seconds: int = Field(default=120, ge=5, le=3600)
    serve_stale_on_error: bool = True
    invalidate_on: list[str] = Field(default_factory=list)

    @field_validator("invalidate_on")
    @classmethod
    def validate_invalidation_reasons(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("cache invalidation reasons cannot be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("cache invalidation reasons must be unique")
        return normalized


class EntityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=100)
    entity_type: Literal[
        "stock",
        "etf",
        "warrant",
        "security",
        "index",
        "fund",
        "bond",
        "currency_pair",
        "interest_rate",
        "commodity",
        "macro_indicator",
        "company",
        "event",
    ]
    canonical_name: str = Field(min_length=1, max_length=500)
    market: str = Field(min_length=1, max_length=100)
    exchange: str | None = None
    currency: str | None = None
    sector: str | None = None
    industry: str | None = None
    lifecycle_status: Literal[
        "pre_listing",
        "active",
        "suspended",
        "delisted",
        "expired",
        "unknown",
    ] = "active"
    listed_at: str | None = None
    delisted_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("listed_at", "delisted_at", mode="before")
    @classmethod
    def validate_optional_timestamp(cls, value: Any) -> str | None:
        return normalize_timestamp(value)


class EntityIdentifierRecord(BaseModel):
    """Source-qualified identifier link with a point-in-time validity interval."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=100)
    identifier_type: Literal[
        "exchange_code",
        "display_symbol",
        "unified_business_no",
        "source_symbol",
        "isin",
        "figi",
        "lei",
        "legal_name",
    ]
    identifier_value: str = Field(min_length=1, max_length=500)
    normalized_value: str | None = Field(default=None, min_length=1, max_length=500)
    entity_id: str = Field(pattern=r"^ENT-[0-9a-fA-F]{32}$")
    valid_from: str | None = None
    valid_to: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_primary: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def validate_identifier_timestamp(cls, value: Any) -> str | None:
        return normalize_timestamp(value)

    @model_validator(mode="after")
    def normalize_and_validate_interval(self) -> "EntityIdentifierRecord":
        self.normalized_value = normalize_identifier_value(
            self.identifier_type,
            self.normalized_value or self.identifier_value,
        )
        if self.valid_from and self.valid_to:
            if datetime.fromisoformat(self.valid_to) <= datetime.fromisoformat(self.valid_from):
                raise ValueError("identifier valid_to must follow valid_from")
        return self


class SecuritySourceSnapshot(BaseModel):
    """One source-specific security or index lifecycle observation."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_dataset: str
    source_url: str
    venue: str
    listing_type: Literal[
        "listed",
        "otc",
        "emerging",
        "etf",
        "warrant",
        "index",
        "delisted",
        "other",
    ]
    entity_type: Literal["stock", "etf", "warrant", "index", "security"]
    code: str
    display_symbol: str
    short_name: str
    legal_name: str
    unified_business_no: str | None = None
    lifecycle_status: Literal[
        "pre_listing",
        "active",
        "suspended",
        "delisted",
        "expired",
        "unknown",
    ]
    listed_at: str | None = None
    delisted_at: str | None = None
    expires_at: str | None = None
    industry: str | None = None
    quote_present: bool = False
    raw_row: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("listed_at", "delisted_at", "expires_at", mode="before")
    @classmethod
    def validate_security_timestamp(cls, value: Any) -> str | None:
        return normalize_timestamp(value)


class TemporalCoordinates(BaseModel):
    """Bitemporal contract separating event/effective time from knowledge time."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.temporal_contract.v1"] = (
        "stock_ai.temporal_contract.v1"
    )
    time_basis: Literal[
        "trade_date",
        "fiscal_period",
        "event_time",
        "lifecycle",
        "snapshot",
    ] = "snapshot"
    trade_date: str | None = None
    fiscal_period: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    observed_at: str | None = None
    published_at: str | None = None
    available_at: str
    acquired_at: str = Field(default_factory=utc_now)
    effective_at: str
    expires_at: str | None = None

    @field_validator(
        "observed_at",
        "published_at",
        "trade_date",
        "period_start",
        "period_end",
        "available_at",
        "acquired_at",
        "effective_at",
        "expires_at",
        mode="before",
    )
    @classmethod
    def validate_timestamp(cls, value: Any, info) -> str | None:
        return normalize_timestamp(
            value,
            required=info.field_name in {"available_at", "acquired_at", "effective_at"},
        )

    @model_validator(mode="after")
    def validate_order(self) -> "TemporalCoordinates":
        available = datetime.fromisoformat(self.available_at)
        acquired = datetime.fromisoformat(self.acquired_at)
        effective = datetime.fromisoformat(self.effective_at)
        if acquired < available:
            raise ValueError("acquired_at cannot precede available_at")
        if self.published_at and available < datetime.fromisoformat(self.published_at):
            raise ValueError("available_at cannot precede published_at")
        if self.expires_at and datetime.fromisoformat(self.expires_at) < effective:
            raise ValueError("expires_at cannot precede effective_at")
        if self.time_basis == "trade_date" and not self.trade_date:
            raise ValueError("trade_date time basis requires trade_date")
        if self.time_basis == "fiscal_period":
            if not self.fiscal_period or not self.period_start or not self.period_end:
                raise ValueError(
                    "fiscal_period time basis requires fiscal_period, period_start and period_end"
                )
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", self.fiscal_period):
                raise ValueError("fiscal_period must use YYYY-MM")
        if self.period_start and self.period_end:
            if datetime.fromisoformat(self.period_end) < datetime.fromisoformat(
                self.period_start
            ):
                raise ValueError("period_end cannot precede period_start")
        return self


TemporalContractV1 = TemporalCoordinates


class SourceFetchPayload(BaseModel):
    """Exact payload returned by one reviewed source-dataset attempt."""

    model_config = ConfigDict(extra="forbid")

    payload: Any
    http_status: int = Field(default=200, ge=100, le=599)
    content_type: str = Field(default="application/json", min_length=1)
    content_encoding: str = Field(default="utf-8", min_length=1)
    raw_body: bytes | str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FailoverObservation(BaseModel):
    """Normalized observation written with the source that actually returned it."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=200)
    observation_key: str = Field(min_length=1, max_length=500)
    temporal: TemporalCoordinates
    payload: dict[str, Any]
    quality_status: Literal["valid", "warning", "invalid", "unavailable"] = "valid"
    quality_flags: list[str] = Field(default_factory=list)
    transformation_id: str = Field(
        default="stock_ai.source_failover_normalizer.v1",
        min_length=1,
    )
    parameters: dict[str, Any] = Field(default_factory=dict)


def payload_leaf_pointers(value: Any, prefix: str = "") -> list[str]:
    """Return deterministic RFC 6901 pointers for every payload leaf."""

    if isinstance(value, dict) and value:
        pointers: list[str] = []
        for key in sorted(value):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            pointers.extend(payload_leaf_pointers(value[key], f"{prefix}/{escaped}"))
        return pointers
    if isinstance(value, list) and value:
        pointers = []
        for index, item in enumerate(value):
            pointers.extend(payload_leaf_pointers(item, f"{prefix}/{index}"))
        return pointers
    return [prefix or "/"]


class FieldProvenanceV1(BaseModel):
    """Trace one normalized payload value to source, time, raw input and quality."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=100)
    temporal: TemporalCoordinates
    updated_at: str
    raw_payload_id: str | None = None
    raw_json_pointer: str = Field(pattern=r"^/")
    quality_status: Literal["valid", "warning", "invalid", "unavailable"]
    quality_flags: list[str] = Field(default_factory=list)
    transformation_id: str = Field(min_length=1)
    input_fields: list[str] = Field(default_factory=list)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_updated_at(cls, value: Any) -> str:
        normalized = normalize_timestamp(value, required=True)
        assert normalized is not None
        return normalized


class DataEnvelopeV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.data_envelope.v2"] = "stock_ai.data_envelope.v2"
    revision_id: str
    dataset: str
    entity_id: str
    observation_key: str
    source_id: str
    revision: int = Field(ge=1)
    temporal: TemporalCoordinates
    payload_hash: str
    raw_payload_id: str | None = None
    quality_status: Literal["valid", "warning", "invalid", "unavailable"]
    quality_flags: list[str] = Field(default_factory=list)
    is_fallback: bool = False
    supersedes_revision_id: str | None = None
    transformation: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any]
    field_provenance: dict[str, FieldProvenanceV1]
    created_at: str

    @model_validator(mode="after")
    def validate_field_traceability(self) -> "DataEnvelopeV2":
        expected = set(payload_leaf_pointers(self.payload))
        actual = set(self.field_provenance)
        missing = expected - actual
        unknown = actual - expected
        if missing:
            raise ValueError(f"payload fields lack provenance: {sorted(missing)}")
        if unknown:
            raise ValueError(f"field provenance references unknown payload fields: {sorted(unknown)}")
        return self


class DataQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str
    entity_id: str | None = None
    as_of: str | None = None
    knowledge_at: str | None = None
    effective_at: str | None = None
    source_id: str | None = None
    limit: int = Field(default=500, ge=1, le=10000)

    @field_validator("as_of", "knowledge_at", "effective_at", mode="before")
    @classmethod
    def validate_query_timestamp(cls, value: Any) -> str | None:
        return normalize_timestamp(value)

    @model_validator(mode="after")
    def resolve_bitemporal_cutoffs(self) -> "DataQuery":
        compatibility_as_of = self.as_of or utc_now()
        self.as_of = normalize_timestamp(compatibility_as_of, required=True)
        self.knowledge_at = normalize_timestamp(
            self.knowledge_at or self.as_of,
            required=True,
        )
        self.effective_at = normalize_timestamp(
            self.effective_at or self.as_of,
            required=True,
        )
        return self
