# Cache Policy V2 / schema v21

Schema v21 completes `DATA-011` by enforcing the per-dataset TTL and
invalidation declarations that were previously descriptive only.

Each policy declares:

- a fresh TTL;
- a bounded stale-while-revalidate window;
- a refresh-lease duration;
- whether stale data may remain readable during a failed refresh;
- reviewed invalidation reasons such as listing, official revision, corporate
  action, session or contract-expiry changes.

The runtime classifies every source/dataset/partition cache entry as:

| State | Behaviour |
| --- | --- |
| `empty` | no successful refresh exists; fetch is required |
| `fresh` | reuse stored data and do not call the upstream source |
| `stale_while_revalidate` | one caller refreshes; explicitly bounded stale data may remain readable |
| `expired` | refresh is required and stale data is not declared serviceable |
| `invalidated` | a reviewed event bypasses the remaining TTL and forces refresh |
| `forced` | an explicit operator refresh bypasses freshness but still obeys single-flight |

Three tables make the behaviour auditable:

| Table | Responsibility |
| --- | --- |
| `data_cache_entries` | policy snapshot, freshness windows, generation, decision and hit/miss counters |
| `data_cache_invalidations` | append-only reason, scope, metadata and timestamp |
| `data_cache_refresh_leases` | bounded cross-process single-flight lease per source/dataset/partition |

`IncrementalLoader` now asks `CachePolicyService` for a decision before opening
the source. A fresh decision returns `skipped_fresh`. The refresh winner owns a
database lease; a concurrent caller returns `skipped_refresh_in_progress`
without a duplicate source request. Successful completion advances the cache
generation and clears prior invalidation; failure or controlled pause releases
the lease without falsely marking the partition fresh.

Only policy-declared invalidation reasons plus the universal
`manual_refresh` operator reason are accepted. Every accepted invalidation is
persisted and the next loader run refreshes even if the old TTL has not elapsed.

The HTTP contract is:

```text
GET  /api/data/cache
GET  /api/data/cache/{dataset}
POST /api/data/cache/{dataset}/invalidate
```

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_cache_policy.py
```

The verifier proves distinct 5-second, 900-second and 21,600-second TTLs,
bounded stale service, hard expiry, concurrent request suppression, fresh
reuse, reviewed invalidation, cursor-preserving refresh and lease cleanup.
