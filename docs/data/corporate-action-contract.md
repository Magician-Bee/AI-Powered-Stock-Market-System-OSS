# Corporate Action Ledger V1

`STOCK-005` records holder-affecting events as immutable, source-attributed
revisions. It never infers a dividend, split or merger from a price jump.

## Supported actions

| Action | Paper-position treatment |
| --- | --- |
| Cash dividend | Credit `quantity × cash_per_share`; apply the official reference-price ratio; leave share quantity and average cost unchanged |
| Stock dividend | Multiply quantity, rebase average cost and apply the official price ratio |
| Capital reduction | Reduce quantity, subtract returned capital from aggregate cost, rebase the remaining cost, apply the official price ratio and credit the official cash return |
| Capital increase | Apply the official ex-right price ratio and record a pending subscription entitlement; never subscribe or deduct cash automatically |
| Split / reverse split | Multiply or consolidate quantity, rebase average cost and apply the official price ratio |
| Merger | Transfer the old position to the explicitly named successor using the official exchange ratio and optional cash boot |
| Treasury stock | Record an explicit holder no-op; a later share cancellation must arrive as a separate reduction action |

Fractional paper shares are preserved. The ledger does not silently round them,
sell them or invent a cash-in-lieu price.

The current implementation evaluates the paper position present when sync is
requested; it does not reconstruct a historical record-date position. This
basis is written into every application note instead of being presented as a
broker-grade entitlement record.

## Provenance and revisions

`POST /api/official/corporate-actions/import` requires a symbol, effective date,
action type, source ID and source URL. A row marked `official_verified=true`
must point to an official TWSE, MOPS or TPEx host. The normalized terms and raw
source payload receive a SHA-256 hash. Reimporting the same payload is
idempotent; a corrected payload creates a new immutable revision linked through
`supersedes_revision_id`.

Incomplete actions remain visible but cannot change an account. Price-affecting
events require an explicit multiplier or official before/after reference
prices. Mergers require the successor and exchange ratio. Capital increases
require the subscription ratio and price.

## Exactly-once account synchronization

`POST /api/open-stock-ai/paper-account/corporate-actions/sync` applies only
officially verified, complete, confirmed/effective events whose effective date
is at or before `as_of`. The unique `(account_id, action_id)` application key
prevents duplicate dividends, splits and transfers.

Every attempt records:

- immutable before/after position snapshots;
- the exact action revision;
- cash delta and the corresponding `cash_ledger` entry;
- a pending entitlement when manual subscription is required;
- the outcome, official source and explicit no-auto-subscription boundary.

`GET /api/data/ui/v1/market/{symbol}/corporate-actions` and its compatibility
alias return the latest revision plus the active paper account's application
state. The stock drawer displays these records separately from news and exposes
the explicit paper-account sync button.
