# Schema v31 — Trading anomaly events and tracking

Schema v31 adds three append-only tables for `STOCK-008`.

## Tables

- `trading_anomaly_events` stores the immutable event identity, symbol,
  trading date, category, direction, severity, detector version, exact source
  record, metrics, policy, source IDs and timestamps.
- `trading_anomaly_observations` records each scan that observed an existing
  event. A scan/event pair is unique.
- `trading_anomaly_tracking_actions` records detector-opened and user
  acknowledged, resolved or reopened lifecycle actions.

Indexes support symbol/date event reads and latest observation/action
projection. Update and delete triggers protect all three histories. The
migration is additive; `apply_migrations()` advances both
`schema_migrations` and `pragma user_version` to `31`.

See [Trading Anomaly Event Contract V1](../data/trading-anomaly-contract.md)
for detection policy and API behavior.
