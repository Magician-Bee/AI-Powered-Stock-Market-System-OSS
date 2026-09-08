# Standard Market Warehouse V1 / schema v17

Schema v17 completes `DATA-007` by materializing the common point-in-time
revision ledger into five shared research-domain tables:

| Domain | Physical table | Logical datasets |
| --- | --- | --- |
| Price | `market_prices` | daily and intraday prices |
| Financial | `financial_facts` | fundamentals, monthly revenue and valuation |
| Flow / positioning | `ownership_flows` | institutional flow, margin, ownership and derivatives |
| Event | `market_events` | company events, documents and impact records |
| Macro | `macro_observations` | macro series and observations |

Every domain row keeps the same `revision_id`, entity, observation key, source,
revision number, point-in-time coordinates, quality state and normalized JSON
record as `data_revisions`. The domain tables therefore improve discoverability
without creating a second source of truth.

`MarketDataWarehouse.write_revision` writes the revision, domain projection and
lineage in one SQLite transaction. Migration 17 backfills existing supported
revisions with `insert or ignore`, so upgrades are repeatable. The
`standard_records` read path applies the same knowledge-time and effective-time
cutoffs as the generic query.

Update and delete triggers keep every domain projection immutable. Process-wide
path locks serialize short SQLite write transactions across warehouse instances
while allowing independent source fetches to remain concurrent.

The HTTP contract is:

```text
GET /api/data/warehouse/{prices|financials|flows|events|macro}
```

Optional `dataset`, `entity_id`, `knowledge_at`, `effective_at` and `limit`
parameters narrow the query. Dataset/domain mismatches and unknown domains are
rejected instead of interpolated into SQL.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_standard_market_warehouse.py
```

The verifier writes one normalized record per research domain and proves that
the generic revision query and domain-table query return the identical revision
ID and payload.
