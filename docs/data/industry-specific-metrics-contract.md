# Industry-specific Metrics Contract V1

FIN-008 prevents the fundamentals center from applying one company template to
industries with materially different economics. The read contract is
`stock_ai.industry_metrics.v1`, exposed at:

```text
GET /api/data/ui/v1/fundamentals/industry-metrics
GET /api/fundamentals/industry-metrics
```

## Classification and recipes

The service first resolves the security master's industry (an explicit
`industry` query value is available for research and verification). It then
selects one of four separate recipes:

| Profile | Metrics |
| --- | --- |
| Financial | net interest income, credit provision, net profit, equity/assets, insurance liabilities/assets, EPS |
| Semiconductor | gross and operating margin, capex/revenue, inventory/assets, free-cash-flow margin |
| Shipping | operating and net margin, debt ratio, asset turnover, free cash flow/net profit |
| Construction | gross margin, inventory/assets, debt ratio, current ratio, operating-cash-flow margin |

Financial holding data comes from the dedicated TWSE OpenAPI income and balance
sheet datasets (`t187ap06_L_fh` and `t187ap07_L_fh`). It is intentionally not
sent through the general-company MOPS statement parser. The other three
profiles calculate their different recipes from the existing immutable MOPS
income, balance-sheet and cash-flow observations.

## Missing and unsupported data

Every metric declares its formula or official field, industry rationale,
availability and unavailable reason. A missing required official field stays
`null`; it never becomes zero. An unrecognized industry returns
`supported=false`, an empty metric list and the explicit policy
`No generic template is substituted.`

Ordinary statement reads preserve `knowledge_at` and `effective_at`. Financial
holding responses retain the exact TWSE dataset URLs and report their actual
ROC-year/quarter converted to the Gregorian fiscal period.

## Acceptance evidence

On 2026-07-30 the local Agent verified official 2025-Q4 statements for
`2330.TW`, `2603.TW` and `2542.TW`, plus the latest dedicated TWSE financial
holding rows for `2882.TW`. The in-app Browser opened the complete fundamentals
center and operated all four selections. Each rendered a different metric
list, with 5/5 metrics available for semiconductor, shipping and construction
and 6/6 for financial.
