# Complete Daily History / schema v26

Schema v26 is an additive performance migration for STOCK-003. It does not
rewrite or delete any existing daily-price revision.

It adds:

- `idx_market_prices_daily_history`;
- `idx_data_revisions_daily_history`;
- `idx_daily_history_checkpoints`.

The indexes support complete symbol/date range reads, current source revision
selection and monthly coverage lookup. Existing schema v25 intraday candle
tables and their immutability triggers remain unchanged.

Application-level batch writers now persist a complete daily import and its
month checkpoints in bounded transactions. Payload hashes still determine
whether a new immutable revision is required.
