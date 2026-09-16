# 官方名錄身分與研究範圍正式導入

本輪推進 M2／05、10，把前輪已由正式資料唯讀確認的 2,071 個官方名錄缺口接入既有證券主檔與研究範圍。這只完成發行身分導入與缺值可見性；M1 真模型紙上交易閉環、逐檔多領域資料完整性、M7 前瞻門檻、M8 實盤與 M9 其他市場均未因此通過。

## 寫入契約

`OfficialProductClassificationLoader` 每次取得或命中新鮮名錄 checkpoint 後，會由 `MarketDataWarehouse.sync_catalogue_identities` 再次驗證原始 payload、來源 URL、HTTP 狀態、完整內容雜湊、逐列雜湊、取得時間與來源日期，再交給同一個證券生命週期 writer。來源失敗、逾時、未來取得、過期、重複 ISIN、類型衝突、不可變 identifier owner 衝突均逐筆或逐分區 fail closed，不回填猜測資料。

名錄列只保存可證明的身分、產品類型、venue 與台灣掛牌日。掛牌日前為 `pre_listing`，掛牌日已到但沒有生命週期清冊／行情時為 `unknown`；兩者都不會由名錄自行變成 active。`quote_present=false`，issuer、expiry 與未提供欄位保持缺值。其他 CFI 商品使用泛型 `security`，不冒充股票、ETF 或基金。

權證穩定鍵採 `warrant:{venue}:isin:{isin}`。相同代號的舊到期發行保留原 ID、歷史 alias 與 revision；新 ISIN 建立新實體。API 清冊稍後只有在 fresh、hash-valid 的 venue＋code＋ISIN＋產品類型＋掛牌日證據一致時採用名錄實體，再補 expiry、quote 與生命週期。普通股／ETF 的現行精確 ISIN owner 可排除沒有 ISIN 的歷史同碼列；多個精確 owner 仍拒絕合併。

## 正式資料證據

正式寫入前以標準 SQLite `mode=ro`、`query_only=on` 與讀取交易完成預檢，沒有 HTTP、模型或 SQL 寫入。預檢收據為 `output/catalogue-ingest-20260913/preflight-20260912T162454Z.json`，列出 2,071 個提案、0 個不可變 identifier collision；寫入前快照為 `ui-ingest-before-20260912T163532Z.json`。

原生 App 的「系統 → 證券主檔 → 同步官方主檔」實際執行正式同步後：

| 項目 | 前 | 名錄投影完成 | 完整清冊刷新後 |
| --- | ---: | ---: | ---: |
| `market_entities` | 55,985 | 58,056 | 58,056 |
| `entity_identifiers` | 114,857 | 121,070 | 224,294 |
| `entity_identity_merges` | 0 | 0 | 0 |
| `catalogue_only` | — | 2,071 | 1,574 |
| `catalogue_adopted_by_lifecycle` | — | 0 | 497 |

新增 2,071 個實體與 6,213 個識別碼，正好是每個實體的 exchange code、display symbol 與 ISIN。類型為 warrant 2,061、stock 5、ETF 1、security 4；生命週期為 unknown 1,320、pre-listing 751。後續完整 TWSE 清冊刷新另為既有實體補齊來源 alias，因而全庫 identifier 總數會高於上表；這是同輪的真實 API adoption，不能誤寫成名錄投影自行新增的數量。

第一輪清冊採用揭露一個既有 `2301` 歷史同碼歧義，修正後第二輪三個 `*:identity` checkpoint 全部 `succeeded` 且 unresolved 為 0。TPEx 完整清冊在第一輪真實遇到 `IncompleteRead`，整體 loader 因此保留 `partial`，但已成功驗證並提交的三份 ISIN 原文及其他分區不回滾。這個來源故障不會轉成假成功。

只讀後驗證逐筆檢查 2,071 個實體、6,213 個名錄 identifier、2,071 筆不可變 revision 與 2,071 個生命週期事件，並核對每筆目前引用的原始 payload、來源資料集、URL、完整 wire hash 與逐列 hash。38 個舊權證實體、76 個舊 identifier、38 筆歷史 revision 的逐列 hash 均未改，merge 仍為 0。紙上帳戶 `autonomous-paper-v1` 的現金、已結算現金與權益仍為 TWD 1,000,000；計畫、委託、broker 委託、成交、持倉與 outcome 都是 0。最終唯讀收據為 `output/catalogue-ingest-20260913/ui-ingest-after-20260913T034246Z.json`，validation 全部零差異、`account_unchanged=true`、`passed=true`，收據 SHA-256 為 `b1c758a1e2df98b10f9a5327ef312e2a1971bcd834877cd52961710902207324`。

## 研究與進場邊界

市場特徵 store 的本機主檔上限由 5,000 提高至 100,000，並保留 unknown、pre-listing、listed-pending-quote 與 suspended 實體。沒有符合該 issuance 的行情時，價格、因子與衍生欄位維持空值並列出原因；TPEx-ESB 不借用 `.TWO` 行情，同代號的多個目前／未來候選輸出 conflict placeholder，不猜其中一個。expired 與 delisted 不進入目前研究範圍。

這些研究列不會繞過既有新增部位門檻。只有已核驗普通板普通股、有效生命週期、正確來源行情與其他既有風控證據才可成為新計畫；名錄存在本身不構成 active、可交易或正期望值。搜尋 API 同時回傳 `is_active` 與原始 `lifecycle_status`；App 只有在兩者都表示 active 時才建立可點擊列與 K 線按鈕。

## 驗證與剩餘缺口

離線單元、整合與 UI 契約涵蓋名錄寫入、重跑冪等、來源失敗、保留 identifier owner、生命週期採用、同碼隔離、research visibility、TPEx-ESB source isolation、空白搜尋與 explicit refresh。最終集合為 **566 passed、0 failed**，JUnit 與 log 分別保存於 `output/catalogue-ingest-20260913/regression-final-v3.xml` 與 `regression-final-v3.log`；較早的失敗重跑仍保留，沒有覆寫成成功。

原生 App 實際搜尋 `00838B` 得到一筆 `00838B.TWO`，顯示「生命週期待核驗」及 disabled「僅供研究」；搜尋 `085974` 同時得到新發行 `東元元大63購04`（預上市）與舊發行 `華邦電統一57購01`（已到期），兩筆都有獨立身分且不能開圖。完整刷新後 UI 的台灣生命週期摘要為 58,048；實體表跨市場物理總數為 58,056，兩者統計範圍不同，不能互換。TPEx 完整清冊的 `IncompleteRead` 仍使該次 loader 顯示部分完成，三個 ISIN identity checkpoint 則均成功且 unresolved 為 0。

下一個自然 M1 驗收點仍是 2026-09-14 14:30 Asia/Taipei 之後、既有 scheduler、來源、模型預算與 provider 同時允許時的模型複查。不得為了取得成交樣本修改預算、日期、策略或強制造單；無合格提案時可保留現金，但不算真模型買賣閉環完成。
