# Borrowed Short and Day-Trade History V1

`CHIP-003` and `CHIP-004` expose borrowed selling and day trading without
combining either measure with margin short selling or presenting a mechanical
ratio as an AI opinion.

## API

```text
GET /api/data/ui/v1/flow/chip/short-daytrade
GET /api/flow/chip/short-daytrade
```

Required query parameter:

- `symbol`: an explicitly supplied TWSE `.TW` or TPEx `.TWO` symbol.

Optional parameters:

- `days`: 1–60 calendar days to query; defaults to 14.
- `refresh`: when true, request missing observations from the official
  exchange endpoints before reading the local history.

The API never supplies a default symbol. Non-trading dates and missing source
records are omitted rather than filled with zero.

## Official inputs

| Exchange | Borrowed selling | Day trading | Total volume |
| --- | --- | --- | --- |
| TWSE | `TWT93U` | `TWTB4U` | `MI_INDEX` |
| TPEx | `margin/sbl` | `intraday/stat` | `afterTrading/tradingStock` |

Each stored observation contains the official rows used by the parser,
source URLs, acquisition time and a SHA-256 hash over the normalized record.
The hash is not described as a wire-payload hash.

## Derived fields

```text
daytrade_ratio_percent =
  official day-trade volume / official total traded volume × 100
```

The UI heat label is a disclosed display band:

- `normal`: ratio below 25%.
- `active`: ratio from 25% to below 40%.
- `elevated`: ratio at or above 40%.
- `unavailable`: the official total volume is unavailable.

These bands are not a recommendation, forecast, confidence score or AI
judgment. The latest two trading observations are marked provisional when
they are still recent because official day-trade statistics may be revised
through T+2.
