# Open Stock AI Integration

This project now includes an `open_stock_ai` integration layer under `src/open_stock_ai`.

The layer follows the requested architecture:

- `OpenStockAIEngine`
- `open_stock_ai.api.router`
- `SignalPipeline`
- `MarketDataHub`
- `IntelligenceHub`
- `StrategyEngine`
- `ResearchEngine`
- `RiskEngine`
- durable SQLite-backed risk-control switches and realized-P&L loss-limit receipts
- `ExecutionEngine`
- `external_sources/*` adapters for TradingAgents, FinGPT, FinRobot, FinRL, Qlib, and AI-Trader

Quickstart: see `docs/integration/open_stock_ai_quickstart.md`.

Current integration status:

- Existing `stock_ai` market, news, margin, revenue, and history functions feed `MarketDataHub`.
- `MarketDataHub` now normalizes local source slices into
  `open_stock_ai.data_source_envelope.v1` records for TWSE, TPEx, Yahoo, MOPS,
  and News. Each envelope includes `open_stock_ai.data_quality.v1` so downstream
  adapters consume a consistent data-source shape.
- The top-level API payload now carries `open_stock_ai.stock_decision.v1`.
  Its core components also expose stable schema versions:
  `stock_request.v1`, `market_snapshot.v1`, `intelligence_result.v1`,
  `trading_signal.v1`, `research_result.v1`, `risk_decision.v1`, and
  `execution_result.v1`.
- External open-source projects are cloned under `external/` and verified in `docs/integration/external_sources_manifest.md`.
- `ExternalProjectRegistry` verifies each cloned repo origin and HEAD commit at runtime.
- The source provenance lock is exposed as `open_stock_ai.external_source_lock.v1`;
  it records each approved `git clone` command, expected HEAD, current local
  HEAD, and per-project lock status.
- Optional external source policy is exposed as
  `open_stock_ai.optional_external_source_registry.v1`. It records Freqtrade as
  an integration-brief reference only: not approved, not cloned, not imported,
  and not a source-lock member unless explicitly approved later.
- The external governance scan is exposed as
  `open_stock_ai.external_license_footprint.v1`; it records each external repo
  license-file classification, dependency manifest count, and read-only runtime
  footprint. AI-Trader currently has MIT metadata in the local clone but no
  top-level license text, so it is marked
  `license_metadata_detected_missing_license_text` and remains a manual review
  item.
- Runtime connector governance is exposed as
  `open_stock_ai.runtime_connector_governance.v1`. It evaluates every approved
  external project as a future runtime connector candidate, records license and
  runtime blockers, and proves that remote order submission remains disabled
  unless explicitly governed through Open Stock AI settings, the central
  `RiskEngine`, and `PaperExecutor`.
- Broker/account import governance is exposed as
  `open_stock_ai.broker_account_import_governance.v1`. It covers FinRL-Trading,
  FinRL, and AI-Trader paths that could import realized orders or mutate remote
  broker/account state. By default it blocks those paths, disables external
  credentials, and only permits paper ledger replay.
- Per-project contribution tracing is exposed as
  `open_stock_ai.external_project_contribution_matrix.v1`. It maps all seven
  approved external repos to their adapter contract source key, emitted
  projection schemas, Open Stock AI pipeline stages, consuming engine, central
  risk boundary, and paper-only execution boundary.
- `build_engine()` now loads `config/open_stock_ai.yaml` through
  `OpenStockAISettings`; YAML controls paper mode, live-trading disabled state,
  risk thresholds, SQLite path, Open Stock AI watchlist, and external project
  paths.
- S-011/S-012 risk control is persisted in the configured SQLite ledger. The
  order boundary checks global, account, broker, strategy, and symbol scopes;
  realized-P&L events are idempotent and evaluate intraday, daily, weekly,
  monthly, and consecutive-loss limits. `TradeStore.save_paper_order()` now
  sends each durable PaperOMS realized delta through
  `SettlementPnLFeed`, which verifies a content-addressed source receipt and
  skips unsettled T+2 rows. Production broker settlement/account activation
  evidence is still required, so the release requirement remains `partial`.
- Environment variables still override paper mode, live-trading flag,
  human-confirm flag, external runtime connector enablement, and local LLM
  connection fields.
- Adapter payloads include external project metadata, discovered capability
  files, commit IDs, and a normalized `adapter_result` envelope.
- `adapter_result.schema_version` is currently
  `open_stock_ai.adapter_result.v1`. The envelope standardizes source key,
  source name, role (`intelligence`, `research`, or `contract`), method,
  status, loaded/origin/source-lock verification state, summary, risks,
  evidence, metrics, external project profiles, capability contract, and
  execution boundary.
- Adapter and projection payloads also include
  `open_stock_ai.external_evidence_lineage.v1`, which records origin, branch,
  expected HEAD, local HEAD, lock verification, license evidence, and execution
  boundary for each external source used by a decision.
- `IntelligenceResult.adapter_results` and `ResearchResult.adapter_results`
  expose normalized adapter envelopes as top-level arrays, so API/UI consumers
  do not need source-specific `raw.<source>.adapter_result` paths for routine
  integration checks. The source-specific raw payloads remain available for
  detailed evidence.
- Adapter payloads now include lightweight local methods inspired by the external projects:
  - FinGPT: keyword/news sentiment aggregation plus local Benchmark,
    Forecaster, RAG, and trading contract parsing. The Forecaster prompt
    contract is projected into
    `open_stock_ai.fingpt_forecast_projection.v1` and contributes bounded
    forecast evidence to the unified strategy score.
  - FinRobot: revenue snapshot fundamental view plus local report-agent
    contract parsing from `finrobot/agents`, `finrobot/functional`, and
    `finrobot_equity`. Its equity-report structure, Expert_Investor role, and
    report analysis tools are projected into
    `open_stock_ai.finrobot_report_projection.v1` as advisory evidence.
  - TradingAgents: structured analyst-style technical/risk views plus local
    typed schema, graph setup, rating parser, signal processing, risk debator,
    memory, and reflection contract parsing.
    The risk debator/memory contract is projected into
    `open_stock_ai.tradingagents_risk_debate.v1` and passed to the central
    `RiskEngine` as advisory evidence.
  - FinRL / FinRL-Trading: deterministic OHLCV backtest metrics plus local
    paper-trading, environment, backtest engine, and adaptive-rotation contract
    parsing. The backtest output and parsed FinRL-Trading backtest engine
    contract are projected into `open_stock_ai.finrl_backtest_projection.v1`
    as advisory risk evidence.
  - qlib: deterministic factor score validation plus local workflow YAML,
    benchmark family, alpha, and workflow documentation contract parsing.
    The research adapter now projects the parsed workflow contract into
    `open_stock_ai.qlib_workflow_summary.v1` so decisions carry the selected
    local workflow family/model/dataset/strategy evidence. The same local
    factor result is projected into `open_stock_ai.qlib_factor_projection.v1`
    with model score, rank-IC proxy, selected workflow metadata, and research
    report artifact linkage. It is passed to `RiskEngine` as advisory evidence
    without importing qlib runtime.
  - AI-Trader: local agent schema and skill contract parsing from
    `research/schemas/*.schema.json` and `skills/*/SKILL.md`. Open Stock AI
    signal and paper-order rows are projected into
    `open_stock_ai.ai_trader_interop_projection.v1` after local schema
    validation. The same local skill frontmatter is now projected into
    `open_stock_ai.ai_trader_skill_route.v1`, selecting the relevant AI-Trader
    bootstrap/context/publish-disabled skills for each Open Stock AI signal
    without calling AI-Trader APIs.
- Their heavy runtimes are not imported into `src/`; adapters still control the integration boundary.
- `StrategyEngine` now emits an internal structured decision schema inspired by
  TradingAgents' typed decision agents:
  - 5-tier `PortfolioRating`: Buy / Overweight / Hold / Underweight / Sell.
  - 3-tier `TraderAction`: Buy / Hold / Sell.
  - Entry, target, stop-loss, position sizing, source ratings, and rationale.
- `TradingSignal` carries the structured decision in `decision_schema`.
  Deterministic output exposes `rule_score`, `confidence_type=rule_score`,
  `confidence_calibrated=false`, and a stable `rule_set_id`; the legacy
  top-level confidence field is a typed compatibility projection only.
- Local strategy modules under `src/open_stock_ai/strategy` are executable
  contracts: moving-average cross, breakout range, RSI/MACD, and an LLM prompt
  boundary that cannot bypass `RiskEngine`.
- Local intelligence modules under `src/open_stock_ai/intelligence` include
  executable news, technical, financial-report, fundamental, sentiment, and
  reflection summaries. They remain local Open Stock AI logic rather than
  direct runtime imports from external projects.
- `RiskEngine` remains the final gate. Buy/Add signals require a stop loss and
  validated research before a paper order can be created.
- `RiskEngine` now enforces configured risk limits from
  `config/open_stock_ai.yaml`: minimum rule-score magnitude, maximum position size,
  maximum backtest drawdown, and estimated stop-loss exposure against the
  daily loss limit.
- `RiskDecision` uses `open_stock_ai.risk_decision.v1`. It preserves the
  existing `approved`, `reason`, and `risk_notes` fields, and adds structured
  `gate_checks` plus a `policy` snapshot for rule score, research validation,
  TradingAgents risk evidence, FinRobot report evidence, drawdown, position
  sizing, stop loss, FinRL backtest evidence, Qlib factor evidence, estimated
  daily loss, and paper portfolio exposure.
- Approved paper executions are persisted to the SQLite `trades` table through
  `TradeStore` as `open_stock_ai.paper_order.v1` ledger entries. The ledger is
  written only after the central `RiskEngine` approves and `ExecutionEngine`
  creates a paper order.
- The paper-order ledger endpoint keeps `open_stock_ai.paper_order.v1` for
  backward-compatible consumers and now also publishes
  `row_schema_version=open_stock_ai.paper_order.v1` so audit checks can verify
  the persisted row contract explicitly.
- LLM, notification, order-preview, kill-switch, and disabled-live-execution
  helper modules expose explicit local schemas such as
  `open_stock_ai.llm_route.v1`, `open_stock_ai.notification_preview.v1`, and
  `open_stock_ai.live_execution_disabled.v1`. These helpers are preview-only or
  blocked-by-policy and do not send remote messages, call an LLM endpoint, or
  place live orders.
- Each paper order is also projected into an AI-Trader-compatible
  `trades.schema.json` row and validated locally through `AITraderSource`.
  This does not publish to AI-Trader or call a remote connector; it only proves
  that the Open Stock AI paper ledger can interoperate with AI-Trader's research
  export schema.
- Each strategy signal is also projected into an AI-Trader-compatible
  `signals.schema.json` row and validated locally. This keeps signal and paper
  trade interoperability checks symmetric while preserving the no-remote-publish
  boundary.
- AI-Trader interoperability now also emits
  `open_stock_ai.ai_trader_interop_projection.v1` for validated signal rows and
  new paper-order rows. The projection records schema path, export version,
  row-field coverage, skill count, and
  `validated_locally_no_remote_ai_trader_publish`.
- AI-Trader skill routing now emits
  `open_stock_ai.ai_trader_skill_route.v1` for validated signals. It reads
  `skills/*/SKILL.md` frontmatter from the approved local clone, selects the
  bootstrap `ai-trader` skill plus context/publish-disabled child routes such
  as `market-intel` and `ai-trader-tradesync`, and records disabled remote
  actions (`publish_signal`, `sync_trade`, `copy_trade`, and
  `heartbeat_polling`) so route readiness cannot bypass Open Stock AI's paper
  execution boundary.
- The signal ledger endpoint exposes `open_stock_ai.signal_ledger.v1`, and
  each returned row uses `open_stock_ai.signal_ledger_row.v1`.
- `TradeStore` also builds `open_stock_ai.paper_portfolio_exposure.v1` from the
  paper order ledger. `RiskEngine` reads this summary before approval and
  blocks new buy/add orders when the configured single-symbol or total paper
  exposure limits would be exceeded.
- `ReportGenerator` emits `open_stock_ai.research_artifacts.v1`, a normalized
  artifact envelope for the generated Markdown research report and backtest JSON
  replay payload. The backtest payload includes the normalized research
  `adapter_results`, so FinRL/Qlib evidence can be replayed without source-
  specific raw paths.
- Qlib research output includes `open_stock_ai.qlib_workflow_summary.v1`,
  a local projection of parsed Qlib workflow YAML into the Open Stock AI
  research payload, and `open_stock_ai.qlib_factor_projection.v1`, a local
  factor-score/workflow projection with model score, rank-IC proxy, and
  research-report artifact linkage consumed by the central risk gate as
  advisory evidence.
- FinGPT intelligence output includes
  `open_stock_ai.fingpt_forecast_projection.v1`, a local projection of the
  parsed FinGPT Forecaster prompt contract plus Open Stock AI sentiment,
  momentum, and revenue inputs. This is deterministic evidence only; it does
  not import or run FinGPT model inference.
- FinRobot intelligence output includes
  `open_stock_ai.finrobot_report_projection.v1`, a local projection of the
  parsed FinRobot equity-report section contract, Expert_Investor agent role,
  report analysis tools, revenue inputs, risk view, and valuation band. This is
  deterministic evidence only; it does not call FinRobot APIs or import
  FinRobot runtime modules.
- FinRL research output includes `open_stock_ai.finrl_backtest_projection.v1`,
  a local projection of Open Stock AI's deterministic backtest result plus
  parsed FinRL/FinRL-Trading paper-trading, environment, BacktestEngine, and
  adaptive-rotation contracts. This is evidence only; it does not import or run
  FinRL runtimes.
- `SignalPipeline` persists each decision into `output/open_stock_ai.sqlite`.
- `OpenStockAIEngine.run_pre_market()`, `run_intraday()`, and
  `run_after_market()` now run the configured Open Stock AI watchlist through
  the same central pipeline. They map to `swing`, `intraday`, and `weekly`
  horizons respectively, so batch sessions do not bypass adapter evidence,
  risk gates, storage, or paper execution controls.
- Batch sessions publish `open_stock_ai.portfolio_construction.v5`, a
  paper-only multi-symbol proposal. Each target first needs a single-name
  volatility/loss-budget/fractional-Kelly receipt, then a versioned PIT
  covariance matrix and factor-limit receipt. The independent portfolio-risk
  receipt also checks issuer/industry/factor/currency/broker/account
  concentration, ADV participation, days-to-liquidate, historical/parametric
  CVaR and declared stress scenarios. Missing or invalid portfolio context
  withholds every target instead of normalising single-name weights. It cannot
  submit live orders or bypass the central `RiskEngine`.
- `SignalPipeline` also writes a TradingAgents-inspired persistent decision log
  with rating, trader action, price levels, sizing, risk status, execution
  status, and a short review lesson.
- `SQLiteStore` exposes a TradingAgents-style decision replay summary over the
  persistent log: approval/execution rates, rating/action distributions,
  average confidence, latest decision, and recent lessons.
- The replay summary includes
  `open_stock_ai.tradingagents_reflection_replay.v1`, a local projection that
  links stored Open Stock AI decisions back to the parsed TradingAgents
  `reflection.py`, rating, and risk-memory contracts. It classifies replay
  cohorts such as `blocked_by_risk`, `approved_executed`, and
  `approved_not_executed` without importing the TradingAgents runtime.
- The replay summary also includes `open_stock_ai.portfolio_attribution.v1`,
  a decision-log attribution envelope by symbol. It aggregates decision count,
  approval/execution rates, proposed paper sizing, dominant ratings/actions,
  and recent lessons as replay analytics only; it cannot submit orders.
- The integration audit also includes
  `open_stock_ai.paper_outcome_attribution.v1`, a signal-ledger forward replay
  that compares each paper signal with the next stored observation for the same
  symbol. It reports directional return, target/stop replay hits, and aggregate
  positive/target/stop rates without modifying risk decisions or execution.
- Storage and replay endpoints now expose stable envelopes:
  `open_stock_ai.storage_stats.v1`,
  `open_stock_ai.decision_log_ledger.v1`,
  `open_stock_ai.decision_log_row.v1`, and
  `open_stock_ai.decision_review.v1`.
- `build_integration_audit()` summarizes the whole integration boundary:
  verified external origins, git origin/HEAD source lock status, read-only
  adapter contracts, runtime connector governance, YAML-backed risk settings,
  paper-only execution, storage status, and decision replay availability.
- The audit also publishes `open_stock_ai.integration_requirement_matrix.v1`,
  a requirement-to-evidence matrix derived from the current audit state. It
  maps the original integration objective to concrete invariants, evidence
  paths, and explicit review items such as AI-Trader's missing top-level
  license text and disabled remote-order-capable runtimes.
- The audit publishes `open_stock_ai.optional_external_source_registry.v1` so
  optional references such as Freqtrade are explicitly excluded from the
  approved seven-repo source lock instead of being silently ignored or
  accidentally treated as integrated.
- The audit also publishes `open_stock_ai.external_project_contribution_matrix.v1`,
  which proves each approved external project has a concrete internal
  contribution path and cannot bypass the central risk or paper execution
  boundary. FinRL-Trading is represented explicitly and tied to the FinRL
  adapter contract plus the portfolio-construction projection.
- The audit also publishes
  `open_stock_ai.broker_account_import_governance.v1`, proving that broker or
  account import connectors are disabled unless explicit credential governance,
  live-trading governance, and paper-ledger reconciliation rules are added.
- The audit publishes `open_stock_ai.external_evidence_lineage.v1` for the
  latest decision, proving that TradingAgents, FinGPT, FinRobot, FinRL, Qlib,
  and AI-Trader evidence can be traced back to verified external source commits
  and cannot cross an unrecorded execution boundary.
- The audit publishes `open_stock_ai.design_system_contract.v1`, which proves
  the requested shadcn/ui, Magic UI, Aceternity UI, 21st.dev Agent Elements,
  and Tailwind-inspired contracts are implemented in the single local static UI
  rather than as a second frontend runtime.
- The FastAPI route surface for `/api/open-stock-ai/*` is owned by
  `src/open_stock_ai/api.py`. The outer `stock_ai.main` app only mounts
  `open_stock_ai.api.router`, so the Open Stock AI API entrypoint remains under
  `src/open_stock_ai`.
- The audit also checks the required `src/open_stock_ai` module surface from
  the integration plan, including required directories such as
  `src/open_stock_ai/llm/prompts`, `docs/integration`, `output/reports`,
  `output/backtests`, and `logs/agent`. It reports missing files/directories or
  any remaining `pass` / `placeholder` implementation markers.
- `src/open_stock_ai/llm/prompts/paper_trading_reviewer.md` defines the local
  paper-trading reviewer prompt boundary. It is evidence-only and cannot
  approve live trading, bypass `RiskEngine`, or mutate paper orders.
- The audit also scans `src/open_stock_ai/**/*.py` to ensure the main process
  does not directly import external heavy runtimes such as TradingAgents,
  FinGPT, FinRobot, FinRL, qlib, or AI-Trader.
- `ResearchEngine` writes Markdown reports to `output/reports` and backtest JSON payloads to `output/backtests`.
- `ResearchModelRuntime` is an explicit opt-in bridge to the isolated FinRL/Qlib workers. It only accepts a
  point-in-time manifest hash and its materialized rows, records the dataset/policy/workflow-config/Recorder hashes in its receipt,
  and refuses artifact reuse when the fitted dataset hash differs from the requested replay dataset.
- `ResearchModelRegistry` persists executed runtime receipts in the primary SQLite database. It creates immutable,
  content-addressed FinRL and Qlib model versions plus one experiment receipt that joins the request, PIT manifest/data hashes,
  model-version IDs, configuration hashes and worker output. A receipt is explicitly only `receipt_backed` until an exact
  runtime package/OS lock is captured; the registry therefore does not claim bitwise reproducibility prematurely.
- Execution remains paper-only.
- Live trading remains disabled.

API:

```text
GET /api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing
```

The endpoint returns the full `StockDecision` payload as a dictionary.

```text
GET /api/open-stock-ai/session/{pre_market|intraday|after_market}
```

The endpoint runs the configured Open Stock AI watchlist through the
corresponding batch session and returns `open_stock_ai.batch_session.v1` with a
full `StockDecision` payload for each configured symbol plus
`open_stock_ai.portfolio_construction.v5` for covariance-constrained,
paper-only multi-symbol research.

```text
GET /api/open-stock-ai/external-sources
```

The endpoint returns verification metadata for TradingAgents, FinRobot, FinGPT,
FinRL-Trading, FinRL, qlib, and AI-Trader. It also includes read-only contract
summaries such as `contracts.ai_trader` for AI-Trader agent schemas/skills and
`contracts.fingpt` for FinGPT sentiment/forecaster/RAG/trading metadata, and
`contracts.finrobot` for FinRobot report agents/tools, `contracts.finrl` for
FinRL/FinRL-Trading research metadata, and `contracts.qlib` for qlib workflow
metadata.
`contracts.tradingagents` summarizes TradingAgents typed decision schemas,
agent graph nodes/edges, rating scale, signal processing, risk debators, memory,
and reflection metadata.

```text
GET /api/open-stock-ai/external-source-lock
```

The endpoint returns the machine-checkable source lock. It lists the exact
approved clone command for TradingAgents, FinRobot, FinGPT, FinRL-Trading,
FinRL, qlib, and AI-Trader, compares local origin and HEAD with the lock, and
returns `origin_verified`, `head_verified`, and `lock_verified`.

```text
GET /api/open-stock-ai/optional-external-sources
```

The endpoint returns `open_stock_ai.optional_external_source_registry.v1`.
It currently records Freqtrade as `not_approved_not_cloned`, with candidate
roles such as dry-run, backtesting, Web UI, and strategy plugins kept outside
Open Stock AI until explicitly approved.

```text
GET /api/open-stock-ai/broker-import-governance
```

The endpoint returns `open_stock_ai.broker_account_import_governance.v1`.
It reports the disabled broker/account import boundary for FinRL-Trading,
FinRL, and AI-Trader, including credential checks, blocked import paths,
remote broker mutation status, and the paper-ledger replay-only policy.

```text
GET /api/open-stock-ai/storage
```

The endpoint returns SQLite storage status and table counts.
It uses `open_stock_ai.storage_stats.v1` and includes decision-log ledger
metadata so audit consumers can verify the persistent replay contract.

```text
GET /api/open-stock-ai/decision-log?limit=20
```

The endpoint returns recent structured decision log rows for replay and review.
The envelope is `open_stock_ai.decision_log_ledger.v1`; each item uses
`open_stock_ai.decision_log_row.v1`.

```text
GET /api/open-stock-ai/decision-review?symbol=2330.TW&limit=100
```

The endpoint returns an aggregated replay/reflection summary from the structured
decision log. The UI uses it to show recent paper-decision approval and
execution rates.
The payload uses `open_stock_ai.decision_review.v1` and nests
`open_stock_ai.tradingagents_reflection_replay.v1` under `reflection_replay`
and `open_stock_ai.portfolio_attribution.v1` under `portfolio_attribution`.

```text
GET /api/open-stock-ai/signals?limit=20
```

The endpoint returns recent persisted Open Stock AI signals from SQLite.
Entries use `open_stock_ai.signal_ledger.v1` and include local
`ai_trader_validation` plus `ai_trader_signal_row`, generated from AI-Trader's
local `research/schemas/signals.schema.json`.
Each new signal decision also includes
`open_stock_ai.ai_trader_interop_projection.v1`.
Each returned row carries `open_stock_ai.signal_ledger_row.v1`.

```text
GET /api/open-stock-ai/paper-orders?limit=20
```

The endpoint returns the recent paper order ledger from SQLite. Entries use
`open_stock_ai.paper_order.v1` and include order id, symbol, action, price
levels, sizing, typed rule score, risk approval state, decision schema, and source
modules. They also include `ai_trader_validation` and `ai_trader_trade_row`,
which are generated from AI-Trader's local `research/schemas/trades.schema.json`.
New paper orders also include
`open_stock_ai.ai_trader_interop_projection.v1`.
The response includes `row_schema_version=open_stock_ai.paper_order.v1`.

```text
GET /api/open-stock-ai/paper-exposure
```

The endpoint returns the current paper portfolio exposure derived from the
paper order ledger. This is the same summary used by `RiskEngine` for
cumulative exposure checks.

```text
GET /api/open-stock-ai/integration-audit?symbol=2330.TW&limit=100
```

The endpoint returns a single audit payload from `src/open_stock_ai`. It checks
that all seven expected external project clones are origin-verified and match
the approved git origin/HEAD source lock, all six read-only adapter contracts
are loaded, all contracts expose the normalized `open_stock_ai.adapter_result.v1`
schema, `stock_ai.main` mounts the `open_stock_ai.api.router`, risk settings
are configured through `OpenStockAISettings`, execution is paper-only, recent
market snapshots expose
`open_stock_ai.data_source_envelope.v1`, full decisions expose
`open_stock_ai.stock_decision.v1`, risk decisions expose
`open_stock_ai.risk_decision.v1`, the signal ledger is available with
row-level schema, the paper order ledger is available, recent paper orders
validate against AI-Trader's trades schema, recent signals validate against
AI-Trader's signals schema, AI-Trader interop projections use
`open_stock_ai.ai_trader_interop_projection.v1`, optional source policy uses
`open_stock_ai.optional_external_source_registry.v1`, broker/account import
governance uses `open_stock_ai.broker_account_import_governance.v1`, external
evidence lineage uses `open_stock_ai.external_evidence_lineage.v1`, external
contribution tracing uses `open_stock_ai.external_project_contribution_matrix.v1`, external license
footprint scans use `open_stock_ai.external_license_footprint.v1`, Qlib workflow summaries use
`open_stock_ai.qlib_workflow_summary.v1`, Qlib factor projections use
`open_stock_ai.qlib_factor_projection.v1`, FinRL backtest projections use
`open_stock_ai.finrl_backtest_projection.v1`, FinGPT forecast projections use
`open_stock_ai.fingpt_forecast_projection.v1`, FinRobot report projections use
`open_stock_ai.finrobot_report_projection.v1`, research artifacts use
`open_stock_ai.research_artifacts.v1`, portfolio construction projections use
`open_stock_ai.portfolio_construction.v5`, paper portfolio exposure is available,
decision replay uses `open_stock_ai.decision_review.v1`, and decision-log
ledger rows use `open_stock_ai.decision_log_row.v1`. It also reports
`runtime_boundary.clean`, which is true only when `src/open_stock_ai` has no
direct runtime imports from the external projects. It also returns
`open_stock_ai.integration_requirement_matrix.v1` so consumers can inspect how
the objective-level requirements map to current evidence.

Testing note:

`stock_ai.realtime_quotes` includes a pytest-only TWSE MIS shaped fixture so the
test suite remains reproducible when the public MIS endpoint is temporarily
unavailable. This fixture is only enabled when `PYTEST_CURRENT_TEST` exists.

Current source boundary:

- TradingAgents is used as a verified source of architecture and schema shape,
  but `src/open_stock_ai` owns its own pure-Python schema implementation.
- TradingAgents contracts are parsed locally for multi-agent workflow metadata
  only. Open Stock AI does not import TradingAgents LangGraph or LLM runtime in
  the current adapter.
- FinGPT, FinRobot, FinRL, FinRL-Trading, qlib, and AI-Trader are verified by
  local clone metadata and lightweight adapters; their heavy runtimes are not
  imported into the application process.
- FinGPT contracts are parsed locally for sentiment, forecasting, RAG, and
  trading prompt metadata only. Open Stock AI does not import FinGPT training
  or inference runtimes in the current adapter.
- AI-Trader contracts are parsed locally for interoperability metadata only.
  Open Stock AI does not publish signals to AI-Trader or call the remote
  AI-Trader API in the current integration layer.
- FinRobot contracts are parsed locally for report planning metadata only.
  Open Stock AI does not import FinRobot runtime modules or call their external
  data providers from the adapter boundary.
- FinRL and FinRL-Trading contracts are parsed locally for research planning,
  paper-trading examples, and backtest workflow metadata only. Their runtimes do
  not execute orders or bypass Open Stock AI's paper executor.
- qlib contracts are parsed locally from workflow YAML and documentation only.
  Qlib factor evidence is projected from local Open Stock AI data and parsed
  workflow metadata, then linked to the generated Markdown research report;
  qlib runtime is not imported into the application process.
- `config/open_stock_ai.yaml` is the runtime wiring file. External paths are
  resolved and verified by `ExternalProjectRegistry`; live execution remains
  disabled even if external projects include broker or paper-trading examples.
- External license and dependency manifests are scanned locally. This scan is
  an integration governance record, not a legal conclusion; missing license
  files remain explicit review items in the audit payload.
- External runtime connector governance is also scanned locally. All runtime
  connectors are blocked by default with
  `open_stock_ai.runtime_connector_governance.v1`; FinRL, FinRL-Trading, and
  AI-Trader are explicitly marked as remote-order-capable runtimes whose live
  submission path remains disabled under Open Stock AI.
- Risk settings are consumed by the central `RiskEngine`, not by individual
  external adapters, so FinRL/Qlib/TradingAgents/FinRobot evidence cannot
  bypass the unified risk gate. Paper portfolio exposure limits are also
  enforced by the central `RiskEngine` from the SQLite paper order ledger.

Next integration phases:

1. Resolve external projects whose license scan reports manual review, starting
   with AI-Trader's MIT metadata but missing top-level license text in the
   current local clone.
2. Implement optional external runtime connectors only where dependency,
   license, and operational risk pass
   `open_stock_ai.runtime_connector_governance.v1`; keep the read-only adapter
   boundary as the default.
3. Expand structured lessons with realized broker/account outcomes only after
   a governed import connector is explicitly enabled and reconciled against the
   paper ledger.
4. Add broker/account import connectors only after explicit live-trading,
   credential, and remote mutation governance are designed.
