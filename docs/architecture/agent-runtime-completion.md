# Agent Runtime 完成度與證據矩陣

本文件以原始 Agent 架構稽核為逐項驗收基準。`已完成` 只代表目前程式與對應測試能直接證明；外部模型、付費資料或 API 尚未配置時仍維持 `受前置條件阻擋`，不會因工具名稱存在而標成成功。

| 原始要求 | 目前狀態 | 權威證據 |
| --- | --- | --- |
| App Server turn/item 真實事件；不偽造或洩漏私有 chain-of-thought | 已完成 | `src/stock_ai/codex_runtime.py` 的 audited turn stream；`tests/test_codex_integration.py` |
| 同一 Durable Agent Runtime 內使用 Codex，不轉送到可見聊天 | 已完成 | `CodexAgentDriver` → `run_agent_turn`；`boundaries.agent_runtime_never_delegates_to_codex_chat` |
| 可替換模型／Agent Provider 是真實執行路徑 | 已完成；外部端點需使用者配置 | `ProviderRegistry` 建立 `codex`、`openai-compatible`、`external-agent`；設定頁、健康檢查、Keychain secret store 與 HTTP MockTransport 離線整合測試 |
| 同一 run 使用同一 hidden Codex thread | 已完成 | `start_agent_run/close_agent_run`；`tests/integration/test_codex_agent_session.py` |
| 任務脫離 HTTP stream、斷線後持續、可重播與取消 | 已完成 | `durable_agent_runtime.py`、`agent_run_store.py`；`tests/test_durable_agent_runtime.py` |
| 後端 advisory scheduler | 已完成 | `tool_providers/schedule.py`、`agent_event_bus.py`；市場／新聞／持倉／UI 事件先進 SQLite v9 持久事件匣，原子租約避免遺失與多 Runtime 重複觸發，scheduler 不允許專案、外部或 Paper 權限 |
| 動態 Capability Registry 是 manifest、UI 與 routing 的單一真相 | 已完成 | `capability_registry.py`、`agent_tools.py`；`tests/test_capability_registry.py` |
| Skills 完整載入並記錄於 activity | 已完成 | `tool_providers/skills.py`；Skill 內容在 run prepare 時注入，不把 inventory 當執行 |
| MCP 真實 schema、健康檢查與 Host 執行 | 已完成 | `tool_providers/mcp.py`；唯讀／外部寫入／破壞性寫入三層；真實帳號 smoke 已讀取 inventory |
| 內建 UI command bridge，不依賴固定 port 或 Chrome | 已完成 | UI 狀態、command、ack/result 保存於 SQLite；`agent_ui_bridge.py`、`agent-ui-bridge.js`；`tests/e2e/test_agent_ui_stream.py` |
| 一般知識與需證據任務使用不同完成規則 | 已完成 | 穩定一般知識可直接完成且不提供不必要工具；即時／冷門／專案／UI／交易任務仍要求 Host 驗證證據；`tests/test_agent_runtime_v2.py` |
| 工具不能以通用 success 欄位偽造完成 | 已完成 | `validators.py` 依 project／paper／UI／browser／notification／schedule／memory／artifact／git／external 類型核對持久憑證；專案結構化檔案寫入前先做語法驗證 |
| 真實 Browser 與通知能力 | 已完成 | Playwright 隔離 context、SSRF/密碼/下載限制；Telegram/LINE credentials 不回傳 |
| 專案 Terminal 不是任意 shell | 已完成 | `sandbox_executor.py`：argv allowlist、無 shell、封鎖 Home/secret/network/destructive command |
| Native Codex approval 綁定 run/capability/root/expiry | 已完成 | `approval_policy.py`；無 grant 預設 decline；原生完整能力仍保留在 developer route |
| SourceEnvelope 與依 horizon 的 execution gate | 已完成 | `source_policy.py` 與 market adapters；非授權即時資料不得通過 intraday gate |
| StrategyEngine point-in-time exact replay、walk-forward、OOS、purged CV、成本、滑價、延遲及 hashes | 引擎已完成；資料受限 | `backtest_research.py` 與 learning evaluation；沒有完整歷史 PIT 資料時 `empirical_valid=false` |
| 三層 memory 與 proposal → evaluation → shadow → 人工 promotion | 已完成 | `src/open_stock_ai/learning/`；Agent/Codex 不得直接 promotion |
| Agent Paper 訂單的精確預覽、30 秒期限、來源簽章與 RiskEngine | 已完成 | `agent_tools.py`、`central_risk_adapter.py`；`tests/e2e/test_agent_paper_order.py`。當同一目標同時要求基本面、技術面、風險與紙上單時，Host 終態會保留各已驗證分析維度或其資料限制；紙上成交永不升格為投資建議或實盤授權。 |
| TradingAgents 完整多代理 workflow | 已完成且本機實證 | `TradingAgentsGraph.propagate` 經 Codex bridge 完成 12 個模型 turn、13 個 tool call、完整報告與最終 signal；無外部 LLM API key |
| FinGPT 真實模型載入與 inference | 來源與入口保留；依使用者決定停用 | `models.fingpt_local_model_enabled=false`；manifest/health 明確回報 unavailable，不下載或載入本地權重 |
| FinRL policy training/loading/inference | 已完成且本機實證 | `external.finrl.train_policy/predict_actions`；真實 policy artifact + SHA-256；release smoke 重新載入成功 |
| Qlib Dataset/Model/Recorder/Portfolio Analysis workflow | 已完成且本機實證 | `qlib.cli.run.workflow` 已完成 Alpha158／LinearModel 訓練、推論、Signal/Portfolio Analysis 與 100 個雜湊 artifact |
| FinRobot AutoGen 財務報告 workflow | 已完成且本機實證 | `SingleAssistant/UserProxyAgent.initiate_chat` 經 Codex bridge 回覆；不需外部 model credential，code execution 固定關閉 |
| 具名 Codex/UI/project/Paper E2E 測試 | 已完成 | `tests/integration/`、`tests/e2e/`；真實 Codex 帳號 release smoke 通過 |

## 外部 workflow 的真實性規則

完整外部 workflow 一律在 `.runtime/external-workflows/` 的獨立 Python 與 artifact 空間執行。Host 只接受同時具備下列證據的結果：

1. 實際上游 module 位於 `config/external_sources.lock.yaml` 鎖定目錄內。
2. 每個上游 module、模型/Recorder artifact 都有 SHA-256。
3. 結果明確記錄 Python、函式、模型/政策 provenance 與執行階段。
4. 缺依賴、模型、API 或資料時回報 prerequisite error；禁止改用公式、模板或本機假模型。
5. 一般外部 worker 不接收 Codex、GitHub、SSH 或其他無關憑證；TradingAgents／FinRobot 只接收該 run 的隨機 loopback bridge token，永不取得帳號 token；真實券商能力固定不存在。

安裝可選 runtime：

```bash
uv run python scripts/bootstrap_external_workflows.py finrl
uv run python scripts/bootstrap_external_workflows.py tradingagents qlib finrobot
```

安裝結果只存在 `.runtime`，不進 Git、CI 或 clone。`external.runtime.health` 會使用實際綁定的 interpreter 探測依賴、設定與最近成功執行證據。

## Release smoke

```bash
STOCK_AI_RUN_CODEX_E2E=1 uv run pytest tests/integration/test_codex_account_runtime.py -q
STOCK_AI_RUN_EXTERNAL_MODEL_E2E=finrl uv run pytest tests/integration/test_external_model_runtime.py -q
STOCK_AI_RUN_BROWSER_E2E=1 uv run pytest tests/test_browser_notification_tools.py -q
```

截至 2026-07-18，這台工作站已通過真實 Codex hidden turn/MCP inventory、Playwright Chrome、一般問答單一 Codex turn／零工具完成、OpenAI-compatible 與外部 Agent HTTP protocol 的離線 MockTransport 整合、FinRL 訓練 → 保存 → 新 subprocess 載入 → policy inference、Qlib 完整 workflow、TradingAgents LangGraph → Codex 12-turn workflow，以及 FinRobot AutoGen → Codex 單輪 workflow。未提供外部 endpoint／credential 時不會冒充已通過真實遠端推論；FinGPT 本地模型是明確停用的保留能力，不列為目前驅動器或待完成缺口。
