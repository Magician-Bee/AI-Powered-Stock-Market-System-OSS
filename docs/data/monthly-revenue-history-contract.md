# Monthly Revenue History Contract V1

`stock_ai.monthly_revenue_history.v1` implements `FIN-001` as an immutable,
source-attributed monthly series. It preserves more than the latest disclosure
and does not recompute or invent missing official growth values.

## Official source and range

Historical rows come from the MOPS monthly-revenue archive:

```text
https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc_year}_{month}_0.html
```

The supported archive range starts at `2010-01`. Each request is bounded to 240
months and resolves the MOPS market partition from the Entity Registry when
possible. `.TW` and `.TWO` suffixes are an explicit compatibility fallback;
callers can supply `market_segment` when the registry cannot resolve a symbol.

Each row preserves the official current month, previous month, prior-year
month, MoM, YoY, year-to-date revenue, prior-year year-to-date revenue and
year-to-date YoY values. Revenue units are always disclosed as
`thousand_twd`. Empty or unavailable official cells remain `null`.

## Point-in-time and provenance

MOPS archive pages do not expose the original publication timestamp.
`published_at` therefore remains `null`, while `available_at` is conservatively
set to the actual acquisition time. The response exposes
`publication_time_status=not_provided_by_archive` so a historical researcher
cannot mistake the archive period for a known announcement time.

The current TWSE OpenAPI `出表日期` is retained as `report_date` with
`report_date_semantics=openapi_export_date`; it is not treated as a company
publication timestamp. Its `published_at` also remains `null` and its
`available_at` is the actual acquisition time.

The exact CP950 HTML bytes, wire hash, request URL, parser version,
transformation version, field-level JSON pointers and every immutable financial
fact revision are stored through the unified market-data platform.

## Incremental behavior

The loader checkpoints each `market:period:symbol` partition independently.
Successful months are skipped on the next sync; failed months remain eligible
for retry. A sync response reports fetched, skipped, recorded and failed
periods. Query coverage lists every missing requested month instead of silently
claiming completeness.

Reads include both the current canonical Entity Registry ID and the deterministic
exchange/code storage ID. This keeps pre-registry archive revisions queryable
after a later security-master refresh resolves the symbol to its canonical
entity, without rewriting immutable history. The response lists the storage
entity IDs used for that projection.

## API and UI

- `GET /api/data/ui/v1/fundamentals/revenue/history`
- `POST /api/data/ui/v1/fundamentals/revenue/history/sync`

Compatibility aliases remain under `/api/fundamentals/revenue/history*`.
The Fundamentals page accepts a symbol and month range, renders every saved
period with MoM, YoY, YTD and YTD YoY, and links each row to its official
archive page.
