# Managed runtime 停止與無模型背景維護驗收

日期：2026-09-07。工作分支：`codex/fix-managed-runtime-stop-20260907-1047`。

本批承接尚未推送的 `8757cc83fcefab0eb18eff10949a0d8163e930ee`，完成停止腳本的 managed `service-source` 所有權辨識、實際程序測試及版本驗證入口權限修復。新增背景暫停環境設定，供目前禁止呼叫模型期間進行桌面驗收；不是完整模型 API 封鎖模式。

## 本地證據

- 以唯一 Desktop 專案的 `./停止股市AI系統.command` 停止既有服務；`lsof -nP -iTCP:8000 -sTCP:LISTEN` 與 Uvicorn 程序查找均沒有結果。
- 以 `STOCK_AI_AGENT_BACKGROUND_PAUSED=1 ./開啟股市AI系統.command` 啟動 `7f20a6007e23`。程序 PID 71560、port 8000，工作目錄為本專案的 Application Support runtime `service-source`。
- `./驗證目前執行版本.command` 通過：commit、instance、完整 managed Python source mirror 與 UI 檔案雜湊一致。首次直接執行發現 `.command` 缺少 executable bit，修復為 Git mode `100755`，新增 POSIX 權限回歸測試。
- 原生 App `Stock AI Liquid Glass` 經 Computer Use 可見操作：首頁、Dock 收合／重新展開、個股完整圖表、折線切换蠟燭 K 線、寬視窗下均線／成交量／MACD、系統 → 介面與一般、窄視窗 Dock 收合／展開，再返回首頁。
- 本機截圖與版本紀錄保存在 `output/acceptance/20260907-managed-runtime-stop/`，不提交截圖或 runtime 資料到 Git。
- 驗收後唯讀 SQL 顯示當日新 Run 為 0；當日 `model.*`、`provider.*`、`run.started` 事件為 0。原有 Run 狀態數量保持不變（completed 142、cancelled 25、max_steps_reached 43、partially_completed 12、suspended 1，其餘等待狀態 38）。畫面中的紙上交易／Critic 是既有歷史，不是本次執行。
- 使用者原有背景圖刪除、`uv.lock` 修改、`Users/` 與協作偏好文件均保留且未加入提交；本地驗收涵蓋此工作區原有 overlay。

## 自動化驗證

`bash -n stop-stock-ai.sh open-stock-ai.sh` 通過。

首輪 launcher、portable launcher、storage health、真實程序隔離與 durable runtime 合計 98 passed。擴充 PID 缺失測試後程序測試 6 passed；新增權限測試後 launcher 測試 12 passed。所有測試使用替身或暫存 SQLite，沒有呼叫模型。

程序測試實際建立三個 loopback listener，涵蓋 Desktop／managed cwd、正確／過期／缺失 PID：只有所屬 Stock AI 程序被停止，其他副本與同目錄其他 App 均存活，所屬 port 釋放。

## 未通過與保留項目

本批僅完成 launcher 啟停與基本導覽驗收，不宣告完整 UI 驗收通過。窄視窗展開 Dock 時仍可見原生搜尋列／頁名重疊，以及設定主題卡片超出中央工作區；見 `07-narrow-settings.jpg`。收合 Dock 後這些區域可正常操作；仍需後续 UI 批次修復。未做手機寬度完整驗收、模型推論、正式外部資料、真實帳戶或實盤驗證。

Release gate 回讀仍為 `passed=false`，68 complete / 56 partial；九項 P0 外部證據阻塞不變。本批未修改需求完成標記，也未執行資料庫壓縮或刪除使用者資料。
