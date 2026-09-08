# Unified Market Data Platform

## Scope

This document is the Phase 1 architecture contract for `DATA-001` through
`DATA-015`. It replaces the earlier pattern where API handlers, UI services,
Agent tools and external frameworks fetched and normalized their own copies of
market data.

The only supported normalized read path is:

```text
Connector / import
  -> immutable raw payload + SHA-256
  -> source-specific normalizer
  -> versioned DataEnvelopeV2
  -> point-in-time MarketDataWarehouse
  -> MarketDataPlatform
  -> API / Agent / research / UI / external workflow
```

Connectors may perform network I/O. Consumers may not call a connector
directly. A source outage may select an explicitly registered failover, but the
returned envelope retains its actual `source_id` and `is_fallback` state.

## Contracts

### Source Registry

`config/market_data_sources.yaml` is the authoritative v2 registry for source
identity, authority, licensing state, update frequency, reliability tier,
priority, supported domains and source-level failover. The same file owns every
production market-data dataset endpoint, normalized field set and typed failure
strategy (attempts, timeout, backoff, retry statuses, stale behavior, failover
dataset and checkpoint/error preservation). Runtime code does not infer these
properties from a URL or a display label.

Dataset endpoint paths must be relative to their registered source. Startup
rejects duplicate IDs, absolute endpoint URLs, unknown source/failover
references, undeclared required fields and a `use_failover` policy without an
actual fallback. Concrete request URLs can be resolved back to the reviewed
dataset contract for runtime policy and audit evidence.

A source marked `*_terms_review_required` is usable only inside its stated
internal research boundary until the applicable terms have been formally
reviewed. TWSE trading information remains contract-gated and is not treated
as redistributable merely because a public page is reachable.

### Entity Registry

`market_entities` stores a stable internal `ENT-*` ID. Display symbols such as
`.TW` and `.TWO` are records in `entity_identifiers`; they are not primary
keys. Identifier validity intervals allow code, exchange and lifecycle changes
without changing historical observations. Migration 13 stores normalized
comparison values, mapping confidence and the current primary alias. The
`EntityRegistry` resolves source-qualified aliases, business numbers and
historical `as_of` intervals; it returns every candidate when a current code is
ambiguous instead of picking the latest row.

### DataEnvelopeV2

Every normalized revision contains:

- dataset, entity, observation key and actual source
- monotonically increasing revision and superseded revision
- observed, published, available, acquired, effective and optional expiry time
- normalized payload hash and immutable raw payload reference
- quality status and explicit flags
- fallback state
- transformation ID, code version and parameters
- an exact field-provenance entry for every payload leaf, including its actual
  source, temporal coordinates, update time, quality, raw object/path and
  transformation inputs

Historical queries filter both `available_at` and `acquired_at`. A record that
was published earlier but only acquired later is not visible to an earlier
research run. Envelope validation rejects both untraced payload values and
provenance entries for fields that do not exist.

## Storage responsibility

Schema migration 11 adds the point-in-time warehouse, migration 12 adds the
security lifecycle event ledger, migration 13 makes the identifier crosswalk
normalized and point-in-time resolvable, migration 14 persists field-level
Data Envelope provenance, migration 15 adds the bitemporal market contract,
and migration 16 separates exact source bytes from parsed payloads and audits
deterministic cleaning replays. Migration 17 materializes the same revisions
into five shared research-domain tables:

| Table | Responsibility |
| --- | --- |
| `data_sources` | Runtime copy of the reviewed Source Registry |
| `market_entities` | Stable entity and lifecycle truth |
| `entity_identifiers` | Source/display identifiers, normalized keys, confidence and validity |
| `raw_data_payloads` | Immutable hash-addressed API/CSV/JSON payloads |
| `raw_data_objects` | Exact response bytes, wire SHA-256, byte length, media type and encoding |
| `raw_payload_objects` | Logical payload to one or more captured byte objects |
| `raw_reprocessing_runs` | Audited parser/cleaning replay inputs, outputs, versions and status |
| `data_revisions` | Point-in-time normalized observations and complete field-level provenance |
| `data_revision_snapshots` | Immutable complete point-in-time dataset manifests |
| `data_revision_snapshot_items` | Ordered revision IDs and payload hashes in each manifest |
| `market_prices` | Daily and intraday price revisions |
| `financial_facts` | Fundamental, monthly revenue and valuation revisions |
| `ownership_flows` | Institutional, margin, ownership and derivative flow revisions |
| `market_events` | Event, document and impact revisions |
| `macro_observations` | Macro series revisions |
| `data_lineage_edges` | Raw/input revision to output transformation chain |
| `data_ingestion_checkpoints` | Incremental cursors, attempts and resumability |
| `data_quality_reports` | Point-in-time daily quality summaries, rule version and state hash |
| `data_quality_issues` | Missing, anomaly, time-misalignment, duplicate and source-conflict evidence |
| `data_cache_entries` | Per-partition policy snapshot, freshness windows, generation and decision counters |
| `data_cache_invalidations` | Audited reviewed invalidation events |
| `data_cache_refresh_leases` | Bounded single-flight ownership for upstream refresh |
| `data_source_failover_runs` | Requested primary, selected actual source, policy snapshot and terminal result |
| `data_source_failover_attempts` | Every retry/failover source, endpoint, error/raw evidence and produced revisions |
| `data_reconciliation_runs` | Point-in-time rule version, scope, counts and immutable run summary |
| `data_reconciliation_conflicts` | Current cross-source evidence and open/resolved lifecycle, never silent overwrite |
| `entity_lifecycle_events` | Venue listings, transfers, delistings and expirations |

SQLite is the local workstation implementation. The contracts intentionally do
not depend on SQLite-specific IDs, so a later time-series or columnar storage
backend can implement the same interface.

## Module responsibility

| Module | Owns | Must not own |
| --- | --- | --- |
| `contracts.py` | Source, entity, temporal and envelope validation | Network calls |
| `source_registry.py` | source/dataset validation, endpoint resolution, URL attribution and failure contracts | Source parsing or silent fallback |
| `warehouse.py` | migrations, raw capture, revisions, PIT queries, lineage, quality, reconciliation | Source parsing |
| `service.py` | registry-backed source registration, identity mapping, ingestion orchestration, failover selection | Endpoint ownership, UI formatting or market opinion |
| `incremental.py` | committed-page cursors, controlled pause/resume, per-partition TTL, fresh-data skip and failure recovery | Source-specific parsing |
| `quality.py` | versioned rule evaluation, daily report identity and five issue classes | source mutation or silent conflict resolution |
| `cache.py` | per-dataset freshness decisions, reviewed invalidation and refresh admission | source parsing or unbounded stale service |
| `failover.py` | reviewed retry traversal, actual-source raw capture and fallback revision persistence | relabeling backup data as the requested primary |
| `reconciliation.py` | per-source latest-state comparison, domain rules and conflict lifecycle | selecting a winner or mutating source revisions |
| `ui_api.py` | versioned UI data route registry, compatibility map and static access audit | source fetching or page-specific rendering |
| `gateway.py` | one connector-facing ingress for research Pipelines and normalized price persistence | a second cache or database |
| `api.py` | authenticated unified query/status/lineage endpoints | Direct connector access |
| source connector | fetch and source-specific parsing | second database or consumer-specific cache |

## Execution and failure flow

1. The source is resolved from the registry.
2. The raw response is saved before normalization.
3. Each normalized record is bound to an internal entity and temporal
   coordinates.
4. An unchanged payload reuses its existing revision. A changed payload creates
   a new revision and `supersedes_revision_id`.
5. A successful partition advances its checkpoint. Failure records the error
   without destroying the previous successful data.
6. Reads specify an `as_of` time and return only then-known revisions.
7. Reconciliation records conflicts from multiple sources. Source priority may
   select a preferred view, but never deletes or relabels conflicting values.
8. The daily quality service evaluates the point-in-time state, preserves every
   issue and reuses the report ID when the same day's state is unchanged.
9. Cache Policy classifies the partition before a source request. Fresh data is
   reused; one lease owner refreshes stale/expired/invalidated data and
   concurrent callers do not duplicate the request.
10. Source Failover retries only reviewed statuses, then traverses only
    registered fallback datasets. Each attempt and error payload keeps the
    source that actually produced it; fallback revisions set `is_fallback`
    without replacing an existing or later recovered primary revision.

Cache TTL and invalidation reasons are declared per dataset in the Source
Registry. `IncrementalLoader` passes the last committed cursor into a loader,
advances it after every persisted page, retains it on failure, supports
controlled pause/resume, and skips a partition only while its successful
checkpoint remains inside the declared TTL. Schema v18 records every attempt
and page in `data_ingestion_runs` and `data_ingestion_batches`.

## APIs

```text
GET  /api/data/status
GET  /api/data/sources
GET  /api/data/entities
GET  /api/data/entity-registry
GET  /api/data/entity-registry/resolve?identifier=...&as_of=...
GET  /api/data/entities/{entity_id}
GET  /api/data/security-lifecycle
GET  /api/data/entities/{entity_id}/lifecycle
POST /api/data/query
GET  /api/data/revisions/{revision_id}/lineage
GET  /api/data/lineage/artifacts
POST /api/data/lineage/artifacts
GET  /api/data/lineage/{target_id}
GET  /api/data/ingestion/runs
GET  /api/data/ingestion/runs/{run_id}
GET  /api/data/revisions/history
POST /api/data/snapshots
GET  /api/data/snapshots/{snapshot_id}
GET  /api/data/cache
GET  /api/data/cache/{dataset}
POST /api/data/cache/{dataset}/invalidate
GET  /api/data/failover
GET  /api/data/failover/runs
GET  /api/data/failover/runs/{run_id}
GET  /api/data/reconciliation
POST /api/data/reconciliation/runs
GET  /api/data/reconciliation/runs
GET  /api/data/reconciliation/runs/{run_id}
GET  /api/data/reconciliation/conflicts
GET  /api/data/ui/v1/contract
GET  /api/data/ui/v1/{registered-view-route}
POST /api/data/quality/{dataset}
POST /api/data/quality/daily
GET  /api/data/quality/reports
GET  /api/data/quality/reports/{report_id}
POST /api/data/reconcile
```

`/api/securities/master` is backed by the same Entity Registry and preserves
its compatibility response.

## Phase 1 acceptance evidence

The Phase is not complete merely because these tables exist. Completion also
requires:

- every production market reader is migrated to `MarketDataPlatform`
- full listed, OTC, emerging, ETF, warrant, index and delisted lifecycle sync
- historical loaders for each declared data domain
- scheduled incremental ingestion and restart recovery
- actual-source replay, PIT, failover and reconciliation tests
- Browser verification of source, freshness, quality and lineage UI

`DATA-001` is complete: the official checkpointed loader covers listed, OTC,
emerging, ETF, warrant, index and delisted records and exposes their lifecycle
history. The remaining reader migrations and historical domain loaders stay
explicitly open in the comprehensive traceability ledger.

`DATA-004` is complete: field provenance is a runtime and storage invariant,
legacy revisions receive an idempotent v14 backfill, transformed values can
point to different raw JSON paths, and the Data Catalog exposes field source,
source/acquisition/update times, quality and raw lineage.

`DATA-005` is complete: Temporal Contract V1 separates research knowledge time
(`published_at`, `available_at`, `acquired_at`) from business effective time.
Daily price and flow records declare a `trade_date`; monthly financial records
declare `fiscal_period`, `period_start` and `period_end`. Unified queries accept
independent `knowledge_at` and `effective_at` cutoffs, while legacy `as_of`
sets both. SQL filters publication and acquisition before revision ranking, so
a later correction or unpublished filing cannot leak into a historical run.

`DATA-006` is complete: Raw Data Lake V1 stores exact response bytes separately
from parsed JSON, records wire SHA-256 and byte length, and blocks database
updates/deletes with immutable triggers. Allowlisted JSON, CSV and text parsers
can be rerun from the raw object; the output must reproduce the captured parsed
payload hash and every replay is recorded with its code/transformation version.

`DATA-007` is complete: Standard Market Warehouse V1 projects each supported
revision into exactly one price, financial, flow, event or macro table in the
same transaction. Domain and generic queries preserve the same revision ID,
payload, source, quality and point-in-time coordinates; schema v17 backfills
existing revisions without overwriting history.

`DATA-008` is complete: Incremental Loader V2 persists a cursor after each
successful page, resumes failed or deliberately paused partitions from that
cursor, rejects non-advancing continuations and skips fresh partitions without
another source request. Run and batch history make every recovery auditable.

`DATA-009` is complete: Revision History V1 prevents updates and deletes of
normalized revisions, validates every supersession chain, and persists
hash-addressed manifests that reconstruct the complete revision set at
independent knowledge and effective cutoffs. Repeated identical states reuse
the same snapshot ID; reads recompute manifest integrity.

`DATA-010` is complete: Data Quality Service V2 runs by Taipei market date over
the point-in-time latest state, detects required-field gaps, contract and
statistical anomalies, temporal mismatches, repeated historical payloads and
open source conflicts, then persists both the daily summary and issue-level
evidence. Rule/state hashes prevent duplicate reports for an unchanged day.

`DATA-011` is complete: Cache Policy Service V2 enforces distinct per-dataset
TTL, stale-while-revalidate and reviewed invalidation rules. Persistent cache
state prevents data beyond the stale window from being described as
serviceable, invalidation bypasses remaining TTL, and bounded database refresh
leases suppress concurrent upstream requests across workers.

`DATA-012` is complete: Source Failover V1 executes the registry's retry and
fallback graph, captures error payloads, and persists every attempt in schema
v22. Raw payloads, field provenance and normalized revisions always use the
actual responding source; fallback revisions are explicit and remain separate
when the primary later recovers. Preferred reads choose non-fallback data at
the same priority without deleting the backup observation.

`DATA-013` is complete: Reconciliation Engine V1 compares only the latest
point-in-time revision from each source under versioned price, financial and
event rules. Numeric absolute/relative tolerances, normalized event text and
event-time windows produce audited schema-v23 runs. A mismatch opens or
refreshes conflict evidence without selecting a winner; later convergence
resolves that conflict while every source revision remains immutable.

`DATA-014` is complete: Unified Data API V1 registers every market-data route
consumed by the UI under `/api/data/ui/v1`. Home, security database, stock,
news, flow, fundamentals, screener, linkage and realtime views use this
versioned boundary. Legacy endpoints remain compatibility aliases only; a
static audit fails when a UI script reintroduces one of those paths. Connector
access stays behind backend services and the UI contract reports it as false.
The registry now exposes 41 routes after `FIN-006` added frequency-separated
monthly, quarterly, annual and multi-year growth history. Monthly revenue, all
three statements, ratios and growth remain behind the same boundary.

`DATA-015` is complete: schema v24 persists immutable derived artifacts and
their revision/artifact input edges. A metric or conclusion is accepted only
when every requested input field exists and every leaf revision reaches an
integrity-checked raw payload. The graph query traverses source, raw payload,
revision, intermediate indicator and final conclusion nodes without flattening
away transformation IDs, code versions or parameters. Data Catalog renders the
latest complete graph and exposes missing nodes, raw gaps or integrity failures
instead of claiming traceability.

The official-source verification is repeatable without changing the normal
runtime database:

```bash
uv run python scripts/verify_unified_data_platform.py
uv run python scripts/verify_data_envelope.py
uv run python scripts/verify_temporal_contract.py
uv run python scripts/verify_raw_data_lake.py
uv run python scripts/verify_standard_market_warehouse.py
uv run python scripts/verify_incremental_loader.py
uv run python scripts/verify_revision_history.py
uv run python scripts/verify_daily_data_quality.py
uv run python scripts/verify_cache_policy.py
uv run python scripts/verify_source_failover.py
uv run python scripts/verify_reconciliation_engine.py
uv run python scripts/verify_unified_ui_data_api.py
uv run python scripts/verify_complete_data_lineage.py
```
