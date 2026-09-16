# Schema v30 — Liquidity assessments

Schema v30 adds two immutable ledgers.

## `official_share_revisions`

Each TWSE or TPEx company OpenAPI row is preserved with:

- normalized display symbol;
- effective date and issued common shares;
- source ID, exact URL and acquisition time;
- canonical source-payload hash and original JSON;
- monotonic revision and superseded revision ID.

Identical source rows are idempotent. Corrections append a new revision.
Database triggers reject updates and deletes.

## `liquidity_assessments`

Every assessment stores its symbol, time window, optional order quantity,
tradability status, optional estimated slippage, source IDs, share revision,
canonical input hash and complete result JSON. It is an audit record and is
also protected from update and delete.

Upgrade is automatic through `apply_migrations()`. `pragma user_version` and
`schema_migrations` both advance to `30`.
