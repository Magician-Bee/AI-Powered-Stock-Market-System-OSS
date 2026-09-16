# 授權盤中即時行情源接入

目標：做到銀行/券商 App 那種盤中秒級跳價，不能用公開 OpenAPI 的日資料或輪詢假裝即時。

## 事實界線

TWSE OpenAPI 適合公開資料、日成交、財報、公司治理、指數等資料；它不是券商 App 的低延遲盤中逐筆行情流。

要做到秒級跳價，需要以下其中一種合法授權來源：

## 路線 A：直接向 TWSE 申請即時交易資訊

TWSE 說明頁：

- `https://www.twse.com.tw/zh/products/information/real-time.html`
- `https://www.twse.com.tw/zh/products/information/use.html`

重點：

- 可直接與 TWSE 電腦設備連線。
- 也可經由已與 TWSE 簽約之即時交易資訊廠商間接取得。
- 需要簽契約、繳授權費/資訊費。
- TWSE 聯絡：數位資安部，電話 `(02)8101-3393`，E-mail `infoman@twse.com.tw`。

適合：要正式對外提供行情、商業展示、多使用者產品。

## 路線 B：使用券商/行情供應商 API

這比較適合先把產品做起來：

### Fugle / 富果行情 API

文件：

- REST: `https://developer.fugle.tw/docs/data/http-api/getting-started/`
- WebSocket: `https://developer.fugle.tw/docs/data/websocket-api/getting-started/`

能力：

- REST 即時報價 `/intraday/quote/{symbol}`
- WebSocket endpoint `wss://api.fugle.tw/marketdata/v1.0/stock/streaming`
- 可訂閱 `trades`, `candles`, `books`, `aggregates`, `indices`
- API Key 驗證

### 富邦新一代 API

文件：

- `https://www.fbs.com.tw/TradeAPI/docs/market-data/intro`
- `https://www.fbs.com.tw/TradeAPI/docs/market-data/websocket-api/getting-started/`

能力：

- Web API + WebSocket API
- Speed / Normal 模式
- 可訂閱 `trades`, `books`, `indices`
- 需登入券商帳號與憑證後建立行情連線

### 玉山 / 台新 / 其他 Fugle-compatible 方案

多數提供：

- WebSocket 即時行情
- `trades`, `books`, `candles`, `aggregates`, `indices`
- 需 API key 或券商帳號/憑證

## 本系統已完成的接入骨架

已新增：

- `src/stock_ai/realtime_quotes.py`
- `GET /api/realtime/status`
- `GET /api/realtime/quote/{symbol}`
- `GET /api/realtime/stream/{symbol}`

設計原則：

- API key 只放後端 `.env`，不暴露到瀏覽器。
- 前端用 Server-Sent Events 連本機後端，由後端連授權 WebSocket。
- 沒有授權 key 時直接回 `503`，不使用輪詢或公開日資料冒充秒級跳價。

## 啟用 Fugle/Fubon-compatible 即時行情

建立本機 `.env`：

```env
REALTIME_QUOTE_PROVIDER=fugle
FUGLE_MARKETDATA_API_KEY=你的授權API_KEY
REALTIME_STREAM_CHANNELS=trades,books,candles
```

重新啟動系統：

```powershell
cd "C:\Users\your-user\Desktop\股市AI系統"
uv run python -m uvicorn stock_ai.main:app --host 127.0.0.1 --port 8000
```

驗證：

```text
GET /api/realtime/status
GET /api/realtime/quote/2330
GET /api/realtime/stream/2330?channels=trades,books,candles
```

UI 會在個股圖表下方顯示即時行情連線狀態與收到的 WebSocket 事件。

## 已完成的共用接入

- REST quote、WebSocket `trades`、`books`、`aggregates` 已正規化為
  `stock_ai.realtime_quote.v1`。
- `trades` 更新最新成交、單筆量與累計量；`books` 更新最佳買賣與五檔。
- REST 盤中／歷史 1 分 K 與 WebSocket `candles` 已正規化並保存為
  不可變 revision；1、5、15、30、60 分 K 由相同 1 分 K 重建。
- UI 顯示交易狀態、交易所／接收時間、新鮮度與來源序號。
- SSE 客戶端與伺服器都支援中斷後重連；heartbeat／provider error 不會
  清空最近完整行情。

`STOCK-002` 的保存、交易日重建及分 K 圖層已完成；沒有授權 key 時只
使用明確標示為研究用途的 Yahoo 1 分 K 備援。拿到使用者自己的授權
key 前，Fugle 真實 session 仍維持未驗證，不會宣稱授權實機已通過。
