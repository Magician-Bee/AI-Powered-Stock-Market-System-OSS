# Intraday Candle V1

`STOCK-002` 將每個可用來源先保存為不可變、可追溯的 1 分 K revision，
再以同一組已保存 1 分 K 重建 1、5、15、30 與 60 分 K。指定日期查詢
不會讀日 K 補值，也不會把缺少成交的分鐘向前填滿。

## 交易時段與重建規則

- 時區固定為 `Asia/Taipei`。
- 正規交易時段固定由 09:00 錨定至 13:30，共 270 分鐘。
- 每個較大週期的開、高、低、收與成交量，依所含 1 分 K 依序聚合。
- 60 分 K 的最後一根是 13:00–13:30 的 30 分鐘交易時段尾段。
- 缺少分鐘仍會重建現有資料，且回傳
  `missing_session_minute_count`；`complete` 表示來源宣告批次完整，不代表
  每分鐘都有成交。盤中批次或 quote 樣本才標示 `partial`。
- `as_of` 只選擇在指定知識時間前已取得的 revision 與 import receipt，
  因此歷史研究不會看到稍後才修訂的值。

完整交易日的預期根數如下：

| 週期 | 根數 |
| --- | ---: |
| 1 分 | 270 |
| 5 分 | 54 |
| 15 分 | 18 |
| 30 分 | 9 |
| 60 分 | 5 |

## 儲存契約

schema v25 新增兩張不可變資料表：

- `intraday_candle_revisions`：保存每個來源、分鐘、OHLCV、來源優先序、
  授權屬性、最終狀態、觀測／取得時間、raw hash 與品質欄位。
- `intraday_candle_import_receipts`：保存每次來源批次、交易日、筆數、
  是否為完整批次、請求／完成時間與 response hash。

來源發生修訂時會新增 revision，不會覆寫舊資料。同一分鐘查詢依來源
優先序、最終狀態、觀測時間與取得時間選出最新值；`revision_ids` 仍可
反查實際重建輸入。

## 來源與授權界線

| 來源 | 用途 | 優先序／界線 |
| --- | --- | --- |
| Fugle MarketData | 授權 REST 歷史／盤中 1 分 K與 WebSocket `candles` | 正式授權來源；設定 API key 後優先使用 |
| Yahoo Finance | 最近七日 1 分 K | 研究用備援，`authorized=false`，不可因此取得交易執行資格 |
| TWSE MIS／Fugle quote | 有實際最後成交的盤中分鐘樣本 | 部分資料；沒有成交時不以買賣中價製造 K 線 |

Fugle 官方 HTTP 文件定義盤中 candles 支援 1、3、5、10、15、30、60
分鐘，歷史分鐘資料提供最近 30 天；本系統仍一律保存 1 分 K，避免不同
來源的高週期切桶方式影響重建結果。WebSocket `candles` 事件也先正規化
為同一份 1 分 K 契約。

參考：

- [Fugle Intraday Candles](https://developer.fugle.tw/docs/data/http-api/intraday/candles/)
- [Fugle Historical Candles](https://developer.fugle.tw/docs/data/http-api/historical/candles/)
- [Fugle WebSocket Candles](https://developer.fugle.tw/docs/data/websocket-api/market-data-channels/candles/)

## API

```text
GET /api/data/ui/v1/intraday/candles/status
GET /api/data/ui/v1/intraday/candles/{symbol}/dates
GET /api/data/ui/v1/intraday/candles/{symbol}?date=YYYY-MM-DD&timeframe=1|5|15|30|60
```

`refresh=true` 會先嘗試授權來源；未設定 Fugle key 時才使用明確標記為
研究用的 Yahoo 備援。來源暫時失敗時，API 仍可回傳已保存資料，並在
`refresh.error` 揭露錯誤，不會清空既有 revision。

舊 `/api/intraday/*` 路徑保留相容用途；內建 UI 只使用版本化
`/api/data/ui/v1/*` 邊界。

## UI 與驗證

個股分析頁可選擇日 K 或五種分 K週期，並輸入已保存交易日。狀態列會
顯示來源 1 分 K 根數、重建後根數、來源、完整／部分狀態及可用日期。
即時 quote 與 `candle` SSE 事件只觸發伺服器端已保存資料的節流重讀，
前端不自行製造歷史 K 線。

可重現驗證：

```bash
PYTHONPATH=src python scripts/verify_intraday_candles.py
```

驗證器建立完整 270 分鐘資料及第二個交易日，檢查五種週期、OHLCV、
13:00–13:30 尾段、統一／相容 API 與 UI 契約。
