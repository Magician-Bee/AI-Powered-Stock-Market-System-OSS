# 終止 Run checkpoint 壓縮與歷史回看驗收

日期：2026-09-07。工作分支：`codex/fix-terminal-run-compaction-20260907-1120`。

本批修正 storage maintenance 將 `failed` 與 `cancelled` Run 視為不可恢復並壓縮 checkpoint 的風險。耐久 runtime 的 `retry()` 會從失敗 Run 的最新安全 checkpoint 恢復，因此壓縮資格收斂為只有 `completed`。`failed`、`cancelled`、`interrupted`、所有 waiting／active、`partially_completed` 與 `max_steps_reached` Run 的 checkpoint 和對應 event payload 必須原樣保留。

## 資料完整性防線

- `completed` Run 仍保留最新 checkpoint 與 `agent_runs.checkpoint_id` 指向的 checkpoint；只移除較舊、可重建的重複 snapshot payload。
- 維護交易在同一 SQLite transaction 內比對 Run、event、checkpoint 與受保護 checkpoint 的資料列數。Run／event 不得減少，受保護 checkpoint 不得改變，checkpoint 減少量必須等於回報的刪除量，否則 rollback 並回傳 blocked。
- 已經壓縮的 event 不再重寫，避免重複維護改變既有 audit reference。
- 測試逐位元比較所有受保護狀態的 checkpoint 與 event serialisation，確認資料沒有被改寫。

## 自動化驗證

`python3 -m py_compile` 與 `git diff --check` 通過。

storage health、durable runtime、checkpoint、launcher 與 portable launcher 合計 112 passed。`uv run ruff` 因目前環境沒有可執行的 `ruff` 而無法啟動；這不是測試失敗。本批沒有安裝套件或修改 `uv.lock`，避免覆蓋使用者現有修改。

完整 release gate 仍為 `passed=false`，68 complete / 56 partial。本批只同步 A-011、R-002、GOV-005 的本機防線說明，維持 partial；沒有把外部驗證、正式還原或長期保留誤標為完成。

## 原生桌面驗收

以 `STOCK_AI_AGENT_BACKGROUND_PAUSED=1 ./開啟股市AI系統.command` 啟動 `72d9180886d4`，再由 `./驗證目前執行版本.command` 核對工作分支、managed `service-source`、recorded commit 與 served UI SHA，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在原生 App `Stock AI Liquid Glass` 開啟 Agent Session 歷史並切換至另一筆既有完成 Session。畫面可回看 Run `AR-429ac23d20634c0aac2cd1d049c76907` 的 sequence 71、3/3 Plan、Revision 2、`market.analyze_symbol`、`web.research` 工具結果與歷史訊息。Observability 顯示 runtime storage 4.43 GB、24.0 KB reclaimable、62 pending events 與 maintenance deferred，證明目前安全阻擋仍生效。

驗收期間背景 Agent 已暫停，沒有輸入或送出任務，也沒有點擊再次執行；未呼叫本地或遠端模型。沒有對真實 runtime 執行備份、checkpoint 壓縮、VACUUM 或刪除。截圖、AX 紀錄與版本輸出保存在 `output/acceptance/20260907-terminal-run-compaction/`，不提交 runtime 或驗收產物到 Git。

## 保留項目

本批驗證 checkpoint 保留契約與既有歷史可讀性，不構成正式異機還原、離站備份、長期 retention 或災難復原演練證據。真實 runtime 仍有 pending events，維護必須繼續保持 blocked，直到服務安全停止且 preflight、備份與完整性條件全數通過。
