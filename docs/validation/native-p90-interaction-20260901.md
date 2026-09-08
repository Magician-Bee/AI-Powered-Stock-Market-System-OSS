# P90 原生使用者協作驗收（2026-09-01）

## 範圍

本紀錄驗證使用者以自然語言要求 Agent 在兩個假設性方案間提出暫定建議時，
Agent 是否能在同一個 Run 提供可操作的 Decision Card，並接受推薦、替代方案和
自由文字三種回覆。這不是市場研究、投資建議、排程、紙上交易、實盤交易或券商
帳戶驗收。

## 已修正的根因

遠端 `gpt-oss:20b` 在真實原生 UI 曾出現兩種不穩定輸出：

1. 只回覆「請使用者選擇」，沒有把暫定偏好綁到原問題的中文選項；舊的
   routine-wait guard 因而提早結束 Run。
2. 下一輪即使已在摘要中提出「保守型」推薦，模型仍帶著自己的 generic
   interaction；舊 guard 再次把這個使用者明確要求的選擇當作內部等待關閉。

Host 現在只針對「使用者明確命名兩個選項」且沒有工具操作的回應處理：先從摘要、
reflection 或已選 provider option 對回原始選項；無具體偏好時僅在同一 Run 內重問
模型一次；有偏好時由 Host 固定重建 Decision Card。卡片被標記為 Host-owned
explicit choice checkpoint，因此不會被 routine-wait guard 自動關閉。任何已收到的
interaction response 仍不會再重開同一張卡。

## 啟動與環境

- 專案：`/Users/your-user/Desktop/AI股市系統-Agent測試版`
- 工作分支：`codex/test-p90-native-interaction-20260901-1117`
- 啟動 commit：`a8733ad8c0a39135a4ceb0c4b4537f4f6f69e331`（驗收中的未提交工作樹）
- 啟動來源指紋：`7aa8200326b44689`
- 本機 instance：`fae9545ae1cab455`
- 啟動方式：`./開啟股市AI系統.command`
- 健康服務：`http://127.0.0.1:8000/`（launcher 顯示 PID 42416）
- 模型：`gpt-oss:20b`，`openai-compatible`，Advisory

## 原生 UI 操作與觀察

每個驗收都在新的桌面 `Stock AI Liquid Glass` Agent Session 輸入同一句自然語言：

> 純假設、不查市場、不交易、不建立自動化：請在保守型與平衡型投資計畫中幫我選一個，並讓我可以選擇建議、替代方案或輸入自己的想法。

Host 在每次都顯示同一張可操作卡：`保守型 · Agent 建議`、
`平衡型投資計畫 · 替代方案`，以及「或直接輸入你的偏好」的自由文字欄位。

| 回覆方式 | Run | 實際操作 | 終態 |
| --- | --- | --- | --- |
| Agent 建議 | `AR-9bf673b4ecc043b3b17b28c52b90b606` | 點選 `保守型 · Agent 建議` | 同一 Run 在 sequence 45 顯示「已完成」。 |
| 替代方案 | `AR-2fb73dcfda064c2b9455d3f6a228cf47` | 點選 `平衡型投資計畫` | 同一 Run 在 sequence 46 顯示「已完成」。 |
| 自由文字 | `AR-382f835240844c4791abc41de0d7ab02` | 輸入「我想採用平衡型，但設定六個月後重新檢視。」並按 `回答` | 同一 Run 在 sequence 45 顯示「已完成」。 |

三條流程都沒有再次顯示「等待外部驗收條件」或要求使用者教 Agent 如何處理；
UI 終態與 Host notice 都明確為「未查詢市場、未執行交易，也未建立自動化」。
模型可以在假設性文字中談及配置或後續檢視，但沒有建立 Automation、紙上單、
實盤訂單或連線任何券商帳戶。

## 判定

P90 的本地 User Collaboration 決策卡已在指定的桌面 launcher、真實遠端模型與
三種回覆方式下完成端到端驗收。這次證據只證明 Host interaction 收束與安全邊界；
不提升任何投資結論、資料品質或外部交易能力的完成狀態。
