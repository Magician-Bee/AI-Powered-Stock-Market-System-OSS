# P72 Failure recovery policy 模組拆分驗收（2026-09-09）

## 變更與邊界

- Branch：`codex/refactor-p72-recovery-policy-20260909-0551`
- Candidate commit：`e321eb61a70a`
- 新增 `repair/recovery_policy.py`，統一持有 provider PlanPatch 修復、可重用 observation 判定、重複失敗攔截、替代工具選擇、recovery tool surface、Host recovery arguments、recovery link、coverage 與未解決失敗節點判定。
- 共抽離 19 個函式；`orchestrator.py` 保留相容 import，呼叫端與測試 import 不變，檔案由 10,143 行降至 9,388 行。
- 模組邊界測試禁止 recovery policy 回流 orchestrator，並將 coordinator 上限鎖在 9,400 行以下。

這批只移動既有 Host-owned deterministic policy，沒有放寬工具能力、重試次數或完成條件。recovery 仍只會從目前 manifest 選擇獨立、非 mutating、無批准要求的替代工具；不同股票、`example.com` 或未綁定失敗節點的成功結果不能消除原失敗。

## 自動化測試

- 定向套件：`204 passed`。
- 完整套件：`1928 passed, 5 skipped, 3 warnings`，耗時 238.84 秒。
- 既有測試持續驗證未知 capability 移除、相同 call id 不誤重用、不同來源 recovery、同標的 scope、重複 recovery link 防止、無關網站不可解除 market failure 與 Completion Gate coverage。
- `tests/test_agent_runtime_module_boundaries.py` 新增 repair/recovery 單一模組責任與 orchestrator 行數上限。

全部測試使用本機 deterministic fixture；未連線、啟動或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、確認 port 8000 已清除、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。版本驗證為 exact candidate `e321eb61a70a`、PID `80665`、port `8000`、managed runtime source，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」完成：

1. 確認首頁、40 / 648 有界市場清單、圖表與既有 Agent Dock 正常恢復。
2. 只讀開啟 Agent Dock 的「任務」分頁，確認已終止歷史 Run 的 Task Forest、Agent 反思與 Plan DAG 可正常投影。
3. 開啟「系統 → 介面與一般」，確認一般設定資料可載入。
4. 以標準視窗與 1,920px 全螢幕檢查首頁、設定頁與 Dock；全螢幕截圖保留 Task Forest 與 DAG 驗收證據。

全程未點擊 Agent Run 控制、未建立／暫停／恢復／取消 Run、未輸入或傳送訊息，也未測試 provider 連線或呼叫模型。

P72 Failure recovery policy 原生桌面驗收（未隨公開版提供；原參考：`native-p72-recovery-policy-20260909.png`）

截圖原始尺寸：3,840 × 2,410；SHA-256：`546666d361f52b8de1b900cade3c0dfda582675c6a4c4d1483618386bbf12712`。

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

P72 尚未宣告完整完成：interaction persistence、provider transcript 與 failure recovery 已獨立；orchestrator 仍包含 completion、paper protocol、evidence projection 與大型 run-loop 協調責任，後續批次繼續拆分。
