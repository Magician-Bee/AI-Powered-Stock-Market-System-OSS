# Neutral Analysis Runtime

## Runtime invariants

The application starts with no selected stock and no implicit market Universe. A
symbol-requiring API either receives an explicit symbol, returns an empty state,
or rejects the request with `422`. It never substitutes a popular stock.

Every market scope is represented by `UniverseRequest` and resolved to an
immutable `UniverseSnapshot`. The snapshot records source, creation time,
filters, symbols, and count. Ranking sources that do not have an attributed
provider fail explicitly.

## Independent result layers

`DecisionEnvelope` keeps these payloads independent:

1. observation;
2. deterministic rule analysis;
3. model analysis;
4. Host risk evaluation;
5. execution status.

`AnalysisProvenance` records the origin, Provider, model ID, model call ID,
Universe source, considered symbols, data sources, readiness, and fallback
state. `ModelInvocationReceipt` records every successful or failed model turn.
A MODEL or HYBRID label is valid only when the associated receipt succeeded.

An uncalibrated strategy output is `rule_score` on `[-1, 1]`; it is not a
probability. The central risk policy uses `min_rule_score_threshold`. Fixed
target and stop multipliers use method
`fixed_strategy_reference_multiplier.v1` and are displayed as rule reference
ranges.

## Provider protocol

Provider capability profiles select `universal_v1` by default. Providers that
pass advanced capability checks may use `advanced_v1`. The universal
normalizer accepts plain JSON, fenced JSON, surrounding prose, reasoning tags,
conservative trailing-comma repair, and object or JSON-string tool arguments.
It preserves raw output and reports protocol errors separately from semantic
claim errors.

Validation is split into protocol, execution, evidence, and semantic layers.
Only the Host controls permissions, approval, sandbox, paper-execution gates,
receipts, rollback, and the prohibition on live trading. Intent hypotheses,
context relevance, and task decomposition remain auditable model/Host inputs
rather than a hidden single-label decision.

## Frontend truth states

The home radar uses `/api/agents/market-radar/runs` and the selected Provider.
It renders explicit states for NO MODEL, NO UNIVERSE, MODEL, and MODEL ERROR.
Model failure never becomes a rule result labeled as AI. The legacy
`/api/codex/market-radar` endpoint is developer diagnostics only.
