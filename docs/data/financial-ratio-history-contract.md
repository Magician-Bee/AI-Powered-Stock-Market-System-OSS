# Financial Ratio History Contract V1

`stock_ai.financial_ratio_history.v1` derives six comparable ratios from saved
official MOPS statements:

- gross margin = gross profit / revenue;
- operating margin = operating income / revenue;
- net margin = after-tax net income / revenue;
- ROE = annualized cumulative net income / average total equity;
- ROA = annualized cumulative net income / average total assets;
- debt ratio = total liabilities / total assets.

Q1-Q3 cumulative profit is annualized by `4 / quarter`; Q4 annual profit uses a
factor of one. ROE and ROA use the consecutive prior-quarter balance when
available. If it is unavailable, the ending balance is used and explicitly
labeled `ending_balance_only`.

Each item contains the exact income-statement, current balance-sheet and prior
balance-sheet inputs, official source URLs, statement scopes, formulas,
annualization factor and denominator basis. Only matching fiscal periods are
joined. Missing or zero inputs produce `null` rather than fabricated ratios.

## API

- `GET /api/data/ui/v1/fundamentals/ratios/history`
