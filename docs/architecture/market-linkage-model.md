# 市場連動模型

## 1. 目標

市場連動模型要把「市場變數」和「股票/產業」之間的關係系統化，讓 AI 不只是查資料，而是能解釋：

- 哪些因素可能影響某檔股票？
- 某個總經或跨市場事件會傳導到哪些產業？
- 這種影響是正向、負向還是不確定？
- 歷史資料是否支持這種連動？

## 2. 連動關係資料結構

```yaml
relationship:
  source_entity: US10Y_YIELD
  target_entity: GROWTH_STOCKS
  relation_type: valuation_pressure
  direction: negative
  mechanism: "利率上升提高折現率，壓縮高成長股估值。"
  confidence: 0.75
  evidence:
    - historical_correlation
    - macro_theory
    - recent_market_reaction
```

## 3. 常見市場變數與影響路徑

### A. 美債殖利率

```text
美債殖利率上升
  → 折現率上升
  → 高本益比/高成長股估值壓力
  → NASDAQ、費半、AI 概念股承壓
  → 外資風險偏好下降
  → 台股電子權值股可能受壓
```

可能負向：

- 高本益比科技股。
- 半導體成長股。
- 長天期現金流資產。

可能正向：

- 銀行、壽險等部分金融股，需看殖利率曲線與投資部位。

### B. 美元指數 / USD/TWD

```text
美元走強 / 台幣貶值
  → 出口商匯兌利益可能增加
  → 外資撤出台股壓力可能增加
  → 進口成本上升
```

可能受惠：

- 出口導向：半導體、電子零組件、工具機。

可能受害：

- 進口原料成本高、美元負債高的公司。

### C. 油價

```text
油價上漲
  → 運輸/航空成本上升
  → 石化上游可能受惠
  → 通膨壓力增加
  → 利率預期改變
```

可能受惠：

- 能源、石化上游。

可能受害：

- 航空、運輸、高耗能產業。

### D. 半導體景氣

```text
費半 / SOX 上漲
  → 全球半導體風險偏好上升
  → 台股半導體供應鏈同步反應
  → 設備、IC 設計、封測、材料族群擴散
```

追蹤指標：

- SOX、NASDAQ、NVDA、AMD、ASML、TSM ADR。
- DRAM/NAND 價格。
- 半導體設備訂單。
- 台積電營收與法說資本支出。

### E. AI 伺服器供應鏈

```text
雲端資本支出增加
  → GPU / ASIC 需求增加
  → 伺服器 ODM、散熱、電源、PCB、連接器受惠
  → 供應鏈營收與毛利變化
```

關聯實體：

- GPU：NVDA、AMD。
- 晶圓代工：TSM。
- 伺服器：ODM/OEM。
- 散熱：水冷/風扇/熱交換。
- 電源：PSU、BBU。
- PCB/CCL：高速材料。

## 4. 連動分析方法

### 規則式連動

先建立人工可解釋規則，適合 MVP：

- 利率上升 → 成長股估值壓力。
- 油價上升 → 航空成本壓力。
- 台幣貶值 → 出口商匯兌影響。

### 統計式連動

用歷史資料計算：

- 相關係數 correlation。
- 滾動相關 rolling correlation。
- 領先落後關係 lead-lag。
- 異常日事件研究 event study。

### 知識圖譜連動

把公司、產業、商品、匯率、利率連成圖：

```text
US10Y → Growth Valuation → NASDAQ → SOX → Taiwan Semiconductor Supply Chain → 2330.TW
```

## 5. 回答時的信心分數

系統不要只說「會影響」，要標記信心：

- 高：規則明確 + 歷史資料支持 + 近期市場同步反應。
- 中：規則合理，但近期資料不完全同步。
- 低：只有敘事，缺乏資料支持。

## 6. MVP 實作順序

1. 建立連動規則表 `linkage_rules`。
2. 建立 entity relationship 表。
3. 寫 `explain_linkage(source, target)` 函式。
4. 加入 historical correlation 計算。
5. 把新聞事件接到連動圖。
6. 回答時引用規則、數據與事件。
