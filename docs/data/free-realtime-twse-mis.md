# 免費盤中即時行情：TWSE MIS 公開網頁端點

目標：不用付費 API key，先做到像銀行/證券 App、iPhone 股市 App、各大網站那種會自動更新的盤中行情畫面。

## 已接入

系統已接入 TWSE MIS 公開網頁行情報價端點：

```text
https://mis.twse.com.tw/stock/api/getStockInfo.jsp
```

範例：

```text
https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_2330.tw&json=1&delay=0
https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_0050.tw&json=1&delay=0
https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=otc_5483.tw&json=1&delay=0
```

實測可取得：

- 股票代號/名稱
- 交易所 tse/otc
- 盤中時間
- 開盤價
- 最高價
- 最低價
- 昨收/參考價
- 漲停價
- 跌停價
- 累計成交量
- 最新單筆成交量
- 委買五檔價格/張數
- 委賣五檔價格/張數
- 依交易所日期與台北時段明確標示開盤前、盤中、收盤試撮或已收盤
- userDelay：目前回傳 5000 ms，約 5 秒更新

## 系統設定

目前 `.env` 已設定：

```env
REALTIME_QUOTE_PROVIDER=twse_mis
REALTIME_STREAM_CHANNELS=quote,books
```

不需要 API key。

## 後端 API

```text
GET /api/realtime/status
GET /api/realtime/quote/2330
GET /api/realtime/quote/0050
GET /api/realtime/stream/2330?channels=quote,books
```

`/api/realtime/stream/{symbol}` 使用 SSE，每 5 秒從 TWSE MIS 拉一次並推到前端。
快照與 SSE 都使用 `stock_ai.realtime_quote.v1`；只有價格、單筆量、
累計量、交易狀態或五檔實際變更才送 `quote`，未變時送保有完整行情的
`heartbeat`。來源暫時失敗會送 `provider_error` 並繼續重試。

## 實測結果

`GET /api/realtime/status`：

```json
{
  "enabled": true,
  "provider": "twse_mis",
  "configured": true,
  "update_interval_ms": 5000,
  "quote_schema_version": "stock_ai.realtime_quote.v1",
  "stream_schema_version": "stock_ai.realtime_stream_event.v1"
}
```

`GET /api/realtime/quote/2330` 實測取得：

```text
台積電 2330
time 11:46:25
bid1 2460.0 / 282
ask1 2465.0 / 379
userDelay 5000
```

`GET /api/realtime/quote/0050` 實測取得：

```text
元大台灣50 0050
time 11:46:08
bid1 108.05 / 174
ask1 108.1 / 272
userDelay 5000
```

## 重要限制

這是免費公開網頁端點，適合個人本機使用與開發原型。它不是正式授權轉散布行情源。

不可宣稱：

- 正式交易所授權商用行情
- 保證低延遲
- 保證永遠可用
- 可大量轉散布

TWSE MIS 沒有可依賴的個股交易狀態旗標，因此系統會將狀態來源標為
`derived_session_clock`，並依官方正規交易時段與快照日期判定。這能避免
把舊交易日快照標成正在交易，但不能代替交易所停牌公告；授權 provider
若有 `tradingHalt` 等旗標則會優先使用來源狀態。

完整統一欄位、Fugle 映射、SSE 行為與驗證方式見
[Realtime Quote V1](realtime-quote-contract.md)。

如果未來要正式商業化、大量使用者或合法轉散布，仍要走 TWSE 授權或授權資訊商。
