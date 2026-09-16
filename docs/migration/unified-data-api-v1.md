# Unified Data API V1

Unified Data API V1 completes `DATA-014` by placing every market-data request
made by the browser behind one versioned boundary:

```text
/api/data/ui/v1
```

The route registry lives in `stock_ai.data_platform.ui_api`. It records the
HTTP method, versioned path, compatibility path and UI consumers for 53 routes.
`GET /api/data/ui/v1/contract` exposes that registry at runtime.

## Boundary

The 53-route façade covers security master/search, market
summary/history/events/trading restrictions/liquidity/trading anomalies,
realtime status/snapshot/stream, watchlist, indices, news, institutional and
margin flows, monthly revenue, income statements, balance sheets, cash flows, financial ratios, growth metrics, financial revisions, industry-specific metrics, reports, screener, linkage, question routing,
sources and the catalog.

`CHIP-003` and `CHIP-004` add the borrowed-short/day-trade history façade.
The backend selects the official TWSE or TPEx source from an explicitly
provided exchange suffix, persists source rows with a hash, and returns
borrowed selling separately from margin short selling. The browser receives
official day-trade volume, total volume, the disclosed ratio formula and
revision status; it never calls either exchange directly.

`CHIP-005` adds the TDCC weekly holding-distribution façade. The backend reads
the latest official 17-grade snapshot, stores raw-row hashes and accumulates
weekly history without zero filling. The browser receives disclosed 1–10,000
share, 400-lot and 1,000-lot groupings and does not call TDCC directly.

`STOCK-002` extends the same boundary with intraday-candle status, available
dates and reconstructable 1/5/15/30/60-minute candle queries. Those three
routes still use compatibility aliases, source selection and persistence
behind backend services; the browser never contacts Fugle or Yahoo directly.

`STOCK-008` adds list, scan and lifecycle-tracking routes for source-attributed
trading anomaly events. The browser still does not run detection or contact an
exchange endpoint directly.

`FIN-001` adds monthly-revenue history query and incremental official-archive
sync routes. MOPS access, parsing, raw preservation and checkpointing remain
behind backend services.

`FIN-002` adds income-statement history query and incremental official-archive
sync routes. The browser receives normalized cumulative and disclosed
single-quarter values but never contacts MOPS or invents missing Q4 metrics.

`FIN-008` adds the industry-specific metric route. The browser selects a
profile, while TWSE/MOPS access, formulas, missing-field policy and source
lineage remain behind the service boundary.

`FIN-009` adds official voluntary financial forecasts and period-matched
audited/reviewed actuals. Qualitative investor-conference guidance remains
non-comparable unless it has a sourced numeric range.

UI scripts build these URLs with `uiDataApi()`. They do not import connectors,
own upstream URLs or choose sources. Backend routes continue to use the
registry-backed services and `MarketDataPlatform`.

Legacy routes remain registered as compatibility aliases. They are not used by
the bundled UI and can be removed only in a separately reviewed breaking
release.

## Enforcement

`audit_ui_data_access()` scans every bundled JavaScript file for compatibility
market-data paths. CI tests fail if a page bypasses the versioned façade.
Route-table tests also require every declared path to be registered and verify
payload parity with a compatibility route.

```bash
uv run python scripts/verify_unified_ui_data_api.py
uv run pytest -q tests/test_api.py tests/test_static_ui.py
```
