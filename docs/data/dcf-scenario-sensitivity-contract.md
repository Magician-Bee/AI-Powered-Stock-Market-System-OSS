# DCF Scenario and Sensitivity Contract V1

`stock_ai.dcf_valuation.v1` is a user-adjustable discounted cash-flow model
served through `GET /api/data/ui/v1/fundamentals/valuation/dcf`.

All scenario output repeats the complete input set: base revenue, gross margin,
free-cash-flow conversion of gross profit, revenue growth, discount rate,
terminal growth, net debt, shares outstanding and forecast years. Forecast
revenue, gross profit, FCF, discount factor and present value are visible for
every year, followed by terminal, enterprise, equity and per-share values.

The endpoint always returns pessimistic, neutral and optimistic scenarios plus
a range. It intentionally has no `target_price` field. Scenario adjustments are
visible rather than hidden, and the UI labels results as model output rather
than guidance, analyst consensus or advice.

Two 5×5 matrices cover:

- discount rate × revenue growth;
- gross margin × terminal growth.

Invalid assumptions fail closed, including non-positive revenue or shares and a
discount rate that does not exceed terminal growth.
