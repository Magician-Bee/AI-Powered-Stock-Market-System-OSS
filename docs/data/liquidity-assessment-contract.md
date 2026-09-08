# Liquidity Assessment Contract V1

`stock_ai.liquidity_assessment.v1` evaluates whether a concrete Taiwan-equity
order is plausibly tradeable without presenting a model estimate as a fill
guarantee.

## Inputs and provenance

| Metric | Input | Required source |
| --- | --- | --- |
| Latest and average turnover | Unadjusted daily turnover | TWSE/TPEx official daily history |
| Average daily volume (ADV) | Unadjusted daily share volume | TWSE/TPEx official daily history |
| Turnover rate | Realtime cumulative volume when available, otherwise the latest official daily volume, divided by issued common shares | TWSE MIS/Fugle plus TWSE/TPEx company OpenAPI |
| Bid/ask spread | Current best ask less current best bid | TWSE MIS or licensed Fugle quote |
| Order participation | User-entered share quantity divided by ADV | Explicit user input plus official history |

The turnover-rate denominator is **issued common shares**, not an inferred
free float. Its effective date, source URL, preserved source row and immutable
revision ID are returned with the assessment.

## Slippage estimate

When both a valid live spread and an explicit order quantity exist, the local
research model reports:

```text
estimated bps = half live spread bps
              + 10 bps × sqrt(order quantity / ADV / 1%)
```

This is a deterministic capacity-screening assumption, not an exchange fill
prediction or promise. The API always returns
`estimate_is_not_a_fill_guarantee=true`.

## Tradability policy

- `highly_tradeable`: spread at most 10 bps, order at most 1% ADV and average
  daily turnover at least TWD 50 million.
- `tradeable`: spread at most 30 bps, order at most 5% ADV and average daily
  turnover at least TWD 10 million.
- `constrained`: all inputs exist but at least one tradeable threshold fails.
- `insufficient_data`: official history, current bid/ask, turnover or explicit
  order quantity is missing.

No historical close is substituted for a current spread. No default order
quantity or invented share count is used. Missing inputs remain JSON `null`
and appear in `tradability.blockers`.

## API and UI

- `GET /api/data/ui/v1/market/{symbol}/liquidity`
- Compatibility: `GET /api/market/{symbol}/liquidity`
- Query: `window_sessions=5..120`, optional positive
  `order_quantity_shares`, and `refresh=true|false`.

The stock UI loads the baseline assessment when a symbol is opened. A user can
enter a share quantity and press **評估滑價** to recompute participation,
estimated slippage and tradability with the same source-attributed contract.
