# Income Statement History Contract V1

`stock_ai.income_statement_history.v1` implements `FIN-002` through the
existing unified financial warehouse.

## Official source and coverage

- Source: MOPS historical individual-company IFRS income statement
  (`mopsov.twse.com.tw/mops/web/ajax_t164sb04`).
- Transport: official `POST` request with market, company code, Minguo year and
  quarter parameters.
- Earliest supported period: `2013-Q1`, the start of the available IFRS
  archive.
- Maximum request range: 64 quarters.
- Default end: the latest quarter whose end is at least 90 days old, avoiding
  a claim that a newly ended quarter is already fully filed.

The verifier preserves 52 consecutive official quarters from 2013-Q1 through
2025-Q4, covering 13 calendar years.

## Normalized values

Every record identifies its fiscal year, quarter, symbol, statement scope,
source request and field labels. Primary metrics are official year-to-date
cumulative values for Q1-Q3 and official annual values for Q4:

- `revenue`
- `gross_profit`
- `operating_income`
- `net_income`
- `eps`

Q1-Q3 also expose the corresponding `current_quarter_*` fields exactly as
disclosed. Q4 single-quarter fields remain null because the annual summary does
not disclose standalone fourth-quarter values. In particular, the system does
not subtract cumulative EPS because weighted-average share counts make that
operation semantically unsafe.

Net income prefers the official amount attributable to owners of the parent
when present and records `net_income_basis=attributable_to_parent`. If a
company or industry uses a different official statement layout, unsupported or
non-meaningful metrics remain null rather than being synthesized.

Financial values use `thousand_twd`; EPS uses `twd_per_share`.

## Point-in-time and provenance

Each symbol/quarter partition preserves:

- exact UTF-8 response bytes and wire hash;
- POST parameters and source URL;
- parser and transformation versions;
- immutable financial revisions;
- field-level source and raw-payload lineage;
- incremental checkpoint state.

The historical page does not provide the original filing timestamp.
Consequently `published_at` remains null and `available_at` is the actual
acquisition time. A backfill therefore cannot claim the data was known on its
historical fiscal-period end date.

## API and UI

Versioned routes:

- `GET /api/data/ui/v1/fundamentals/income-statement/history`
- `POST /api/data/ui/v1/fundamentals/income-statement/history/sync`

Compatibility aliases exist under `/api/fundamentals`. The Fundamentals UI
shows coverage, units, cumulative versus single-quarter semantics, missing Q4
single-quarter values and official source links.
