# Taiwan secondary-market impact baseline

This document defines the versioned **research baseline** used by the local
event-driven replay.  It is deliberately not a broker execution calibration,
does not represent a customer account's realized fills, and must never be used
as live-order evidence.

The model quotes a simulated fill from point-in-time spread, realised
volatility, ADV participation and current-bar participation.  Its base,
volatility and square-root-impact coefficients are split by venue, product and
side in `config/taiwan_market_impact_rules.yaml`; every quote records the
selected effective-dated rule and this document's content hash.

Promoting any rule from `research_baseline` to `empirically_calibrated`
requires a separately retained calibration dataset, reproducible fitting
receipt, reviewer sign-off and a replacement artifact hash.  Until then,
execution evidence is rejected even when all input liquidity fields are
present.
