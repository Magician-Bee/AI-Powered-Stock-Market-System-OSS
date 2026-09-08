# Schema v29: Official Trading Restrictions

Schema v29 adds `trading_restriction_revisions`.

Each logical restriction has one or more append-only revisions. Unique
`(restriction_id, revision)` and `(restriction_id, payload_hash)` constraints
make correction history explicit and imports idempotent. SQLite update/delete
triggers prevent source history from being rewritten.

Indexes support current-symbol and restriction-type lookups by effective time.
Existing databases migrate additively from schema v28; paper orders, fills,
positions, cash, corporate actions, and user data are not rewritten.
