# Data Quality Service V2 / schema v20

Schema v20 completes `DATA-010` by turning the former on-demand summary into a
point-in-time daily quality service with issue-level evidence.

The report day uses `Asia/Taipei`. A run evaluates the latest revision known by
its cutoff for each dataset/entity/observation/source key, while duplicate
detection also inspects the visible revision history. Future report dates and
cutoffs beyond the selected market day are rejected.

Rules are versioned in `config/data_quality_rules.yaml`. The service detects:

| Category | Evidence |
| --- | --- |
| Missing | Required normalized field absent/empty, or an unavailable/invalid revision |
| Anomaly | Declared quality flags, nonnumeric/nonfinite/negative values, OHLC inconsistency and robust MAD outliers |
| Time misalignment | Publication/availability/acquisition order, missing temporal coordinates, payload/contract date mismatch and observation-key mismatch |
| Duplicate | A logical source record repeats an earlier payload after another revision |
| Source conflict | Open reconciliation differences, including both revision IDs and values |

`data_quality_reports` now records report date, knowledge cutoff, time
misalignment count, issue count, rule version and a state hash.
`data_quality_issues` stores every issue's category, severity, code, revision,
entity, observation, source, field, expected value and actual value.

Report identity is derived from the rule version, revision state, open
conflicts and detected issues. Repeating an unchanged daily state reuses the
same report instead of creating duplicate evidence.

The HTTP contract is:

```text
POST /api/data/quality/{dataset}
POST /api/data/quality/daily
GET  /api/data/quality/reports
GET  /api/data/quality/reports/{report_id}
```

The dataset endpoint creates one report. The daily endpoint accepts an optional
dataset list; otherwise it scans every dataset currently in the warehouse.
List and detail endpoints expose saved summaries and complete issue evidence.

## Verification

```bash
uv run pytest -q tests/test_unified_market_data_platform.py
uv run python scripts/verify_daily_data_quality.py
```

The verifier constructs deterministic missing, statistical-outlier,
time-misalignment, duplicate and cross-source-conflict cases, proves every
category is persisted, and confirms an unchanged repeat reuses the report ID.
