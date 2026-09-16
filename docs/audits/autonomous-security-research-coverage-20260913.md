# 逐檔、多領域自主研究覆蓋帳本

本輪推進 M2／任務 10，並支援任務 06–09 的缺口選擇。基準提交為 `495c0f59a5166644a937a4cfb355b69c8c15c33d`，工作分支為 `codex/add-security-research-coverage-ledger-20260913-1300`。本報告只驗收覆蓋狀態、查詢與稽核契約；它不把帳本列、fixture 資料或掃描結果當成全面分析、交易資格、正期望值或 M1 閉環。

## 修正的實際斷點

既有 `autonomous_deep_research_coverage` 只在代號被選入深入研究後建立一列，保存累積選取／成功次數與最後錯誤。它無法列出整個官方研究範圍、無法區分「曾成功」與「證據現在仍有效」，也沒有財務、營收、籌碼、事件等領域的來源與失效狀態。更重要的是，symbol-only 歷史在權證代碼重用或同碼多發行身分時不能證明屬於哪個 entity。

新 `SecurityResearchCoverageLedger` 接在既有 `AutonomousCampaign.research` 保存 cycle 之後，讀取同輪完整 `all_features`，不另做第二次掃描、模型呼叫或交易。每個目前研究範圍 entity 都有 account-scoped row；同碼歧義展開成各自的 immutable entity row，沒有 entity 的列使用明確 unresolved key。空範圍、同一 entity 重複出現、損壞的壓縮 payload 均 fail closed，且不取代上一份有效 current snapshot。較舊的並行掃描即使較晚完成，也不能覆寫更新的 current universe。

## 狀態與證據契約

帳本固定記錄 11 個領域：`identity`、`daily_price`、`intraday_price`、`order_book`、`price_history`、`financials`、`revenue`、`ownership_flows`、`news_events`、`industry`、`cross_market`。每個領域保存：

- `availability`: `ready`、`partial`、`unavailable` 或 `conflict`；
- `freshness`: `current`、`stale` 或 `unknown`；
- 實際來源、原因、evidence time、expiry、next update time 與 evidence reference；
- `needs_update`，另以 bitmask 支援不解壓全列的篩選。

深入研究另分為 `never_researched`、`current`、`stale`、`failed` 與 `not_attributed`。只有通過 retained evidence ID hash、cycle hash、來源 provenance、instrument identity、至少 62 根完成日線、嚴格時間順序、future-data 及資料 hash 檢查，而且 cycle 內帶有相同 entity 身分的歷史，才能歸屬給該 entity。舊 symbol-only 成功紀錄只會形成 `not_attributed`；新發行身分不會借用舊發行歷史。

目前未接入的 intraday、order book 與 cross-market 路徑逐檔保存為 `unavailable`。行情 partial、財務過期、新聞缺失與身分 conflict 都保持不同狀態；有一列不代表資料齊全。資料領域 JSON 使用 zlib 壓縮保存，每列及摘要都有 canonical JSON SHA-256。歷史 snapshot 保留，current row 只指向最新 official research universe。

## 模型、API 與畫面接線

`autonomy.coverage` 是無網路、無模型、無交易的 read-only tool，可依 symbols、deep status、domain、needs update、新增部位資格與 cursor 查詢。它在 research、decision 與 paper context 中優先保留；6,000 字元投影先保留最多 20 個 entity／symbol 狀態索引，再保留最多 4 列細節。模型提示要求先查 never／stale／failed 或指定領域缺項，再以原 `autonomy.research` 最多 20 檔按需補查。

HTTP `GET /agent/autonomy/coverage` 提供同一查詢；不合法 domain、limit 或缺少 domain 的 needs-update 請求回 422。自主紙上監控新增「逐檔覆蓋帳本」，顯示實際 denominator、可新增部位數、深入研究五態及 daily/history/financial/news 更新需求，並明示「有狀態不代表資料齊全或策略已驗證」。帳本尚未初始化時顯示下一次全市場研究才建立，不把 0 誤當成功。

## 獨立稽核與效能證據

`scripts/audit_autonomous_research_coverage.py` 只以 SQLite URI `mode=ro`、`query_only=on`、`BEGIN` 讀取，不使用 `immutable`，也不建立 production store。它解壓並驗證所有 current rows、11 領域 enum、needs-update bitmask、row hash、summary hash、snapshot ID、entity 唯一性、row cycle/evidence reference 及 snapshot source-cycle reference。測試各自竄改壓縮 blob、bitmask、row hash 或刪除 cycle，稽核必須非零失敗。

58,056 列的較完整合成特徵 fixture 在本機同步為 5.223581 秒；查詢 200 列 `news_events needs_update=false` 為 0.006737 秒；SQLite 為 69,070,848 bytes（65.871 MiB）；`/usr/bin/time -l` 最大 RSS 351,256,576 bytes、peak memory footprint 332,547,056 bytes。fixture 明確不含正式 HTTP 延遲、資料來源失敗、模型或交易，也不構成任務 08 的真實吞吐量驗收。

相關單元、provider、API、投影、UI、enrichment、研究 family、native assembly 及事件迴圈專項為 **102 passed**。最終完整回歸為 **3,438 passed、5 skipped、0 failed**；3 個 warning 都是既有 Starlette／websockets 相依套件的棄用訊息。Python compileall、JavaScript syntax check 與 `git diff --check` 通過。正式部署後 snapshot 數字、原生 UI 與只讀 production 稽核會另以發布收據核對。

## 尚未通過

任務 10 的核心狀態契約已接通，但正式帳本要等此版本部署後的下一輪全市場研究才有 production snapshot。任務 06 的 intraday、order book、cross-market 等實際來源仍缺，財務、營收、籌碼與新聞也尚非每檔 ready/current；因此 M2 整體沒有完成。M1 仍需在原模型、預算、日期與風險下自然產生真模型計畫，並以真行情完成進場、持倉、退出與對帳。M7 的 120 日／30 筆、M8 的明示實盤授權與 M9 其他市場均維持未通過。
