# Valuation Model and Evidence Contract V1

The model policy chooses applicable and excluded valuation methods from the
industry plus profitability, free cash flow, dividends, asset intensity and
growth characteristics. Financial firms, high-growth loss makers, asset-heavy
businesses and stable cash generators therefore do not share one formula.
Every inclusion and exclusion carries a visible reason.

Valuation evidence is stored in four non-interchangeable layers:

- `reported_fact`: sourced historical or audited observations;
- `company_guidance`: sourced issuer forward-looking statements;
- `analyst_estimate`: sourced named-provider forecasts;
- `model_assumption`: user or scenario inputs.

Facts, guidance and estimates require both source and as-of date. The service
never merges these layers into a single unlabeled number.
