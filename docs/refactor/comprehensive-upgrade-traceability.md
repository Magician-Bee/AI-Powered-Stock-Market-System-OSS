# Comprehensive Upgrade Traceability

The legacy Phase 1–10 roadmap ledger is
`config/comprehensive_upgrade_status.yaml`. The current production-correctness
requirements and their direct evidence are authoritative in
`config/production_requirement_status.yaml`; execution-level authority remains
in `config/capability_status.yaml` and derives its Quant P0 gate from that
requirement ledger. A requirement is never promoted to `complete` from a file
name, tool manifest, mock provider or static UI test.

## Quant correctness foundation (2026-08-13)

Q-001–Q-009, S-003 and E-001 are now individually registered with acceptance,
evidence and blockers. Seven of the nine Quant requirements have direct local
acceptance evidence. Q-003 remains `partial`: its local official Taiwan tax
snapshot is now versioned and tested by exchange-local date/product/side/lot,
but broker/exchange account schedules and impact calibration provenance still
need immutable receipts. Q-008 is now `complete`: every Standard Market
Warehouse dataset has an explicit source contract and replay decisions use the
later of availability and ingestion; every active source dataset ID is also
bound to an exact endpoint-level contract rather than inheriting a broad
source/domain rule. Therefore
`research_execution_evidence_enabled` is derived as `false`, even though the
event ledger, fold isolation, purge/embargo, sizing rejection and OMS-authority
paths have passing local tests.

The OHLCV-only `BacktestResearch` fallback now refuses to apply its former
fixed 20 bps shortcut. Without a point-in-time venue/product/lot/account cost
context it reports `transaction_cost_schedule_missing`; only the event-driven
replay's row-level cost receipts can support execution evidence.

Q-008/D-004 now also expose a content-hashed
`stock_ai.data_availability_audit.v1` over the active endpoint set and all
research datasets. The same receipt is attached to the source registry and
research gateway payloads; the active endpoint audit is now exact at dataset
ID granularity and passes fail-closed coverage validation.

The availability audit now also stores a canonical manifest hash for every
active endpoint contract and every research-dataset source contract. Receipt
verification checks those nested manifests after the outer receipt hash, so
recomputing only the outer hash cannot conceal a rewritten contract. External
provider-wide historical breadth remains a separate D-002/D-003 concern; it no
longer weakens the exact availability-contract acceptance for Q-008/D-004.

D-005 now has a dependency-free shared `stock_ai.data_quality_contracts`
module. Market-intelligence snapshots and realtime screener results emit the
same hash-verified quality receipt contract; the screener UI exposes the
receipt dimensions instead of presenting condition matches as data-quality
certification. Legacy market summary/detail payloads now carry the same receipt
through the overview API and decision detail UI. They explicitly remain
partial when an independent same-time cross-source observation is unavailable;
other decision surfaces without the shared receipt remain partial as well.

M-003 now has a recorded external smoke receipt: the vendored TradingAgents
graph ran in the isolated macOS runtime through the remote OpenAI-compatible
`gpt-oss:20b` endpoint, with schema-validated output and an explicit
research-only execution boundary. The opt-in command and result hash are
recorded in `docs/integration/tradingagents-release-smoke-20260825.md`; the
runtime remains fail-closed when its dependency, provider, or schema contract
is unavailable.

The news/event lake also emits a content-hashed ingestion coverage audit that
binds each provider coverage receipt to the exact unique event-key manifest
written by the current immutable ingestion. Count mismatches, duplicate event
keys, and incomplete publication timestamps remain fail-closed for PIT
research; this is an integrity guard, not a claim that provider-wide
historical coverage has already been reviewed.

Chip/ownership PIT coverage now also emits per-stream content-hashed audits
for institutional, margin, borrowed-short and TDCC observations. Each audit
binds a unique observation-key manifest and raw-record manifest to the source,
date field and missingness blockers; duplicate observations, missing dates,
missing acquisition timestamps or uncertified availability revoke that
stream's PIT eligibility. TDCC historical acquisition and production source
review remain explicit blockers.

## Portfolio risk constraints (2026-08-25)

S-006–S-010 now have a deterministic, fail-closed constraint path. The
`PortfolioRiskConstraintEngine` emits a content-hashed receipt covering issuer,
industry, factor, currency, broker and account concentration, ADV
participation, days-to-liquidate, historical/parametric CVaR and declared
stress scenarios. Missing point-in-time metadata, account limits, return
history or stress policy withholds every target. These requirements remain
`partial` until the production account context, historical liquidity corpus,
factor matrix and reviewed stress policy are materialized.

## Durable PIT portfolio risk evidence (2026-08-26)

`PortfolioRiskContextStore` now persists both verified and withheld portfolio
risk contexts in an immutable SQLite ledger keyed by the context receipt
SHA-256. A new process can replay a receipt deterministically; every load
rechecks its schema, status, and content hash. SQLite update/delete triggers
protect the ledger, while a tampered payload still fails closed at read time.
This makes the evidence path restart-safe without claiming that production
active-universe risk inputs or account-owner approvals already exist.

The paper execution path also persists historical order-book replay receipts
in an immutable SQLite store. Paper Broker can resolve an explicit receipt ID
after restart and refuses execution when the requested durable snapshot cannot
be resolved; this remains a local replay boundary, not evidence that a provider
has supplied complete historical depth or empirical fill calibration.

## Durable risk controls (2026-08-25)

S-011/S-012 now have a SQLite-backed risk-control path. Global, account, broker,
strategy and symbol switches survive process restart and are checked before the
normal RiskEngine gates. Realized-P&L events are idempotent by event ID and
produce intraday, daily, weekly, monthly and consecutive-loss receipts that
activate the matching scope when a limit is breached. The durable PaperOMS
path is now connected through `SettlementPnLFeed`, including source-receipt
verification and a fail-closed pending-T+2 check. The requirements remain
`partial` until verified broker/account activation and a production settlement
P&L feed are connected.

## Current audit

| Phase | Current status | Direct evidence | Blocking gaps |
| --- | --- | --- | --- |
| 1 — Unified market data | Complete (`DATA-001` through `DATA-015`) | v11 PIT warehouse, v12 lifecycle events, v13 point-in-time Entity Registry, Source Registry v2, v14 field-level Data Envelope provenance, v15 bitemporal trade/fiscal contract, v16 immutable exact-byte Raw Data Lake, v17 five-domain Standard Market Warehouse, v18 committed-batch Incremental Loader, v19 immutable Revision History manifests, v20 point-in-time daily Data Quality Service, v21 enforced Cache Policy and single-flight refresh, v22 audited Source Failover with actual-source provenance, v23 price/financial/event Reconciliation Engine, versioned Unified Data API V1, v24 complete source-to-conclusion lineage, official listed/OTC/emerging/ETF/warrant/index/delisted partitions, research gateway and data-status UI | no open Phase 1 acceptance item; historical breadth and production scheduling continue under their later numbered requirements |
| 2 — Complete stock research | Unverified | legacy endpoints and partial official stores exist | ten-year PIT statements, valuation, complete ownership/events/documents and industry-specific contracts not audited |
| 3 — Market/macro intelligence | Unverified | overview and derivative endpoints exist | complete historical market breadth, rotation, macro, cross-market and temporal alignment not audited |
| 4 — Research platform | Partial | linked event-driven ledger, fold-isolated fit/freeze/test, purged CV, embargo, PIT lifecycle validation, cost/impact receipts and evidence-derived certification | Q-003 official rule snapshots, Q-008 production PIT source coverage, common datasets, attribution, registry and later OOS promotion gates remain |
| 5 — External frameworks | Unverified | locked source trees and some runtime entrypoints exist | every full workflow, provider-neutral execution, common artifacts and formal pipeline evidence required |
| 6 — Model runtime | Unverified | Universal/Advanced negotiation and conformance probe exist | split capabilities, five-turn recovery, Prompt JSON/read-only, task splitting, persistence/invalidation and real model matrix required |
| 7 — Deep Agent planning | Unverified | durable PlanGraph and revisions exist | model-authored objective contract, pre-execution planning, methods/budgets/fallbacks, independent critic and adaptive multi-Agent roles required |
| 8 — Memory/autonomy | Unverified | durable memory, schedules and proposal tables exist | preference confirmation, result tracking, conflict/version expiry and strategy calibration evidence required |
| 9 — UI/observability | Unverified | durable Agent UI and Market Radar cards exist | full data center, framework mode, comparison, artifacts, typed errors and Research Compute authorization required |
| 10 — Acceptance | Unverified | unit, integration, Browser and CI suites exist | all named real-source/model/platform/stress/fault tests and downloadable artifacts required |

## Evidence policy

- `partial` means the implementation is useful but the stated acceptance
  condition is not yet fully proven.
- `unverified` is intentionally stricter than “not found”: existing code must
  be executed and matched to the requirement before it can be credited.
- Only `complete` items may contribute to the final completion claim.
- The overall objective remains active while any item is `partial` or
  `unverified`.

## DATA-001 acceptance (2026-07-24)

`scripts/verify_unified_data_platform.py` loaded current official sources into
a clean temporary database and then performed a second checkpointed run.

| Evidence | Result |
| --- | --- |
| Official source rows | TWSE 41,698; TPEx OpenAPI 11,789; TPEx delisted history 257 |
| Canonical entities | 51,000 |
| Entity types | 2,748 stocks; 374 ETFs; 47,603 warrants; 275 indices |
| Listing coverage | listed, OTC, emerging, ETF, warrant, index and delisted |
| Lifecycle events | 60,036 |
| Immutable raw payloads / revisions | 10 / 51,282 |
| Lifecycle quality | passed; zero missing display symbols, invalid identifier intervals or orphan events |
| Incremental replay | all three fresh partitions returned `skipped_fresh` without another fetch |
| Clean-database elapsed time | 239.665 seconds on the acceptance workstation; quality/registry checks 1.585 seconds |

These counts describe the official responses observed at verification time;
they are evidence, not hard-coded acceptance constants.

The v13 revalidation corrected two v12 interpretation defects instead of
preserving earlier counts: TWSE ETF company and fund-master rows are now one
entity, and TWSE warrant `履約開始日` is retained as exercise metadata rather
than mislabelled as the listing date. European-style same-day exercise/expiry
therefore no longer creates a zero-length identifier interval.

## DATA-002 acceptance (2026-07-24)

The same clean real-source run exercised the normalized, point-in-time Entity
Registry and then repeated all three loaders through fresh checkpoints.

| Evidence | Result |
| --- | --- |
| Internal IDs | 51,000 canonical `ENT-*`; zero malformed IDs |
| Source identifiers | 105,158 exact plus normalized identifier records |
| Identifier types | 51,282 display symbols; 51,282 exchange codes; 2,594 business numbers |
| Cross-source identities | 42 entities have identifiers from more than one official source |
| Real cross-source resolution | `ENT-0ed5fa6dda02570f82783ae4a5af77d1` resolved from TPEx official-web and TWSE OpenAPI aliases |
| Registry quality | passed; zero missing normalized values, invalid intervals or current display-symbol ambiguity |
| Code reuse behavior | unit/integration acceptance returns historical candidates and requires `as_of` when no unique current entity exists |
| UI | Data Catalog exposes Registry health and an interactive code/business-number/ENT resolver |
| Incremental replay | `skipped_fresh`; identity validation does not refetch official partitions |

Expected ambiguity is returned as candidates and is not silently converted into
an arbitrary entity. `.TW` and `.TWO` remain identifiers only, never internal
primary keys.

## DATA-003 acceptance (2026-07-26)

`scripts/verify_source_registry.py` validated the reviewed registry and read
representative live upstream responses.

| Evidence | Result |
| --- | --- |
| Registry coverage | 12 sources and 29 source-owned dataset contracts |
| Managed properties | 29/29 datasets declare fields, licensing inheritance, frequency, reliability and typed failure strategy |
| Live official responses | TWSE companies 1,092; TPEx companies 891; TAIFEX institutional futures 66; TWSE MIS `2330` quote 1 |
| Endpoint attribution | TWSE, TPEx, TAIFEX and MIS concrete URLs resolved back to the intended dataset IDs |
| Hardcoded reader hosts | zero across the production market-data reader allowlist |
| API / UI | `/api/data/sources` exposes dataset contracts; Data Catalog shows source counts, licensing, frequency, reliability, endpoint fields, exhausted behavior and failover |

The counts are the upstream responses observed during acceptance and are not
hard-coded thresholds. Credential-gated Fugle and auxiliary Yahoo/Google
contracts are validated and displayed without pretending that an unauthenticated
request proves licensed access.

## DATA-004 acceptance (2026-07-26)

`scripts/verify_data_envelope.py` captured a current TWSE company response in a
clean database, normalized representative records and followed every output
field back to the immutable response.

| Evidence | Result |
| --- | --- |
| Live source | TWSE OpenAPI company directory |
| Envelope invariant | every payload leaf has exactly one field-provenance entry |
| Per-value context | actual source, source/available/acquired/effective/update times, quality and transformation |
| Raw trace | every field raw JSON pointer resolves inside the hash-addressed payload |
| Legacy compatibility | migration 14 backfills existing revisions without replacing payloads, raw objects or lineage |
| Warehouse health | zero untraced revisions; traced-value total agrees with validated envelopes |
| API / UI | unified query returns field provenance; Data Catalog exposes totals and expandable field lineage |

Live row and verified-field counts are recorded from each acceptance run rather
than encoded as thresholds.

## DATA-005 acceptance (2026-07-26)

`scripts/verify_temporal_contract.py` captured current TWSE monthly revenue and
TAIFEX institutional-futures responses in a clean database, then queried the
same revisions through independent knowledge-time and effective-time cutoffs.

| Evidence | Result |
| --- | --- |
| Live fiscal source | TWSE monthly revenue: 1,082 rows; period `2026-06`, publication `2026-07-17` in the acceptance sample |
| Live trade-date source | TAIFEX institutional futures: 66 rows; trade date `2026-07-24` in the acceptance sample |
| Knowledge-time guard | fiscal revision hidden before publication and before local acquisition |
| Effective-time guard | fiscal revision hidden before period end; trade revision hidden before trade date |
| Restatement behavior | unit acceptance retains the earlier revision until the later acquisition cutoff |
| Legacy compatibility | `as_of` sets both cutoffs; migration 15 infers legacy daily, fiscal and lifecycle dimensions without replacing payloads |
| Full regression | 494 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | query accepts `knowledge_at` and `effective_at`; the integrated Data Catalog showed 56,291 temporal revisions, 2,575 trade-date rows, 2,164 fiscal rows, zero violations and expandable per-field time lineage |

The Data Catalog acceptance used a full application process and current
official refreshes. Both margin and monthly-revenue checkpoints completed
successfully; the displayed counts are observed evidence, not thresholds.

## DATA-006 acceptance (2026-07-26)

`scripts/verify_raw_data_lake.py` captured the exact current TWSE monthly
revenue OpenAPI response and exercised schema v16 in a clean database.

| Evidence | Result |
| --- | --- |
| Live official response | 1,082 TWSE monthly-revenue rows |
| Exact raw object | 603,059 bytes; independently calculated wire SHA-256 matched the stored hash |
| JSON cleaning replay | passed; 1,082 rows and output hash reproduced the captured parsed payload |
| CSV cleaning replay | passed; allowlisted CSV parser reproduced the captured parsed payload |
| Immutability | direct SQL update of the raw object was rejected by the database trigger |
| Audit | both attempts persisted parser, transformation/code version, input/output hash, row count, status and timestamps |
| Full regression | 498 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | Data Catalog showed 14 raw objects / 50,837,045 bytes, exact wire hash and integrity; its lineage button completed another replay and the refreshed audit count advanced from 2 to 3 |

Counts and hashes are observed acceptance evidence and are not encoded as
thresholds. The Browser pass also exposed concurrent first-use construction of
the process-wide platform; initialization is now single-flight and replay audit
writes use a bounded SQLite busy wait.

## DATA-007 acceptance (2026-07-26)

`scripts/verify_standard_market_warehouse.py` wrote one valid representative
record into every shared research domain in a clean schema-v17 database.

| Evidence | Result |
| --- | --- |
| Physical domain tables | `market_prices`, `financial_facts`, `ownership_flows`, `market_events` and `macro_observations` |
| Shared source of truth | generic and domain reads returned the identical revision ID and payload in all five domains |
| Point-in-time contract | domain reads preserve independent knowledge-time and effective-time cutoffs |
| Upgrade compatibility | migration 17 backfills supported historical revisions and is repeatable |
| Immutability | direct SQL update of every standard domain table was rejected by a database trigger |
| Concurrent writers | two platform instances completed four parallel ingestion batches without SQLite lock failures |
| Targeted regression | 84 passed |
| Full regression after latest `main` integration | 516 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | `/api/data/warehouse/{domain}` reads the shared tables; isolated Browser acceptance showed 5 initial records, then 946 after refresh: price 1, financial 470, flow 473, event 1 and macro 1 |

The Browser pass opened Data Catalog, inspected the Market Warehouse card,
clicked `重新整理`, switched to the watchlist view during concurrent source
work and returned to Data Catalog. The status remained `checkpoint 無失敗`;
no database-lock error or HTTP 500 occurred. Counts describe the official
responses observed during acceptance and are not hard-coded thresholds.

## DATA-008 acceptance (2026-07-27)

`scripts/verify_incremental_loader.py` exercised Incremental Loader V2 against
an isolated schema-v18 database using three deterministic source pages.

| Evidence | Result |
| --- | --- |
| Delta fetch sequence | `None` → `cursor-1` → `cursor-2`; no committed page was fetched twice |
| Controlled interruption | first run paused after 2 batches with committed `cursor-2` |
| Resume | second run started at `cursor-2`, committed the third batch and ended at `cursor-3` |
| Fresh replay | `skipped_fresh`; the source callback was not invoked |
| Failure recovery | unit acceptance failed after page one, retained `cursor-1`, and resumed without refetching page one |
| Process restart | an abandoned `running` attempt became `interrupted`; its successor resumed from the stored cursor |
| Loop protection | a source declaring `has_more` without cursor advancement is rejected |
| Audit | schema v18 persisted run status/totals and all 3 committed batch cursor transitions |
| Targeted regression | 36 unified-market-data tests passed |
| Full regression | 520 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | `/api/data/ingestion/runs` and run detail expose audit history; Browser acceptance showed `2 次更新 / 3 批`, then remained readable after switching to the watchlist and back |

The Browser pass used the complete application with an isolated database,
opened Data Catalog, visually inspected the Incremental Loader card, switched
to the watchlist, and returned to Data Catalog. The card then showed the
additional background run while preserving the three committed verifier
batches; the status remained `checkpoint 無失敗`. Server requests returned
successfully with no SQLite lock or HTTP 500.

## DATA-009 acceptance (2026-07-27)

`scripts/verify_revision_history.py` created an original monthly-revenue
revision and a later restatement in an isolated schema-v19 database.

| Evidence | Result |
| --- | --- |
| Revision chain | revision two superseded revision one; history validation passed with no gaps or broken links |
| Historical reconstruction | the early snapshot restored revenue 100.0 and the later snapshot restored 105.0 |
| Deterministic identity | recreating the same early state reused the identical hash-derived snapshot ID |
| Manifest integrity | both persisted snapshot manifests passed full hash recomputation |
| Immutability | direct SQL update/delete of revisions and update of snapshot metadata were rejected by database triggers |
| Targeted regression | 37 unified-market-data tests passed |
| Full regression | 521 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | Browser acceptance showed `2371 個版本 / 3 份快照`, one corrected observation, passed chain and snapshot integrity, and immutable storage |

The Browser pass ran the complete application against clean isolated storage,
opened Data Catalog, created a current-state snapshot, refreshed the data, then
switched to the watchlist and back. The new snapshot reported 794 manifest
items while the concurrent official-data load was active; the refreshed card
reported 2,371 revisions and three snapshots with `checkpoint 無失敗`. All
server requests completed successfully without SQLite lock or HTTP 500.

## DATA-010 acceptance (2026-07-27)

`scripts/verify_daily_data_quality.py` generated a Taipei-market-date report in
an isolated schema-v20 database with deterministic fixtures for all required
issue classes.

| Evidence | Result |
| --- | --- |
| Missing | one `close` field absent from a required normalized price record |
| Anomaly | four issues, including a robust statistical outlier |
| Time alignment | one payload trade date disagreed with its temporal contract |
| Duplicate | one logical source record repeated an earlier payload after an intervening revision |
| Source conflict | one open reconciliation difference retained both source revision IDs and values |
| Issue evidence | 8 issue rows persisted with category, severity, code, revision/entity/observation/source and expected/actual values |
| Deterministic identity | an unchanged repeat reused the same daily report ID; only one report remained stored |
| Targeted regression | 38 unified-market-data tests passed |
| Full regression | 522 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | daily run/list/detail API passed; Browser acceptance displayed `1 份每日報告 / 8 個問題` and the five per-category counts |

The Browser pass used the complete application with isolated storage, opened
Data Catalog, clicked the `prices_daily` quality action and observed
`缺漏 1 · 異常 4 · 時間錯位 1 · 重複 1 · 來源衝突 1`. It refreshed the
catalog, switched to the watchlist and returned; the report remained one
deduplicated daily record, `checkpoint 無失敗` remained visible, and all
affected HTTP requests returned 200 without SQLite-lock or HTTP-500 errors.

## DATA-011 acceptance (2026-07-27)

`scripts/verify_cache_policy.py` exercised the enforced cache policy against an
isolated schema-v21 database and deterministic source adapter.

| Evidence | Result |
| --- | --- |
| Per-dataset TTL | quote, daily-price and reference-data fixtures used distinct 5, 900 and 21,600 second TTL values |
| Stale window | the quote fixture became stale after 6 seconds and remained serviceable during stale-while-revalidate |
| Hard expiry | the same fixture was expired and no longer serviceable after 16 seconds |
| Duplicate-request suppression | two concurrent refreshes made one upstream request; the second returned `skipped_refresh_in_progress` |
| Fresh-request suppression | the immediate repeat returned `skipped_fresh` without another upstream request |
| Invalidation | reviewed `listing_event` and universal `manual_refresh` events invalidated matching entries without deleting cursor `cursor-v1` |
| Refresh lifecycle | successful refresh restored a fresh entry and released its database-backed lease |
| Targeted regression | 39 unified-market-data tests passed |
| Full regression | 523 passed, 3 skipped, 8 xfailed, 1 xpassed |
| API / UI | cache list/detail/invalidation APIs passed; Browser acceptance changed `prices_daily` from fresh to invalidated and finished at `fresh 3 · invalidated 1 · refresh 0` |

The Browser pass ran the complete application with isolated storage, opened
Data Catalog, used `使快取失效` for `prices_daily`, refreshed the catalog, then
switched to the watchlist and returned. The invalidated state remained visible,
the refresh-lease count returned to zero, `checkpoint 無失敗` remained visible,
and the cache invalidation request and affected catalog requests returned 200
without SQLite-lock or HTTP-500 errors.

## DATA-012 acceptance (2026-07-27)

`scripts/verify_source_failover.py` injected a retryable primary outage into an
isolated schema-v22 database, exercised the reviewed fallback path and then
recovered the primary.

| Evidence | Result |
| --- | --- |
| Primary retries | `twse_stock_day` made three recorded `twse_official_web` attempts after HTTP 503 responses |
| Reviewed failover | the fourth attempt used registered dataset `twse_quotes` and actual source `twse_openapi` |
| Failure evidence | each failed attempt retained its HTTP status, typed error and raw response under the source that returned it |
| Normalized provenance | fallback revision, raw payload and every field-provenance entry used `twse_openapi`; `is_fallback=true` |
| Primary recovery | a later `twse_official_web` success remained separate from the `twse_openapi` revision and became the preferred non-fallback read |
| Fail closed | a non-retryable HTTP 401 on a dataset without a reviewed fallback made one attempt and failed without selecting another source |
| API | failover status/list/detail functions returned two runs, five attempts and one successful fallback |
| Targeted regression | 41 unified-market-data tests passed; combined data/runtime target had 73 passes |
| Full regression | 525 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance displayed `1 次備援 / 1 次執行`, `twse_official_web → twse_openapi` and `實際來源身分已保留` |

The Browser pass ran the complete application with isolated storage, opened
Data Catalog, expanded the primary source contract and verified its
`use_failover` policy plus `twse_openapi` backup. The primary and actual backup
source cards displayed the same persisted run ID and their distinct roles. It
then switched to the watchlist, returned, refreshed the catalog and retained
the same failover evidence with `checkpoint 無失敗`; affected UI requests
returned 200 without SQLite-lock or HTTP-500 errors.

## DATA-013 acceptance (2026-07-27)

`scripts/verify_reconciliation_engine.py` built three two-source observations in
an isolated schema-v23 database and exercised both conflict and convergence.

| Evidence | Result |
| --- | --- |
| Point-in-time source selection | each comparison used the latest eligible revision per source and observation |
| Price rules | a 100.00 versus 101.00 close opened one numeric conflict with absolute and relative difference evidence |
| Financial rules | 1,000,000 versus 1,000,500 revenue passed the configured 0.1% relative tolerance |
| Event rules | punctuation-normalized titles and a three-minute timestamp difference matched; a changed event type opened only that field |
| Conflict lifecycle | a later 100.05 price converged, resolved the existing conflict and retained all three price revisions |
| Persistence / API | run status, list/detail and open/resolved conflict queries expose rule, source, revision and value evidence |
| Targeted regression | 42 unified-market-data tests passed; combined data/API/runtime target had 104 passes |
| Full regression | 526 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Data Catalog exposes per-dataset reconciliation actions, run/conflict totals and source-to-source values without a silent winner |

The Browser pass opened the complete application with an isolated schema-v23
fixture, entered Data Catalog and clicked event reconciliation. It displayed
`conflict · 3 項`, `來源 2 · 衝突 1 · 原始來源資料已保留` and the exact
`twse_openapi material_information ↔ yahoo_finance earnings` evidence. After
refreshing, switching to the watchlist and returning, the card persisted as
`1 個未解衝突 / 6 次校驗` with the latest `events · conflict` run. All
affected API requests returned 200 without HTTP-500 or SQLite-lock errors.

## DATA-014 acceptance (2026-07-27)

`scripts/verify_unified_ui_data_api.py` audits the built UI and live FastAPI
route table rather than accepting route names as proof.

| Evidence | Result |
| --- | --- |
| Versioned boundary | `/api/data/ui/v1` owns 22 declared market-data routes for 13 UI consumers |
| Page migration | home, watchlist, stock, realtime monitor, database, screener, news, flow, fundamentals, linkage, research question and catalog calls use `uiDataApi()` |
| Direct-access audit | all 23 JavaScript files scanned with zero legacy market-data path violations |
| Connector boundary | the published contract reports `ui_connector_access=false`; only backend routes call service/connector code |
| Compatibility | old paths remain aliases and return the same payload while integrations migrate |
| API smoke | contract, catalog, sources and linkage unified routes returned 200 |
| Targeted regression | 76 API/static-UI tests passed; combined API/static-UI/data-platform target had 118 passes |
| Full regression | 528 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance exercised Data Catalog, News Center, Securities Database and Screener through the unified façade |

The Browser pass ran the complete application with isolated storage. Data
Catalog displayed `22 條路由 / 13 個頁面`,
`/api/data/ui/v1 · UI 直接存取 connector 0 · enforced`; News Center and
Securities Database rendered normally. The Screener opened and its
`執行篩選` action completed through `POST /api/data/ui/v1/screener` with HTTP
200. Catalog contract, source, news, index, watchlist, flow, report and
screener requests all used the versioned façade and returned 200 without
HTTP-500 or SQLite-lock errors.

## DATA-015 acceptance (2026-07-27)

`scripts/verify_complete_data_lineage.py` built an isolated schema-v24 graph
from three source observations through one indicator to one conclusion.

| Evidence | Result |
| --- | --- |
| Complete chain | `twse_openapi → 3 raw payloads → 3 price revisions → sma_3 → price_above_sma_3` |
| Field inputs | every derived edge retains its semantic role and exact `/close` or `/value` RFC 6901 pointer |
| Transformations | price normalizer, SMA indicator, price-versus-SMA conclusion and raw parser IDs remain separately visible |
| Fail closed | an input revision without verified raw ancestry is rejected before an artifact is written |
| Integrity | all three raw objects pass wire SHA-256 checks; graph completeness is `complete` with depth 4 |
| Immutability | schema-v24 triggers reject artifact update and edge deletion |
| API | status, artifact list, artifact graph and compatible revision-lineage requests returned 200 |
| Targeted regression | 120 data/API/static-UI tests passed; expanded data/API/UI/runtime/migration target had 154 passes |
| Full regression | 530 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Data Catalog rendered 2 derived artifacts, 5 relations, 1 source, 3 raw payloads, 3 revisions and the complete transformation chain |

The Browser pass ran the complete application with isolated storage, opened
Data Catalog and expanded the persisted conclusion path. It displayed
`完整資料血緣 · conclusion` plus `狀態 complete · 來源 1 · raw 3`,
`revision 3 · 衍生結果 2 · 深度 4`, the three raw IDs, all transformation IDs and each
input field. After refresh, switching to Market Overview and returning, the
same artifact and graph remained visible. The graph request returned 200,
the browser console had no errors, and no HTTP-500 or SQLite-lock error
occurred.

## STOCK-001 acceptance (2026-07-27)

`scripts/verify_realtime_quotes.py` normalizes deterministic TWSE MIS and Fugle
fixtures into the same versioned quote contract and proves consecutive stream
events rather than accepting one successful snapshot as continuous updates.

| Evidence | Result |
| --- | --- |
| Canonical contract | both providers emit `stock_ai.realtime_quote.v1` with exchange time, sequence, latest trade, best bid/ask, five-level books, cumulative volume, trading status and freshness |
| TWSE MIS | official public feed maps its `z`, `tv`, `v`, `b`, `g` and session fields without inventing unavailable trades |
| Fugle | authorized REST snapshots and trade/book/aggregate WebSocket events merge into the same contract |
| Trading state | explicit `pre_open`, `trading`, `closing_auction`, `closed` and `halted` states replace the former generic realtime label |
| Continuous updates | deterministic MIS verification emitted two ordered prices (`100.0`, `100.5`) and cumulative volumes (`1200`, `1204`) |
| API / SSE | quote API returned 200; stream responses publish retry hints, stable event IDs and no-buffer headers |
| Targeted regression | 203 realtime/API/static-UI/runtime/data/execution/broker tests passed |
| Full regression | 536 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance displayed live 2330 and 0050 trades, best bid/ask, all five levels, cumulative volume, exchange state and continuously changing timestamps/sequences |

The Browser pass ran the complete application with isolated storage, selected
`2330.TW`, opened Realtime Monitor and observed the live cumulative volume move
from 2,966 to 3,042 lots with matching order-book and sequence changes. It then
selected `0050.TW` and displayed a real last trade of 100.7, one-lot trade size,
best bid/ask 100.65/100.7 and five complete levels on both sides. A later
snapshot showed cumulative volume increase from 5,460 to 6,930 lots and a newer
sequence. Switching to Individual Analysis and back restored the selected
symbol and active stream. A quote without an actual trade was explicitly
labeled as a bid/ask midpoint instead of being presented as a last trade; all
affected API and stream requests returned 200.

## STOCK-002 acceptance (2026-07-28)

`scripts/verify_intraday_candles.py` creates two isolated trading days and
reconstructs every required timeframe from the same immutable one-minute
revision store.

| Evidence | Result |
| --- | --- |
| Base contract | `stock_ai.intraday_candle.v1` persists one-minute OHLCV, source, authorization, observed/ingested times, raw hash, quality and revision ID |
| Immutable storage | schema v25 rejects revision and import-receipt updates/deletes |
| Required timeframes | a full 270-minute fixture rebuilds to 270 / 54 / 18 / 9 / 5 candles for 1 / 5 / 15 / 30 / 60 minutes |
| Aggregation | open/close ordering, high/low extrema, summed volume and the final 13:00–13:30 session bucket are deterministic |
| Any saved date | independent 2026-07-24 and 2026-07-27 batches remain discoverable and reconstructable |
| Revision policy | source priority selects the best current minute without deleting lower-priority history; `as_of` selects both the revision and import receipt known at that cutoff |
| Source boundary | Fugle candles are authorized; Yahoo is labeled research-only; quote samples require a real last trade and never substitute bid/ask midpoint |
| API | unified status/date/candle routes and compatibility routes returned 200 with matching candle payloads |
| Full regression | 546 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance selected 0050, loaded a saved trading day in all five timeframes, rendered the chart, preserved the selection across views and produced zero console errors |

The Browser pass ran the complete application with isolated storage and real
Yahoo one-minute data labeled as research-only. For `0050.TW` on 2026-07-24,
the visible status reported 263 saved one-minute candles and rebuilt 263
one-minute, 53 five-minute, 18 fifteen-minute, 9 thirty-minute and 5
sixty-minute candles. The chart showed the final 60-minute candle and volume
bars. Switching to Market Overview and returning preserved
`2026-07-24 · 60 分 K`, its source and all five rendered candles. All affected
intraday API calls returned 200 and the browser console contained no errors.

## STOCK-003 acceptance (2026-07-28)

`scripts/verify_daily_history.py` imports a multi-decade deterministic fixture
through the same immutable revision store and complete-range API used by the
application.

| Evidence | Result |
| --- | --- |
| Canonical contract | `stock_ai.daily_history.v1` exposes raw unadjusted OHLCV, TWD turnover, range coverage, pagination, source IDs and fallback boundaries |
| Complete range | 6,200 candles across 286 requested months were returned as 5,000 + 1,200 rows with no repeated or missing trade date |
| Official sources | TWSE and TPEx monthly endpoints retain per-month success/failure checkpoints; TPEx lots and thousand-TWD fields normalize to shares and TWD |
| Truthfulness | rows without a valid close are skipped, missing turnover stays null, research fallback never upgrades official completeness and no default symbol is invented |
| Revision policy | official data wins the read projection while fallback and restatements remain independently traceable as immutable revisions |
| API / storage | schema v26 range indexes, batch revisions/checkpoints and both unified history routes passed |
| Deterministic verifier | 6,200 points, 286 months, 2 immutable revisions, complete turnover and HTTP 200 |
| Full local regression | 554 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance queried TWSE `0050.TW` and TPEx `6488.TWO`, rendered their daily charts and reported complete official coverage with zero console warnings/errors |

The Browser pass ran the complete application with isolated storage and explicit
user-entered symbols. For `0050.TW`, the visible result reported
`2024-01-01 ～ 2026-06-30`, 596 raw unadjusted daily candles, complete turnover
and `twse_official_web`. For `6488.TWO`, it reported
`2026-06-01 ～ 2026-06-30`, 21 daily candles, complete turnover and
`tpex_official_web`. Both charts rendered OHLC candles and volume bars, the
affected requests returned HTTP 200, and the browser console contained no
warnings or errors. Because the repository CI quota was unavailable, this
acceptance used the local Agent verifier, complete local pytest suite and actual
in-app Browser operation rather than waiting for GitHub Actions.

## STOCK-004 acceptance (2026-07-28)

`scripts/verify_price_adjustments.py` builds one deterministic 6,200-candle
range, persists official-event fixtures and proves the raw, forward-adjusted
and backward-adjusted projections from the same immutable inputs.

| Evidence | Result |
| --- | --- |
| Explicit price basis | API, UI and backtest contract accept `unadjusted`, `forward_adjusted` and `backward_adjusted` |
| Official factors | TWSE and TPEx result parsers use official previous-close, reference-price and total-adjustment fields; yearly coverage fails closed |
| Anchors | forward-adjusted keeps the requested end unchanged; backward-adjusted keeps the first raw trade date unchanged |
| Complete range | 6,200 projections were returned as 5,000 + 1,200 rows without a repeated or missing date |
| Immutable storage | raw/front/back OHLC and both factors share one factor-set ID and link to raw candle, event and factor-set revisions |
| Units and truthfulness | volume remains raw shares, turnover remains raw TWD, incomplete factor coverage is never labeled complete |
| Deterministic verifier | 6,200 points, 2 events, a 6,200-row paginated backtest, 3 selectable bases and both 1.0 anchors passed in 5.0 seconds |
| Targeted regression | 85 data/API/migration/static-UI tests passed |
| Full local regression | 560 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance switched 2330.TW 2025 between front/back adjustment, rendered 243 candles and reported 4 complete official events with zero console warnings/errors |

The Browser pass ran the complete application with isolated storage. It entered
`2330.TW`, selected 2025-01-01 through 2025-12-31 and queried the official
forward-adjusted basis. The visible status reported 243 candles,
`前復權（終點錨定）`, complete turnover and four complete events from
`twse_official_web`. Switching the selector to backward-adjusted and querying
again changed the heading, chart scale and status to `後復權（起點錨定）`.
Both history requests returned HTTP 200, the OHLC/volume/MACD chart rendered,
and the browser console contained no warnings or errors. Because the repository
CI quota was unavailable, acceptance used the deterministic local Agent
verifier, the complete local pytest suite and actual in-app Browser operation
rather than waiting for GitHub Actions.

## STOCK-005 acceptance (2026-07-28)

`scripts/verify_corporate_actions.py` creates isolated paper positions and
proves all supported holder treatments through the same schema-v28 ledger and
Paper OMS transaction path used by the application.

| Evidence | Result |
| --- | --- |
| Canonical ledger | Cash/stock dividends, capital increase/reduction, split/reverse split, merger and treasury stock are immutable source-attributed revisions |
| Official boundary | `official_verified` accepts only TWSE, MOPS or TPEx source hosts; missing structured terms remain visible but cannot mutate an account |
| Position and price sync | Share multipliers, official reference-price ratios, average-cost rebasing, merger successor transfer and cash returns update atomically |
| Rights safety | Capital increases create a pending manual entitlement without subscribing or deducting cash; treasury stock is an explicit holder no-op |
| Exactly once | Eight first-sync events applied once; the second sync created zero mutations and returned eight idempotent results |
| Cash and assets | Corporate cash appends `cash_ledger`; dividend income reads applied cash-dividend rows; fractional paper shares remain precise |
| Deterministic verifier | 8 official actions, 7 resulting positions, 1 pending entitlement and all 5 ledger/storage surfaces passed |
| Targeted regression | 212 API/OMS/asset/migration/static-UI tests passed, with 6 expected failures and 1 existing xpass |
| Full local regression | 569 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Browser acceptance rendered the official 2371.TW reduction, synchronized 100 shares to 95 and credited TWD 50 exactly once |

The Browser pass ran the complete application with isolated storage and a real
TWSE reduction record. The official TWSE detail reported 950 new shares per
1,000 old shares and TWD 0.5 returned per share; the resume-price result
reported a TWD 40.15 pre-halt close and TWD 41.73 reference price. The stock
drawer displayed one official verified reduction with its source link. Clicking
`同步模擬持倉` reported one new application; clicking it again reported zero
new applications and one already-processed action. The asset page then showed
95 shares, average cost TWD 41.74, cash TWD 996,035 and total assets
TWD 999,999.35, while the database held
one immutable application and one corporate cash entry. The affected corporate
action, sync, position and asset routes returned HTTP 200. Because GitHub CI
quota was unavailable, acceptance used the local Agent verifier, the complete
local pytest suite and actual in-app Browser operation.

## STOCK-006 acceptance (2026-07-28)

`scripts/verify_trading_restrictions.py` exercises the same schema-v29 ledger,
broker lifecycle, and final OMS gate used by the application.

| Evidence | Result |
| --- | --- |
| Official ledger | Attention, disposition, halt, resume and price-limit records preserve immutable TWSE/MOPS/TPEx source revisions |
| Attention | Warning is visible while a valid order remains tradable |
| Disposition | Market orders are rejected; explicit limit orders remain eligible under disclosed paper-simulation assumptions |
| Halt/resume | New orders and fills stop during an official halt; resting ROD orders remain open and can resume only after the later official resume event |
| Daily bounds | Limit/stop prices outside the exchange range are rejected |
| Limit-locked liquidity | Market buys at limit-up and sells at limit-down require visible opposing-book liquidity evidence |
| Strategy boundary | Direct Paper OMS calls run the identical restriction evaluator and cannot bypass an active halt |
| Deterministic verifier | 4 official revisions, idempotent re-import, all five restriction behaviors and both broker/OMS boundaries passed |
| Targeted regression | 200 API/OMS/migration/static-UI tests passed |
| Full local regression | 575 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Live TWSE 2492.TW disposition: market preview blocked, TWD 245 limit preview allowed and one-share paper order filled; official URL and TWD 299/245 bounds rendered with zero console warnings/errors |

The Browser pass ran the complete application with isolated storage and the
current TWSE `announcement/punish` row for 2492.TW (華新科), effective
2026-07-21 through 2026-08-03. A one-share market preview displayed
`disposition_requires_limit_order`, the official source link, and the live
TWD 299/TWD 245 daily bounds without creating an order. Switching the same
form to a TWD 245 limit order changed the visible check to approved; submitting
it produced exactly one order, fill and position, cash TWD 999,755, and no
browser-console warnings or errors. Because GitHub Actions quota was
unavailable, acceptance used the deterministic local Agent verifier, complete
local pytest suite and actual in-app Browser operation.

## STOCK-007 acceptance (2026-07-28)

`scripts/verify_liquidity.py` exercises the same schema-v30 share ledger,
calculation contract and fail-closed missing-data behavior used by the
application.

| Evidence | Result |
| --- | --- |
| Turnover | Latest and 20-session average TWD turnover use official unadjusted TWSE/TPEx daily rows |
| Average volume | ADV uses official raw share volume and reports the exact sample window |
| Turnover rate | Realtime cumulative volume, or the explicitly labeled latest official daily volume, is divided by revisioned official issued common shares |
| Spread | Best bid/ask comes only from a current TWSE MIS or licensed Fugle quote; no historical close is substituted |
| Slippage | Explicit order shares produce transparent ADV participation and half-spread-plus-square-root-impact bps; result is labeled as an estimate, not a fill guarantee |
| Tradability | Highly tradeable, tradeable, constrained and insufficient-data states expose fixed policy thresholds and blockers |
| Missing inputs | Missing order size, live book or official shares remain `null`; the UI displays `無資料` rather than zero |
| Persistence | Official share corrections append immutable revisions; every assessment preserves source IDs, share revision, complete JSON and input hash |
| Deterministic verifier | Average volume, turnover, spread, turnover rate, slippage, tradability, missing-input and persisted-assessment checks passed |
| Targeted regression | 112 data-platform, liquidity, static-UI and localization tests passed |
| Full local regression | 581 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Live 2330.TW: entered 1,000 shares and clicked `評估滑價`; official ADV/turnover, 0.1395% turnover, 21.91 bps spread, 11.47 bps estimated slippage and `可交易` rendered with zero console warnings/errors |

The Browser pass launched the complete application with isolated SQLite
storage. It loaded official TWSE history for 2026-05-01 through 2026-07-28,
the current TWSE MIS book, and the TWSE company OpenAPI share row effective
2026-07-27. The UI first showed an explicit missing-order-quantity blocker,
then recomputed after the user-visible 1,000-share input. SQLite contained the
matching `tradeable` assessment and the immutable 25,932,370,067-share source
revision. UI operation caught and fixed both a misplaced card and a `null`
to zero formatting bug. GitHub Actions was not used because quota was
unavailable; acceptance used the local Agent verifier, complete local pytest
suite, live official endpoints and actual in-app Browser operation.

## STOCK-008 acceptance (2026-07-28)

`scripts/verify_trading_anomalies.py` exercises the same schema-v31 event
ledger, deterministic detector and append-only tracking lifecycle used by the
application.

| Evidence | Result |
| --- | --- |
| Detector inputs | Only official unadjusted daily OHLCV rows are accepted; unavailable history fails visibly without synthetic or fallback candles |
| Supported events | Volume spike, opening gap, rapid rise/fall and price-volume divergence use documented deterministic thresholds |
| Traceability | Every event preserves the official source IDs, source record, detector version, metrics and policy that produced it |
| Immutable ledger | Stable fingerprints prevent duplicate events; repeat scans append observations and retain the original event |
| Tracking lifecycle | Open, acknowledged, resolved and reopened actions are append-only, while the API/UI project the latest state |
| Deterministic verifier | All four event types, idempotent repeat scan, two observations per event and resolved-state projection passed |
| Targeted regression | 50 anomaly, migration and unified-API tests passed; both affected JavaScript files passed syntax checks |
| Full local regression | 586 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Live 2330.TW scan processed 116 official candles, displayed 41 events, completed acknowledge/resolve, and a repeat scan saved zero duplicate events |
| Persistence audit | SQLite contained 41 immutable events, 123 observations and 43 tracking actions after three real UI scans |

The Browser pass launched the complete application with isolated Agent storage
and loaded 243 official TWSE daily candles plus the current TWSE MIS close
snapshot for 2330.TW. Clicking `掃描近半年` detected all four required event
families across 116 candles. The first 2026-07-28 downward-gap event was
acknowledged and resolved through the visible controls; rescanning reported
41 detections, zero new events and increased every observation count to three
without changing the resolved state. The only console message at error level
came from the existing liquid-glass `html2canvas` snapshot renderer while
loading its background; a direct request to the same local asset returned HTTP
200 with `image/jpeg`, and the anomaly UI/API flow completed successfully.
Because the GitHub Actions quota was unavailable, acceptance used the
deterministic local Agent verifier, the complete local pytest suite, SQLite
auditing and actual in-app Browser operation.

## FIN-001 acceptance (2026-07-29)

`scripts/verify_monthly_revenue_history.py` exercises the same official MOPS
archive parser, unified financial warehouse, raw lake and incremental
checkpoints used by the application.

| Evidence | Result |
| --- | --- |
| Historical coverage | 13 consecutive official periods from 2025-06 through 2026-06 were saved for 2330.TW; coverage reported complete |
| Required fields | Every accepted latest row exposed current revenue, MoM, YoY, YTD revenue and YTD YoY in disclosed thousand-TWD units |
| Source truthfulness | MOPS archive URLs and exact CP950 HTML bytes were preserved; missing original publication timestamps remained `null` and were not inferred from fiscal periods |
| Persistence | 13 financial revisions, 13 raw archive pages and 13 symbol/month checkpoints were audited in isolated SQLite storage |
| Incremental behavior | The second 13-month synchronization downloaded zero periods, skipped all 13 successful partitions and created no duplicate history |
| API boundary | History query and sync are two new versioned Unified Data API routes; compatibility aliases return the same contract |
| Targeted regression | 170 affected API, migration, platform, runtime and static-UI tests passed after updating the source-registry count |
| Full local regression | 657 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | Fundamentals synchronized 2330.TW for 2025-06–2026-06, rendered 13 rows with official links, reported 0 downloads and 13 skipped periods on repeat, and still rendered all 13 rows after a full application restart |

The Browser pass launched the complete application with isolated Agent and
market-data storage. It displayed the current 2026-06 official TWSE OpenAPI
revision and the preceding MOPS archive revisions in a single point-in-time
projection, including current revenue 442,679,969 thousand TWD, MoM 6.16%,
YoY 67.87%, cumulative revenue 2,404,483,690 thousand TWD and cumulative YoY
35.61%. SQLite retained the parallel MOPS 2026-06 revision rather than
overwriting it. A restart initially exposed Entity Registry identity drift;
the final projection reads both the canonical entity and deterministic
exchange/code storage identity, and a second Browser pass confirmed all 13
periods remained visible after restart. The only console errors were the pre-existing liquidGL
`html2canvas` background-snapshot failure on CSS `color()`; both monthly
revenue API calls returned HTTP 200 and the affected UI completed normally.
Because GitHub Actions quota was unavailable, acceptance used the local Agent
official-source verifier, complete local pytest suite, SQLite audit and actual
in-app Browser operation.

## FIN-002 acceptance (2026-07-29)

`scripts/verify_income_statement_history.py` exercises the official MOPS
individual-company IFRS history parser, unified `fundamentals_quarterly`
warehouse, raw lake and incremental checkpoints used by the application.

| Evidence | Result |
| --- | --- |
| Historical coverage | 52 consecutive official quarters from 2013-Q1 through 2025-Q4 were saved for 2330.TW, covering 13 years with complete coverage |
| Required fields | Annual/cumulative revenue, gross profit, operating income, net income attributable to owners and basic EPS were present for the latest official year |
| Quarter semantics | Q2/Q3 official single-quarter columns were preserved; Q4 standalone values remained null because the annual summary does not disclose them |
| Source truthfulness | Exact UTF-8 HTML, POST parameters and source labels were preserved; missing original publication timestamps remained null |
| Persistence | 52 financial revisions, 52 raw archive pages and 52 symbol/quarter checkpoints were audited in isolated SQLite storage |
| Incremental behavior | The second 52-quarter synchronization downloaded zero periods and skipped all successful partitions |
| API boundary | History query and sync are two new versioned Unified Data API routes; compatibility aliases return the same contract |
| Deterministic verifier | 2025 annual values were revenue 3,809,054,272; gross profit 2,281,293,979; operating income 1,936,091,677; parent-attributable net income 1,717,882,627 thousand TWD; EPS 66.26 TWD |
| Targeted regression | 151 affected API, migration, platform, runtime and static-UI tests passed |
| UI | Fundamentals synchronized 2330.TW from 2013-Q1 through 2026-Q1, rendered 53 quarters covering 14 years, skipped all 53 periods on repeat, and queried all rows after a full application restart |

Because GitHub Actions quota was unavailable, acceptance uses the local Agent
official-source verifier, complete local pytest suite, SQLite audit and actual
in-app Browser operation. The first Browser pass also exposed an incorrect
default end-quarter calculation; the UI was corrected to choose the latest
quarter whose actual quarter-end is at least 90 days old, then the full
sync, repeat-sync and restart-query flows were repeated successfully.

## FIN-003 acceptance (2026-07-29)

`scripts/verify_balance_sheet_history.py` synchronized 52 consecutive official
MOPS IFRS balance sheets for 2330.TW from 2013-Q1 through 2025-Q4. All six
required period-end fields were present in the latest statement: cash
2,767,856,402; assets 7,933,023,878; liabilities 2,472,228,595; equity
5,460,795,283; inventory 288,109,485; and accounts receivable 279,051,553
thousand TWD.

The isolated SQLite audit found 52 immutable financial revisions, 52 exact raw
HTML pages and 52 successful symbol-quarter checkpoints. A second sync
downloaded zero periods and skipped all 52. The query compared 2025-Q4 with
the consecutive saved quarter, 2025-Q3, rather than substituting the archive
page's year-end comparison column. GitHub Actions quota was unavailable, so
acceptance uses the local Agent verifier, local pytest suites, SQLite audit and
actual in-app Browser operation. The Browser synchronized 2330.TW from 2025-Q1
through 2026-Q1, rendered all six metrics and positive/negative previous-quarter
changes for five rows, skipped all five periods on repeat, and queried the same
five rows after a complete application restart.

## FIN-004 acceptance (2026-07-29)

`scripts/verify_cash_flow_history.py` synchronized 52 consecutive official MOPS
IFRS cash-flow statements for 2330.TW from 2013-Q1 through 2025-Q4. The latest
official cumulative values were operating cash flow 2,274,975,625, investing
cash flow -1,144,393,407, financing cash flow -440,344,692 and capital
expenditure -1,272,410,529 thousand TWD. The explicit formula produced free
cash flow 1,002,565,096.

The same-period official net income produced an operating-cash-flow conversion
ratio of 1.3243 and `strong_cash_conversion`. The isolated audit found 52 cash
flow revisions and 52 successful checkpoints, while the second sync downloaded
zero periods and skipped all 52. GitHub Actions quota was unavailable, so
acceptance uses the local Agent verifier, local pytest suites, SQLite audit and
actual in-app Browser operation. The Browser synchronized official income and
cash-flow statements for 2330.TW from 2025-Q1 through 2026-Q1, rendered all
five cash-flow dimensions and cash-conversion quality for five rows, skipped
all five cash-flow periods on repeat, and queried the same rows and quality
labels after a complete application restart.

## FIN-005 acceptance (2026-07-29)

`scripts/verify_financial_ratios.py` synchronized 52 matching official income
statements and balance sheets for 2330.TW from 2013-Q1 through 2025-Q4. Latest
ratios were gross margin 59.8913%, operating margin 50.8287%, net margin
45.1000%, ROE 32.7329%, ROA 22.4749% and debt ratio 31.1638%.

The verifier recomputed each value from the API's exposed formula inputs,
confirmed Q4 annualization factor 1, consecutive-quarter average denominators,
and official URLs for the current income statement, current balance sheet and
2025-Q3 prior balance sheet. GitHub Actions quota was unavailable, so
acceptance uses the local Agent verifier, local pytest suites and actual
in-app Browser operation. The Browser synchronized five official income and
balance-sheet periods, initially showed the truthful missing-balance state
while synchronization was still running, then rendered all six ratios and both
source links for five matching periods. After a full restart it recomputed the
same five rows from persisted official inputs.

## FIN-006 acceptance (2026-07-29)

`scripts/verify_growth_metrics.py` combined 24 official monthly revenue periods
for 2024–2025 with 52 official quarterly statements and 13 annual endpoints
for 2013–2025. December 2025 official growth was MoM -2.50%, YoY 20.43% and
cumulative YoY 31.60%. Q2/Q3 official standalone-quarter growth was 11.2646%
and 6.0106%; Q4 remained null because the annual page does not disclose
standalone Q4.

Latest annual growth was 31.6050%, with 3-year CAGR 18.9380%, 5-year CAGR
23.2511%, 10-year CAGR 16.2715% and 2013–2025 CAGR 16.6994%. All endpoints
retain official source URLs. GitHub Actions quota was unavailable, so
acceptance uses the local Agent verifier, local pytest suites and actual
in-app Browser operation. The Browser opened the complete application, selected
the fundamentals center, entered `2330.TW`, requested 2025 monthly data and
2013-Q1 through 2025-Q4 quarterly data, then rendered 12 monthly rows, 52
quarterly rows and 13 annual rows. It displayed December 2025 MoM/YoY/YTD YoY,
the comparable Q2/Q3 QoQ values, the truthful Q4 missing state and the
3/5/10-year CAGR values listed above. After a full server restart against the
same isolated database, the Browser repeated the calculation without a new
sync and rendered the identical counts and available-range CAGR, proving
persistence. During acceptance the historical MOPS host returned an HTTP 307
without a redirect target for recent statements; the downloader now retries
the current MOPS host while preserving the historical source URL, and the
isolated UI database completed all 52 quarters.

## FIN-007 acceptance (2026-07-30)

`src/stock_ai/financial_revisions.py` adds a financial-specific view over the
existing immutable schema-v19 revision ledger. It supports monthly revenue,
income statements, balance sheets and cash-flow statements; differences are
calculated only between consecutive revisions from the same entity and
source. Parallel sources therefore remain independent observations rather
than being mislabeled as restatements.

| Evidence | Result |
| --- | --- |
| Immutable chain | Two saved versions retained distinct revision IDs and the second linked to the first with `supersedes_revision_id` |
| Field difference | The controlled revenue restatement rendered and returned `100 → 105`, absolute `5` and `5.00%` |
| Point-in-time read | `knowledge_at=2026-05-12` selected v1; `knowledge_at=2026-05-22` selected v2 |
| Ordinary financial API | Income-statement history returned 100 before the restatement was known and 105 only after it became available |
| Cross-source isolation | A parallel MOPS source remained an original source chain and was not included in the same-source restatement diff |
| Unified API | Versioned and compatibility routes returned identical contracts |
| Deterministic verifier | All 8 FIN-007 checks passed; the report explicitly identifies its fixture as controlled and not an actual MOPS filing |
| Targeted regression | 31 financial, API and static-UI tests passed |
| Full local regression | 680 passed, 3 skipped, 8 xfailed, 1 xpassed |
| UI | The complete local application on isolated port 8017 displayed two immutable versions, selected v1 at the early cutoff, selected v2 at the late cutoff and showed the visible revenue difference |

The Browser pass opened `H 基本面中心`, entered `9000.TW`, selected the income
statement, entered `2025-Q4`, then queried both known-at times through the
Unified Data API. Both requests returned HTTP 200. The existing Liquid Glass
`html2canvas` CSS `color()` snapshot warning was present, but it did not affect
the revision query or table. GitHub Actions was not used because its quota was
unavailable; acceptance used the deterministic local Agent verifier, complete
local pytest suite, JavaScript/Python syntax checks and actual in-app Browser
operation.

## FIN-008 acceptance (2026-07-30)

`src/stock_ai/industry_metrics.py` replaces the single generic analysis
template with explicit financial, semiconductor, shipping and construction
profiles. Missing official inputs remain unavailable and an unknown industry
returns no metrics instead of silently falling back to the generic recipe.

| Profile | Actual official verification | Visible industry-specific evidence |
| --- | --- | --- |
| Financial (`2882.TW`) | TWSE OpenAPI financial-holding income/balance, 2026-Q1 | net interest income, credit provision, equity/assets and insurance-liability intensity |
| Semiconductor (`2330.TW`) | MOPS 2025-Q4 income/balance/cash flow | 59.89% gross margin, 33.40% capex intensity, 26.32% FCF margin |
| Shipping (`2603.TW`) | MOPS 2025-Q4 income/balance/cash flow | 19.55% operating margin, 0.42x asset turnover, 1.16x cash conversion |
| Construction (`2542.TW`) | MOPS 2025-Q4 income/balance/cash flow | 73.02% inventory/assets, 77.18% debt ratio, -31.24% OCF margin |

The in-app Browser opened the complete isolated application on port 8018,
selected `H 基本面中心`, entered each of the four symbols, selected its
industry and clicked `套用產業指標`. It rendered four different metric lists:
5/5 available for semiconductor, shipping and construction, and 6/6 for
financial. The versioned API returned HTTP 200 for each operation. GitHub
Actions was not used because its quota was unavailable; acceptance used local
Agent tests, official-source verification and actual Browser operation.

## FIN-009 acceptance (2026-07-30)

TWSE OpenAPI `t187ap15_L` supplies the voluntary forecast range and the later
auditor-reviewed actual in the same official row. The acceptance run found
eight current issuers. `2412.TW` 2026-Q1 reported actual comprehensive income
10,031,589 thousand TWD against a forecast range of 8,991,774–9,010,933
thousand TWD, so the contract classified it as above range. Qualitative
investor-conference or management outlook without numbers remains explicitly
not comparable.
