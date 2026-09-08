# Agent 儲存維護防線

`AgentDatabaseMaintenance.storage_report()` 與既有 `/api/agents/storage` 回傳唯讀 metadata、未處理事件數及 `disk_space`。啟動檢查與 preflight 都以 SQLite `mode=ro` 開啟來源；路徑不存在時回報阻擋，不會建立空資料庫冒充成功。

## 空間估算與阻擋

預估資料庫大小取 `max(page_count × page_size, 主檔大小) + WAL 大小`。備份目的地需要一份預估大小；完整重寫額外預留兩份，並在每個檔案系統加入 64 MiB 安全餘量。同一磁碟上的用途必須相加，不可分別對同一份可用空間判斷通過。備份在不同磁碟時則逐一檢查。可透過建構參數 `reserve_bytes` 增加安全餘量；此值不是環境變數。

- `insufficient_disk_space`：至少一個目的磁碟低於預估所需空間。
- `storage_preflight_unavailable`：來源或檔案系統資訊無法讀取，保持阻擋。
- `runtime_events_pending`：仍有耐久事件尚未處理，不執行壓縮或 VACUUM。

這是操作前的保守估算，不是磁碟配額或空間保留。其他程序仍可能消耗空間；實際維護仍需停止服務、驗證來源與備份，並檢查操作後完整性及歷史回看。不要為了清空 pending 計數直接刪除事件。

## 備份完整性與保留

備份使用 UTC 微秒時間與 UUID，先用 exclusive create 建立權限 `0600` 的 `.sqlite.partial`。SQLite online backup 保留 WAL 中已提交內容；資料通過 `quick_check`、計算完整檔案 SHA-256 並 fsync 後才改為 `.sqlite`。

同名 `.receipt.json` 保存備份位置、時間、備份 SHA-256／大小、完整性結果與空間 preflight，權限也是 `0600`。完整性收據先 fsync，再執行既有保留政策（`backup(retain=N)`，至少兩份）。收據不提前宣稱刪除完成；回傳的 `deleted` 僅在刪除操作成功後返回。副本或收據建立失敗時不修剪既有備份。失敗 `.partial` 不計入已驗證世代，不會拿它取代前一份可用備份。

收據雜湊用於確認備份位元組，沒有簽章或防竄改儲存；正式異機還原、離站保存與長期保留仍需獨立驗收。

## Checkpoint 壓縮資格與資料列驗證

壓縮只接受 `completed` Run。`failed` 仍可從最新安全 checkpoint 執行 retry；`cancelled`、`interrupted`、所有 waiting、active、`partially_completed` 與 `max_steps_reached` Run 的 checkpoint 和 `checkpoint.created` event payload 均逐位元保留。已完成 Run 會保留最新 checkpoint 與 `agent_runs.checkpoint_id` 指向的 checkpoint；較舊的累積 transcript／trace 副本可刪除，而同 sequence 的事件仍保留 checkpoint ID、Run／Session、plan revision、狀態、時間與 snapshot hash 作為 audit reference。

寫入和刪除位於同一 SQLite transaction。提交前會比較 Run、event、checkpoint 與受保護 checkpoint 的資料列數；只有 Run／event 完全不減少、受保護 checkpoint 不變，且 checkpoint 減少數等於實際刪除數才提交。之後仍需通過完整 `quick_check`。這些本機防線不構成正式異機還原、離站保存或長期保留證據，因此 R-002、A-011、GOV-005 維持 partial。

## 2026-09-07 本機檢查

本次僅在真實 runtime 讀取 preflight：資料庫 4,758,396,928 bytes，當次可用磁碟 28,418,158,592 bytes，備份加重寫預估 14,342,299,648 bytes。空間充足，但有 30 筆 pending runtime events，因此維護保持 blocked；未執行真實資料庫備份、壓縮、VACUUM 或刪除。數字會隨正常桌面讀取／事件記錄改變。

低磁碟、分開磁碟、同一時間備份、WAL 與失敗情境全部使用暫存測試資料庫驗證，不能替代正式 runtime 還原演練。

原生桌面驗收以 `STOCK_AI_AGENT_BACKGROUND_PAUSED=1 ./開啟股市AI系統.command` 啟動 `0d03b576676f`，由 `./驗證目前執行版本.command` 核對 managed source。Computer Use 在原生 App 查看首頁與 Observability 的 4.43 GB／maintenance deferred 狀態，打開歷史並選取另一筆已完成 Session，確認 5/5 Plan、sequence 123 與原有紙上工具回執可回看；未送出新任務。截圖、版本與 served preflight 保存在 `output/acceptance/20260907-storage-preflight/`。

自動化驗證：storage health、SQLite backup 與 durable runtime 共 90 passed；最後收據欄位調整後 storage health 22 passed。完整 release gate 仍為 68 complete / 56 partial，沒有更改完成標記。

終止 Run checkpoint 資格、transaction 資料列防線與原生歷史回看證據見 [`../validation/terminal-run-compaction-20260907.zh-TW.md`](../validation/terminal-run-compaction-20260907.zh-TW.md)。
