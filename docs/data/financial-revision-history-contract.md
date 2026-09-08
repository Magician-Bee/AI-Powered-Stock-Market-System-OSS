# Financial Revision History Contract V1

## Scope

FIN-007 requires financial restatements to remain queryable as immutable
versions and historical research to use only the version that was actually
available at its knowledge cutoff. The contract covers monthly revenue,
income statements, balance sheets and cash-flow statements.

This feature does not assert that every issuer or period has a restatement.
An empty or one-version chain means only that the local warehouse currently
contains zero or one saved version.

## Architecture and data contract

The implementation reuses the schema-v19 `data_revisions` ledger and its
standard financial projection. No second financial database or mutable
"current statement" table is introduced.

`stock_ai.financial_statement_revision_history.v1` returns:

- the resolved entity, dataset and observation key;
- every immutable revision, its source, raw-payload link, temporal coordinates,
  quality state and `supersedes_revision_id`;
- field-level `before`, `after`, absolute and percentage differences;
- the revision selected for the requested `knowledge_at` and `effective_at`;
- chain-integrity issues and explicit truthfulness notes.

Differences are calculated only between consecutive revisions with the same
entity and `source_id`. Parallel sources are preserved independently and are
never described as though one restated the other.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `data_platform/warehouse.py` | Immutable revision chain, standard projection and bitemporal selection |
| `financial_revisions.py` | Financial statement mapping, same-source field diff and point-in-time response |
| `monthly_revenue.py` and statement modules | Propagate `knowledge_at` and `effective_at` into ordinary history reads |
| `financial_ratios.py` and `growth_metrics.py` | Compute derived values from inputs selected at the same cutoffs |
| `main.py` and `data_platform/ui_api.py` | Unified and compatibility HTTP routes |
| Fundamentals UI | Operator query and visible original/restated/selected-version table |

## Execution flow

1. An official loader saves normalized financial data as an immutable revision.
2. A changed payload from the same source creates the next revision and links
   it to the prior revision; the old row cannot be updated or deleted.
3. `GET /api/data/ui/v1/fundamentals/revisions/history` resolves the issuer and
   period, reads the complete chain, then compares tracked financial fields.
4. The same request applies `knowledge_at` and `effective_at` to the standard
   warehouse and marks only the version that research could have observed.
5. Ordinary revenue, statement, ratio and growth history APIs pass the same
   cutoffs through their entire dependency graph.

## Migration

No schema migration is required. Schema v19 already enforces immutable
financial revisions, and schema v24 already preserves complete lineage.
FIN-007 adds a domain-specific read contract and cutoff propagation. Existing
rows remain valid and no backfill rewrites them.

## Test and acceptance plan

- Unit: same-source `100 → 105` restatement produces the exact field diff and
  `supersedes_revision_id`.
- Isolation: a parallel source remains an original chain and is not included
  in the restatement diff.
- Point in time: a cutoff before the second acquisition returns `100`; a later
  cutoff returns `105`.
- Integration: ordinary income-statement history follows the same cutoff.
- API: unified and compatibility routes return identical responses.
- UI: enter issuer, statement kind, period and optional known-at time; verify
  the visible original/restated rows, selected version and field changes.
- Regression: run affected financial suites, the complete local pytest suite
  and JavaScript syntax checks.

`scripts/verify_financial_revision_history.py` provides a deterministic,
controlled restatement fixture. Its report explicitly sets
`fixture_is_claimed_as_real_mops_filing=false`; it verifies behavior without
misrepresenting synthetic acceptance data as an actual issuer filing.

## Known limits

- MOPS archive pages that do not expose their original filing timestamp retain
  `published_at=null`; acquisition time is never relabeled as publication time.
- The UI shows only revisions already saved locally. It does not infer an
  unobserved historical restatement.
- A production restatement audit still depends on the corresponding official
  source loader having acquired both versions.
