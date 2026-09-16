# Raw Data Lake V1 / schema v16

Schema v16 completes `DATA-006` by preserving source responses independently
from their parsed and normalized representations. A raw capture now has two
linked identities:

- `RAW-*` identifies the parsed logical payload used by revisions and lineage.
- `RDO-*` identifies the exact source bytes by source ID and wire SHA-256.

`raw_data_objects` stores API, JSON, CSV or text bytes, media type, encoding,
request/receive time, URL, HTTP status, byte length and SHA-256. Database
triggers reject updates and deletes from both raw object and logical payload
tables. Repeated equivalent content is deduplicated without changing existing
objects.

## Cleaning replay

`POST /api/data/raw/{raw_payload_id}/reprocess` reads the immutable bytes,
verifies the wire hash, selects the recorded allowlisted parser and reruns the
deterministic parse/clean stage. A successful run must reproduce the stored
parsed-payload hash. Every attempt is written to `raw_reprocessing_runs` with
its input/output hashes, parser, transformation, code version, row count,
status, error and timestamps.

The supported V1 parser contracts are:

- `stock_ai.raw.json.v1`
- `stock_ai.raw.csv.v1`
- `stock_ai.raw.text.v1`

No endpoint accepts executable code or an arbitrary import path.

## Legacy migration

Existing schema-v11 raw JSON rows remain unchanged. Migration 16 creates one
raw object from each stored canonical JSON document, labels it
`canonical_json_reconstruction`, and links it to the existing `RAW-*` ID.
New connectors can pass `raw_response` to the unified ingestion service to
store exact source bytes and receive the `exact_source_bytes` marker.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_raw_data_lake.py
```

The live verifier captures the exact response bytes from the TWSE monthly
revenue OpenAPI, compares the stored wire hash with a separately calculated
SHA-256, reruns JSON cleaning, exercises CSV cleaning, and proves the immutable
database trigger rejects mutation.
