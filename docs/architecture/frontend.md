# 前端架構

## 目標

前端維持無建置步驟的原生 HTML、JavaScript 與 CSS，但不再把所有功能集中在單一 `app.js` 或 `styles.css`。每個檔案只負責一個明確領域，由 `index.html` 依固定順序載入。

## JavaScript 分區

```text
ui/static/js/
├── core/
│   ├── dom-state.js          DOM helper、全域狀態、圖表色票
│   ├── preferences.js        語言、主題、密度與本機偏好
│   └── presentation.js       HTML escaping、文字與格式化 helper
├── shell/
│   ├── glass.js              Liquid Glass 與畫布取樣生命週期
│   ├── freefrontend-liquid-glass.js  將完整上游 Pen 濾鏡與元件接到既有語意 UI
│   └── navigation.js         view 切換與 lazy view loader
├── features/
│   ├── codex.js              登入、帳號、首頁雷達與 prompt
│   ├── trading-workspaces.js 交易預覽、建議、風控與資產頁
│   ├── quant-research.js     Open Stock AI 決策、證據與批次研究
│   ├── market-chart.js       即時行情、K 線、技術指標與個股摘要
│   ├── explorer.js           搜尋、問答、選股、連動與資料目錄
│   ├── system-status.js      系統契約、排程、來源政策與更新狀態
│   └── dashboard.js          自選股、新聞、通知、籌碼與總覽資料
└── bootstrap.js              DOM 事件綁定與初始化，固定最後載入
```

`paper-training.js` 與 `agent-trading-workspace.js` 是各自封裝的獨立功能模組；它們不讀取上述模組的私有狀態。

## CSS 分區

```text
ui/static/css/
├── core/base.css             tokens、基本元素、導覽與頁面骨架
├── shell/
│   ├── material.css          工作站材質與玻璃 chrome
│   ├── freefrontend-liquid-glass.css  上游液態玻璃原始樣式與三平台薄連接層
│   ├── responsive.css        桌面、平板與手機斷點
│   └── adaptive-glass.css    深淺色自適應玻璃收斂規則
└── features/
    ├── workspaces.css        表格、卡片、行情、交易與研究工作區
    ├── preferences.css       設定頁與主題選擇器
    ├── codex-home.css        登入 gate、首頁決策與 Codex composer
    └── themes.css            背景、深色與淺色主題差異
```

根目錄其餘 CSS 只服務單一獨立領域，例如紙上交易或 Liquid Glass contract，不得再作為所有頁面的修補堆疊。

## 載入與依賴規則

- `index.html` 中的 JS 和 CSS 順序是契約，由 `tests/test_static_ui.py` 驗證。
- feature 可以使用先載入的 core helper，但 core 不得依賴 feature。
- `bootstrap.js` 只綁定事件和啟動流程，不放資料轉換或畫面模板。
- 新頁面優先新增一個 feature 檔；不要把程式塞回 `bootstrap.js`。
- 前端不得自行複製後端策略、風控、帳戶餘額或執行資格規則。
- DOM id 是 UI contract；修改時要同步 `index.html`、對應 feature 與 contract test。
- 可重複的格式化或安全 helper 放 `core/presentation.js`，不得在多個 feature 重複實作。

## 定位方式

| 畫面或問題 | JavaScript | CSS |
| --- | --- | --- |
| 登入、Codex 首頁、帳號 | `features/codex.js` | `features/codex-home.css` |
| 個股、即時行情、K 線 | `features/market-chart.js` | `features/workspaces.css` |
| 量化策略研究 | `features/quant-research.js` | `features/workspaces.css` |
| 交易預覽、風控、資產 | `features/trading-workspaces.js` | `features/workspaces.css` |
| 市場總覽、自選股、新聞、通知 | `features/dashboard.js` | `features/workspaces.css` |
| 搜尋、選股、問答、關聯推演 | `features/explorer.js` | `features/workspaces.css` |
| 設定、主題、語言 | `core/preferences.js` | `features/preferences.css`、`features/themes.css` |
| 導覽切換 | `shell/navigation.js` | `core/base.css`、`shell/responsive.css` |
| 玻璃效果或桌面 chrome | `shell/glass.js` | `shell/material.css`、`shell/adaptive-glass.css` |
| 初始化或按鈕沒綁定 | `bootstrap.js` | 不適用 |

## 驗證

```bash
find src/stock_ai/ui/static/js -name '*.js' -exec node --check {} \;
uv run --extra dev pytest -q tests/test_static_ui.py tests/test_native_liquid_glass.py
```
