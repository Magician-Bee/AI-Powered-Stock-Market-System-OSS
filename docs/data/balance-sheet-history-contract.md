# Balance Sheet History Contract V1

`stock_ai.balance_sheet_history.v1` stores official MOPS IFRS period-end balance
sheet values from 2013-Q1 onward.

Required research fields are cash and cash equivalents, total assets, total
liabilities, total equity, inventory and accounts receivable. Current assets
and current liabilities are retained when disclosed. Values use thousand TWD
and remain `null` when an issuer's industry statement does not disclose a
meaningful equivalent.

Each item preserves the official statement scope, raw field labels, MOPS POST
parameters, exact UTF-8 HTML, acquisition time, source URL and immutable
revision. The archive does not expose the original filing timestamp, so
`published_at` remains `null`; `available_at` is the actual acquisition time.

## Quarterly comparison

The API orders saved official period-end observations by fiscal quarter. For
each metric it returns the previous saved quarter, absolute change and
percentage change. Missing values and zero denominators produce `null`
comparisons instead of fabricated zeroes.

## API

- `GET /api/data/ui/v1/fundamentals/balance-sheet/history`
- `POST /api/data/ui/v1/fundamentals/balance-sheet/history/sync`

Compatibility routes remain available under
`/api/fundamentals/balance-sheet/history`.
