# P71 核心資料表升級與重啟驗收（2026-09-08）

## 驗收範圍

- Candidate branch：`codex/feat-p71-schema-contract-20260908-2032`
- Candidate commit：`6fca6edf8277`
- 規格：P71 核心資料表
- 限制：未操作 Agent Run、未送出 Agent 訊息、未呼叫本地或遠端模型。

## 後端契約

`src/open_stock_ai/storage/p71_schema.py` 將 P71 durable boundary 明列為 30 張資料表及 13 個必要索引。`apply_migrations` 完成後會以唯讀 schema inspection 驗證每張表的必要欄位；若資料庫只更新 `schema_migrations`／`user_version`，實體 schema 卻缺表、缺欄位或缺索引，啟動會以 `P71SchemaContractError` 失敗關閉並列出缺漏項目。

自動化升級 fixture 從 migration v1 逐版建立到 v37，寫入 Objective、Artifact、Automation Intent、Automation、Automation Version、Execution 與 Notification 的代表資料，再由正常 `SQLiteStore` 啟動路徑升級到 v47。驗證內容包括：

- migration 前建立完成且可驗證的備份收據，`from_version=37`、`to_version=47`；
- migration 38 將舊 `payload_json` 回填到正式欄位；
- Objective、Artifact、Automation 正式 Store 在升級後可回讀相同資料；
- 第二次啟動保持冪等；
- `pragma foreign_key_check` 無違規，`pragma quick_check` 回傳 `ok`；
- 最新版本但缺必要索引的資料庫會 fail-closed。

## managed runtime 與原生 App

依指定流程執行：

1. `./停止股市AI系統.command`
2. `./開啟股市AI系統.command`
3. `./驗證目前執行版本.command`

版本腳本確認服務 PID `44785`、port `8000`、recorded commit 與 URL commit 均為 `6fca6edf8277`，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。實際服務主資料庫 `output/open_stock_ai.sqlite` 以唯讀方式回查為 `user_version=47`、47 筆 migration，P71 契約回報 `valid=true`、30 張表、13 個索引。

接著以 Computer Use 操作原生 App「股市AI系統 Agent測試版」，從首頁點選「系統」，進入「資料平台」。畫面成功載入 MarketDataPlatform、來源註冊、Unified Data API、62,983 筆標準紀錄、118,643 個 revision 與 Data Quality 等狀態；右側 Agent Dock 保持可見，但沒有操作 Run、輸入框、傳送、provider 或模型控制。

P71 原生桌面資料平台重啟驗收（未隨公開版提供；原參考：`native-p71-schema-restart-20260908.png`）

截圖 SHA-256：`3c2c2ddbdf3fcaefbc9d46733f895290f831f46b0593e63a4cf74fff76daa41a`

## 自動化結果

- P71 定向測試：`3 passed`
- migration、runtime v2、Objective、Automation 與 Notification 相關測試：`137 passed`
- 完整測試套件：`1922 passed, 5 skipped`。首次執行時 README 自動產生能力區塊仍顯示舊的 `83/124`；本批由權威 YAML 重新產生為 `85/124` 後，完整套件重新執行並全數通過。

完整 release gate 維持 fail-closed：`passed=false`、85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

外部帳戶、券商、付費資料或模型均不屬於 P71 本批驗收，也沒有以 mock 宣稱任何外部整合完成。
