# Temporal Contract V1 / schema v15

Schema v15 completes `DATA-005` by separating the time when market facts are
effective from the time when a research process could actually know them.
Backtests and agents therefore use two independent query cutoffs:

- `effective_at` controls market, event, trade-date and fiscal-period validity.
- `knowledge_at` controls publication, source availability and local
  acquisition. A revision must pass all three knowledge-time checks.
- legacy `as_of` remains supported and sets both cutoffs to the same timestamp.

## Contract dimensions

Every `DataEnvelopeV2.temporal` and every field-provenance record now carries
`stock_ai.temporal_contract.v1` plus one reviewed `time_basis`:

| Dimension | Meaning |
| --- | --- |
| `trade_date` | Official trading date for prices and position/flow records |
| `fiscal_period`, `period_start`, `period_end` | Accounting period represented by a financial observation |
| `published_at` | Time declared by the publisher |
| `available_at` | Earliest time the source made the value obtainable |
| `acquired_at` | Time this system durably captured the value |
| `effective_at` / `expires_at` | Business-valid interval |

The contract rejects an incomplete trade-date or fiscal-period declaration,
publication after availability, acquisition before availability and reversed
period or validity intervals.

## Storage and migration

Migration 15 adds indexed revision dimensions without rewriting immutable
payloads or lineage:

- `temporal_contract_version`
- `time_basis`
- `trade_date`
- `fiscal_period`
- `period_start`
- `period_end`

Existing daily price, institutional-flow and margin rows are classified by
trade date. Monthly revenue rows are backfilled with their `YYYY-MM` period,
month bounds and report publication date. Security-master rows use lifecycle
time; all other legacy rows retain the explicit `snapshot` basis. Nested field
provenance receives the same dimensions.

The migration is idempotent. Rows that cannot be safely inferred remain
snapshots instead of receiving fabricated dates.

## Point-in-time read rule

A revision is eligible only when:

```text
published_at is null or published_at <= knowledge_at
available_at <= knowledge_at
acquired_at <= knowledge_at
effective_at <= effective_at cutoff
expires_at is null or expires_at > effective_at cutoff
```

Revision ranking happens after these predicates. A correction acquired later
therefore cannot replace the revision a historical run would have known.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_temporal_contract.py
```

The live verifier captures current TWSE monthly revenue and TAIFEX
institutional-futures responses in a clean database. It proves that fiscal data
is hidden before publication, before local acquisition and before period end,
and that trade-date data is hidden before its effective trading date.
