# Financial Research Platform

Phase 2 financial work extends the same source registry, entity registry,
temporal contract, raw lake, immutable revision store and standardized
`financial_facts` warehouse established in Phase 1. Financial features may add
source-specific parsers and read projections, but must not create a parallel
truth store or let the browser contact a disclosure source directly.

## Contracts and responsibilities

| Layer | Responsibility |
| --- | --- |
| Source registry | Declares official endpoint, authorization, frequency, archive boundary, fields and retry policy |
| Connector/parser | Downloads exact upstream bytes and parses only disclosed values |
| Temporal normalization | Separates fiscal period, publication time, source availability, acquisition and effective time |
| Unified platform | Stores raw objects, hashes, immutable revisions, field provenance and incremental checkpoints |
| Financial projection | Reads the standard `financial_facts` domain under point-in-time filters |
| API | Validates ranges and returns coverage, missing periods, source IDs and truthful limitations |
| UI | Operates only the versioned unified API and renders nulls, units, coverage and source links |

## FIN-001 monthly revenue

FIN-001 begins the platform with the official MOPS monthly-revenue archive from
2010 onward. The observation key is the fiscal month rather than a fetch/report
date, so all periods coexist. MoM, YoY and cumulative YoY are the official
disclosed fields and are never replaced by Host-generated values.

Incremental synchronization checkpoints every symbol/month partition and keeps
the original archive page. Because the archive omits the original announcement
timestamp, the normalized row keeps `published_at=null` and records the actual
acquisition time as `available_at`. This preserves truthfulness even though it
means archive backfills cannot by themselves prove what was known on the
original historical announcement date.

The same pattern will be reused for later statement, ratio, revision and
industry-specific work: first preserve official source history and temporal
evidence, then derive metrics with an explicit transformation and lineage.

## FIN-002 income statements

FIN-002 uses the MOPS historical individual-company IFRS income-statement page
from 2013-Q1 onward. Each symbol/quarter is fetched with the official POST
parameters and stored in the existing `fundamentals_quarterly` financial
warehouse. Revenue, gross profit, operating income, after-tax net income and
basic EPS are normalized while the exact source labels remain attached to the
record. Industry-specific statements may legitimately omit gross profit or
operating income; those values remain null.

The primary fields are the official year-to-date cumulative or annual values.
Q2 and Q3 also carry the official single-quarter columns. Q1 is both the first
quarter and year-to-date. The Q4 summary exposes annual values but not a
standalone fourth quarter, so Q4 single-quarter fields remain null instead of
subtracting Q3 or treating cumulative EPS as additive.

Incremental synchronization checkpoints every symbol/quarter partition,
preserves the exact UTF-8 HTML and retains immutable revisions. The archive
page does not expose the original company filing timestamp, so
`published_at=null` and actual acquisition time is used as the conservative
`available_at`.

## FIN-003 balance sheets

FIN-003 uses the matching MOPS historical individual-company IFRS
balance-sheet page from 2013-Q1 onward. It preserves official period-end cash,
assets, liabilities, equity, inventory and accounts receivable, plus current
assets and current liabilities when disclosed. Each record keeps source field
labels and the official comparison-date headings.

Quarter comparison uses consecutive saved fiscal-quarter observations. It
returns the previous quarter, absolute change and change percentage for every
required field. Missing fields and zero denominators remain null. Observation
keys are prefixed with `balance-sheet:` so balance-sheet and income-statement
revisions for one issuer and quarter cannot collide.

## FIN-004 cash-flow statements

FIN-004 preserves official cumulative operating, investing and financing cash
flows plus cash paid for property, plant and equipment from the MOPS IFRS
archive. Free cash flow is derived only when both official inputs exist, using
`operating_cash_flow - abs(capital_expenditure)`.

Profit quality joins the official after-tax net income for the same issuer and
cumulative fiscal period. The cash-conversion ratio is classified as strong,
aligned, weak or negative; missing income and zero denominators remain
`insufficient_data`. This prevents cross-period comparisons and fabricated
zeroes.

## FIN-005 financial ratios

FIN-005 derives gross, operating and net margins from same-period cumulative
income statements. It annualizes Q1-Q3 cumulative net income by `4 / quarter`
for ROE and ROA, then uses average consecutive-quarter equity and assets when
available. Debt ratio uses same-period liabilities and assets.

Every result preserves exact formula inputs, annualization factor, denominator
basis and official URLs for the income statement, current balance sheet and
previous balance sheet. Missing values and zero denominators remain null, so a
partial industry statement cannot silently become a zero ratio.

## FIN-006 growth metrics

FIN-006 keeps frequency semantics separate. Monthly MoM, YoY and cumulative
YoY remain the official disclosed fields. Quarterly QoQ is calculated only
when both consecutive quarters carry official standalone-quarter revenue; Q4
annual summaries remain non-comparable. Annual growth compares Q4 annual
revenue, while 3/5/10-year and available-range CAGR require positive official
endpoints and expose their source URLs.

The statement downloader first requests the historical MOPS host. If that host
returns a transport failure such as its observed targetless HTTP 307, the same
official request is retried against the current MOPS host. The recorded source
URL remains the historical statement URL so provenance is not rewritten as a
different dataset.

## Verification boundary

`scripts/verify_monthly_revenue_history.py` downloads 13 real official periods
for `2330.TW` into an isolated database, asserts complete coverage and all four
required growth/revenue dimensions, audits raw/revision/checkpoint counts, and
proves a second sync performs no duplicate downloads. Unit/API/migration tests
and an actual browser interaction cover parsing, persistence, validation and
the rendered workflow.

`scripts/verify_income_statement_history.py` downloads 52 official quarters
for `2330.TW` from 2013-Q1 through 2025-Q4 into an isolated database. It
asserts 13 covered years, all five required metrics, official Q3 single-quarter
values, truthful Q4 nulls, raw/revision/checkpoint counts and a zero-download
repeat synchronization.

`scripts/verify_balance_sheet_history.py` downloads the same 52-quarter range,
asserts all six required period-end metrics, audits computed previous-quarter
comparisons and raw/revision/checkpoint persistence, and proves a repeat sync
downloads zero duplicate periods.

`scripts/verify_cash_flow_history.py` downloads 52 official cash-flow
statements, verifies the explicit free-cash-flow formula, joins the official
2025 annual net income for cash-conversion quality, audits persistence and
proves a repeat synchronization downloads zero periods.

`scripts/verify_financial_ratios.py` synchronizes 52 official income statements
and balance sheets, requires complete same-period coverage, recomputes all six
ratios from exposed inputs and confirms all three source URLs used by ROE/ROA.

`scripts/verify_growth_metrics.py` verifies 24 official monthly periods, 52
quarterly statements and 13 annual endpoints. It asserts official monthly
growth, consecutive Q2/Q3 QoQ, truthful Q4 nulls, 3/5/10-year CAGR and the
2013–2025 available-range CAGR.
