# 專案結構與維護定位

## 分層原則

本專案只有兩個第一方 Python package：

1. `stock_ai` 是應用與介面層，負責 HTTP、資料連接、Codex runtime 和前端資源。
2. `open_stock_ai` 是交易研究領域核心，負責資料契約、情報、策略、研究、風控、紙上執行和持久化。

依賴方向應維持：

```text
UI -> stock_ai API -> open_stock_ai core -> storage/external adapters
```

`open_stock_ai.data.MarketDataHub` 可以透過明確橋接讀取 `stock_ai` 的市場服務；其他核心模組不得任意依賴 FastAPI、DOM 或桌面外殼。

## 來源目錄責任

### `src/stock_ai`

| 模組 | 唯一責任 |
| --- | --- |
| `main.py` | FastAPI app、頂層路由、靜態檔掛載 |
| `models.py` | 應用層 request／response model |
| `services.py` | 市場摘要、搜尋、事件、資產與 UI workspace 組裝 |
| `realtime_quotes.py` | TWSE MIS／授權 provider 即時行情與 SSE |
| `realtime_data.py` | Yahoo 等一般行情正規化 |
| `taiwan_official.py` | 台灣官方證券目錄、收盤與歷史資料 |
| `phase1_data.py` | 法人、融資融券、月營收與每日報告 |
| `official_events.py` | MOPS 事件匯入與查詢 |
| `official_derivatives.py` | TDCC／TAIFEX 匯入與查詢 |
| `source_policy.py` | 第一方、第二方與輔助來源資格規則 |
| `codex_runtime.py` | Codex App Server lifecycle 與事件處理 |
| `codex_api.py` | Codex HTTP API 與 context 組裝 |
| `codex_market.py` | Agent 市場雷達與動作邊界 |
| `agent_trading_api.py` | Agent 交易 session、分析與控制 |
| `agent_api.py` | 供應商無關 Agent Runtime HTTP API |
| `agent_service.py` | Agent Runtime lifecycle 與 driver/tool 組裝 |
| `agent_drivers.py` | Codex Provider 與停用的未來 provider contract；目前只會執行 Codex |
| `agent_tools.py` | 行情、研究、帳戶、風控與 Paper Broker 可執行工具 |
| `agent_general_tools.py` | 專案檔案、ripgrep、終端機、網站與原生 Codex 橋接工具 |
| `paper_training_api.py` | 紙上帳戶、委託、成交、評估與反思 API |
| `ui/static/js/core/` | DOM helper、共享狀態、偏好與格式化 |
| `ui/static/js/shell/` | 導覽、頁面生命週期與 Liquid Glass 外殼 |
| `ui/static/js/features/` | Codex、行情、研究、交易、儀表板等功能模組 |
| `ui/static/js/bootstrap.js` | 事件綁定與啟動順序，必須最後載入 |
| `ui/static/` 其他檔案 | HTML、獨立紙上交易模組與視覺樣式，不保存交易規則 |

### `src/open_stock_ai`

| 目錄 | 唯一責任 |
| --- | --- |
| `data/` | 市場快照、來源 envelope、品質與執行資格 |
| `intelligence/` | 新聞、技術、基本面、財報、情緒與反思摘要 |
| `strategy/` | 訊號、評級、價格區間與部位建議 |
| `research/` | 回測、因子、投組建構、歸因與研究報告 |
| `risk/` | 信心、停損、回撤、曝險與 kill switch |
| `execution/` | Paper OMS、Broker、委託、成交與訓練回合 |
| `storage/` | SQLite migration、signal、decision、trade、report store |
| `external_sources/` | 第三方來源驗證、契約解析與受控投影 |
| `agent_runtime/` | 供應商無關 Agent 合約與 host-owned 工具迴圈 |
| `llm/` | 保留的本地／相容 LLM contract；目前不載入模型，不能繞過風控 |
| `notify/` | 通知 preview 與 provider 邊界 |
| `pipeline.py` | 上述階段的唯一執行順序 |
| `engine.py` | 對外分析與批次 session facade |
| `runtime.py` | 設定感知的 engine cache |
| `api.py` | Open Stock AI HTTP router |

## 不可跨越的邊界

- UI 不得自行計算可買、可賣、風控批准或帳戶餘額。
- Agent prompt 不得成為第二套交易規則。
- StrategyEngine 不得直接建立委託。
- ResearchEngine 的 advisory projection 不得被標成實證有效模型。
- ExecutionEngine 不得接受未經 RiskEngine 評估的可執行買進。
- 外部專案不得直接提交遠端訂單或讀取外部券商憑證。
- SQLite Paper Account 是現金、持倉、委託與成交的唯一真相。

## 新增功能的位置

- 新資料來源：先新增 `stock_ai` connector 或 `open_stock_ai/data` adapter，再補 source policy 和品質測試。
- 新策略：放進 `open_stock_ai/strategy`，由 `StrategyEngine` 統一調用。
- 新風控：放進 `open_stock_ai/risk`，不得只在 API 或 UI 加判斷。
- 新 API：依資料所有權放入 `stock_ai/main.py`、Codex router 或 `open_stock_ai/api.py`。
- 新 Agent framework：未來只新增 `agent_drivers.py` adapter；不得複製工具、風控或帳戶邏輯。當前 production mode 固定 Codex。
- 新 Agent 工具：註冊在 `agent_tools.py`，並呼叫既有領域服務；不得由模型自行實作規則。
- 新通用工具 provider：放在 `agent_general_tools.py` 或獨立 provider 模組，必須有真實 executor、權限欄位與測試。
- 新頁面：修改 `ui/static/index.html`、一個 `ui/static/js/features/` 模組和一個明確對應的 CSS 檔；不要把功能重新集中到 `bootstrap.js`。
- 新資料表：只能透過 `storage/migrations.py` 建立，再由專用 store 存取。

## 本機資料生命週期

`.runtime/`、`logs/` 和 `output/` 都是可變狀態，不屬於來源碼。測試不得依賴其中既有檔案；需要固定輸入時使用 `tests/fixtures/`。任何由時間戳命名的回測、報告或截圖預設都是生成物，不得提交。
