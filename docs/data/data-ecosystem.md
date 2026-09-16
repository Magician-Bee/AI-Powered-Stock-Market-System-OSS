# 股市資料生態系

> Runtime contract: all normalized production reads must use
> [`MarketDataPlatform`](../architecture/unified-market-data-platform.md).
> Direct connector access described below is inventory and migration context,
> not an alternative production data path.

## 1. 資料分類總覽

| 類別 | 內容 | 更新頻率 | 用途 |
|---|---|---:|---|
| 即時/延遲行情 | 價格、成交量、買賣價、盤中漲跌 | 秒級~分鐘級 | 即時查詢、盤中異動、預警 |
| 歷史行情 | OHLCV、調整後價格、技術指標 | 日/分鐘 | 趨勢、回測、技術分析 |
| 基本面 | 營收、EPS、毛利率、ROE、資產負債表、現金流 | 月/季 | 估值、成長性、財務健康 |
| 籌碼面 | 外資、投信、自營商、融資融券、借券、持股分布 | 日/週 | 資金流、短線力道、風險 |
| 產業鏈 | 產業分類、上下游、供應商、客戶、同業 | 低頻/事件更新 | 族群連動、同業比較 |
| 總經 | 利率、通膨、PMI、GDP、匯率、就業、央行決策 | 日/月/季 | 大盤方向、估值壓力、景氣循環 |
| 跨市場 | 美股指數、費半、債券、商品、外匯、加密 | 秒級~日 | 全球連動、風險偏好 |
| 新聞事件 | 公司新聞、法說、財報、政策、地緣政治 | 即時 | 原因解釋、事件風險 |
| 文件資料 | 年報、財報、法說逐字稿、研究報告摘要 | 事件更新 | RAG 問答、長文分析 |

## 2. 優先資料來源建議

> 實作時要依授權、費用與穩定性再確認。免費資料可先做 MVP，正式產品應考慮付費 API 或合法授權。

### 台股

- TWSE / TPEx 官方資料：上市櫃行情、基本資料。
- MOPS 公開資訊觀測站：財報、重大訊息、法說。
- Yahoo Finance：歷史行情備援。
- FinMind：台股資料 API，適合 MVP。
- Goodinfo / CMoney / MoneyDJ：資料完整但要注意爬蟲與授權。

### 美股與全球

- Yahoo Finance：行情與歷史資料。
- Alpha Vantage / Finnhub / Polygon / IEX Cloud：API 選項。
- FRED：美國總經、利率、通膨。
- Nasdaq Data Link：總經、商品與金融資料。

### 新聞/事件

- 公司公告。
- 交易所公告。
- RSS 新聞源。
- Google News / GDELT / Event Registry 類服務。
- 法說逐字稿與財報 PDF。

## 3. 資料庫設計方向

### 關聯式資料表

- `entities`：所有股票、ETF、指數、商品、匯率、利率。
- `prices_daily`：日線 OHLCV。
- `prices_intraday`：分鐘/秒級行情。
- `fundamentals_quarterly`：季財報指標。
- `revenues_monthly`：月營收。
- `institutional_flows`：三大法人與資金流。
- `events`：新聞、公告、財報、法說、政策事件。
- `entity_relationships`：產業、供應鏈、概念股、同業關係。

### 時序資料

適合：價格、成交量、利率、匯率、商品、指數。

可用：TimescaleDB、ClickHouse、DuckDB/Parquet 作 MVP。

### 向量資料

適合：新聞、財報文字、法說逐字稿、研究報告摘要。

可用：Chroma、Qdrant、pgvector。

### 圖資料

適合：產業鏈、公司關係、跨市場影響。

可用：Neo4j，或 MVP 先用 PostgreSQL relationship table。

## 4. 資料品質規則

每筆資料進入系統前要檢查：

- 時間戳與交易日是否正確。
- 價格是否為負或異常跳動。
- 成交量是否缺失。
- 股票代號是否能對應到 entity master。
- 不同來源衝突時保留 source priority。
- 保存原始資料 raw data，清洗後資料 clean data 分開。

## 5. MVP 資料優先順序

1. `entities`：股票/指數/匯率/利率/商品主檔。
2. `prices_daily`：日線行情。
3. `fundamentals_quarterly`：季財報與估值。
4. `institutional_flows`：法人籌碼。
5. `events`：新聞與公告。
6. `entity_relationships`：產業、概念股、同業。
7. `macro_series`：利率、匯率、通膨、PMI、主要指數。
