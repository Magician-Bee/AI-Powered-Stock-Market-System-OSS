# 原生文字 Artifact 建立與選取驗收（2026-09-01）

## 實機環境

- 專案：`/Users/your-user/Desktop/AI股市系統-Agent測試版`
- 啟動方式：`./開啟股市AI系統.command`
- 工作分支：`codex/fix-negated-artifact-request-20260901-1715`
- 啟動器 source fingerprint：`01d9903fc7e864bb`
- 原生 App：`Stock AI Liquid Glass` / `股市AI系統 Agent測試版`
- 模型：遠端 Ollama `gpt-oss:20b`（openai-compatible）
- 操作方式：Computer Use（macOS 原生窗口，不是瀏覽器）

## 操作與結果

1. 在右側 Agent Dock 輸入「為本次 2887.TW 的資料不足分析建立純文字研究摘要 artifact；不新增市場查詢、不下單、不建立自動化，也不操作真實帳戶」。
2. Host 沒有把 `不下單` 誤判為實盤要求；建立 `AR-b034ae14f0de4030bdf7beebc3230281`，task kind 為 `artifact_task`。
3. 遠端模型沒有提出限定的 artifact tool call 時，原生 Timeline 顯示 `artifact.host_create_compiled`，並只編譯 `artifact.create_text`。
4. 原生批准卡將該操作標為 `local_reversible`；批准後狀態變為 `consumed`，Host 驗證通過且 Timeline 顯示 `artifact.created`。
5. Run 顯示 `2 / 2 完成`，建立 `2887.TW 研究摘要.md`，Artifact ID `AA-05af3b46f8b14ba48a5f5570f42a0a66`，版本 `v1`。
6. 點選「選取 Artifact」後，Composer context 顯示完整本機 path 與 `v1`；產物可在 Dock 開啟或下載。

本次沒有新增市場查詢、沒有紙上或真實訂單、沒有建立 Automation、沒有讀取或操作真實帳戶，也沒有傳送外部資料。

## 自動化回歸

```text
uv run pytest tests/test_agent_runtime.py tests/test_durable_agent_runtime.py -q
185 passed in 16.09s
```

其中新增回歸覆蓋：否定式 `不下單` 的安全判定、Session Run 不再被 safety rejection 阻斷，以及 provider 遺漏 direct text Artifact tool call 時 Host 建立受限 `artifact.create_text` 的行為。
