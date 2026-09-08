# Basic Valuation Contract V1

`stock_ai.basic_valuation.v1` produces PE, PB, PS, dividend yield,
EV/EBITDA and FCF Yield from one explicit valuation snapshot.

- Price is the official unadjusted close on the exchange daily-valuation date.
- Market capitalization uses the immutable official issued-share revision.
- PE, PB and PS use the same market capitalization and same-period MOPS inputs.
- EV uses interest-bearing debt and cash; total liabilities are never a debt proxy.
- EBITDA is annualized operating income plus annualized depreciation and amortization.
- FCF is operating cash flow less absolute capital expenditure.
- Dividend yield preserves the exchange-reported daily value.
- Every formula, numerator, denominator, source URL and unavailable reason is returned.
- Missing inputs remain `null`; negative earnings do not produce a misleading PE.

The current endpoint is not a historical point-in-time replay. Historical valuation
percentiles and their PIT evidence are delivered separately by VAL-002.
