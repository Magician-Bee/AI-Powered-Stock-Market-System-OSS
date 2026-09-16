# Stock AI Agent Dock 本機最終驗收（2026-07-29）

## 啟動來源

- 實際從 `/Users/your-user/Desktop/AI股市系統-Agent測試版/開啟股市AI系統.command` 啟動。
- 啟動器以 Git commit、專案根目錄、instance id 與工作樹來源指紋辨識目前版本；來源變更後會重啟，不再把相同 commit 的舊程序誤判為最新程式。
- 實際啟動的 API 與原生 `Stock AI Liquid Glass.app` 都來自上述桌面資料夾。

## 自動化測試

- 完整本機測試：`651 passed, 3 skipped, 8 xfailed, 1 xpassed`。
- 聚焦 Agent、Dock、靜態 UI 與 durable runtime 測試：`87 passed`。
- 啟動器聚焦測試：`30 passed`。
- 所有修改過的 JavaScript 通過 `node --check`。
- `open-stock-ai.sh`、`stop-stock-ai.sh` 通過 `bash -n`。
- `src` 通過 Python `compileall`，Git diff 通過 whitespace 檢查。

## 實際瀏覽器與模型驗收

### Ollama / OpenAI-compatible

- UI 儲存設定：`openai-compatible / gpt-oss:20b`。
- API Base URL：`http://192.0.2.1:11434/v1`。
- 「測試目前連線」回報正常，模型清單健康檢查找到 13 個模型。
- 實際自然問題 Run：`AR-99642403833e4a0c9a10f815392cf20e`。
- 結果：`completed`，sequence 56，2 個動態 Plan 節點、2 次模型呼叫、2 個 Host 驗證工具結果。
- 最後回答在對話底部，以自然產生的 4 個段落、比較表格與 5 個下一步呈現。

### Codex App Server

- 實際自然問題 Run：`AR-4377c7488d064745a92c72f666ea5508`。
- Codex 自動建立動態 Plan 並持續執行至 `max_steps_reached`，沒有被錯標成 completed。
- UI 顯示「繼續執行／增加步驟上限／重新規劃／建立新 Run」四個恢復操作。
- 實際點擊「增加步驟上限」後，同一 Run id 保持不變，`max_steps` 由 12 增至 24，`resume_count` 由 0 增至 1。
- 驗收後以 UI 暫停，確認可保存檢查點；預設模型再切回已完整完成 Run 的 Ollama。

## UI 操作結果

- 左側功能欄實測寬度 188 px；右側 Dock 實測寬度 360 px，中央工作區保留更多空間。
- Pearl 淺色主題下，Dock 標題、狀態色、Plan、Tasks、Artifacts、操作卡與 composer 均可辨識。
- Plan 展開時按先後順序顯示狀態；收合時顯示 `完成數 / 總數` 與目前執行或下一步摘要。
- Plan 節點顯示每步結果、仍缺資訊與下一步；狀態點使用固定欄位對齊。
- Chat 的 Skill、Package、Host、工具等操作事件是摘要字卡，按「顯示完整資訊」才展開。
- 最後回答依事件順序留在對話底部，不需要回到最上方尋找。
- Tasks 顯示真實 Plan tree、完成標準、結果與 revision history。
- Artifacts 僅顯示目前 session/run 的產物；本次沒有產物時顯示明確空狀態。
- 1000 事件回放、長對話視窗化、讀取較舊訊息、使用者閱讀舊內容時不強制捲到底部均有瀏覽器 E2E 測試。

## 瀏覽器附註

頁面沒有功能性 JavaScript 例外。Console 仍會看到既有第三方 `liquidGL/html2canvas` 對瀏覽器序列化 `color(...)` 的快照降級訊息；該套件會回退到非快照玻璃效果，實際畫面與 Agent 操作不受影響。
