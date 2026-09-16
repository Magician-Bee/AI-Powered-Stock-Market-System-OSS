# P72 紙上交易 Host protocol 模組拆分驗收（2026-09-09）

## 變更與邊界

- Branch：`codex/refactor-p72-paper-protocol-20260909-0625`
- Candidate commit：`6f01be02e903`
- 新增 `paper_protocol.py`，統一持有紙上交易 task kind、精確預覽參數綁定、analysis → preview → submit 協定、明確市場證據 coverage、Critic capability、自然語言紙上單解析、等待抑制、公開摘要與可提交預覽回讀。
- 共抽離 11 個函式；`orchestrator.py` 保留相容 import，既有呼叫端與測試 import 不變，檔案由原始 10,414 行降至目前 8,299 行。
- 模組邊界測試禁止 paper protocol 回流 orchestrator，並將 coordinator 上限鎖在 8,310 行以下。

這批只移動既有 Host-owned deterministic policy。紙上交易仍只允許明確授權的本機 Paper Broker；提交參數必須與 Host 驗證預覽完全相同，缺少必要條件時不得猜測，任何實盤券商能力都沒有開啟。

## 自動化測試

- Agent runtime、completion、interaction 與 recovery 定向套件：`254 passed`。
- 紙上交易、台股市場規則與治理套件：`118 passed`。
- 完整套件：`1930 passed, 5 skipped, 3 warnings`，耗時 198.90 秒。
- 邊界與既有行為測試持續驗證單一標的 sandbox default、明確方向／數量、精確 preview argument reuse、provider 變造 submit 攔截、market coverage、Critic receipt、本機 paper boundary 與 approval isolation。

全部測試使用本機 deterministic fixture；未連線、啟動或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、確認 port 8000 已清除、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。版本驗證為 exact candidate `6f01be02e903`、PID `94961`、port `8000`、managed runtime source，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」完成：

1. 等待首頁市場快照與 40 / 648 有界清單載入，確認圖表與中央工作區正常。
2. 只讀檢查既有已完成紙上模擬歷史：執行計畫仍顯示 `market.analyze_symbol`、`paper.preview_order`、`paper.submit_order` 三段 Host 回執。
3. 在「任務」分頁確認 Task Forest、完成標準、Plan DAG 與 `local_paper_broker_only_no_live_submission` 歷史結果可見，介面沒有遮擋或控制列溢出。

全程未點擊 Agent Run 控制、未建立／暫停／恢復／取消 Run、未輸入或傳送訊息，沒有新增紙上單，也未測試 provider 連線或呼叫模型。

P72 紙上交易 Host protocol 原生桌面驗收（未隨公開版提供；原參考：`native-p72-paper-protocol-20260909.png`）

截圖原始尺寸：2,880 × 1,864；SHA-256：`0a5b97d82138cde9a996e309ad4aee546d3bc9ec8ba7e768906562ed70508409`。

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

P72 尚未宣告完整完成：interaction persistence、provider transcript、failure recovery、completion policy 與 paper protocol 已獨立；orchestrator 仍包含 evidence projection 與大型 run-loop 協調責任，後續批次繼續拆分。
