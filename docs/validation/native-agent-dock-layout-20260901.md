# Agent Dock 版面與證據摘要原生驗收（2026-09-01）

## 範圍

本次只修正 Dock 的可讀性投影：Evidence Graph 的 node 是導覽控制項，不能把完整
`open_stock_ai.agent_workspace.v1` JSON 當作按鈕標題。完整 evidence receipt 仍保留在
artifact／detail selection，沒有更動工具內容、資料品質、交易權限或任何 OMS state。

## 實機環境

- 專案：`/Users/your-user/Desktop/AI股市系統-Agent測試版`
- 啟動方式：`./開啟股市AI系統.command`
- 工作分支：`codex/fix-native-agent-dock-layout-20260901-1715`
- 啟動器 source fingerprint：`512a1dfa96c66b0d`
- 原生 App：`Stock AI Liquid Glass` / `股市AI系統 Agent測試版`
- 操作方式：Computer Use（macOS 原生窗口，不是瀏覽器）

## 操作與結果

1. 以 launcher 開啟已存在的 2887.TW advisory Run，右側 Dock 顯示仍維持狹窄工作區的實際布局。
2. 驗證 Dock 開啟時主工作區與頂欄保留右側 Dock 寬度；Dock 沒有覆蓋中央行情圖或頂欄互動區。
3. 從左側功能列按「收合 Agent 控制台」，Dock 消失且中央 K 線工作區可擴展；同一按鈕改為「開啟 Agent 控制台」。再次按下後 Dock 正常回復。
4. Evidence Graph 節點從完整 JSON 改為 `2887.TW · 持續觀察 · 執行已阻擋`；AX tree 直接確認該按鈕不再以 `{ "schema_version": ... }` 開頭。
5. 點選該 Evidence node 後，Composer context 從「目前頁面 Context」切換成 `Evidence Graph ＞ 2887.TW · 持續觀察 · 執行已阻擋`，並顯示 artifact version `v1`。本次沒有送出後續自然語言修改，故只記錄 selection→context 連動，不把完整 Artifact 修改流程誤報完成。

本次沒有輸入新任務、沒有建立 Automation、沒有紙上或真實交易、沒有讀取帳戶或傳送外部資料。

## 自動化回歸

```text
uv run pytest tests/test_agent_dock_state.py tests/test_light_theme_polish.py tests/test_light_theme_readability.py tests/test_native_liquid_glass.py -q
89 passed in 1.46s
```

新增 Node 子程序測試會載入 `agent-evidence-graph.js`，確認 workspace receipt 的 node 文案是短摘要，而不是 raw JSON。
