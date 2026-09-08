# Data Envelope V2 Field Provenance Migration v14

Schema v14 completes `DATA-004` by making traceability a payload-field
invariant rather than a property of the revision as a whole.

## Contract

Every leaf value in `DataEnvelopeV2.payload` has one entry in
`field_provenance`, keyed by its RFC 6901 output pointer. Each entry records:

- the actual `source_id`
- observed, published, available, acquired, effective and expiry coordinates
- the field update time
- quality status and flags
- immutable raw payload ID and the source JSON pointer
- transformation ID and input fields

The envelope validator rejects missing field traces and traces for values that
do not exist. A transformation may map an output field to a different raw JSON
pointer; the two pointers are intentionally not required to be identical.

## Storage migration

Migration 14 adds the non-null `field_provenance_json` column to
`data_revisions`. Existing revisions are backfilled with their revision-level
source, temporal coordinates, quality, raw payload and transformation data.
Their `created_at` becomes the field update time. The migration is idempotent
for development databases whose `user_version` or migration ledger was
advanced independently.

No old payload, raw object, revision or lineage edge is overwritten. Existing
query endpoints retain their paths; each returned envelope now includes the
required `field_provenance` map.

## Runtime and UI

`MarketDataWarehouse.write_revision()` creates complete field traces by
default. Multi-source or transformed records can override individual field
entries while retaining the same envelope. The ingestion service accepts a
field-provenance factory for source-specific mappings.

The Data Catalog reports traced values and untraced revisions. Its lineage
panel exposes each visible field's source, quality, source/acquisition/update
times, raw payload ID and raw JSON path.

## Verification

Run the contract and migration tests:

```bash
pytest -q tests/test_unified_market_data_platform.py tests/test_agent_runtime_v2.py
```

Run acceptance against a current TWSE company response in an isolated
database:

```bash
python scripts/verify_data_envelope.py
```

The verifier fails unless all normalized leaf values resolve to a live raw
response path, every time and quality field is present, the raw hash is
reachable through lineage, and the warehouse reports zero untraced revisions.
