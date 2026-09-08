# TradingAgents release smoke — 2026-08-25

This is a non-trading, research-only acceptance receipt for the vendored
`external/TradingAgents` graph. It uses the project's isolated macOS runtime,
the exact locked TradingAgents source, and the configured OpenAI-compatible
remote Ollama endpoint. The API-key value is a local compatibility token and
is not stored in this repository.

Command shape (the actual token is supplied only in the process environment):

```bash
export STOCK_AI_ENABLE_TRADINGAGENTS_RUNTIME=1
./開啟股市AI系統.command

STOCK_AI_RUN_EXTERNAL_MODEL_E2E=tradingagents \
STOCK_AI_TRADINGAGENTS_BASE_URL=http://192.0.2.1:11434/v1 \
STOCK_AI_TRADINGAGENTS_MODEL=gpt-oss:20b \
OPENAI_API_KEY=<local-compatibility-token> \
.runtime/venv-macos-arm64/bin/python -m pytest \
  tests/integration/test_external_model_runtime.py \
  -k tradingagents
```

Observed receipt:

- endpoint `/v1/models`: HTTP 200; `gpt-oss:20b` was advertised;
- endpoint `/v1/chat/completions`: HTTP 200; the model returned the requested
  JSON smoke response;
- vendored graph: `status=executed`, model `gpt-oss:20b`, symbol `2330.TW`,
  selected analyst `market`, trade date `2026-08-20`;
- graph decision: `Buy`;
- result SHA-256:
  `3310980c5d0c82dc2212e8083b0c8c4f40bbed2d75ec311cb7c79ea022e3ba29`;
- execution authority: `none`;
- execution boundary: `research_evidence_only_no_order_authority`.

The smoke does not enable live trading, submit an order, or promote model
output into an execution permission. A normal test run keeps this external
network smoke skipped unless the operator explicitly opts in with the
environment variables above.
