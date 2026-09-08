# Realtime Quote V1

`STOCK-001` 的目標是讓目前開啟的個股在盤中持續顯示最新成交、
最佳買賣、委買賣五檔、累計成交量與明確交易狀態。來源不同不能改變
前端契約，也不能把歷史收盤或買賣中價標成真實成交。

## 統一契約

TWSE MIS 與 Fugle REST／WebSocket 都正規化為
`stock_ai.realtime_quote.v1`。核心欄位如下：

| 類別 | 欄位 |
| --- | --- |
| 身分 | `provider`, `source`, `authorized`, `symbol`, `exchange`, `market_session` |
| 最新成交 | `last_price`, `last_trade_size_lots`, `change`, `change_percent` |
| 買賣報價 | `best_bid`, `best_ask`, `bids[0..4]`, `asks[0..4]`, `complete_five_levels` |
| 成交統計 | `total_volume_lots`, `total_volume_shares`, `turnover`, `inner_volume_lots`, `outer_volume_lots`, `transaction_count` |
| 交易狀態 | `trading_status`, `trading_status_source`, `is_market_open` |
| 時間與新鮮度 | `exchange_timestamp`, `received_at`, `quote_age_ms`, `freshness`, `is_stale`, `sequence` |

不存在的來源欄位固定回傳 `null`，不以其他資料推測。例如 TWSE MIS
未提供可靠成交金額時，`turnover` 保持 `null`。

## 來源正規化

### TWSE MIS

- 約五秒取得一次目前標的的公開網頁快照。
- `z` 是最後成交，`tv` 是最新單筆量，`v` 是累計量。
- `b/g` 與 `a/f` 分別轉為最多五檔的價格／張數。
- MIS 未提供可依賴的狀態旗標，因此 `trading_status_source` 標為
  `derived_session_clock`。狀態依快照日期、台北時間與 TWSE 正規交易時段
  區分 `pre_open`、`trading`、`closing_auction`、`closed`；舊交易日
  快照絕不標成盤中開市。
- 這個來源適合本機個人觀看，但 `authorized=false`，不會因此取得商業
  轉散布或真實交易執行資格。

### Fugle 授權行情

- REST quote 的 camelCase 欄位會轉為同一份 snake_case 契約。
- `lastTrade`、`total`、`bids`、`asks`、`serial` 與 `lastUpdated`
  分別保存成交、累計量值、五檔、序號與交易所時間。
- `tradingHalt.isHalted`、`isTrial`、`isDelayedOpen`、
  `isDelayedClose`、`isOpen`、`isClose`、`isContinuous` 具有來源旗標
  優先權，`trading_status_source=provider_flag`。
- WebSocket `trades` 與 `books` 事件會合併至最近完整快照，再輸出
  `stock_ai.realtime_quote.v1`；前端不需要理解來源專屬欄位。

來源格式依
[Fugle Intraday Quote](https://developer.fugle.tw/docs/data/http-api/intraday/quote/)、
[Fugle Trades](https://developer.fugle.tw/docs/data/websocket-api/market-data-channels/trades/)、
[Fugle Books](https://developer.fugle.tw/docs/data/websocket-api/market-data-channels/books/)
的官方欄位定義。交易時段依
[TWSE Trading Mechanism](https://www.twse.com.tw/en/products/system/trading.html)
所列正規交易 09:00–13:30、開收盤試撮與五秒快照規則。

## API 與連續更新

```text
GET /api/realtime/status
GET /api/realtime/quote/{symbol}
GET /api/realtime/stream/{symbol}?channels=quote,books,candles
```

`/status` 公布 quote／stream schema、SSE transport 與六項能力。
stream 使用 `stock_ai.realtime_stream_event.v1` envelope，傳送
`event`、可用的來源 `sequence`、`retry: 1500`，並設定
`Cache-Control: no-cache, no-transform` 與 `X-Accel-Buffering: no`。

TWSE MIS 在價格、單筆量、累計量、交易狀態或五檔任一項變更時發送
`quote`；內容未變時發送帶完整最近快照的 `heartbeat`。來源暫時錯誤時
連線不會中止，而是發送 `provider_error` 與 `last_good_data` 後繼續重試。

Fugle 的 `quote` channel 會映射為官方 `aggregates` channel；無效 channel
不會送到上游。`trades`、`books`、`aggregates` 的更新都輸出統一 quote。
`candles` 保留獨立事件，正規化並保存為 1 分 K，再依
[Intraday Candle V1](intraday-candle-contract.md) 重建五種週期。

## UI 規則

個股頁顯示：

- 中文交易狀態與新鮮度；
- 最新成交價、單筆量與報價時間；
- 最佳委買／委賣價格及張數；
- 完整委買、委賣五檔表；
- 累計成交量、更新方式與序號。

UI 只會把 `stock_ai.realtime_quote.v1` 送入行情卡與圖表。驗證／心跳等
非 quote event 會保留最近正規化快照，不會清空或用其他標的資料覆蓋。

## 驗證

```bash
PYTHONPATH=src python scripts/verify_realtime_quotes.py
```

驗證器以可重現的 TWSE MIS 與 Fugle 官方結構 fixture 驗證兩種來源欄位、
交易狀態、五檔、連續兩次價格／累計量更新、API 200 與 UI 契約。真正的
Fugle 授權連線仍需使用者自己的 API key；沒有憑證不會被標成實機驗收。
