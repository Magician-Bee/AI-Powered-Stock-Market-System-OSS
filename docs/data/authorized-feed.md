# 授權盤中秒級即時行情源接入

目標：做到銀行/證券 App 那種盤中秒級跳價，必須使用授權即時行情源，不能用 TWSE OpenAPI 收盤資料輪詢冒充即時。

## 已確認的可行路線

### A. TWSE 直接授權即時交易資訊

來源：`https://www.twse.com.tw/zh/products/information/real-time.html`

TWSE 官方說明：

- 可直接與證交所電腦設備連線。
- 或經由已與證交所簽約之即時交易資訊廠商間接連線取得資訊。
- 非證券商/期貨商傳輸授權費可達每月 60,000 元起，依資訊使用戶數/傳輸方式另計。

適合：正式商業產品、大量用戶、需要合法轉散布行情。

### B. 富邦新一代 API / Fugle-compatible 行情源

來源：

- `https://www.fbs.com.tw/TradeAPI/docs/market-data/intro`
- `https://developer.fugle.tw/docs/data/websocket-api/getting-started`
- `https://developer.fugle.tw/docs/data/http-api/intraday/quote`

能力：

- REST intraday quote：`/intraday/quote/{symbol}`
- WebSocket streaming：`wss://api.fugle.tw/marketdata/v1.0/stock/streaming`
- Channels：`trades`, `books`, `candles`, `aggregates`, `indices`
- 可取得：最後成交、最佳五檔、成交量、分鐘 K、狀態旗標等。

適合：個人/開發階段先做銀行證券 App 的盤中跳價體驗。

## 系統目前已做好的接入層

後端：

- `GET /api/realtime/status`
- `GET /api/realtime/quote/{symbol}`
- `GET /api/realtime/stream/{symbol}?channels=trades,books,candles`

前端：

- `realtimePanel` 會顯示即時行情狀態。
- 有授權 key 時，使用 EventSource 連到後端 SSE。
- 後端再連授權 WebSocket，不把 API key 暴露給瀏覽器。
- 未設定授權源時，會顯示未啟用，不會用輪詢資料冒充秒級跳價。

## 啟用方式

在專案根目錄建立 `.env`，不要提交到 git：

```env
REALTIME_QUOTE_PROVIDER=fugle
FUGLE_MARKETDATA_API_KEY=你的授權行情API_KEY
REALTIME_STREAM_CHANNELS=trades,books,candles
```

重啟系統後檢查：

```text
GET http://127.0.0.1:8000/api/realtime/status
```

應看到：

```json
{
  "enabled": true,
  "provider": "fugle",
  "configured": true
}
```

測試即時報價：

```text
GET http://127.0.0.1:8000/api/realtime/quote/2330
```

測試秒級串流：

```text
GET http://127.0.0.1:8000/api/realtime/stream/2330?channels=trades,books,candles
```

## 不做的事

- 不把 TWSE OpenAPI 收盤資料輪詢成「即時」。
- 不把 Yahoo/yfinance 當成台股盤中秒級跳價。
- 不把 API key 寫進程式碼或文件。
- 不在前端直接放行情 API key。

## 後續正式證券 App 化

已完成：

1. 統一即時 quote 卡片：last price、漲跌、last size、交易狀態與時間。
2. 最佳買賣與委買賣五檔 order book。
3. `trades`／`books`／`aggregates` 合併為同一份 quote 契約。
4. authenticated／subscribed／heartbeat／error 與 SSE 重連狀態。
5. `candles` 正規化為不可變 1 分 K，並可依任一已保存交易日重建
   1、5、15、30、60 分 K。

來源 rate limit、大規模訂閱容量與 Fugle 真實連線仍必須以使用者實際
方案及授權 session 驗收；未取得 key 時不會以研究備援冒充授權來源。
