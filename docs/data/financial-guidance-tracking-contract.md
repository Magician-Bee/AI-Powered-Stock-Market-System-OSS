# Financial Guidance Tracking Contract V1

FIN-009 uses `stock_ai.financial_guidance_tracking.v1` to keep a forecast or
management statement separate from the later actual result.

`GET /api/data/ui/v1/fundamentals/guidance?symbol=2412.TW` reads TWSE OpenAPI
`t187ap15_L`, preserving its forecast range, covered period and
auditor-reviewed actual comprehensive income. The comparison reports whether
the actual was below, within or above the range, plus midpoint variance and
achievement percentage.

The normalized contract accepts `company_forecast`,
`investor_conference_guidance` and `management_outlook`, but requires an HTTPS
source. Only a sourced numeric range with a period-matched actual is
comparable. Qualitative outlook stays `not_comparable`; companies without a
voluntary quantified forecast return an explicit empty state and are never
filled with analyst estimates or AI predictions.
