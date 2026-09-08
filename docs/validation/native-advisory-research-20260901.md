# 原生公開研究與 Host 路由驗收（2026-09-01）

## 環境

- 專案：`/Users/your-user/Desktop/AI股市系統-Agent測試版`
- 啟動方式：`./開啟股市AI系統.command`
- 原生 App：`Stock AI Liquid Glass` / `股市AI系統 Agent測試版`
- 操作方式：Computer Use（macOS 原生窗口，不是瀏覽器）
- Provider：遠端 OpenAI-compatible Ollama `gpt-oss:20b`
- 受管 source fingerprint：`527f3a274cd8a1a8`

## 驗收輸入

```text
請查找並交叉整理台新新光金 2887.TW 的公開新聞或市場來源，列出資料品質限制與主要風險；只做研究，不建立 Artifact、紙上交易、實盤交易或自動化。
```

## Host 修正與結果

1. 舊 terminal paper Run 後送入新自然語句時，Host 將 terminal Run 視為歷史而非可繼承執行權限；新 Run `AR-429ac23d20634c0aac2cd1d049c76907` 在原生控制台顯示 `自主權：advisory`。
2. Host 接受 `web.research` 的 bounded `limit` 相容別名，並讓內建 coverage planner 改用正式的 `source_count`。這避免 read-only research plan 因 `invalid_tool_arguments: $.limit` 而在工具執行前反覆編譯失敗。
3. 原生計畫完成 `market.analyze_symbol` 與 `web.research`；後者回傳 16 個搜尋結果、讀取 5 個來源。Run 在 sequence 71 以已完成收束。
4. 最終 Host 摘要明確保留資料限制（包括 quote 過期、point-in-time dataset 缺失與交易成本／backtest 限制），並明確寫出沒有建立自動化、紙上訂單或實盤券商操作。

## 邊界

- 這是公開、唯讀研究驗收；沒有點擊批准卡、沒有建立 Artifact、沒有紙上或實盤交易，也沒有外部通知。
- A-005 的 hash-only provider untrusted-context receipt 已有單元／兩回合 runtime 測試；本次原生 Run 在 Host 取得證據後直接以 deterministic final 收束，沒有再要求 provider turn，因此不將它當成 live provider receipt。A-005 仍維持 partial，等待實際後續 provider turn 與獨立 hostile corpus review sign-off。
