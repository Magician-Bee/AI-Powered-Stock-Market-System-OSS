# Unified Market Data Migration v11

## Baseline

The pre-v11 application used multiple direct-fetch paths:
`phase1_data.py`, `services.py`, `MarketDataHub`, event stores, derivative
stores and external adapters. They could expose provenance, but did not share
one revisioned point-in-time warehouse.

## Migration sequence

1. Apply schema migration 11 without removing any legacy table.
2. Register reviewed source identity and explicit terms-review state from
   `config/market_data_sources.yaml`.
3. Backfill the current TWSE/TPEx security master through
   `MarketDataPlatform.sync_security_master_payloads`.
4. Write new institutional-flow, margin and monthly-revenue observations to the
   warehouse while retaining compatibility response models.
5. Route the OpenStockAI research Pipeline through
   `UnifiedResearchDataGateway`; persist its daily price history as
   `DataEnvelopeV2`.
6. Use `IncrementalLoader` for checkpointed partitions and enforce the
   dataset-specific TTL/invalidation policy.
7. Migrate remaining intraday, financial, ownership, event, macro and
   derivative readers one domain at a time.
8. Compare legacy and warehouse results for the same source payload.
9. Switch each consumer only after PIT, lineage and quality checks pass.
10. Remove a legacy fetch path only after repository scans and Browser E2E prove
   it has no production callers.

## Compatibility

- Existing Paper OMS, Agent and analysis tables are unchanged.
- `/api/securities/master` keeps its current fields and adds `entity_id`.
- Failed warehouse persistence never relabels an in-memory official response as
  warehouse data; platform health reports the failure.
- No automatic data deletion is part of this migration.

## Rollback

Code may stop reading v11 tables while leaving them intact. Schema downgrade or
destructive deletion is intentionally unsupported because raw payloads and
revision history are audit records.

## Verification plan

```bash
python -m compileall -q src tests scripts
pytest -q tests/test_unified_market_data_platform.py
pytest -q tests/test_sqlite_migrations.py tests/test_taiwan_universe_sync.py
pytest -q
python scripts/check_neutrality_ci.py
uv run python scripts/verify_unified_data_platform.py
git diff --check
```

Runtime acceptance additionally requires actual official-source ingestion,
point-in-time replay, API inspection and Browser operation of the data status
surface.
