# Corporate Actions and Entitlements — Schema V28

Schema v28 is additive and creates:

- `corporate_action_revisions`, the immutable canonical event and provenance
  history;
- `paper_corporate_action_applications`, the exactly-once before/after account
  journal;
- `paper_corporate_action_entitlements`, manual rights and other holder
  entitlements.

All three tables reject update and delete operations through SQLite triggers.
The migration adds symbol/effective-date, event-type/effective-date,
account/application-time and account/entitlement-status indexes. Existing
orders, fills, positions and cash rows are not rewritten.

Cash corporate-action effects append an ordinary `cash_ledger` row with the
action and revision IDs. Position changes use the existing
`paper_positions` source of truth in the same transaction, so account cash,
holdings and the application journal cannot partially diverge.
