# Neutral Runtime Migration v10

## Configuration

New installations use an empty `watchlist` and `universe.default_source: none`.
The risk key is `min_rule_score_threshold`; the loader still reads the legacy
`min_confidence` YAML key during migration but never emits that terminology in
the current policy contract.

An existing personal watchlist may be imported into `user_watchlists`. It must
not become a system-wide default Universe.

## SQLite

Schema version 10 adds:

- `user_watchlists` and `user_watchlist_symbols`;
- `universe_snapshots`;
- `analysis_runs`;
- `model_invocations`;
- `analysis_provenance`;
- `rule_strategy_results`;
- `model_analysis_results`;
- `validation_reports`.

Migrations are additive and idempotent. Existing paper OMS, Agent Runtime, and
ledger tables are retained.

## API compatibility

- `/api/codex/market-radar` is diagnostics-only.
- `/api/agents/market-radar/runs` requires a non-empty explicit Universe.
- Symbol-requiring workspaces return `422` when the symbol is absent.
- Daily reports without a Universe return an empty report.
- Empty entity search and the legacy default-watchlist endpoint return empty
  collections.
- Quantitative trading cards expose `rule_score`, `rule_set_id`, and
  `origin=rule_strategy`, not a fabricated confidence percentage.
- `top_by_volume` resolves from attributed TWSE/TPEx official quote volume;
  unavailable ranking sources return an explicit `422` rather than a stock list.
- Model-backed trading and native Codex diagnostic responses include
  `model_invocation` and `provenance`; confidence is typed as an uncalibrated
  model self-report.
