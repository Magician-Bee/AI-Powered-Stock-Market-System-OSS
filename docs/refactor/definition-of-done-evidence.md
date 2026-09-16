# Neutral Runtime Definition of Done Evidence

Original implementation: `ef02f2f1daf3976d8f3d076e67e02585e7497afe`

Current regression-verification base:
`94e8cb2d65c9f5d8f8de04717847d77c513cee27`

The detailed Phase 0–11 reports are in
`docs/refactor/neutrality-phase-reports.md`; repeatable inventories are under
`docs/audits/neutrality/`.

## Quant / sizing / OMS correctness evidence (2026-08-13)

The machine-readable ledger is `config/production_requirement_status.yaml`.
It contains every Master Plan P0 requirement (46 detailed records) plus the
full P1/P2 release checklist (78 explicit records), not only the first Quant
batch. A `complete` record must have direct existing evidence and no
blockers; `partial` requires direct evidence plus explicit blockers; an
`unverified` record deliberately has no implementation evidence and cannot be
promoted because a name or an un-audited code path is not proof. The runtime
derives the P0 gates from this ledger rather than a manually set boolean.

| Definition of Done | Direct evidence |
|---|---|
| Accounting identity | Hand-calculated gap ledger plus generated price-path invariant tests |
| Event lifecycle | Shared decision/order/receipt/fill IDs; cross-linked fills fail validation even with ordered timestamps |
| Cost and impact | Separate commission/exchange/tax/spread/volatility/participation receipts; a versioned official Taiwan tax snapshot selects by local date/product/side/lot. Account cost schedules now require an immutable SQLite receipt bound to broker/account/scope/effective dates/source hash before they can certify a quote. The OHLCV-only baseline no longer applies a fixed 20 bps fiction and explicitly reports missing cost context; reviewed real broker/exchange documents and empirical impact calibration still block Q-003. |
| Walk-forward safety | Per-fold deep-copied strategy state, train-only selection, frozen test instance, full information-interval purge and embargo |
| PIT features | Five-time contract, immutable contract hashes, verified-known-at replay timestamps, exact dataset-level active-source contracts and fail-closed as-of tests; Q-008 availability acceptance is complete, while provider-wide historical coverage remains tracked separately |
| Position sizing | Missing, zero, negative, NaN and infinities reject without falling back to the policy maximum |
| Execution truth | Intent remains `executed=false`; only an OMS fill receipt can make it true, while reject/missing receipts stay false |

| Definition of Done | Runtime evidence | Test / guard evidence |
|---|---|---|
| Production has no default stock | Empty watchlist/config, required `StockRequest.symbol`, nullable `SymbolContext` | `test_new_install_has_no_default_stock_or_batch_universe`; neutrality CI |
| Production has no fixed market Universe | `UniverseRequest` + `resolve_universe`; services receive resolved symbols | empty, explicit, market, sector, screening, ranking tests |
| New install displays no stock | UI symbol state and API defaults are empty | static UI and default-watchlist tests |
| No model means no AI decision | Home emits `NO MODEL`; model-backed arrays stay empty | static UI and market-radar neutrality tests |
| Deterministic controls do not claim AI | J is labelled `非模型規則彙整`; its action says `更新規則參考` | neutrality CI and static UI tests |
| Symbol-less workspaces are truthful | I/J/K render `尚未指定股票` and do not call analysis APIs | static UI contract and Browser acceptance |
| Model failure never silently substitutes a rule | Rule/model sections are separate; failed primary `items=[]` | failure, `max_steps_reached`, receipt and formal-result tests |
| Home uses selected Provider | `/api/agents/market-radar/runs` resolves the formal Universe and selected driver; UI renders validated `MarketRadarResult` cards | Agent API, strict schema, static UI and Browser E2E |
| Rule and model results are separate | `DecisionEnvelope.rule_analysis` and `model_analysis` | decision-envelope tests |
| Host risk is not presented as model opinion | `risk_evaluation` has Host origin/method; UI labels rule/risk separately | risk and UI contract tests |
| Every confidence has a type | `ConfidenceValue`, typed signals/events/linkage/model decisions | strategy, Agent schema, source-policy, UI tests |
| Uncalibrated scores are not success probabilities | Rule score is decimal/scale-labelled; model score says self-report/uncalibrated | static UI and contract scans |
| Fixed multipliers are not AI target prices | `price_method_id=strategy.fixed_reference_multiplier.v1`; rule-reference UI | strategy and UI tests |
| Insufficient data clears direction/target/stop | Strategy returns `decision_status=insufficient_data` and null fields | data-readiness tests |
| Symbol provenance is complete | `SymbolContext.source`, evidence and confirmation | routing/provenance tests |
| Universe provenance is complete | `UniverseSnapshot.source`, filters, symbols and time | resolver and screener API tests |
| Model invocation has a receipt | Runtime, radar, native Codex and model-trading responses return `ModelInvocationReceipt` | Agent, radar, Codex and trading receipt tests |
| Provider has a Capability Profile | Profile is upgraded only by a real `ProviderConformanceSuite` probe | Codex/OpenAI-compatible/External Agent conformance E2E |
| Weak models use Universal Protocol | Normalizer maps `status/message/actions` and string/object arguments | Ollama and External Agent E2E tests |
| Strong models use Advanced Protocol | Codex profile + native structured tool loop | Codex Agent Runtime/integration tests |
| Four validators are separate | Protocol, Execution, Evidence and Semantic classes/reports | layered taxonomy and Host integration test |
| General/project context is isolated | `ContextBroker` exposes task-specific slices | context-isolation tests |
| Passive UI selection is not user intent | UI symbol is candidate-only until referential prompt | passive-selection tests |
| Query and Agent share runtime and routing | `/api/query` creates the same durable validated run and calls `UnifiedMultiIntentRouter` | query/project/numeric/negative-intent tests |
| Screener scans formal Universe | Screener accepts `UniverseRequest`; official top volume supported | screener provenance and Universe matrix tests |
| News is not a fixed-stock market proxy | News scope is resolved/empty, never a popular-stock fallback | neutrality/service tests |
| Daily report has no fixed list | Empty Universe produces an empty report | empty daily-report test |
| CI blocks fixed stocks and false AI | `scripts/check_neutrality_ci.py` runs in core safety workflow | guard self-test |
| Codex, Ollama and External Agent have E2E coverage | Native Codex, Qwen/Gemma fallback, OpenAI Mock and Hermes Mock | provider/integration suites |
| Documentation matches Runtime | architecture, migration, phase reports and generated audit maps | audit regeneration + review |

## Verification commands

```bash
python scripts/audit_neutrality.py
python scripts/check_neutrality_ci.py
python -m compileall -q src tests scripts
node --check <each changed JavaScript file>
pytest -q
git diff --check
```

Branch CI, Browser E2E, commit and GitHub `main` verification are required
before release. The original user-owned dirty files must remain excluded.

The 2026-07-30 regression verification completed the full local suite with
`676 passed, 3 skipped, 8 xfailed, 1 xpassed`, regenerated all eight Phase 0
inventories, passed the neutrality guard, and exercised the modified UI through
the macOS `.command` launcher.
