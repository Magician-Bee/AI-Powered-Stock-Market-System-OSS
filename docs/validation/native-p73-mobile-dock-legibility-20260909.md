# P73 窄幅 Agent Dock 可讀性驗收（2026-09-09）

## 驗收範圍

- 工作分支：`codex/fix-p73-mobile-dock-legibility-20260909-0701`
- 候選版本：`2fb55be14a8b`
- 問題：1,023px 以下的 Agent Dock 會占滿視窗，但半透明玻璃材質及 body-level WebGL lens 仍會把首頁導覽、行情卡與控制文字畫到 Dock 上，造成內容互相疊字。
- 修正：全螢幕 Dock 增加不透明的實體底層；Dock 展開時只在窄幅版面隱藏其下方的 sidebar、main、光學取樣層、lens proxy 與 WebGL canvas。Dock 收合後規則即失效，桌面版玻璃效果不受影響。
- 資產：更新 `agent-dock.css` 的版本鍵，確保 managed runtime 的原生 WebView 取得本次樣式。

## 自動化驗證

- `uv run pytest tests/test_agent_dock_state.py tests/test_static_ui.py tests/test_light_theme_polish.py -q`
  - `132 passed`
- `uv run pytest -q`
  - `1933 passed, 5 skipped`
- release gate：`85 complete, 39 partial, 0 unverified`，共 124 項；維持 fail-closed，ledger SHA-256 為 `598f81f3d4a699ca5033e13748c2d6bcdd01a321b444a3e8bab7bd1f666a7848`。

契約測試固定以下行為：

- 窄幅 Dock 有不透明底層並建立自己的 stacking context。
- Dock 內容固定在遮蔽層上方。
- 展開的窄幅 Dock 會移除下層頁面及純視覺 WebGL 層；收合狀態不套用該規則。
- HTML 使用新的 CSS 資產版本，避免原生 WebView 沿用舊快取。

## 原生桌面 App 驗收

使用專案根目錄的 `./開啟股市AI系統.command` 啟動 **Stock AI Liquid Glass**，並以 `./驗證目前執行版本.command` 確認：

- PID：`28112`
- 監聽：`127.0.0.1:8000`
- UI URL 的 `stock_ai_commit`：`2fb55be14a8b`
- 驗證結果：`VERIFIED CURRENT PROJECT INSTANCE`

Computer Use 在可見的原生 App 將視窗縮為 760 × 900 pt 後確認：

1. 畫面只呈現 Agent Dock；首頁導覽、行情卡與玻璃光學副本均未穿透。
2. AX 可操作樹只保留 Dock 內的標題、Context、狀態、計畫、分頁、內容與 Composer；下層首頁控制不再同時可操作。
3. 七個 Context 欄位、執行狀態、計畫、對話／任務／產物分頁、文字輸入框與傳送按鈕均在視窗內，沒有重疊或右緣截斷。

本批只讀取既有歷史內容，沒有操作 Run 控制，也沒有呼叫本地或遠端模型。

## 證據

窄幅 Agent Dock 不透明全螢幕驗收（未隨公開版提供；原參考：`native-p73-mobile-dock-opaque-20260909.png`）

- 尺寸：1,520 × 1,800 px（760 × 900 pt Retina 視窗）
- SHA-256：`5d6c9f7dd14f3f18a5c23a70bb5b46691aa50a507cddd03639bcba879fc300f8`

此驗收完成 P73 的窄幅全螢幕 Dock 可讀性子項；Artifact、Automation 與 Evidence 的完整互動流程仍保留為部分完成。
