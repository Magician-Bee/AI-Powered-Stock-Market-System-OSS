# Stock AI UI Contract V1

狀態：凍結中（實作不得偏離本文件）
Schema：`stock_ai.ui_contract.v1`

本文件是 Stock AI 第一方 UI 的固定契約。它取代先前所有過渡導航、臨時頁面與舊選單遷移方案。功能可以在既有頁面內擴充，但工作區、Tab、Route、主要 UI ID、Action ID 與 Workspace Context 必須維持相容。

## 1. 架構邊界

```text
UI
→ stock_ai application / API / Agent Driver
→ open_stock_ai market / intelligence / strategy / research / risk / Paper OMS
→ SQLite / external data / controlled adapters
```

- UI 不計算買賣資格、風控批准、持倉餘額或交易真相。
- UI 與 Agent 只使用統一資料 API，不直接呼叫 Connector。
- 規則、模型、研究驗證、RiskEngine 與 Paper OMS 狀態分開呈現。
- 前端不得建立第二套 Agent Plan、Approval、Recovery 或 Artifact 系統。
- 正式交易保持不可用；Paper、唯讀、Sandbox 與 Live 必須明確區分。

## 2. 固定工作區與 Route

左側只有以下六個工作區，順序不可改：

1. `home`：首頁
2. `market`：市場
3. `instrument`：個股
4. `portfolio`：投資組合
5. `research`：研究
6. `system`：系統

首頁：

| Route | UI ID | 責任 |
| --- | --- | --- |
| `#/home` | `home.root` | AI 全市場決策中心；不顯示第二層 Tab |

市場：

| Tab | Route | UI ID | 責任 |
| --- | --- | --- | --- |
| `overview` | `#/market/overview` | `market.overview.root` | 市場環境、指數、寬度、產業與資金 |
| `rankings` | `#/market/rankings` | `market.rankings.root` | 市場排行與排序 |
| `watchlists` | `#/market/watchlists` | `market.watchlists.root` | 自選群組與提醒 |
| `screener` | `#/market/screener` | `market.screener.root` | 條件建構、篩選與儲存 |
| `monitor` | `#/market/monitor` | `market.monitor.root` | 即時行情、異常及觸發 |
| `events` | `#/market/events` | `market.events.root` | 市場、產業、公司及總經事件 |

個股：

| Tab | Route | UI ID | 責任 |
| --- | --- | --- | --- |
| `overview` | `#/instrument/{symbol}/overview` | `instrument.overview.root` | 單一股票決策摘要 |
| `chart` | `#/instrument/{symbol}/chart` | `instrument.chart.root` | 完整 K 線工作台 |
| `technical` | `#/instrument/{symbol}/technical` | `instrument.technical.root` | 指標、訊號與技術統計 |
| `ownership` | `#/instrument/{symbol}/ownership` | `instrument.ownership.root` | 法人、融資券、當沖與集保 |
| `financials` | `#/instrument/{symbol}/financials` | `instrument.financials.root` | 營收、三表、比率與展望 |
| `valuation` | `#/instrument/{symbol}/valuation` | `instrument.valuation.root` | 歷史估值、同業與 DCF |
| `events` | `#/instrument/{symbol}/events` | `instrument.events.root` | 公告、新聞與公司事件 |
| `evidence` | `#/instrument/{symbol}/evidence` | `instrument.evidence.root` | 風險、資料品質與 Receipt |

投資組合：

| Tab | Route | UI ID | 責任 |
| --- | --- | --- | --- |
| `overview` | `#/portfolio/{account_id}/overview` | `portfolio.overview.root` | 資產與組合狀態 |
| `positions` | `#/portfolio/{account_id}/positions` | `portfolio.positions.root` | 持倉與加減碼建議 |
| `orders` | `#/portfolio/{account_id}/orders` | `portfolio.orders.root` | 預覽、批准、委託與成交 |
| `simulation` | `#/portfolio/{account_id}/simulation` | `portfolio.simulation.root` | Paper Account 與訓練回合 |
| `performance` | `#/portfolio/{account_id}/performance` | `portfolio.performance.root` | 報酬、回撤與歸因 |
| `risk` | `#/portfolio/{account_id}/risk` | `portfolio.risk.root` | 集中度、曝險與限制 |

研究：

| Tab | Route | UI ID | 責任 |
| --- | --- | --- | --- |
| `compare` | `#/research/compare` | `research.compare.root` | 2–6 檔股票共同基準比較 |
| `deep-research` | `#/research/deep-research` | `research.deep-research.root` | 真正 Agent Research Run |
| `strategy-lab` | `#/research/strategy-lab` | `research.strategy-lab.root` | 策略建立與 point-in-time 回測 |
| `factor-lab` | `#/research/factor-lab` | `research.factor-lab.root` | 因子有效性、衰減與關聯 |
| `relationships` | `#/research/relationships` | `research.relationships.root` | 供應鏈與跨市場關聯 |
| `reports` | `#/research/reports` | `research.reports.root` | 研究與 Agent Artifact |

系統：

| Tab | Route | UI ID | 責任 |
| --- | --- | --- | --- |
| `securities` | `#/system/securities` | `system.securities.root` | 證券主檔與生命週期 |
| `data-platform` | `#/system/data-platform` | `system.data-platform.root` | 來源、品質、快取、血緣及匯入 |
| `brokers` | `#/system/brokers` | `system.brokers.root` | 五券商能力、連線與授權 |
| `agent-models` | `#/system/agent-models` | `system.agent-models.root` | Runtime、Provider、模型與自主權 |
| `tools` | `#/system/tools` | `system.tools.root` | Skills、MCP、外部框架與通知 |
| `interface` | `#/system/interface` | `system.interface.root` | 外觀、密度、語言與無障礙 |

正式子頁共 32 個。每頁必須是固定 DOM 根元件，不得以 `routePanel()`、空卡、靜態 Mock 或永遠 Loading 代替。

## 3. 固定 Layout Token

```css
--sidebar-width-expanded: 164px;
--sidebar-width-collapsed: 64px;
--agent-dock-width: 360px;
--agent-dock-min-width: 320px;
--agent-dock-max-width: 520px;
--topbar-height: 72px;
--workspace-tabs-height: 42px;
--workspace-gap: 14px;
--panel-radius: 12px;
--control-height: 34px;
```

左側導覽下方固定只有「開啟／收合 Agent」與「Agent 控制台」兩個 Agent 入口，再接資料服務健康摘要。「開啟／收合 Agent」只控制右側 Dock 的可見性；「Agent 控制台」只切換 Dock 的一般／最大化工作區模式。頂部列固定包含工作區名稱、說明、全域 Agent 指令、傳送及 Provider／模型／連線狀態。

## 4. Workspace Context V2

公開真相只有 `stock_ai.workspace_context.v2`：

```json
{
  "schema_version": "stock_ai.workspace_context.v2",
  "revision": 1,
  "route": {"workspace": "instrument", "tab": "chart", "params": {"symbol": "6603.TWO"}},
  "selection": {
    "entity_id": null,
    "symbol": "6603.TWO",
    "entity_kind": "equity",
    "candidate_id": "6603.TWO",
    "universe_id": "all_taiwan_active",
    "explicit_intent_symbols": []
  },
  "chart": {
    "type": "candles",
    "timeframe": "1d",
    "range": "1y",
    "price_basis": "unadjusted",
    "indicators": ["MA5", "MA20", "MA60", "VOLUME", "MACD"],
    "focus_mode": false,
    "viewport": null
  },
  "comparison": {"symbols": []},
  "portfolio": {"account_id": "paper-default", "mode": "paper", "selected_position_id": null, "order_draft_id": null},
  "research": {"strategy_id": null, "report_id": null},
  "agent": {"session_id": null, "run_id": null, "dock_open": true, "dock_tab": "chat", "dock_width": 360, "maximized": false},
  "data": {"market_snapshot_id": null, "as_of": null, "freshness": {}, "quality": null},
  "layout": {"sidebar_collapsed": false, "density": "comfortable"}
}
```

畫面正在顯示的股票與使用者明確要求分析的股票必須分開。不得以 `state.symbol`、多輸入框同步或 `.view.active` 推測公開 Context。

## 5. UI Action Contract

Agent 只透過以下入口操作第一方 UI：

- `ui.contract.get`
- `ui.state.get`
- `ui.action.execute`
- `ui.state.wait`

每個 Action 必須包含 Action ID、Input／Output Schema、Target UI ID、Context、Risk、Permission、Approval、Timeout、Idempotency、Precondition、Postcondition、Rollback 及可見摘要。

固定 Action 分組：

- 導覽：`workspace.open`、`workspace.tab.open`、`entity.search`、`entity.select`、`route.back`、`route.forward`
- 市場：`market.snapshot.refresh`、`market.category.select`、`market.candidate.select`、`market.ranking.select`、`market.event.open`、`market.monitor.subscribe`、`market.monitor.unsubscribe`
- 自選／提醒／篩選：`watchlist.group.create`、`watchlist.group.rename`、`watchlist.symbol.add`、`watchlist.symbol.remove`、`watchlist.symbol.move`、`alert.create`、`alert.update`、`alert.delete`、`screener.condition.set`、`screener.run`、`screener.save`、`screener.schedule`
- 圖表：`chart.type.set`、`chart.timeframe.set`、`chart.range.set`、`chart.price_basis.set`、`chart.indicator.toggle`、`chart.indicator.configure`、`chart.drawing.create`、`chart.drawing.update`、`chart.drawing.delete`、`chart.annotation.undo`、`chart.annotation.clear`、`chart.viewport.reset`、`chart.focus.enter`、`chart.focus.exit`、`chart.event_layer.toggle`
- 投資組合：`portfolio.account.select`、`portfolio.position.select`、`portfolio.mark_to_market`、`order.draft.create`、`order.draft.update`、`order.preview`、`order.submit.paper`、`order.cancel.paper`、`simulation.account.reset`
- 研究：`research.compare.set`、`research.compare.run`、`research.deep_run.start`、`research.strategy.save`、`research.backtest.run`、`research.factor.run`、`research.relationship.run`、`research.report.open`、`research.report.export`
- 系統：`system.data.refresh`、`system.cache.invalidate`、`system.reconciliation.run`、`system.broker.authorize`、`system.broker.test`、`system.provider.select`、`system.provider.test`、`system.provider.save`、`system.tool.enable`、`system.tool.disable`、`system.tool.test`、`system.interface.update`、`system.interface.reset`
- Agent：`agent.dock.open`、`agent.dock.close`、`agent.dock.resize`、`agent.dock.maximize`、`agent.dock.restore`、`agent.tab.open`、`agent.session.new`、`agent.run.pause`、`agent.run.resume`、`agent.run.cancel`、`agent.approval.approve`、`agent.approval.deny`、`agent.artifact.open`

正式元件具備：

```html
data-ui-id="instrument.chart.range.1y"
data-ui-action="chart.range.set"
data-ui-scope="instrument"
data-ui-version="1"
aria-label="顯示一年 K 線"
```

## 6. 資料、決策與風控誠實性

- 市場分類只使用 `BUY_NOW`、`WATCH`、`FUTURE_BUY`、`AVOID_NOW`、`INSUFFICIENT_DATA`。
- 持倉行動只使用 `ADD`、`HOLD`、`REDUCE`、`EXIT`，不得污染市場分類。
- 模型狀態只使用 `NOT_ANALYZED`、`QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`STALE`。
- 沒有成功 Model Receipt 不顯示「AI 已驗證」。
- 沒有 Exact Order Risk 不顯示「已批准下單」。
- Paper Account 不顯示成真實券商帳戶。
- UI 狀態必須支援 `INITIAL`、`LOADING`、`READY`、`EMPTY`、`PARTIAL`、`STALE`、`OFFLINE`、`ERROR`、`BLOCKED`，資料失敗保留上一份成功內容。
- 系統不得預設偏好 `2330.TW`、`0050.TW` 或任何股票；`^TWII` 只能作為無股票 Context 的顯示基準。

## 7. Chart Contract

首頁與個股總覽使用精簡 Renderer；完整工作台只存在 `instrument/chart`。三者共用 `ChartDataService`、`ChartStore`、`ChartController`、`ChartRenderer` 與 `ChartAnnotationStore`，不得搬移 DOM、複製 Canvas 或重複請求。

完整工作台固定支援搜尋、價格／狀態、自選、提醒、比較、Paper 預覽；週期、圖型、價格基準、指標、事件、版面與全螢幕；九個區間、五種圖型、七個預設指標、九個畫圖操作；縮放、平移、OHLCV 十字線、Pane 收合／調高、按股票及週期保存與 Esc 退出聚焦。

## 8. Agent Dock Contract

- 桌面右側常駐 320–520px；中型覆蓋；小型全畫面；圖表聚焦時為窄 Rail。
- Agent Dock 顯示 Agent、Provider、模型、自主模式、連線、新對話、歷史與技術檢視；最大化／還原、設定與收合由左側導覽的唯一入口負責。
- Context 顯示工作區、Tab、顯示股票、明確任務股票、Universe、比較、Portfolio、Strategy、資料時間與 Snapshot ID。
- Chat、Tasks、Artifacts 直接使用既有 Runtime 資料；Tasks 渲染 PlanGraph，不顯示私密 chain-of-thought。
- 每個 UI Action 顯示目標、高亮、Action ID、Before、Result 及 Postcondition。
- Agent 收合後能從每個工作區重新開啟。

## 9. Completion Gate

只有下列全部成立才能標記 `stock_ai.ui_contract.v1` 完成：

- 32 個固定子頁根元件存在且有真實 Loader／Empty／Error 契約。
- `LEGACY_VIEW_ROUTES`、`ROUTE_COPY`、`routePanel()`、`renderRoute()`、`CONTEXT_SYMBOL_INPUT_IDS`、舊 alias、DOM 反查、舊公開 `state` 與硬編碼 Agent UI 分支全部移除。
- Context V2、Router、Store、UI Contract API、Action Registry、Acknowledgement、Postcondition 與 Approval 完成。
- 首頁、市場、個股、投資組合、研究、系統責任符合本文件。
- 單元、Schema、API、Playwright、Agent Action、Paper Order、Launcher、響應式、Accessibility 與 Screenshot 驗收通過。
- 分支與 GitHub `main` 已同步並從遠端再次確認。
