# Market Radar Runtime

首頁 Market Radar 是 Durable Agent Runtime 的正式多股票任務，不是前端以規則資料拼出的假模型卡片。

## 成功契約

只有下列條件同時成立，API 才回傳 `market_radar_status=succeeded`：

1. durable run 外層狀態為 `completed`
2. Agent 內層 `result.status` 為 `completed`
3. Host `completion_validation.passed` 為 `true`
4. 至少一筆真實模型 invocation receipt 為 `succeeded`
5. `structured_result` 通過 `MarketRadarResult` schema、Universe 完整性與 Host evidence 驗證

`max_steps_reached`、缺少 receipt、未知 evidence、漏掉 Universe 股票或 schema 錯誤都會顯示失敗；規則分析不會被升格為模型輸出。尚在執行的 run 顯示 `ANALYZING`，不會因前端輪詢期限而改標為模型失敗。

## 結構化結果

`stock_ai.market_radar_result.v1` 必須包含：

- 每檔股票唯一的 `items` 卡片
- `buy_now`、`sell_now`、`wait_to_buy`、`wait_to_sell`、`hold` 分組
- 每檔下一步、時機與觸發條件
- 可追溯至 Host tool trace 的 observations 與 evidence IDs
- 分離的 rule analysis、model analysis 與 Host risk evaluation
- 由 Host 依真實 receipt 覆寫的 Provider、Model、call ID 與 Universe provenance

Provider-facing schema 會展開 `$ref`、封閉每一層 object、補齊 strict required，確保 Codex／OpenAI strict structured output 可直接接受；Host 仍以 Pydantic 契約重新驗證回傳內容。

## Universe

首頁可選：

- 目前明確選股
- 使用者自選清單
- Paper OMS 持倉
- TWSE／TPEx 官方成交量排名
- 指定產業
- 已保存 Workflow 參數
- 既有 Agent tool result

`user_watchlist`、`portfolio_positions`、`workflow_parameters` 與 `tool_discovered` 都讀取真實持久化來源。來源沒有股票時回傳 `NO UNIVERSE`，不注入固定股票或預設清單。

## Provider 與驗證

每個 Provider 先經實際 conformance probe，再由 Host 選擇 Universal 或 Advanced protocol。Context Broker 會依 task kind 與步驟逐步揭露實際 `AgentTurnInput.tools`；模型可提出 routing correction，但不能解除 Host 的負面限制或安全政策。

`/api/query` 與首頁共用同一條 durable runtime、router、驗證器與 receipt 規則。選股問題沒有明確代號時，先由正式 Universe Resolver 解析官方成交量 Universe，不再呼叫空清單 legacy screener。

## 驗證入口

```text
POST /api/agents/market-radar/runs
GET  /api/agents/market-radar/runs/{run_id}
GET  /api/agents/market-radar/universe-options
POST /api/agents/providers/{provider_id}/conformance
```

CI 會執行全套測試、neutrality guard 與 Codex／OpenAI-compatible／External Agent conformance E2E；首頁另以實際 Browser 操作多股票 Universe、執行中狀態與模型卡片。
