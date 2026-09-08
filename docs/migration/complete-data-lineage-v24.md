# Complete Data Lineage V1 / schema v24

Schema v24 completes `DATA-015` by extending the existing raw-payload and
revision lineage into immutable derived artifacts.

## Stored chain

`data_lineage_artifacts` stores versioned indicators, scores, reports and
conclusions as content-addressed records. `data_artifact_lineage_edges` stores
each input revision or earlier artifact together with:

- its semantic role and RFC 6901 input fields;
- transformation ID, code version and parameters;
- the immutable output artifact and creation time.

The existing revision edges continue to connect normalized observations to
their raw payloads and source objects. A complete graph therefore preserves:

```text
source → raw payload → normalized revision → derived indicator → conclusion
```

Artifact and edge rows are protected by database triggers against update and
delete. Identical content and identical inputs resolve to the same artifact
hash instead of creating an ambiguous duplicate.

## Fail-closed writes

`record_lineage_artifact()` validates every referenced ID and requested input
field. It recursively checks each leaf revision for a stored raw payload whose
wire SHA-256 passes integrity verification. A missing node, missing raw object,
unknown field or integrity failure rejects the artifact before it is written.

This prevents a final conclusion from claiming provenance merely because it
mentions a revision ID.

## API and UI

- `GET /api/data/lineage/artifacts` lists persisted derived artifacts.
- `POST /api/data/lineage/artifacts` writes a validated artifact and returns
  its complete graph.
- `GET /api/data/lineage/{target_id}` traverses either a revision or artifact.
- Existing `GET /api/data/revisions/{revision_id}/lineage` now includes the
  same complete graph while keeping its compatibility fields.

The Data Catalog shows the latest conclusion, sources, raw payload count,
revision and artifact counts, graph depth, transformation chain and requested
input fields.

## Verification

```bash
uv run python scripts/verify_complete_data_lineage.py
uv run pytest -q tests/test_unified_market_data_platform.py tests/test_api.py tests/test_static_ui.py
```

The verifier builds three immutable price observations, derives a three-period
moving average, derives a conclusion from the indicator and latest close, then
proves that the conclusion reaches all three raw payloads and the actual source.
It also checks API responses, immutability and the reject-incomplete policy.
