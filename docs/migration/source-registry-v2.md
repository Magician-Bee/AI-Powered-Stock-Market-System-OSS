# Source Registry v2

`stock_ai.source_registry.v2` completes the DATA-003 source-registry contract.
`config/market_data_sources.yaml` is now the only production-owned location for
market-data upstream hosts and dataset endpoint paths.

## Contract

Every source declares:

- identity, authority and reviewed licensing state
- default update frequency, reliability tier and priority
- supported domains, source-level failover and usage boundaries

Every source dataset declares:

- source ownership, transport, method and relative endpoint template
- normalized fields plus required and optional field sets
- effective update frequency and reliability tier
- retry attempts, timeout, backoff, retryable HTTP statuses, exhausted behavior,
  stale window, dataset failover and checkpoint/error preservation policy

The runtime validates unique IDs and every source/failover reference before the
application can start. Absolute dataset URLs are rejected, so a connector
cannot quietly introduce a second endpoint registry.

## Runtime migration

The official security master, lifecycle lineage, daily price, institutional
flow, margin, revenue, MOPS, TAIFEX, TWSE MIS, Fugle, Yahoo and Google News
readers resolve endpoints by dataset ID. `MarketDataPlatform`, `/api/data/sources`
and the Data Catalog UI expose the same validated registry.

Legacy exported constants such as `TWSE_COMPANIES` remain for compatibility,
but their values are resolved from the registry and no longer own endpoint
strings.

## Verification

Run:

```bash
uv run python scripts/verify_source_registry.py
```

The verifier checks every dataset field/failure contract, scans production
market-data readers for upstream-host ownership, resolves concrete URLs back to
their dataset IDs, and reads live TWSE, TPEx, TAIFEX and TWSE MIS responses.

