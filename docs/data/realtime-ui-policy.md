# 即時資料顯示規則

使用者要求：頁面上有列出來的股市資訊與參數，都必須由盤中即時資料更新，不能混用收盤資料讓人誤解。

## 已套用規則

1. 個股詳情頁顯示的行情卡片改由 `/api/realtime/quote/{symbol}` / SSE 推播更新。
   兩個介面都只接受 `stock_ai.realtime_quote.v1`，不同 provider 的原始
   欄位不會直接流入 UI。
2. `/api/market/{symbol}/summary` 對台股也改為優先回傳 TWSE MIS 盤中資料，而不是只回傳收盤資料。
3. 圖表改為盤中即時圖：
   - 價格軸：由即時成交價或即時 bid/ask 中價計算。
   - 時間軸：由 TWSE MIS 回傳的盤中時間更新。
   - Bid 線：即時委買一檔。
   - Ask 線：即時委賣一檔。
   - 成交/中價線：若 MIS 當下 `z`/最後成交為 `-`，用即時 bid/ask 中價顯示，並不冒充為成交價。
   - MA5/MA10/MA20/MA60：由本頁收到的盤中即時樣本計算。
   - BOLL(20,2)：由本頁收到的盤中即時樣本計算。
   - MACD(12,26,9)：由本頁收到的盤中即時樣本計算。
   - 累計量/量柱：由 MIS 累計成交張數變化計算。
4. 頁面不再先顯示日收盤 OHLC 卡片再等即時資料覆蓋；會先顯示「等待盤中報價」，收到即時資料才填入參數。
5. 新聞、法人、融資融券、財報等尚未做成即時/最新模組前，不放在主要即時行情卡片中。
6. 卡片固定顯示交易狀態、新鮮度、最新單筆量、最佳買賣、委買賣五檔、
   累計成交量與來源序號。非行情的驗證／心跳事件不得清空最近行情。

## 已驗證

- `GET /api/market/2330.TW/summary` 回傳 `TWSE MIS public intraday quote endpoint`。
- `latest_price.date` 包含盤中日期與時間，例如 `20260702 12:00:34`。
- `GET /api/realtime/quote/2330` 回傳統一契約、盤中開高低、最新成交與
  單筆量、累計量、最佳買賣、五檔、交易狀態及時間新鮮度。
- `GET /api/realtime/stream/2330?channels=quote,books` 會持續送出 SSE
  `event: quote`／`heartbeat`；來源錯誤會留在同一連線重試。
- `scripts/verify_realtime_quotes.py` 可重現驗證 TWSE MIS、Fugle、
  連續兩次更新、API 與 UI 契約。

## 注意

TWSE MIS 目前回傳 `userDelay=5000`，代表約 5 秒更新。這符合免費網頁行情常見模式，但不是交易所正式商業轉散布授權源。
