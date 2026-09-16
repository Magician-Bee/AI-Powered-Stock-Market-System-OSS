# 原生 Artifact 修改、比較與還原驗收（2026-09-01）

## 實機環境

- 專案：`/Users/your-user/Desktop/AI股市系統-Agent測試版`
- 啟動方式：`./開啟股市AI系統.command`
- 工作分支：`codex/fix-artifact-revision-flow-20260901-1750`
- 原生 App：`Stock AI Liquid Glass` / `股市AI系統 Agent測試版`
- 模型：遠端 Ollama `gpt-oss:20b`（openai-compatible）
- 操作方式：Computer Use（macOS 原生窗口，不是瀏覽器）

## 操作與結果

1. 在 Artifact 分頁開啟既有 `2887.TW 研究摘要.md` 的「修改 Artifact」。
2. 驗證修改表單在 Dock state 重繪後仍存在；先前會被完整 Dock render 清除的問題已修正。
3. 對 v1 加上「僅供研究與資料限制追蹤，不構成投資建議」的本機文字說明，填入明確修改原因並確認。
4. 原生 App 顯示 v1、v2、`已建立 Artifact v2`，並顯示 v1 → v2 的版本比較。
5. 選取 v1 後，App 顯示「目前已更新到 v2，送出時會先檢查衝突」與 v1 ↔ v2 比較。
6. 從 v1 執行還原，確認對話框說明會建立新版本；確認後 App 顯示 v1、v2、v3，且 v3 的原因為 `Restore artifact version 1`。舊版本未被覆寫。
7. 重新以 `.command` 啟動後，原生 App 仍選取 v3、完整保留三個版本；表單不再殘留「已建立 Artifact v2」的過期訊息。

本次只修改本機 run-scoped 研究 Artifact；沒有新增市場查詢、沒有紙上或真實訂單、沒有建立 Automation，也沒有讀取或操作真實帳戶。

## 自動化回歸

```text
uv run pytest tests/e2e/test_agent_dock_browser.py tests/test_agent_runtime_api.py -q
45 passed, 1 warning in 9.39s
```

回歸涵蓋：修改草稿跨 Dock 重繪保留、正確帶入 revision API contract，以及新版本出現後不顯示舊版本的成功狀態。
