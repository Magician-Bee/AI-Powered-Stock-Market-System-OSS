# UI Contract V1 現況差距稽核

稽核基準：`stock_ai.ui_contract.v1`
起始 commit：`b3bf95d411a011498dc39ffd97da7996459b41cd`

本文件記錄上述起始 commit 的差距，不等同目前工作樹。自該基線後，本輪已建立 32 個固定根頁面、正式深層路由、Workspace Context V2、UI Action Registry/API，並完成全部固定路由的 Playwright 點擊驗收；尚存的內部相容層與完整 Action E2E 仍以正式契約的 Completion Gate 為準。

## 結論

起始版本不是最終 UI。它只完成六工作區外殼、部分第二層導航、首頁決策工作區、可操作 K 線及常駐 Agent Dock，仍是遷移期架構。

## 直接反證

| 契約要求 | 起始現況證據 | 起始判定 |
| --- | --- | --- |
| 32 個固定 DOM 頁 | `index.html` 只有少量 `workspace-panel`，且多個 Tab 共用同一節點 | 未完成 |
| 不得使用過渡 Route Panel | `navigation.js` 仍有 `routePanel()`、`renderRoute()` | 未完成 |
| 固定 V1 Tab ID | Manifest 仍使用多個舊 ID | 未完成 |
| Context V2 | 前後端都是 `stock_ai.workspace_context.v1` | 未完成 |
| 不依賴舊 state | 多處仍讀取 `state.symbol`、`activeAgentRunId` | 未完成 |
| 不由 DOM 反查 Route | Agent Bridge、Context Bar、Trading Workspace 仍查 `.view.active` | 未完成 |
| UI Action Registry | 現有 UI Provider 是少數硬編碼分支，沒有完整 Registry | 未完成 |
| 正式深層 Route | 尚未完整支援 V1 hash route 與前進／後退 | 未完成 |
| 每頁真實 Loader | 多個頁籤仍依賴通用卡片或舊 DOM | 未完成 |
| 所有 E2E | 有 Agent／Paper E2E，但未覆蓋 32 頁及所有主要 Action | 未完成 |

## 已可保留的基礎

- 六個左側工作區順序正確。
- Agent Dock 可在所有工作區收合與重新開啟。
- Agent 設定可導向系統的模型區段。
- 首頁與市場快照具備規則／模型分離的部分資料契約。
- K 線具備縮放、圖型、指標開關及標註的可用基礎。
- Durable Agent Runtime、PlanGraph、Approval、Artifact 與 SQLite UI Bridge 已存在，可升級而不另建假 Runtime。

本稽核會隨各 Phase 完成更新；只有最終驗收全部通過才改為「已完成」。
