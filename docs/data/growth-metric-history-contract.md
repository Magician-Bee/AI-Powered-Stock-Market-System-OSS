# Growth Metric History Contract V1

`stock_ai.growth_metric_history.v1` separates growth by observation frequency.

- Monthly MoM, YoY and cumulative YoY are the official MOPS-disclosed fields.
- Quarterly QoQ uses only consecutive official standalone-quarter revenue.
  Annual Q4 summaries do not disclose standalone Q4, so those comparisons
  remain `null`.
- Annual YoY compares official Q4 annual revenue with the prior Q4.
- Three-, five- and ten-year CAGR require matching positive official annual
  endpoints. Available-range CAGR exposes its exact start/end years and source
  URLs.

Missing endpoints, zero bases, non-positive CAGR endpoints and non-consecutive
quarters remain `null`. Each observation retains its official source URL and
comparison status, preventing a single-period change from being presented as
a long-term trend.

## API

- `GET /api/data/ui/v1/fundamentals/growth/history`
