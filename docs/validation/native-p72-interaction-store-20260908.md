# P72 Interaction persistence 模組拆分驗收（2026-09-08）

## 變更與邊界

- Branch：`codex/refactor-p72-interaction-store-20260908-2057`
- Candidate commit：`caf83db4cd49`
- 新增 `interaction/store.py::DurableInteractionStore`，統一持有 `agent_decision_checkpoints`、`agent_user_proposals`、`agent_proposal_evaluations` 的讀寫與狀態轉換。
- `FinalAgentRuntime` 保留既有公開方法作 facade，內部改為委派 domain store；協調器由 1,482 行降至 1,358 行。
- Architecture regression test 明確禁止上述 interaction SQL 回流 `final_runtime.py`，並限制 coordinator 不得再超過 1,400 行。

這次拆分沒有改 schema、API 路徑或回傳欄位。Interaction 建立時仍同步寫入 Decision checkpoint；回覆、取消未解決 interaction、記錄 Proposal 與新增 evaluation 的既有語意保持不變。

## 測試

以下定向套件共 `186 passed`：

- `tests/test_agent_runtime_module_boundaries.py`
- `tests/test_agent_final_system_scenarios.py`
- `tests/test_agent_runtime_final_interaction.py`
- `tests/test_durable_agent_runtime.py`
- `tests/test_agent_runtime_api.py`
- `tests/test_agent_dock_state.py`

涵蓋 Interaction 建立與回覆、等待狀態、取消、process restart 恢復、API facade 與原生 Dock 靜態契約。測試使用 deterministic fixture，沒有連線或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。版本驗證結果為 exact candidate `caf83db4cd49`、PID `53676`、port `8000` 與 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」中完成：

1. 確認首頁與既有 Agent Dock 從 durable 資料恢復。
2. 開啟「系統 → 資料平台」，確認系統資料正常載入。
3. 開啟「Agent 與模型」，只確認設定資料可讀取；未按「測試目前連線」、未儲存、未觸發模型。
4. 返回「資料平台」保存不含帳號資訊的驗收截圖。

全程未建立、暫停、恢復或取消 Agent Run，未送出 Agent 訊息，也未呼叫模型。

P72 原生桌面重啟驗收（未隨公開版提供；原參考：`native-p72-interaction-store-20260908.png`）

截圖 SHA-256：`b135ec461282a1804fae3c188c0bddfec049586e17e516a23b938f7546ac040d`

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；本批沒有更動外部驗證狀態。

P72 尚未宣告完整完成：`orchestrator.py` 仍包含過多協調責任，後續需繼續抽離並以相同模組邊界測試保護。
