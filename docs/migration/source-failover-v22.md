# Source Failover V1 / schema v22

Schema v22 completes `DATA-012` by turning the Source Registry failure policy
into an executable and auditable runtime path.

## Runtime contract

`SourceFailoverService.ingest()` receives a registered primary dataset, a
source-specific fetch callback and a normalizer. It:

1. follows the deterministic registry chain returned by
   `SourceRegistry.failover_chain()`;
2. applies each dataset's maximum attempts, retryable HTTP statuses and
   backoff;
3. captures reviewed failure responses when `preserve_error_payload` is set;
4. records the successful raw payload using the source that actually returned
   it;
5. writes normalized revisions and field provenance with that same source;
6. sets `is_fallback=true` only when a non-primary candidate succeeded.

The requested primary source is retained separately in the failover run. It is
never copied into the normalized revision's `source_id`.

## Storage

| Table | Purpose |
| --- | --- |
| `data_source_failover_runs` | requested dataset/source, policy snapshot, selected dataset/source, fallback flag, revisions and terminal error |
| `data_source_failover_attempts` | ordered retry/failover source, endpoint, HTTP status, captured raw payload, revisions and typed error |

Both tables reference the existing Source Registry copy in `data_sources`.
Attempt rows are append-only evidence. The run is updated once from `running`
to its terminal state.

## Read behavior

Primary and backup observations have different source-qualified revision
chains. A recovered primary therefore does not overwrite, supersede or relabel
the backup revision. `MarketDataPlatform.preferred_query()` prefers a
non-fallback observation when source priority is equal and reports
`fallback_used=true` when only fallback data is selected.

## API and UI

- `GET /api/data/failover`
- `GET /api/data/failover/runs`
- `GET /api/data/failover/runs/{run_id}`

Data Catalog shows total failover runs, actual selected source and whether
source identity preservation is active.

## Verification

```bash
uv run python scripts/verify_source_failover.py
```

The verifier injects three retryable primary HTTP 503 failures, succeeds on the
registered backup, checks raw/revision/field source identity, then recovers the
primary and confirms both source histories remain queryable.
