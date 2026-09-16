# Balance Sheet History Schema V35

Schema version 35 adds partial indexes for official balance-sheet observations
and incremental symbol-quarter checkpoints inside `fundamentals_quarterly`.

- `idx_financial_facts_balance_sheet_period`
- `idx_balance_sheet_history_checkpoints`

Balance-sheet observation keys use `balance-sheet:YYYY-Qn`, preventing
collisions with income-statement observations for the same issuer and quarter.
The migration is additive and does not rewrite existing financial revisions.
