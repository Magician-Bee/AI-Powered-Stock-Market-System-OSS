# Stock AI V1 工作區遷移 Ledger

契約：[`stock_ai.ui_contract.v1`](./ui-contract-v1.md)
原則：每個舊功能只有一個正式位置；「有 Tab 名稱」不代表遷移完成。

本表保留的是改造起始基線，用來追蹤舊功能原本落點；目前工作樹已建立 32 個固定根頁面、正式深層路由、Workspace Context V2 與 UI Action Registry/API，並移除 `routePanel()`、`renderRoute()` 與舊 Route fallback。仍有部分功能內部沿用 `state.symbol` 相容層與既有功能元件，因此整份 V1 完成門檻尚未宣告通過。

| 舊功能 | 最終位置 | Route | 起始狀態 |
| --- | --- | --- | --- |
| 首頁 | 首頁 AI 全市場決策中心 | `#/home` | 遷移中 |
| 市場總覽 | 市場 → 總覽 | `#/market/overview` | 遷移中 |
| 市場排行 | 市場 → 排行 | `#/market/rankings` | 過渡 Route Panel，未完成 |
| 自選清單 | 市場 → 自選與提醒 | `#/market/watchlists` | 舊 DOM，未完成 |
| 即時監控 | 市場 → 即時監控 | `#/market/monitor` | 舊 DOM，未完成 |
| 條件選股 | 市場 → 條件選股 | `#/market/screener` | 舊 DOM，未完成 |
| 新聞中心 | 市場 → 新聞與事件；個股 → 新聞與事件 | `#/market/events`、`#/instrument/{symbol}/events` | 舊 DOM／過渡 Panel，未完成 |
| 通知中心 | 市場 → 自選與提醒；系統 → 工具與整合 | `#/market/watchlists`、`#/system/tools` | 舊 DOM，未完成 |
| 個股首頁 | 個股 → 總覽 | `#/instrument/{symbol}/overview` | 遷移中 |
| 完整 K 線 | 個股 → 圖表 | `#/instrument/{symbol}/chart` | 可操作，但未收斂共用 Chart Core |
| 技術分析 | 個股 → 技術 | `#/instrument/{symbol}/technical` | 過渡 Route Panel，未完成 |
| 籌碼追蹤 | 個股 → 籌碼 | `#/instrument/{symbol}/ownership` | 舊 DOM／舊 Tab ID，未完成 |
| 基本面中心 | 個股 → 財務 | `#/instrument/{symbol}/financials` | 共用舊 DOM，未完成 |
| 估值 | 個股 → 估值 | `#/instrument/{symbol}/valuation` | 過渡 Panel／共用舊 DOM，未完成 |
| 風險控管 | 個股 → 風險與證據；投資組合 → 組合風險 | `#/instrument/{symbol}/evidence`、`#/portfolio/{account_id}/risk` | 分散舊 DOM，未完成 |
| 交易預覽 | 投資組合 → 委託與成交 | `#/portfolio/{account_id}/orders` | 舊 View，未完成 |
| 資產部位 | 投資組合 → 總覽／持倉與建議 | `#/portfolio/{account_id}/overview`、`positions` | 一個 DOM 支援多舊 Tab，未完成 |
| 模擬交易 | 投資組合 → 模擬帳戶 | `#/portfolio/{account_id}/simulation` | 共用舊 DOM，未完成 |
| 績效 | 投資組合 → 績效 | `#/portfolio/{account_id}/performance` | 過渡 Panel，未完成 |
| 量化交易參考 | 研究 → 策略與回測 | `#/research/strategy-lab` | 舊 DOM，未完成 |
| 量化策略研究 | 研究 → 策略與回測／因子研究 | `#/research/strategy-lab`、`factor-lab` | 舊 DOM／過渡 Panel，未完成 |
| 關聯推演 | 研究 → 關聯分析 | `#/research/relationships` | 舊 DOM，未完成 |
| 研究問答 | Agent Dock；研究 → AI 深度研究 | `#/research/deep-research` | 獨立入口已移除；深度研究頁未完成 |
| 研究產物 | 研究 → 研究報告 | `#/research/reports` | 過渡 Panel，未完成 |
| 證券資料庫 | 系統 → 證券主檔 | `#/system/securities` | 舊 DOM，未完成 |
| 資料目錄 | 系統 → 資料平台 | `#/system/data-platform` | 舊 DOM／舊 Tab ID，未完成 |
| 券商與 API | 系統 → 券商與連線 | `#/system/brokers` | 舊設定區段，未完成 |
| Agent 與模型 | 系統 → Agent 與模型 | `#/system/agent-models` | 可用，但仍使用舊 Tab ID |
| Skills／MCP／通知 | 系統 → 工具與整合 | `#/system/tools` | 舊設定區段，未完成 |
| 主題與一般設定 | 系統 → 介面與一般 | `#/system/interface` | 可用，但仍使用舊 Tab ID |

## 起始基線的過渡債務

- 起始版本曾使用 `LEGACY_VIEW_ROUTES`、`ROUTE_COPY`、`routePanel()`、`renderRoute()`；本輪已移除。
- 起始版本的 Tab ID 混用 `watchlist`、`filters`、`news`、`chips`、`holdings`、`advice`、`trading`、`simulated`、`models`、`strategy`、`factors`、`relations`、`sources`、`connections`、`agent`、`skills`、`settings`；本輪 Manifest 已固定為 V1 Tab ID。
- 起始版本多個正式 Tab 共用同一舊 DOM 根元件；本輪已建立 32 個固定根元件，但部分 Loader 仍呼叫既有功能模組。
- 起始版本前端 Context 是 V1；本輪已升級公開 Workspace Context V2，但功能內部仍有 `state.symbol` 相容層待逐步收斂。
- 起始版本沒有完整 UI Action Registry 與 `/api/ui/*` Contract API；本輪已建立契約與執行入口，後續仍需擴充全部高風險動作的端到端批准驗收。

目前剩餘門檻是移除功能內部相容 state、補齊所有主要 Action 的完整 E2E，以及完成響應式與 Accessibility 全矩陣；在這些門檻清除前不得宣告整份 V1 UI 最終完成。
