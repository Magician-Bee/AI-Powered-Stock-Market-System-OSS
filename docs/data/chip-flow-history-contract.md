# Institutional and Margin History Contract V1

The chip history endpoint preserves official daily observations and keeps
foreign investors, investment trusts and dealers separate. It exposes each
series, cumulative net flow and the latest consecutive buy/sell streak.

Margin and short balances remain separate from institutional flow. Every
observation includes balance changes, published limits, utilization, the
short-to-margin ratio and offsetting volume. Non-trading or failed dates are
never inserted as zero.

The UI refresh can backfill recent TWSE T86 calendar dates; successful trading
dates are persisted through the unified warehouse. Margin history uses the
stored official MI_MARGN observations accumulated by normal refreshes.
