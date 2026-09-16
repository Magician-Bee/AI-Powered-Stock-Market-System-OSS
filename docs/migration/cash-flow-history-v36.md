# Cash Flow Statement History Schema V36

Schema version 36 adds partial indexes for cash-flow observations and
incremental archive checkpoints inside `fundamentals_quarterly`.

- `idx_financial_facts_cash_flow_period`
- `idx_cash_flow_history_checkpoints`

Observation keys use `cash-flow:YYYY-Qn`, preventing collisions with income
statements and balance sheets for the same issuer and quarter.
