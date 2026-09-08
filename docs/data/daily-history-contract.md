# Complete Daily History V1

`STOCK-003` defines one complete-range daily candle contract for Taiwan listed
and OTC securities. The contract preserves raw, unadjusted exchange prices and
exposes every requested month, source and gap instead of silently keeping only a
recent chart window.

## API

Both the versioned UI façade and compatibility route use the same service:

```text
GET /api/data/ui/v1/market/{symbol}/history
GET /api/market/{symbol}/history
```

Complete-range parameters:

| Parameter | Meaning |
| --- | --- |
| `start` | Required ISO start date for a complete-range query |
| `end` | Inclusive ISO end date; defaults to today when `start` is present |
| `limit` | Page size from 1 to 5,000 |
| `cursor` | Last trade date from the previous page |
| `refresh` | Fetch official monthly partitions before reading the warehouse |
| `allow_fallback` | Permit clearly marked Yahoo research fallback when an official month fails |
| `price_basis` | `unadjusted`, `forward_adjusted`, or `backward_adjusted` |
| `refresh_adjustments` | Fetch official ex-right/dividend events before rebuilding factors |
| `require_complete_adjustment` | Fail closed when any requested factor year is unavailable |

Pages are ordered by trade date. `next_cursor` is exclusive, so concatenating
pages cannot repeat or omit a candle. `total_point_count` is the complete
selected-source count for the requested range, independent of page size.

## Candle fields and units

`stock_ai.daily_history.v1` returns:

- `open`, `high`, `low`, `close`: original exchange prices, TWD.
- `volume`: shares. TPEx `成交張數` is multiplied by 1,000.
- `turnover`: TWD. TPEx `成交仟元` is multiplied by 1,000.
- `price_basis`: explicitly selects raw, forward-adjusted, or
  backward-adjusted OHLC.

Suspended/no-trade rows without a valid close are not converted into zero-price
candles. Missing turnover remains `null`; it is never estimated from close times
volume. STOCK-004 persists all three price projections and the cumulative
factors while retaining the original raw fields on every adjusted point. Volume
and turnover remain raw exchange units. See
[Price Adjustment Contract](price-adjustment-contract.md).

## Completeness and provenance

TWSE uses the registered `twse_stock_day` source and TPEx uses
`tpex_trading_stock`. Every requested calendar month receives a durable
checkpoint:

- `succeeded`: the official request completed, including a legitimate empty
  pre-listing or no-trade month.
- `failed`: the request failed and the error type is retained.
- `not_fetched`: the month has not been requested in the local warehouse.

`range_complete=true` only when every requested official month succeeded.
Fallback rows can preserve research continuity but never make official coverage
complete. `coverage`, `errors`, `source_ids`, `fallback_count`,
`field_coverage` and `turnover_complete` make those boundaries machine-readable.

Every source retains independent immutable `prices_daily` revisions, raw payload
capture, field provenance and lineage. The read projection chooses a
non-fallback, higher-priority official source for each trade date without
deleting Yahoo or older official revisions.

## Storage and performance

Schema v26 adds composite raw daily-history indexes. Schema v27 adds immutable
adjusted-price and factor-checkpoint indexes. Complete imports use one
transaction for all candle revisions and one transaction for all monthly
checkpoints, while unchanged payload hashes reuse the current revision. This
keeps multi-decade first imports and repeat reads bounded without weakening the
immutable revision model.

## UI

Individual Analysis exposes start/end date controls, a price-basis selector and
`查詢完整區間`. The visible status reports:

- requested range and complete/partial coverage;
- total daily candles and selected price basis;
- adjustment completeness, official factor source and event count;
- turnover coverage;
- selected source IDs and fallback use.

The chart receives the complete concatenated page sequence. Switching to an
intraday timeframe keeps the separate STOCK-002 trading-date controls.

## Verification

```bash
PYTHONPATH=src python scripts/verify_daily_history.py
pytest -q tests/test_daily_history.py tests/test_taiwan_universe_sync.py
```

The deterministic verifier imports 6,200 candles across 286 months, reads
5,000 + 1,200 rows with no gap, verifies complete OHLCV/turnover coverage,
official-over-fallback priority, immutable restatement history and the unified
API response.
