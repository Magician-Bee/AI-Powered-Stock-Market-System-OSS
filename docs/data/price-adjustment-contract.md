# Revisioned Price Adjustment Contract V1

`STOCK-004` makes the daily price basis an explicit, reproducible input rather
than silently modifying historical candles. It supports Taiwan listed (`.TW`)
and OTC (`.TWO`) securities for periods covered by the official
ex-right/dividend result services.

## Price bases

| API value | Definition | Anchor |
| --- | --- | --- |
| `unadjusted` | Original exchange OHLC | none |
| `forward_adjusted` | Historical OHLC multiplied by every later event ratio in the requested range | requested end; the latest raw price is unchanged |
| `backward_adjusted` | Forward factor divided by the first raw trade date's forward factor | requested start; the earliest raw price is unchanged |

For an official event, the factor ratio is:

```text
(previous close - official total adjustment value) / previous close
```

If the official total adjustment value is absent, the published reference
price divided by previous close is used. The calculation keeps 12 decimal
places for factors and rounds persisted OHLC projections to 6 decimal places.
It never derives a factor from price jumps.

TWSE official result history starts on 2003-05-05. TPEx official result history
starts on 2008-01-02. Every requested year receives a durable `succeeded`,
`failed`, or `unsupported` checkpoint. Adjusted queries fail closed by default
if any requested year is incomplete.

## API and backtests

Both history routes accept the same explicit selector:

```text
GET /api/data/ui/v1/market/{symbol}/history?start=...&end=...&price_basis=...
GET /api/market/{symbol}/history?start=...&end=...&price_basis=...
```

The response includes `factor_set_id`, factor revision, source, method,
anchors, yearly coverage, event count, and `adjustment_complete`. Each point
contains the selected OHLC plus raw, forward-adjusted, and backward-adjusted
OHLC and its selected factor. Backtests use `backtest_price_series` with the
same mandatory basis and retain the factor-set ID.

The first request persists one range-specific factor set and later pages reuse
that exact immutable factor-set revision. The backtest helper follows every
page automatically, so multi-decade ranges are not truncated at 5,000 rows.

## Storage and lineage

Schema v27 stores:

- official source payloads and immutable `price_adjustment_events` revisions;
- a range-specific `price_adjustment_factor_sets` revision;
- immutable `prices_adjusted_daily` revisions containing all three OHLC bases
  and both cumulative factors;
- lineage from each projection to its raw daily-price revision and factor-set
  revision.

An identical refresh reuses the existing content revisions. A changed official
event or raw candle creates a superseding revision without deleting history.
Volume remains raw shares and turnover remains raw TWD for every basis.

## Scope boundary

This contract covers official ex-right/dividend reference-price events only.
ETF splits, reverse splits, capital reductions, mergers, symbol changes and
other corporate-action lifecycle events are not guessed from market prices;
they belong to the separate `STOCK-005` corporate-action ledger.

## Verification

```bash
PYTHONPATH=src python scripts/verify_price_adjustments.py
pytest -q tests/test_price_adjustments.py tests/test_daily_history.py
```

The deterministic verifier projects 6,200 raw candles through two official
event fixtures, reads 5,000 + 1,200 rows without gaps, proves both anchor
definitions, checks persisted raw/front/back values and confirms that backtests
can explicitly select any price basis.
