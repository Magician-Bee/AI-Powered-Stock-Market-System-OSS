# Model Invocation Map

- Baseline commit: `94e8cb2d65c9f5d8f8de04717847d77c513cee27`
- Generated at: `2026-07-29T19:18:01.417729+00:00`
- Scope: Production `src/` and `config/`; vendored UI files excluded.
- Matches: **45**

## Purpose

Locate actual provider/model invocation boundaries and distinguish them from deterministic adapters.

## Classification notes

- HTTP market-data calls are not model invocations.
- Every true invocation must emit a durable invocation receipt.
- Provider format/protocol failures must be distinct from semantic answer failures.

## Inventory

- `src/open_stock_ai/agent_runtime/orchestrator.py:943` — `model_call_id=f"{run_id}:turn:{step}",`
- `src/open_stock_ai/agent_runtime/orchestrator.py:949` — `raw_turn = await driver.decide(turn_input)`
- `src/open_stock_ai/agent_runtime/orchestrator.py:954` — `model_call_id=f"{run_id}:turn:{step}",`
- `src/open_stock_ai/agent_runtime/orchestrator.py:1016` — `model_call_id=f"{run_id}:turn:{step}",`
- `src/open_stock_ai/agent_runtime/orchestrator.py:1443` — `"id": str(node.metadata.get("model_call_id") or node.node_id),`
- `src/open_stock_ai/agent_runtime/orchestrator.py:1613` — `recovery = self.recovery.decide(`
- `src/open_stock_ai/agent_runtime/orchestrator.py:1783` — `retry = self.recovery.decide(`
- `src/open_stock_ai/agent_runtime/orchestrator.py:1954` — `recovery = self.recovery.decide(`
- `src/open_stock_ai/agent_runtime/orchestrator.py:2864` — `"metadata": {"model_call_id": call["id"], "turn_step": step},`
- `src/open_stock_ai/agent_runtime/orchestrator.py:3328` — `call_id = str(event.get("model_call_id") or "")`
- `src/open_stock_ai/analysis_contracts.py:25` — `model_call_id: str | None = None`
- `src/open_stock_ai/analysis_contracts.py:26` — `model_call_succeeded: bool = False`
- `src/open_stock_ai/storage/migrations.py:1002` — `model_call_id text primary key,`
- `src/open_stock_ai/storage/migrations.py:1019` — `model_call_id text references model_invocations(model_call_id) on delete set null,`
- `src/open_stock_ai/storage/migrations.py:1044` — `model_call_id text not null references model_invocations(model_call_id) on delete cascade,`
- `src/open_stock_ai/storage/migrations.py:1053` — `model_call_id text references model_invocations(model_call_id) on delete set null,`
- `src/stock_ai/agent_api.py:655` — `"model_call_id": receipt.get("call_id"),`
- `src/stock_ai/agent_api.py:656` — `"model_call_succeeded": model_succeeded,`
- `src/stock_ai/agent_trading_api.py:370` — `model_call_id = f"MI-{uuid4().hex}"`
- `src/stock_ai/agent_trading_api.py:373` — `decision = await codex_runtime.run_structured(`
- `src/stock_ai/agent_trading_api.py:383` — `call_id=model_call_id,`
- `src/stock_ai/agent_trading_api.py:418` — `model_call_id=model_receipt.call_id,`
- `src/stock_ai/agent_trading_api.py:419` — `model_call_succeeded=True,`
- `src/stock_ai/agent_trading_api.py:450` — `result = await codex_runtime.run(`
- `src/stock_ai/codex_api.py:77` — `result = await codex_runtime.run(`
- `src/stock_ai/codex_llm_bridge.py:144` — `@app.post("/v1/chat/completions")`
- `src/stock_ai/codex_market.py:128` — `"model_call_id": call_id,`
- `src/stock_ai/codex_market.py:129` — `"model_call_succeeded": True,`
- `src/stock_ai/codex_market.py:202` — `model_call_id=receipt.call_id if receipt.status != "not_run" else None,`
- `src/stock_ai/codex_market.py:203` — `model_call_succeeded=receipt.status == "succeeded",`
- `src/stock_ai/codex_runtime.py:179` — `model_call_id = f"codex-native-{uuid4().hex}"`
- `src/stock_ai/codex_runtime.py:233` — `call_id=model_call_id,`
- `src/stock_ai/codex_runtime.py:247` — `model_call_id=model_receipt.call_id,`
- `src/stock_ai/codex_runtime.py:248` — `model_call_succeeded=succeeded,`
- `src/stock_ai/market_radar.py:85` — `model_call_id: str = Field(min_length=1, max_length=500)`
- `src/stock_ai/market_radar.py:86` — `model_call_succeeded: Literal[True]`
- `src/stock_ai/market_radar.py:224` — `"model_call_id": receipt_id,`
- `src/stock_ai/market_radar.py:225` — `"model_call_succeeded": True,`
- `src/stock_ai/market_radar.py:286` — `"model_call_succeeded": True,`
- `src/stock_ai/ui/static/js/core/dom-state.js:267` — `const isModel = item.origin === 'model' && item.model_call_succeeded === true;`
- `src/stock_ai/ui/static/js/features/codex.js:186` — `const isModel = item.origin === 'model' && item.model_call_succeeded === true;`
- `src/stock_ai/ui/static/js/features/codex.js:338` — `model_call_succeeded: false,`
- `src/stock_ai/ui/static/js/features/codex.js:359` — `model_call_succeeded: false,`
- `src/stock_ai/ui/static/js/features/codex.js:405` — `&& snapshot.provenance?.model_call_succeeded === true`
- `src/stock_ai/ui/static/js/features/codex.js:437` — `model_call_succeeded: false,`
