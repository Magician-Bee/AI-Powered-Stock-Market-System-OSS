# Cash Flow Statement History Contract V1

`stock_ai.cash_flow_statement_history.v1` stores official MOPS IFRS
year-to-date cash-flow statements from 2013-Q1 onward.

Official fields are operating, investing and financing cash flow plus cash
paid to acquire property, plant and equipment. Free cash flow is a derived
field with the explicit formula:

```text
operating_cash_flow - abs(capital_expenditure)
```

If either official input is missing, free cash flow remains `null`.

## Profit quality

The query joins the saved official after-tax net income for the same issuer and
cumulative fiscal period. It reports operating-cash-flow-to-net-income and one
of `strong_cash_conversion`, `aligned`, `weak_cash_conversion`,
`negative_operating_cash_flow` or `insufficient_data`. It does not compare a
quarterly cash-flow amount with an annual profit or silently replace missing
income with zero.

Every period preserves the exact UTF-8 HTML, POST parameters, source labels,
field provenance, immutable revision and incremental checkpoint. The archive
does not expose original filing timestamps, so `published_at` remains `null`.

## API

- `GET /api/data/ui/v1/fundamentals/cash-flow/history`
- `POST /api/data/ui/v1/fundamentals/cash-flow/history/sync`
