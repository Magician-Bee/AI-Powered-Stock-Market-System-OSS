# P72 Provider transcript 模組拆分驗收（2026-09-09）

## 變更與邊界

- Branch：`codex/refactor-p72-provider-transcript-20260908-2113`
- Candidate commit：`5a7160edca16`
- 新增 `providers/transcript.py`，統一持有送往 provider 前的對話投影、Branch Result 壓縮、工具結果限量、敏感欄位遮罩與 provider-neutral token 預估。
- `orchestrator.py` 保留相容 import，既有呼叫點與測試 import 不變；檔案由 10,414 行降至 10,143 行。
- 模組邊界測試禁止上述函式重新定義於 orchestrator，並把 coordinator 上限鎖在 10,200 行以下。

完整 audit trace 仍留在 durable store。送往 provider 的 projection 最多保留最近 8 筆對話、12 筆工具結果、20 個 evidence id；credential-like 欄位會在複製後遞迴遮罩，原始輸入不會被修改。這批沒有更動資料表、API、模型設定、Run 狀態或工具權限。

## 自動化測試

- 定向套件：`206 passed`。
- 完整套件：`1927 passed, 5 skipped, 3 warnings`，耗時 200.09 秒。
- 新增 `tests/test_provider_transcript.py`，驗證巢狀憑證遮罩、歷史投影界線、工具結果與 evidence 限量、原始資料不變及 token 預估。
- `tests/test_agent_runtime_module_boundaries.py` 驗證 provider projection 的單一模組責任與 orchestrator 行數上限。

所有測試均使用本機 deterministic fixture；未連線、啟動或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。停止後確認 port 8000 與 managed service process 均已清除；重啟後版本驗證為 exact candidate `5a7160edca16`、PID `73001`、port `8000`、managed runtime source，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」完成：

1. 確認首頁、40 / 648 有界市場清單、圖表與既有 Agent Dock 正常載入。
2. 開啟「個股 → 圖表」，確認 2887.TW K 線、成交量、MACD、資料時間與來源標記正常顯示。
3. 由唯一功能列入口收合 Agent Dock，再由同一入口重新展開。
4. 開啟「系統 → 資料平台」與既有「介面與一般」設定入口，確認設定資料正常載入。
5. 在 1,188px 標準視窗與 1,920px 全螢幕寬視窗檢查首頁、設定頁與 Dock；沒有遮擋、按鈕重疊或低對比黑塊。

全程未點擊 Agent Run 控制、未輸入或傳送 Agent 訊息、未測試 provider 連線，也未呼叫模型。

P72 Provider transcript 原生桌面驗收（未隨公開版提供；原參考：`native-p72-provider-transcript-20260909.png`）

截圖原始尺寸：3,840 × 2,410；SHA-256：`2b0814cbf5a379008c9ad6703b71564f579117c5f4b9aa14e0b29b0dde741af6`。

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

P72 尚未宣告完整完成：provider projection 已成為可獨立測試的模組，但 orchestrator 仍包含過多 recovery、completion、paper protocol 與 evidence projection 責任，後續批次繼續抽離。
