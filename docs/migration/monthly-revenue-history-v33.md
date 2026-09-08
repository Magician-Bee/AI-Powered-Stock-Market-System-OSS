# Schema v33 — Monthly revenue history range access

Schema v33 adds partial indexes for `FIN-001` without creating a second
financial store.

- `idx_financial_facts_monthly_revenue_period` accelerates symbol/period/source
  reads in the existing immutable `financial_facts` projection.
- `idx_monthly_revenue_history_checkpoints` accelerates successful
  `mops_archive` incremental-partition lookup.

Historical MOPS pages continue to use the existing Raw Data Lake, revision,
field-provenance and ingestion-checkpoint tables. The migration is additive;
`apply_migrations()` advances both `schema_migrations` and
`pragma user_version` to `33`.

See [Monthly Revenue History Contract V1](../data/monthly-revenue-history-contract.md)
for source, temporal and API behavior.
