# Peer Comparison Contract V1

`stock_ai.peer_comparison.v1` compares a target company with explicitly
selected peers at:

```text
GET /api/data/ui/v1/fundamentals/valuation/peers
```

- The official unified security master resolves every symbol and industry.
- A peer must be an active ordinary stock with the exact same official
  industry code as the target. Rejected symbols and reasons stay visible.
- The service never silently chooses the comparison set. It shows deterministic
  same-industry candidates, but only user-selected symbols enter the table.
- PE, PB and dividend yield use each exchange's disclosed daily valuation and
  retain its valuation date and source URL.
- Gross, operating and net margin, ROE and debt ratio use the same requested
  official financial-statement period. The UI action backfills that one period
  from MOPS when it is not already stored.
- Missing inputs remain `null`; they are not converted to zero.
- Cross-company medians and target percentiles are descriptive. Higher or lower
  is not labeled universally better and no recommendation is generated.
