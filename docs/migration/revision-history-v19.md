# Revision History V1 / schema v19

Schema v19 completes `DATA-009` by making normalized revisions and persisted
point-in-time manifests immutable.

`data_revisions` remains the source of truth. A changed payload creates the next
revision number and points `supersedes_revision_id` at the immediately previous
version; an unchanged payload reuses the existing revision. Database triggers
reject updates and deletes, so callers cannot bypass the append-only contract.

Two new tables persist reproducible historical states:

| Table | Responsibility |
| --- | --- |
| `data_revision_snapshots` | Dataset, knowledge/effective cutoffs, filters, item count and manifest hash |
| `data_revision_snapshot_items` | Deterministically ordered revision IDs and payload hashes in the state |

Snapshot construction applies publication, availability, acquisition,
effective and expiry cutoffs before selecting the latest known revision for
each dataset/entity/observation/source key. It has no query-result cap, so a
complete dataset state is not truncated at the interactive API's 10,000-row
limit.

The snapshot ID is derived from the canonical manifest hash. Repeating the same
query against the same revision set returns the same snapshot instead of
creating a duplicate. Reads recompute the ordered revision hash and manifest
hash, then report an explicit integrity result.

The HTTP contract is:

```text
GET  /api/data/revisions/history
POST /api/data/snapshots
GET  /api/data/snapshots/{snapshot_id}
```

History requires dataset, entity and observation key, with optional source. It
returns every revision in order and validates sequence and supersession links.
Snapshot reads are paginated without changing the immutable complete manifest.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_revision_history.py
```

The verifier writes an original monthly revenue and a later restatement,
reconstructs the state before and after acquisition, validates both manifests,
proves deterministic reuse, and confirms direct SQL mutations are rejected.
