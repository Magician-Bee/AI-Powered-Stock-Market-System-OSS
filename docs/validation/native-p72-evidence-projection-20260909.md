# P72 Evidence projection 模組拆分驗收（2026-09-09）

## 變更與邊界

- Branch：`codex/refactor-p72-evidence-projection-20260909-0634`
- Candidate commit：`1a829eec4469`
- 新增 `evidence_projection.py`，統一持有 evidence feedback、claim、canonical record、來源與新鮮度、精確數值顯示、workspace details、reasoning step summary 與 evidence timestamp。
- 共抽離 10 個函式；`orchestrator.py` 保留相容 import，既有呼叫端與測試 import 不變，檔案由原始 10,414 行降至目前 7,784 行。
- 模組邊界測試禁止 evidence projection 回流 orchestrator，並將 coordinator 上限鎖在 7,795 行以下。

這批只移動既有 Host-owned deterministic evidence normalization 與 projection。來源角色、URL、publication time、freshness、run-scoped 去重、精確數值與 unsupported claim feedback 規則保持不變；provider 草稿仍不能取代 Host 驗證證據。

## 自動化測試

- Agent runtime、completion、interaction、recovery 與財務證據定向套件：`258 passed`。
- 完整套件：`1931 passed, 5 skipped, 3 warnings`，耗時 200.14 秒。
- 既有測試持續驗證 model／worker／tool／data provider 分離、同 Run canonical 去重、跨 Run identity、官方與網頁來源角色、資料不足 claim、未支持數值 feedback、法人與月營收精確摘要。

全部測試使用本機 deterministic fixture；未連線、啟動或執行本地／遠端模型。

## 原生桌面 App

依序執行 `./停止股市AI系統.command`、確認 port 8000 已清除、`./開啟股市AI系統.command`、`./驗證目前執行版本.command`。版本驗證為 exact candidate `1a829eec4469`、PID `1613`、port `8000`、managed runtime source，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。

Computer Use 在可見的原生「股市AI系統 Agent測試版」完成：

1. 確認首頁市場快照、40 / 648 有界清單、圖表與 Agent Dock 正常載入。
2. 只讀開啟「任務」分頁，確認歷史 plan nodes 仍顯示 Host 驗證 schema、symbol、recommendation、execution boundary 與紙上交易回執。
3. 點選既有已完成的「驗證 2887.TW 策略訊號與風險」Plan DAG node，確認 Composer context 正確投影為 `Plan DAG ＞ 驗證 2887.TW 策略訊號與風險`、版本 `v4`，並保存可見截圖。

全程未點擊 Agent Run 控制、未建立／暫停／恢復／取消 Run、未輸入或傳送訊息，也未測試 provider 連線或呼叫模型。

P72 Evidence projection 原生桌面驗收（未隨公開版提供；原參考：`native-p72-evidence-projection-20260909.png`）

截圖原始尺寸：2,880 × 1,864；SHA-256：`7c9dbf319067b8c8653a5dce8d21765a4afb9254c6261299a9a6d01c3ec1c4c7`。

完整 release gate 維持 fail-closed：85 complete、39 partial、0 unverified，共 124 項；ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

P72 尚未宣告完整完成：interaction persistence、provider transcript、failure recovery、completion policy、paper protocol 與 evidence projection 已獨立；orchestrator 仍包含大型 run-loop 協調責任，後續批次繼續拆分並驗證。
