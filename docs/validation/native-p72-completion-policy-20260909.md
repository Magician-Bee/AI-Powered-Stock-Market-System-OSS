# P72 Host completion policy 模組拆分驗收（2026-09-09）

## 變更與邊界

- Branch：`codex/refactor-p72-completion-policy-20260909-0604`
- Candidate commit：`4c29070a59e3`
- 新增 `completion_policy.py`，統一持有 Host completion evaluation、objective contract、validated evidence、紙上委託收據、analysis-only、資料不可用、UI task、Critic observation 與 redundant Critic call 判定。
- 共抽離 20 個函式；`orchestrator.py` 保留相容 import，呼叫端與測試 import 不變，檔案由原始 10,414 行降至目前 8,751 行。
- 模組邊界測試禁止 completion policy 回流 orchestrator，並將 coordinator 上限鎖在 8,760 行以下。

這批只移動既有 Host-owned deterministic completion policy，沒有放寬完成條件、證據要求、工具能力或交易權限。沒有足夠 evidence、缺少 paper order receipt、Critic 未真正完成或存在未解決失敗時，Host 仍會 fail-closed。

## 自動化測試

- 定向套件：`242 passed`。
- 完整套件：`1929 passed, 5 skipped, 3 warnings`，耗時 206.02 秒。
- 既有測試持續驗證 objective contract、validated evidence、numeric rejection、analysis-only、資料不可用、UI task、紙上委託 receipt、Critic observation 與 Completion Gate。
- `tests/test_agent_runtime_module_boundaries.py` 新增 completion policy 單一模組責任與 orchestrator 行數上限。

全部測試使用本機 deterministic fixture；未連線、啟動或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、確認 port 8000 已清除、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。版本驗證為 exact candidate `4c29070a59e3`、PID `87655`、port `8000`、managed runtime source，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」完成：

1. 確認首頁、40 / 648 有界市場清單、圖表與 Agent Dock 正常恢復。
2. 只讀開啟 Agent Dock 的「任務」分頁，確認已終止歷史 Run 的 Task Forest、完成標準、Agent 反思與 Plan DAG 可正常投影。
3. 以標準視窗確認中央工作區、左側導覽與右側 Agent Dock 沒有遮擋或控制列溢出，並保存可見驗收截圖。

全程未點擊 Agent Run 控制、未建立／暫停／恢復／取消 Run、未輸入或傳送訊息，也未測試 provider 連線或呼叫模型。

P72 Host completion policy 原生桌面驗收（未隨公開版提供；原參考：`native-p72-completion-policy-20260909.png`）

截圖原始尺寸：2,880 × 1,864；SHA-256：`6b5d2840ffbbacda53e189bec0bf5c3d0f5d0c8992dbabf302a9df8a3a5f2194`。

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

P72 尚未宣告完整完成：interaction persistence、provider transcript、failure recovery 與 completion policy 已獨立；orchestrator 仍包含 paper protocol、evidence projection 與大型 run-loop 協調責任，後續批次繼續拆分。
