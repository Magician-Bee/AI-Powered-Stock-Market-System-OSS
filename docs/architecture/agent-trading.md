# 可替換 Agent Runtime 與模擬交易

AI 股市系統把「主系統」和「Agent」明確分開。主系統是持有資料、研究、風控、帳戶與執行能力的載具；Codex、Hermes 或其他模型／Agent framework 是可替換的操作員。更換操作員不會把行情真相、風控規則或 Broker 權限交給模型。

## 兩條能力路徑

### 原生 Codex 路徑

`POST /api/codex/run` 保留完整專案 thread、檔案、命令、瀏覽器與 Computer Use 能力。Agent Runtime 是加強功能，不會替換這條路徑、降低 sandbox 權限或關閉既有 Codex 功能。

### 主系統工具迴圈

`POST /api/agents/run` 執行供應商無關、UI 內嵌的 observe → think → act 迴圈；`POST /api/agents/run/stream` 使用 NDJSON 依序傳送相同任務的活動與最終結果：

```text
使用者目標
  -> AgentOrchestrator
  -> 可替換 AgentDriver 在 UI Agent Runtime 內決定下一個工具
  -> StockAgentToolRegistry 在主系統內實際執行
  -> 結構化工具結果回到同一輪 Agent
  -> 最終判斷／下一步／可選的 Paper Broker 執行
```

即時市場資料與股票決策至少要取得一筆成功的 Stock AI 工具觀察，才會把 run 標示為 completed；不需外部事實的一般問答可以直接完成。每個工具名稱、參數、成功狀態與結果摘要都保存在回傳的 `tool_trace`。活動串流另外提供推理摘要、工具狀態、資料來源、技能、套件與排程標記；敏感參數會遮罩，且不會輸出模型私有的逐字 chain-of-thought。

任務分為 `general_answer`、`current_information`、`project_task`、`market_information` 與 `market_decision`。前四者的輸出 schema 強制 `decision=null`，不顯示股票 action、symbol 或 confidence；只有使用者明確詢問買賣、進出場或持有判斷時，才允許 `market_decision` 格式。即使 UI 帶有目前頁面的股票 context，一般問題也不會自動變成 `2330.TW` 決策。

穩定的一般知識不強制使用工具；即時、冷門或指定來源的問題必須取得可讀取的來源。`web.search` 只負責發現直接網址，不能單獨滿足完成條件；`web.research` 會以 DuckDuckGo 與 Bing 搜尋、解開跳轉網址、實際讀取多個頁面。若搜尋為空、頁面不可讀或內容缺欄位，Agent 會收到 policy feedback，必須改寫查詢或切換官方／其他來源後再回答。

### 原生工具與主系統工具並存

Codex 原有的終端機、瀏覽器、Skills、MCP 與 framework plugins 不會被縮減。原生 Codex 能力由受限的開發者診斷路徑保留；它不列為共享 Agent 的工具，因此一般 UI 任務不會被偷偷轉送成一段可見的 Codex 聊天。Codex Provider 以 ChatGPT 帳號登入的 App Server 建立 ephemeral 結構化 turn，只回傳計畫與摘要，並立即由 Stock AI Runtime 執行。主系統工具提供的是跨未來 provider 一致、可稽核的共同操作面，而不是原生工具的上限。

預設模型提供者是 Codex；設定頁也能實際啟用 OpenAI-compatible API／本機 gateway 或外部 Agent Framework。三種 Provider 只負責結構化規劃，Host 工具迴圈、驗證、權限、風控、稽核與持久化完全相同。TradingAgents 與 FinRobot 目前仍可透過 run-scoped Codex bridge 使用已登入的 App Server，不需要外部 LLM API key。FinGPT 的非模型資料處理工具仍可用，本地 base model／LoRA executor 固定停用。

## 可執行工具

| 工具 | 實際能力 | 是否變更狀態 |
| --- | --- | --- |
| `market.scan_watchlist` | 執行真實 OpenStockAIEngine watchlist pipeline | 否 |
| `market.analyze_symbol` | 執行資料、情報、策略、研究與中央風控 | 否 |
| `market.research_pack` | 讀取驗證價格、技術特徵、事件與記憶 | 否 |
| `market.taifex_foreign_open_interest` | 直接讀取期交所 OpenAPI 最新外資期貨未平倉多空 | 否 |
| `portfolio.snapshot` | 讀取共享 SQLite Paper Account | 否 |
| `memory.learning` | 讀取評估回合、reward 與反思 | 否 |
| `paper.preview_order` | 以伺服器驗證價格與風控預覽精確委託 | 否 |
| `paper.submit_order` | 將已預覽的相同委託送入本機 Paper Broker | 是 |
| `paper.mark_to_market` | 更新模擬帳戶部位與掛單估值 | 是 |
| `system.capabilities` | 讀取權威工具 manifest 與固定邊界 | 否 |
| `external.tradingagents.model_capabilities` | 執行 TradingAgents 模型能力解析器 | 否 |
| `external.tradingagents.investment_decision` | 執行 TradingAgents 五級投資決策解析 | 否 |
| `external.fingpt.sentiment_consensus` | 執行 FinGPT 多來源情緒彙總 | 否 |
| `external.finrobot.report_quality` | 執行 FinRobot 報告品質檢查 | 否 |
| `external.finrobot.market_data` | 透過 FinRobot YFinanceUtils 讀取真實 OHLCV | 否 |
| `external.finrl.walk_forward_windows` | 執行 FinRL walk-forward 視窗產生器 | 否 |
| `external.finrl.simulate_environment` | 執行 FinRL StockTradingEnv reset/step 模擬 | 否 |
| `external.finrl_trading.information_ratio` | 執行 FinRL-Trading robust IR | 否 |
| `external.finrl_trading.regime_signals` | 執行 FinRL-Trading 慢速市況訊號 | 否 |
| `external.qlib.align_signals` | 執行 Qlib 索引訊號對齊 | 否 |
| `external.qlib.factor_dataset` | 執行 Qlib 因子／標籤資料集對齊與 IC | 否 |
| `external.ai_trader.variant_metrics` | 執行 AI-Trader 實驗變體統計 | 否 |
| `external.ai_trader.score_signal` | 執行 AI-Trader prediction extraction 與訊號品質評分 | 否 |
| `project.list_files` | 列出真實專案檔案，排除 runtime 與 Git internals | 否 |
| `project.search_text` | 使用 ripgrep 搜尋專案文字與定位行號 | 否 |
| `project.read_file` | 讀取專案檔案；本機密鑰檔不進入模型 context | 否 |
| `project.write_file` | 建立或覆寫專案文字檔 | 是 |
| `project.replace_text` | 精確取代唯一文字區塊 | 是 |
| `terminal.run` | 在專案內執行真實 zsh 命令並回傳輸出 | 是 |
| `web.fetch` | 讀取 HTTP／HTTPS 網頁、JSON API 或文字資源 | 否 |
| `web.search` | 公開網路搜尋並回傳可繼續讀取的網址 | 否 |
| `web.research` | 多搜尋供應商發現來源並實際讀取多個頁面 | 否 |

股市工具實作位於 `src/stock_ai/agent_tools.py`；通用操作工具位於 `src/stock_ai/agent_general_tools.py`；外部金融專案執行器位於 `src/stock_ai/external_project_tools.py` 與 `external_runtime_worker.py`；供應商無關合約與主控迴圈位於 `src/open_stock_ai/agent_runtime/`。外部工具在隔離 subprocess 載入 vendored source，結果必須包含實際檔案、函式與雜湊，且不得靜默 fallback。新工具以 provider 方式加入，不應把幾百個不具執行器的假名稱灌進 manifest。UI 或 prompt 不得複製執行規則。

外部核心工具的正常資料鏈是 `market.research_pack` → 一個或多個 `external.*` executor → Agent 最終決策。Agent 活動事件會顯示每次外部工具的名稱、參數、skill、package、成功結果與來源證據；TradingAgents／FinRobot 的內部 Codex turn 另外顯示 SDK 生命週期與選用的框架工具。FinRL、Qlib 與 AI-Trader 使用隔離 process；AI-Trader 只寫一次性 SQLite。FinGPT 本地模型目前由政策停用，主系統不會把保留入口回報成已執行。

## Agent 驅動器

`config/agent_runtime.yaml` 預設使用 `codex`。它以已登入的 ChatGPT 帳號執行 App Server 結構化 turn，不需要 API key，也不會把 UI 任務回傳到 Codex 對話。`openai-compatible` 直接呼叫已設定的 Chat Completions endpoint；`external-agent` 直接使用 `open_stock_ai.provider_turn.v1`。`local` 透過相容 gateway，Anthropic／Gemini 則透過相容 gateway 或外部 Agent adapter 接入。FinGPT 只保留非模型的來源／資料轉換契約；本地 base model 與 LoRA executor 固定停用。

設定頁可選擇三個可執行驅動器、設定端點／模型、測試連線並保存預設操作員。Provider 密鑰保存於 macOS Keychain 或後端環境，不進設定 JSON，也不會由 API 讀回。頂部常駐對話列接收任務，首頁工作台顯示自主層級與完整活動時間軸。

Codex 每一輪會收到目標、精簡但完整的 Stock AI 工具 schema、過往結構化 observation 與最終輸出 schema，並回傳：

```json
{
  "state": "continue",
  "summary": "先讀取個股和帳戶",
  "tool_calls": [
    {"id": "call-1", "name": "market.analyze_symbol", "arguments": "{\"symbol\":\"2330.TW\"}"},
    {"id": "call-2", "name": "portfolio.snapshot", "arguments": "{}"}
  ],
  "decision": null
}
```

`arguments` 使用 JSON object 的字串編碼，以相容嚴格結構化輸出；主系統會解析並再次驗證它必須是 object。取得足夠 observation 後，Codex Provider 回傳 `state=complete`、空的 `tool_calls` 與最終 `decision`。它不能自行宣稱工具已成功；只有主系統執行結果有效。

## 單一帳戶真相

首頁、自主 Agent、模擬交易頁面與既有 Codex context 全部讀取相同 SQLite Paper Account。現金、持倉、掛單、成交、reward 與反思由 Paper Broker／storage 層保存，UI 展示卡與模型 transcript 都不是第二套帳本。

既有工作區 API 仍保留：

```text
GET  /api/open-stock-ai/agent/trading/session
POST /api/open-stock-ai/agent/trading/account/reset
POST /api/open-stock-ai/agent/trading/analyze
POST /api/open-stock-ai/agent/trading/control
```

新的供應商無關 API：

```text
GET  /api/agents
GET  /api/agents/tools
GET  /api/agents/settings
POST /api/agents/settings
POST /api/agents/run
```

## 不可繞過的執行邊界

- `advisory` 可讀市場、專案與外部網站，也可預覽委託，但不可改檔、跑終端機或改變 Paper Account。
- `project_execute` 開放終端機、專案寫入與原生 Codex 委派，但不開放 Paper Broker 送單。
- `paper_execute` 只開放本機模擬交易；每筆送單必須先在同一 run 預覽完全相同的委託內容。
- `full_execute` 同時開放專案操作與模擬交易，仍不包含真實券商交易。
- 行情、持倉、成交與研究結果必須來自主系統工具，不可由 Agent 虛構。
- RiskEngine、Paper Broker、現金與庫存檢查留在主系統，任何 Agent framework 都不能繞過。
- 真實券商下單固定不存在；`live_execution_count` 永遠為 0。

這些規則的目的不是削弱 Codex，而是把模型原本缺少的「可驗證實際操作」補齊，同時讓執行結果不依賴某一個模型供應商。
