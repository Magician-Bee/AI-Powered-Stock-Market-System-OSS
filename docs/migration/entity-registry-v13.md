# Entity Registry Migration v13

## Scope

Migration 13 completes `DATA-002` on top of the v11 warehouse and v12
security-lifecycle ledger. It does not rewrite any `ENT-*` ID or delete an
identifier. It adds deterministic comparison keys, resolution metadata and an
auditable supersession ledger to `entity_identifiers`.

## Contract

Every identifier keeps both the exact source value and a normalized comparison
value:

| Field | Meaning |
| --- | --- |
| `source_id` | Source that published or used the identifier |
| `identifier_type` | Exchange code, display symbol, business number or another reviewed namespace |
| `identifier_value` | Exact source/display value |
| `normalized_value` | NFKC and namespace-specific comparison value |
| `entity_id` | Stable internal `ENT-*`; never a `.TW`/`.TWO` symbol |
| `valid_from` / `valid_to` | Point-in-time interval for code reuse and venue changes |
| `confidence` | Mapping confidence, not investment confidence |
| `is_primary` | Current preferred display identifier |
| `superseded_by_entity_id` / `superseded_at` | Non-destructive canonicalization of a legacy alias |

Existing rows are backfilled in place. New writes are validated by
`EntityIdentifierRecord`. An identifier interval already owned by another
entity is rejected instead of being silently reassigned.

Legacy v12 databases are reconciled on startup without deleting history:
misinterpreted warrant exercise dates are removed from identifier validity,
all-identical business-number placeholders such as `00000000` are retired, and
duplicate official ETF aliases are superseded. `entity_identity_merges` records
each safe physical-to-canonical merge with its reason and effective time.
When a later official refresh separates securities that v12 had collapsed
through such a placeholder, the old source aliases receive
`superseded_by_entity_id` and remain queryable before the refresh `as_of`; the
corrected aliases become current from the acquisition time. A composite legacy
entity is not falsely merged into any one of the separated companies.

## Resolution policy

`EntityRegistry` is the only supported identifier resolver:

- `GET /api/data/entity-registry` exposes coverage and quality.
- `GET /api/data/entity-registry/resolve?identifier=...` resolves codes,
  display symbols, business numbers and internal IDs.
- `as_of` restricts identifier validity for historical research.
- multiple source identifiers that point to one entity resolve normally;
- a reused historical code may select the sole current entity while returning
  `historical_identifier_reuse`;
- multiple current entities return `ambiguous` with every candidate. The
  Registry never guesses or silently overwrites one.

Official master ingestion, Phase 1 persistence and the research data gateway
all call this Registry rather than performing their own suffix-based lookup.

## Runtime and UI acceptance

The real-source verifier requires at least one canonical entity whose
identifiers originate from two official sources and resolves both source
aliases back to the same `ENT-*`. The Data Catalog shows identifier coverage,
cross-source counts and current ambiguity quality, and provides an interactive
identifier resolver.

## Rollback

The migration is additive. A previous binary ignores the new columns, but a
database that has reached schema 13 must not be presented as schema 12.
Identifier histories and `ENT-*` values remain unchanged.
