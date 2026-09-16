# Open Stock AI Quickstart

Open Stock AI is served by the existing FastAPI app:

```powershell
uv sync --extra dev
uv run python -m uvicorn stock_ai.main:app --host 127.0.0.1 --port 8000
```

Open the workspace:

```text
http://127.0.0.1:8000/#openstock
```

The embedded Codex runtime, ChatGPT account login, Computer Use flow, and
market-radar API are documented in [codex_runtime.md](codex_runtime.md).

Useful verification endpoints:

```text
GET /api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing
GET /api/open-stock-ai/integration-audit?symbol=2330.TW&limit=20
GET /api/open-stock-ai/external-source-lock
GET /api/open-stock-ai/signals?limit=20
GET /api/open-stock-ai/paper-orders?limit=20
GET /api/open-stock-ai/decision-review?symbol=2330.TW&limit=20
```

The runtime source lock verifies the approved clones:

```powershell
git clone https://github.com/TauricResearch/TradingAgents.git external/TradingAgents
git clone https://github.com/AI4Finance-Foundation/FinRobot.git external/FinRobot
git clone https://github.com/AI4Finance-Foundation/FinGPT.git external/FinGPT
git clone https://github.com/AI4Finance-Foundation/FinRL-Trading.git external/FinRL-Trading
git clone https://github.com/AI4Finance-Foundation/FinRL.git external/FinRL
git clone https://github.com/microsoft/qlib.git external/qlib
git clone https://github.com/HKUDS/AI-Trader.git external/AI-Trader
```

Freqtrade is documented as an optional reference only. It is not part of the
approved source lock, and `/api/open-stock-ai/optional-external-sources`
returns `open_stock_ai.optional_external_source_registry.v1` with Freqtrade
marked `not_approved_not_cloned`.

Execution remains paper-only. Live execution is disabled by configuration and
by the local `LiveExecutorDisabled` boundary.

External runtime connectors are also disabled by default:

```env
EXTERNAL_RUNTIME_CONNECTORS_ENABLED=false
BROKER_ACCOUNT_IMPORTS_ENABLED=false
EXTERNAL_CREDENTIALS_ENABLED=false
```

The integration audit exposes `open_stock_ai.runtime_connector_governance.v1`
to prove that external runtimes cannot submit remote orders or bypass the
central `RiskEngine` and `PaperExecutor`.

It also exposes `open_stock_ai.broker_account_import_governance.v1` to prove
that FinRL-Trading, FinRL, and AI-Trader account/import paths cannot read
external credentials, import realized broker orders, or mutate remote broker
state while Open Stock AI is in paper mode.

The same audit also exposes
`open_stock_ai.external_project_contribution_matrix.v1`, which traces every
approved external repo to its Open Stock AI pipeline stage, emitted projection
schemas, central risk boundary, and paper-only execution boundary.
