# Schema v34 — Income statement history range access

Schema v34 adds partial indexes for `FIN-002` without creating a second
financial truth store.

## Indexes

- `idx_financial_facts_income_statement_period` accelerates entity/quarter
  projections for `fundamentals_quarterly`.
- `idx_income_statement_history_checkpoints` accelerates symbol/quarter
  incremental synchronization and restart recovery.

All records continue to use the Phase 1 raw-data lake, immutable revisions,
temporal contract, field provenance and standardized `financial_facts` table.
Applying migrations advances SQLite `pragma user_version` to `34`.

See [Income Statement History Contract V1](../data/income-statement-history-contract.md)
for source, field and point-in-time semantics.
