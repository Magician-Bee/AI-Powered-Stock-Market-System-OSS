# P73 Agent Dock 環境欄換行驗收（2026-09-09）

## 驗收範圍

- 工作分支：`codex/fix-p73-agent-dock-context-wrap-20260909-0734`
- 候選版本：`463105a4bbec`
- 問題：固定寬度 Agent Dock 的環境資訊原本使用單列橫向排列，右側欄位會超出可見範圍，使用者無法同時讀取任務股票、問題類型、自主權與 Snapshot。
- 修正：七個環境欄位增加穩定的語意 key；Dock 使用可收縮的二欄 grid，問題類型與 Snapshot 跨滿整列。欄位可正常換行，窄視窗依 3 欄與 2 欄規則重新排列。

## 自動化驗證

- `node --check src/stock_ai/ui/static/js/features/agent/agent-context-bar.js`
- `uv run pytest tests/test_agent_dock_state.py tests/test_static_ui.py tests/test_light_theme_polish.py -q`
  - `131 passed`
- `uv run pytest -q`
  - `1932 passed, 5 skipped`
- release gate：`85 complete, 39 partial, 0 unverified`，共 124 項；維持 fail-closed，ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

新增的契約測試確認：

- 七個欄位都有語意 key。
- Context Bar 使用可收縮 grid 且禁止水平溢出。
- chip 允許文字換行及長字串斷行。
- 問題類型與 Snapshot 使用整列版面。
- 600px 以下仍有明確的窄幅排列規則。

## 原生桌面 App 驗收

以專案根目錄的 `./開啟股市AI系統.command` 啟動 **Stock AI Liquid Glass**，再以 `./驗證目前執行版本.command` 確認 managed runtime：

- PID：`9321`
- 監聽：`127.0.0.1:8000`
- UI URL 的 `stock_ai_commit`：`463105a4bbec`
- 驗證結果：`VERIFIED CURRENT PROJECT INSTANCE`

Computer Use 在可見的原生桌面 App 執行以下檢查：

1. 標準桌面視窗確認七個欄位收在 Agent Dock 內，任務類型及 Snapshot 取得完整列寬。
2. 將原生 App 視窗縮為 760 × 900 pt，確認 Context Bar 重新排列，頁面、分頁、畫面股票、任務股票、問題類型、自主權與 Snapshot 均沒有向右溢出。
3. 確認標題導覽、內容分頁、文字輸入框與傳送按鈕仍位於可見範圍，沒有按鈕互相重疊。

本次只檢查既有歷史內容的顯示狀態，沒有操作 Run 控制，也沒有呼叫本地或遠端模型。

## 證據

| 畫面 | 尺寸 | SHA-256 |
| --- | --- | --- |
| `native-p73-agent-context-wrap-standard-20260909.png` | 2880 × 1864 px | `f58f54cc63dbaef98e6911c7663eea320637b4447904f246ca4bd7da4f2a1da0` |
| `native-p73-agent-context-wrap-narrow-20260909.png` | 1520 × 1800 px | `48fb612d17aa65d560bc577cfee2b18bcc05d35f1dce0a433a031f87b211beba` |

此驗收只證明 P73 的 Agent Dock 環境資訊響應式呈現已修正；Artifact、Automation 與 Evidence 的完整互動流程仍保留為部分完成。
