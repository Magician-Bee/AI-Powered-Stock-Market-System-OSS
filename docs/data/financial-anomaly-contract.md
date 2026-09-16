# Financial Anomaly Flags Contract V1

FIN-010 compares two consecutive, period-matched official income statements,
balance sheets and cash-flow statements. `stock_ai.financial_anomaly_flags.v1`
publishes observed inputs and thresholds for receivables, inventory, cash
conversion, gross-margin movement and one-time gains/losses.

Missing fields are `unavailable`, never zero and never an inferred anomaly.
The UI exposes every threshold and the two compared fiscal periods.
