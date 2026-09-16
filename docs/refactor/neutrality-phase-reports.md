# Neutrality Refactor Phase Reports

Baseline: `c8fb00a58edf2a44059c2b8130aeafd0ca513d5b`

## Phase 0 completion report

### 1. Summary

Generated all eight required inventories under `docs/audits/neutrality`.

### 2. Files

| File | Change |
|---|---|
| `scripts/audit_neutrality.py` | Repeatable Production audit |
| `docs/audits/neutrality/*` | Stock, Universe, AI label, formula, model call, fallback, context, and route inventories |

### 3–6. Bias, contracts, runtime, compatibility

Production stock constants are classified as aliases/examples or defects.
Every retained formula receives a stable Method ID. This phase is read-only
except for audit artifacts and has no API/DB/UI compatibility impact.

### 7. Tests

`python scripts/audit_neutrality.py` and `python scripts/check_neutrality_ci.py`.

### 8–10. Remaining, risk, commit

No Phase 0 inventory is missing. Broad exception matches still require semantic
review; silent `except: pass` is CI-blocked. The original implementation was
committed as `ef02f2f`; inventories are regenerated against the current release
before every neutrality verification.

## Phase 1 completion report

### 1–5. Summary and runtime

Removed default watchlists, batch symbols, service fallback symbols, empty
search recommendations, fixed screeners, report/news fallback lists, and UI
initial symbols. Added `SymbolContext`, `UniverseRequest`,
`UniverseSnapshot`, and the formal resolver used by both Screener and Agent
Market Radar. Before, empty input selected a stock; after, empty input remains
empty or raises `MissingSymbolError`.

### 6. Compatibility

Symbol-required APIs return `422`; daily reports and default watchlists return
empty states. Existing explicit-symbol calls remain valid.

### 7–10. Tests and status

Covered empty/new-install, watchlist, portfolio, workflow, explicit, TWSE,
TPEx, sector, screening query, official top-volume, invalid-symbol, and limit
cases in `tests/test_neutrality_contracts.py`. Top volume is ranked from
attributed TWSE/TPEx official quote rows; top market cap still fails explicitly
until an attributed capitalization provider exists. Original implementation
commit: `ef02f2f`.

## Phase 2 completion report

### 1–6. Summary

Added `DecisionEnvelope`, `AnalysisProvenance`, `ConfidenceValue`, and
`ModelInvocationReceipt`. Rule, model, risk, and execution payloads no longer
overwrite one another. Model errors are explicit and never silently relabeled.
Agent Runtime, native Codex diagnostics/control, market radar, and the model
trading workspace now return a real invocation receipt and model provenance.

### 7–10. Tests and status

Decision separation, no-model, model-error, and receipt behavior are covered by
neutrality, Agent Runtime, and provider tests. Legacy compatibility projections
are still named explicitly as deterministic. Original implementation commit:
`ef02f2f`.

## Phase 3 completion report

### 1–6. Summary

Added durable market-radar create/read/stream routes using the selected driver.
The home no longer uses the Codex-only radar path. It requires a configured
model and a formally resolved non-empty Universe, and shows NO MODEL,
NO UNIVERSE, MODEL, or MODEL ERROR. The old route is diagnostics-only.

### 7–10. Tests and status

API route, static UI, selected-driver, and no-fallback contracts are covered in
the full suite. Original implementation commit: `ef02f2f`; the follow-up strict
Market Radar verification landed in `3344077`.

## Phase 4 completion report

### 1–6. Summary

Host safety, approval, receipt, checkpoint, rollback, paper execution, and live
trading prohibition remain enforced. Provider `actions` are compiled into Host
plan nodes so models need not manage a full PlanGraph. Custom tool names can
produce valid execution evidence without belonging to a Host cognitive prefix
taxonomy.

### 7–10. Tests and status

Approval resume, custom tools, durable plans, and execution boundaries are
covered by Agent Runtime tests. Original implementation commit: `ef02f2f`.

## Phase 5 completion report

### 1–6. Summary

Added multi-intent hypotheses, negative constraints, symbol provenance, and the
task-aware Context Broker. Passive UI selection stays candidate-only unless the
prompt contains a referential phrase. `/api/query` now passes through the same
router component as Agent Runtime.

### 7–10. Tests and status

Project/API numeric IDs, negative stock constraints, general/project context
isolation, market account exposure, and passive UI selection are covered.
Original implementation commit: `ef02f2f`.

## Phase 6 completion report

### 1–6. Summary

Added Provider capability profiles, Universal/Advanced protocol selection, and
output normalization for OpenAI-compatible and external providers. Raw output
is preserved on protocol failure.

### 7–10. Tests and status

Pure/fenced/surrounded/reasoning-tag/trailing-comma JSON, object/string tool
arguments, capability protocol selection, OpenAI-compatible Mock, Ollama Qwen,
Ollama Gemma, Codex, no-schema fallback, and External Agent/Hermes Mock paths
are covered. Streaming, vision, and parallel calls remain capability-gated
rather than assumed for weak providers. Original implementation commit:
`ef02f2f`.

## Phase 7 completion report

### 1–6. Summary

Separated Protocol, Execution, Evidence, and Semantic validators with distinct
error codes and reports. Protocol formatting errors cannot be mislabeled as
answer correctness errors.

### 7–10. Tests and status

All four taxonomies and representative failures are covered in
`tests/test_provider_protocol_layers.py`. Protocol/Execution reports participate
in every Host tool-result decision; Evidence/Semantic reports participate in
completion validation. Original implementation commit: `ef02f2f`.

## Phase 8 completion report

### 1–6. Summary

Strategy output uses uncalibrated `rule_score`; risk policy uses
`min_rule_score_threshold`; fixed price multipliers are rule reference methods.
Data-not-ready results clear direction, confidence, target, stop, and sizing.
RiskEngine alone owns position sizing.

### 7–10. Tests and status

Strategy, risk, data readiness, integration audit, and rule-score API behavior
are covered by the full suite. No calibrated probability is claimed. Original
implementation commit: `ef02f2f`.

## Phase 9 completion report

### 1–6. Summary

Frontend labels distinguish model, rule, model error, no model, no Universe,
data blocking, and risk blocking. Deterministic trading cards display a rule
score, not an AI confidence percentage.

### 7–10. Tests and status

Static HTML/JS contract tests and JavaScript syntax validation cover the
truthful labels and current route. Original implementation commit: `ef02f2f`.
The current regression hardening additionally removes the stale “更新 AI 建議”
label from the deterministic J workspace and renders explicit symbol-less empty
states in I/J/K instead of indefinite loading.

## Phase 10 completion report

### 1–6. Summary

Added neutrality, Universe, provider protocol/E2E, four-layer validation,
receipt, migration, screener provenance, and UI regression tests. Core CI now
rejects fixed Production stock defaults, silent broad-exception swallowing,
unclassified stock constants, and prohibited false-AI labels.

### 7–10. Tests and status

The original canonical result was `423 passed, 3 skipped, 8 xfailed, 1 xpassed`;
Python compile, changed-JavaScript syntax, neutrality CI, audit regeneration,
and `git diff --check` also passed. Original implementation commit: `ef02f2f`.

## Phase 11 completion report

### 1–6. Summary

Added additive SQLite migration v10, configuration compatibility parsing,
runtime architecture documentation, and this phase report. Existing ledgers and
explicit-symbol API calls remain compatible.

### 7–10. Tests and status

Migration tests verify all new tables and `user_version=10`; docs describe the
actual Runtime boundaries. Original implementation commit: `ef02f2f`.

## Current regression verification

- Verification date: `2026-07-30` (Asia/Taipei)
- Verification base: `94e8cb2d65c9f5d8f8de04717847d77c513cee27`
- Phase 0 inventories regenerated from the current Production tree.
- Neutrality CI rejects fixed stock defaults, silent broad-exception swallowing,
  and deterministic UI copy such as `更新 AI 建議` / `Refresh AI Advice`.
- Browser acceptance covers the home `NO MODEL` / `NO UNIVERSE` gates and the
  I/J/K symbol-less empty states; J is labelled `非模型規則彙整` and its action
  is `更新規則參考`.
- Full local suite: `676 passed, 3 skipped, 8 xfailed, 1 xpassed`.
- The exact macOS `.command` launcher started the modified worktree on an
  isolated loopback port before Browser acceptance.
