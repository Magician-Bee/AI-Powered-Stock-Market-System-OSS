# Trading Anomaly Event Contract V1

`stock_ai.trading_anomaly.v1` turns unusual daily trading observations into
source-attributed events. It is a deterministic screening contract, not a
prediction of manipulation, news impact, or future returns.

## Inputs

Detection reads only official TWSE/TPEx unadjusted daily OHLCV history. A scan
does not substitute Yahoo fallback data, generate missing candles, or ask an
AI model to infer an event. Each saved event contains the exact daily source
record, source IDs, metrics, detector version, rule thresholds and a stable
fingerprint.

## Rules

The v1 detector uses up to 20 preceding sessions and requires at least five
valid prior observations:

- `volume_spike`: volume is at least 2.0 times the prior-window median.
- `gap`: absolute open-to-previous-close change is at least 3%.
- `rapid_move`: absolute close-to-previous-close change is at least 5%.
- `price_volume_divergence`: absolute price change is at least 2%, absolute
  one-session volume change is at least 30%, and their directions are opposite.

These thresholds are returned in every collection and scan response. More than
one event type may be saved for the same trading date.

## Event and tracking lifecycle

`trading_anomaly_events` is immutable and deduplicated by its canonical
fingerprint. Repeated scans append `trading_anomaly_observations`, so the
projection reports first/last observation time and count without rewriting the
event. User actions append `open`, `acknowledged`, `resolved`, or `reopened`
records to `trading_anomaly_tracking_actions`; the API projects the latest
status while preserving every prior action.

## API and UI

- `GET /api/data/ui/v1/market/{symbol}/anomalies`
- `POST /api/data/ui/v1/market/{symbol}/anomalies/scan`
- `POST /api/data/ui/v1/market/{symbol}/anomalies/{event_id}/tracking`

Compatibility aliases remain under `/api/market/*`. The stock page can scan
the latest six-month range, inspect metrics and provenance, acknowledge an
event, resolve it, or reopen it.
