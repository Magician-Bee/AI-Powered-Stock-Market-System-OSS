# Revisioned Price Adjustments — Schema V27

Schema v27 is an additive migration for `STOCK-004`. Existing raw
`prices_daily` records and APIs remain compatible.

It adds three indexes:

- `idx_market_prices_adjusted_history` for range reads of one adjusted source;
- `idx_data_revisions_adjustment_events` for official event revision history;
- `idx_adjustment_factor_checkpoints` for yearly factor coverage checks.

The existing revision, raw-payload, standard-market projection, checkpoint and
lineage tables store the new datasets. No raw candle is updated in place.
Adjusted projections are content-idempotent and link to the exact raw candle
and factor-set revisions used to build them.

Applications can continue omitting `price_basis` to receive unadjusted data.
Adjusted callers should supply a complete range and preserve the returned
`factor_set_id` with their backtest or research artifact.
