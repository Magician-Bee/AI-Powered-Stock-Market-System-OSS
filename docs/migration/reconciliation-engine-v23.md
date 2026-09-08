# Reconciliation Engine V1 / schema v23

Schema v23 completes `DATA-013` by replacing a one-off field comparison with a
versioned, point-in-time cross-source reconciliation workflow.

## Comparison contract

`config/data_reconciliation_rules.yaml` is the reviewed ruleset. For each
dataset it declares fields and comparison methods:

- price fields use absolute and relative numeric tolerances;
- financial fields use unit-aware absolute and relative tolerances;
- event types use exact matching, titles use Unicode/punctuation-normalized
  text and event timestamps use an explicit seconds window;
- a value missing from only one source is a conflict.

At `knowledge_at`, the engine selects one latest eligible revision for every
`dataset + entity + observation + source`. It never compares an old revision
from a source against that source's current state.

## Persistence

| Table | Evidence |
| --- | --- |
| `data_reconciliation_runs` | scope, rules version, knowledge cutoff, source/observation/comparison/conflict counts and complete summary |
| `data_reconciliation_conflicts` | stable source-pair identity, current revision IDs and values, method, tolerances, numeric differences and open/resolved lifecycle |

Conflict IDs are stable for one dataset/entity/observation/field/source pair.
A later mismatch refreshes its evidence; convergence marks it resolved with the
run that observed convergence. `data_revisions` and raw payloads are never
updated or deleted.

## API and UI

- `GET /api/data/reconciliation`
- `POST /api/data/reconciliation/runs`
- `GET /api/data/reconciliation/runs`
- `GET /api/data/reconciliation/runs/{run_id}`
- `GET /api/data/reconciliation/conflicts`

The legacy `POST /api/data/reconcile` route remains compatible and now uses the
same engine. Data Catalog shows configured actions, totals, the latest run and
open source-to-source values.

## Verification

```bash
uv run python scripts/verify_reconciliation_engine.py
uv run pytest -q tests/test_unified_market_data_platform.py
```

The verifier checks price conflict, financial tolerance, event normalization,
conflict resolution and immutable preservation of every source revision.
