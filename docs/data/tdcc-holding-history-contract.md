# TDCC Holding Distribution History V1

`CHIP-005` exposes the official weekly TDCC holding-grade snapshot through the
backend-owned unified UI boundary:

```text
GET /api/data/ui/v1/flow/chip/tdcc-history
GET /api/flow/chip/tdcc-history
```

The query requires a Taiwan security symbol and accepts `weeks=1..104` plus
`refresh=true`. An explicit `.TW` or `.TWO` suffix is preserved. A bare numeric
symbol remains backward-compatible and defaults to `.TW`.

## Official source and history boundary

The backend reads TDCC OpenAPI dataset `1-5`:

```text
https://openapi.tdcc.com.tw/v1/opendata/1-5
```

TDCC compiles the distribution after the final business day of each week,
after consolidating accounts by investor ID. The API dataset supplies the
latest snapshot. Each refresh stores the matched official rows, normalized
summary and SHA-256 in SQLite, so local history grows one published week at a
time. Missing weeks are not filled with zero.

The TDCC web query states that historical files are retained for one year, but
this implementation does not claim a one-year automated backfill because the
OpenAPI endpoint used here only exposes the latest snapshot.

## Disclosed grouping rules

The response preserves all 17 official holding grades. UI summaries use these
explicit mechanical groupings:

- `small_shareholder_*`: grades 1–3, or 1–10,000 shares. This is a UI grouping,
  not an investor classification asserted by TDCC.
- `major_holder_1000_lot_ratio`: grade 15, or 1,000,001 shares and above.
- `holder_400_lot_ratio`: sum of grades 12–15, or 400,001 shares and above.
- `concentration_score`: exactly the 400,001-shares-and-above ratio. It is a
  display metric, not a model score or investment signal.
- Grade 16 is preserved as a difference adjustment row and excluded from the
  UI group sums. Grade 17 supplies totals.

Every stored snapshot includes `raw_hash`, whose scope is the canonical JSON
of the security's matched official grade rows. Weekly comparisons are derived
only from stored observations and keep total holders, small-holder count and
ratio, 1,000-lot ratio and 400-lot ratio separate.

## Failure behavior

If refresh fails and prior snapshots exist, the response status is
`stale_cache` and the sync error is disclosed. If no saved observation exists,
status is `no_data`. The service never fabricates a row, silently substitutes a
different security, or treats the weekly statistics as realtime trading data.
