# Valuation Percentile Contract V1

`stock_ai.valuation_percentiles.v1` calculates the relative position of the
latest official exchange valuation over 1, 3, 5 and 10 years.

- One last available exchange observation is retained per calendar month.
- TWSE monthly stock-code history and TPEx official daily tables are supported.
- Raw exchange rows, source URLs, acquisition times and SHA-256 hashes are retained.
- PE, PB and dividend yield use the exchange's then-current MOPS basis, so the
  historical series does not retroactively apply today's financial statements.
- Percentiles use an empirical midrank, including explicit tie handling.
- Every window reports requested months, actual sample count and covered dates.
- Missing metrics are excluded from the denominator and are never treated as zero.
- `relative_low` means at or below the 25th percentile; `relative_high` means
  at or above the 75th percentile. These labels are context, not advice.

The official exchange history does not provide PS, EV/EBITDA or FCF Yield.
Those metrics stay explicitly unsupported until a separate point-in-time
statement reconstruction exists; current values are not copied into history.
