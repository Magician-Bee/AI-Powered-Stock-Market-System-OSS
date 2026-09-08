# Security Lifecycle Migration v12

## Scope

Migration 12 completes `DATA-001` without replacing or deleting the v11
point-in-time warehouse. It adds `entity_lifecycle_events` so listings,
emerging-market entry, OTC/TWSE transfers, delistings and warrant expirations
remain queryable even when the entity's current venue or status changes.

## Official partitions

The checkpointed `OfficialSecurityMasterLoader` uses three independent
partitions:

| Partition | Source | Coverage |
| --- | --- | --- |
| `twse_official_master` | TWSE OpenAPI | listed companies, quotes, ETFs, warrants, indices and delisted companies |
| `tpex_official_master` | TPEx OpenAPI | OTC companies, emerging companies, quotes, warrants and official index series |
| `tpex_delisted_history` | TPEx official website data API | annual delisted-company history |

Each partition stores immutable raw payloads and a content-hash cursor. A fresh
checkpoint skips network I/O. A failed refresh keeps the previous successful
cursor and normalized revisions.

## Identity and lifecycle rules

- `.TW` and `.TWO` are display identifiers, never entity primary keys.
- Unified business number is the preferred corporate identity.
- Code plus normalized legal name is used when no business number exists.
- A company moving from emerging to OTC or TWSE keeps one `ENT-*`.
- A reused code with a different legal entity receives a different `ENT-*`.
- A historical delisting event from one venue does not overwrite an active
  listing on another venue.
- Source observations remain separate revisions even when they resolve to the
  same entity.

## Compatibility

- Existing v11 entities and identifiers are reused when their business number
  or code/name match.
- `/api/securities/master` keeps all previous fields and adds `entity_type`,
  `delist_date` and `venue_history`.
- Universe resolution continues to request the active stock/ETF compatibility
  view. The HTTP security-master endpoint can query `emerging`, `etf`,
  `warrant`, `index` and `delisted` views.
- No audit record is deleted or downgraded.

## Verification

```bash
python -m compileall -q src tests scripts
pytest -q tests/test_unified_market_data_platform.py tests/test_taiwan_universe_sync.py
python scripts/verify_unified_data_platform.py
pytest -q
```

Runtime acceptance also requires opening Data Catalog in a real browser,
confirming lifecycle counts/quality, running the `security_master` quality
button and inspecting a revision lineage record.
