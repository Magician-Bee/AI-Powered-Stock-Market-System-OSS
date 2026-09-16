# P73 首頁市場候選清單原生驗收

- 日期：2026-09-08（Asia/Taipei）
- 實作 candidate：`e7faa0f6ed3dabade0c2c79d1a789b18a64f2d19`
- 分支：`codex/fix-ui-market-list-window-20260908-2045`
- 啟動方式：`./停止股市AI系統.command` → `./開啟股市AI系統.command` → `./驗證目前執行版本.command`
- 原生 App：`Stock AI Liquid Glass`
- 模型：未呼叫本地或遠端模型
- Agent Run：未建立、恢復、取消或操作 Run 控制

## 自動化檢查

```text
node --check src/stock_ai/ui/static/js/features/market-intelligence/market-navigation.js
uv run pytest tests/test_light_theme_polish.py tests/test_light_theme_readability.py tests/test_static_ui.py tests/test_market_scanner_factor_ui.py -q
61 passed in 0.50s
```

## 原生桌面操作

1. 版本腳本確認 managed runtime 的 branch 為 `codex/fix-ui-market-list-window-20260908-2045`，commit 為 `e7faa0f6ed3d`，結果為 `VERIFIED CURRENT PROJECT INSTANCE`。
2. 以 Computer Use 讀取原生 App，URL 的 `stock_ai_commit=e7faa0f6ed3d`。
3. 首頁自動落在「持續觀察」，Accessibility tree 只有 40 張候選卡，顯示 `40 / 648 檔`，按鈕標籤為「再顯示最多 40 檔」。
4. 按一次「顯示更多」後，畫面改為 `80 / 648 檔`。
5. 切到「未來可買」後只顯示 `34 / 34 檔`，不顯示無效的更多按鈕；切回「觀察」後重設為 `40 / 648 檔`。
6. 將原生視窗縮至約 1,440px 寬，候選卡代號、名稱、價格、漲跌、分類、成交量、排名與日期仍可辨識；中央圖表、頂部操作列與右側 Agent Dock 沒有互相遮擋。

## 證據

- 截圖：`docs/validation/native-p73-market-list-window-20260908.png`
- 截圖 SHA-256：`d04a2a02c7ce1dd374c369fc331ebc1e8402b087155aa8af2704c393d2f9be77`

本次只驗證 P73 首頁市場清單的 bounded rendering、卡片可讀性與窄視窗版面，不把 Artifact、Automation 或 Evidence 的其他前端流程標成完成。
