# Trading Restriction Contract V1

`STOCK-006` makes Taiwan market restrictions an execution input instead of a
display-only label. Every record is an immutable revision with the exact TWSE,
MOPS, or TPEx URL, acquisition time, and preserved source row. Records that are
not officially verified remain queryable but never become execution gates.

Supported restriction types are:

- `attention_stock`: warning only; it does not imply that trading is prohibited.
- `disposition_stock`: the paper simulator requires an explicit limit order and
  exposes periodic-auction/full-cash terms when supplied by the source.
- `halt_trading`: blocks new orders and fills. Resting ROD orders remain open.
- `resume_trading`: a later official resume event ends the halt gate.
- `price_limit`: rejects limit/stop prices outside the official daily range.
  A market buy locked at limit-up or market sell locked at limit-down is also
  blocked unless the quote contains opposing-book liquidity evidence.

The same evaluator runs at preview, broker submission, resting-order tick, and
the final `PaperOMS` fill boundary. Direct strategy calls therefore cannot bypass
the restrictions enforced by the interactive paper-trading UI.

## API

- `POST /api/official/trading-restrictions/import`
- `GET /api/data/ui/v1/market/{symbol}/trading-restrictions`
- `GET /api/market/{symbol}/trading-restrictions` (compatibility)

The preview response includes `trading_restrictions`, with `allowed`, blockers,
warnings, active official records, daily price bounds, and the deterministic
simulation policy. No restriction or liquidity is inferred from a stock name or
from a missing source response.
