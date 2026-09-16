# 現行掛牌身分與資料覆蓋審計（2026-09-12）

本輪修復 39 檔上市普通股被舊上櫃代號遮蔽的讀取問題，並量化主檔與官方目錄的剩餘缺口。這是 M2／任務 05、07 的有限修正與驗證；**任務 10 的完整資料覆蓋帳本尚未完成，M1 交易閉環亦未完成**。

基準 commit 為 `5ed1317ee2ea35541ca4b85b81f3cb0989ad84ae`，工作分支為 `codex/fix-current-listing-selection-20260912-2253`。初次 probe 的 `head` 是基準 commit；實際受測工作樹另記在報告的 `source_hashes`，不能把基準值當成本輪最終部署版本。唯讀調查與離線驗證没有外部來源請求；後續實際 UI 掃描與圖表驗收有取得官方批次及圖表資料，必須分開描述。

## 39 檔舊 alias：根因、修正與驗證

根因重播（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/coverage-root-cause.json`） 精確重現原快照的 4,606 個代號。39 檔皆已有 active TWSE entity，且位於原始前 5,000 筆內；例如 `1558.TW`、`1795.TW`、`6446.TW`。舊 `list_entities()` 只按 identifier 的 `created_at` 取最後寫入資料，未核有效期間與 entity 現行市場，因而把較晚匯入、已失效的 `.TWO` alias 當成現行代號。這 39 檔不是主檔上限造成的遺漏，也不是原 entity 缺失。

修正位於 `warehouse.list_entities()` 與 `MarketDataPlatform.securities()`：以有效期間、現行 venue、尾碼一致性及唯一代號選取讀取標籤；以 registry 的 `normalized_value` 收斂大小寫、外圍空白及多來源的相同代號，兩個不同有效代號則留下歧義。市場與 venue 篩選在 SQL `LIMIT` 之前執行，避免小頁面先被其他市場占滿。既有 entity ID、identifier、生命週期及歷史證據均不因讀取修正而重寫。

`delisted`、`expired`、`pre_listing` 可保留同市場的歷史研究標籤，但不能變成 active。缺失或歧義身分仍留在 master，標示 `listing_identifier_status=unavailable_or_ambiguous`、`trading_status=unknown`，商品分類亦為 unknown；metadata 的舊標籤不能恢復可交易身分。

正式庫唯讀 probe（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/current-listing-probe-v1.json`） 於台北時間 22:52:58，以 SQLite URI `mode=ro`、`query_only`、`BEGIN` 讀取，未執行平台建構／遷移，亦未使用 `immutable`。它使用正式庫既有身分及保留的官方原始資料，確認 39 檔全數回到正確 `.TW`、商品分類 verified，active 批次不再包含它們的舊 `.TWO`；TWSE／TPEx／興櫃的 `limit=1` 均返回指定市場。

| probe 分母／結果 | 筆數 |
|---|---:|
| 有界 master 查詢，`limit=5000` | 5,000 |
| `trading_status=active` 研究批次 | 4,477 |
| 該 active 批次中的普通股分類 | 2,324 |
| 該 active 批次分類 verified／unknown | 3,886／591 |
| master 的 identifier resolved／缺失或歧義 | 4,863／137 |

**137 筆仍只在 master 可見，scan 的 active 篩選會排除它們。** 4,477 是身分有效的有界研究批次，不是全市場清冊、已研究檔數或交易資格。分類 verified 也不代表報價、歷史、成本、策略或風控驗證完成；591 unknown 不能當成行情無機會。5,000 筆總上限仍存在，故修正後覆蓋仍不完整。

初次回歸為 319 項。獨立評審補上正規化代碼、有效期相等邊界及單筆無標籤不能清空整批主檔後，最終核心回歸（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/listing-final-regression.log`） **322 passed in 37.56s**，包含 28 項新掛牌測試；畫面回歸（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/listing-ui-regression.log`） **58 passed in 1.07s**，涵蓋搜尋、靜態頁面與因子展示。兩組檔案不重疊，合計 **380 passed**，不累加較早的重跑結果。

正規化修正後的 probe v2（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/current-listing-probe-v2.json`） 再次取得相同主檔數字及 39 檔結果，商品門檻通過 1,936；檔案 SHA-256 `b5b06089f3eac9d4165869c40f5a747c7d569581fc13e72601907e8493de939a`。這次本地讀取 5.19 秒，前次 3.20 秒，均不是並行 UI 延遲保證或全市場吞吐量資格。

## 官方目錄未匹配的 2,071 筆

完整唯讀比對（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/audit-v1.json`） 在正式庫 55,985 entities／114,857 identifiers／0 merges 的快照上，精確重現前次維護的 2,071 個 unmatched bindings；截至該次讀取，未匹配集合沒有改變。這一集合與上述「已有 entity、選錯 alias」的 39 檔是不同問題。

| 官方目錄 | 權證 | ETF | 其他證券 | 普通股 | 合計 |
|---|---:|---:|---:|---:|---:|
| 上市 TWSE | 507 | 0 | 0 | 0 | 507 |
| 上櫃 TPEx | 1,554 | 1 | 4 | 0 | 1,559 |
| 興櫃 TPEx-ESB | 0 | 0 | 0 | 5 | 5 |
| 合計 | 2,061 | 1 | 4 | 5 | 2,071 |

分類與純記憶體重現（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/interpretation-v1.json`） 將它們分為：713 筆未來掛牌且無舊身分的權證、38 筆未來掛牌但代碼已有到期身分的權證，以及 1,320 筆掛牌日不晚於目錄日期、沒有符號／代碼／同名身分的資料。**未來 751 筆掛牌日為 9/14–9/15，不可計為目前 active。**

1,320 筆包含 1,310 權證、ETF `00838B.TWO`、其他證券 `01111S.TWO`–`01114S.TWO`，以及興櫃 `6950.TWO` 科科科技-KY、`7686.TWO` 捷立康、`7914.TWO` 碩明綠能、`7932.TWO` 昱鐳應材、`7934.TWO` 鋒霈環境科技。此 unmatched 集合中沒有 TWSE／TPEx 普通股，也未找到 provisional 或跨 venue 身分候選。1,300 筆非未來 TPEx 權證在 8/26–9/11 掛牌，而本機 TPEx 權證 entity 最後更新為 8/25，支持來源身分覆蓋過舊的判斷；Host `updated_at` 不是獨立的官方來源時間證明。其餘缺失的確切來源原因與現行交易狀態仍待核實。

38 筆權證另有尚未修復的 UUID 碰撞風險：`security_lifecycle._identity_aliases()` 使用 `warrant:{code}`，名稱不符而拒用舊 entity 後，`_stable_entity_id()` 仍可能生成相同 ID。例 `085974.TW`：舊「華邦電統一57購01」於 7/1 到期；目錄的新「東元元大63購04」、ISIN `TW26Z0859747` 將於 9/14 掛牌，純記憶體 consolidation 卻仍產生 `ENT-256e7be18ee55fe894d51fd0b9e6896f`。38 筆皆重現相同舊 ID／不同新名稱，未持久化。

因此不能以全面刷新生命週期來直接補齊 unmatched。商品分類同步應維持 metadata-only；引入再發行權證前須有區分發行批次的身分依據，例如官方 ISIN 與 venue，保留舊 ID、有效期間及歷史資料。其他缺失則須以明確、有界、具完整來源證據的身分維護另行處理，不能把目錄存在當成 active 或新單資格。

原始資料來自前次保留的 取得紀錄（未隨公開版提供；原參考：`../../output/product-identity-investigation-20260912/official-fetch-receipts.json`）；本輪逐一比對正式庫保留 raw 的內容、HTTP 狀態、URL、dataset、parser 及雜湊，未重新取得外部資料。目錄日期均為 2026-09-12，只代表來源版本，不是歷史公開時間的獨立證明。

| 原始檔案 | 檔案／raw SHA-256 |
|---|---|
| isin-mode-2.html（未隨公開版提供；原參考：`../../output/product-identity-investigation-20260912/isin-mode-2.html`） | `503d056d11b0adb180bf30e72e32e2caf6df7f858e1e59bbfbe8b077e4f58645` |
| isin-mode-4.html（未隨公開版提供；原參考：`../../output/product-identity-investigation-20260912/isin-mode-4.html`） | `a1b03565bfe1872d2d4e8be1fb5d80484724ca01fd77b045c46661120a27f4b4` |
| isin-mode-5.html（未隨公開版提供；原參考：`../../output/product-identity-investigation-20260912/isin-mode-5.html`） | `046ef4223841b9c86e44daabe984b601bf61a5690a648fc7b2ee911f13c3788c` |

## 任務 10 與 M1 的未完成範圍

任務 10 尚未完成。既有 `autonomous_deep_research_coverage` 只記錄曾被深度選取的代號；`success_count>0` 是「曾成功」，不是目前資料仍有效。仍缺完整掛牌分母、從未研究者、精確 entity／venue、各資料領域狀態、證據／cycle 關聯、到期及下次更新時間。後續應以完整現行清冊連接既有 coverage 與證據，分清未研究、受預算排除及資料過期，保留每輪 20 檔與 600 秒界線。

M1 唯讀狀態（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/m1-current.json`） 於台北時間 **22:44:23**：自主紙上帳戶現金及已交割現金均為 TWD 1,000,000；計畫、委託、成交、持倉、平倉成果皆為 0，已實現損益為 0。這是未交易狀態，不能作為獲利或閉環證據。模型當日預算已用 **1／1**，剩餘 0；未重設或追加。依現行日曆、授權與持續運行條件推得的下次自動研究資格時間為 **9/14 14:30（台北）**，不是本輪新增排程或保證會成交。

## 證據檔案識別與待補驗收

以下皆為本機 ignored `output/` 產物；相對路徑自本文位置解析。**檔案 SHA-256** 計算包含縮排、換行與 receipt 欄位的完整檔案位元組；**receipt hash** 是移除頂層 `receipt_sha256` 後，以排序鍵、UTF-8、無多餘空白的 canonical JSON 計算，兩者不可互換。

| 報告 | 檔案 SHA-256 | 頂層 receipt hash |
|---|---|---|
| `coverage-root-cause.json` | `a5f4d994022d3693e878edfcc72913797ad47973ccad7d30dd39cb1483ff2dad` | `d96933add38158072361d15c9454b8848a7d2254755f484e83e285b964b0e826` |
| `current-listing-probe-v1.json` | `30c2b06afa685fa4bbf515bdfb8ecca4f92c1011ee20730f974ffdfb3397cea6` | 無頂層 receipt hash；內含個別 assessment hash 不是整份報告 hash |
| `audit-v1.json` | `e210f500c63007f3660ffeb7469f2d98018b098f8149c9738c97d9dd7bdbacdb` | `ca80149712017a47c138cb8da784923b2c2d6d5f582f45bca1e2aaaca46e824e` |
| `interpretation-v1.json` | `fff36949ad2394b8c492448e44a0bbb3f2c2e7e4697f7f65c2ab435ab68e0e82` | `2270d51971bc02926e68a35fc99e596e8c32ddffe4d0186e21a02b673cdf3dad` |
| `m1-current.json` | `c0d1406b9dd7085068b54a2c112bb60718f26037b09b766d0d98d751f5089d7c` | 無頂層 receipt hash |
| `listing-regression.log` | `3b8b94afec9098767506f732194f432684f486ded4c1e69b553fdd692ad96a3e` | 不適用 |

重現入口：根因（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/capture_coverage_root_cause.py`）、現行掛牌 probe（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/verify_current_listing.py`）、未匹配比對（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/audit_unmatched.py`）、UUID 純記憶體檢查（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/interpret_unmatched.py`）。既存報告記錄的是當次讀取，不應以後續重跑結果取代原證據。

## 實際 UI 與發布核對

工作樹部署 PID `97335`、source fingerprint `d239f44bcf1ce0bd` 通過既有服務實例核對。22:57:57 按下「執行市場掃描」後保存 `MIS-e5e91575987449a2b54123d41aa72967`，畫面顯示 4,477 檔、商品門檻通過 1,936、已核驗 3,886、待核驗 591，深入分析及完整有效資料皆 0。搜尋 `1558.TW` 正常顯示伸興、普通股／來源核驗及商品門檻通過；報價來源 `TWSE_ALL_QUOTES`、資料日 9/11、價格 108.5，仍明示部分資料與缺少獨立同日觀測，不可當成立即可執行。圖表亦顯示 243 根已驗證 K 線；此為該次畫面結果，不代表全批次歷史齊全。

這次 TPEx 批次真實發生 `IncompleteRead`，2,161 筆候選保留來源錯誤與缺報價狀態；全批次 partial 1,367／insufficient 3,110。TWSE 的 `1558.TW` 保留可供研究的單一來源報價。這是既有單市場失敗隔離的實際觀測，不是兩交易所資料皆完整；不能用商品分類數量掩蓋行情取得失敗。快照 DB 原始 payload SHA-256 為 `b4dd92b7d9bca324f035dedebf10670d873c2eb23e26c093ba133bbb906fcef0`，UI 驗證 receipt hash 為 `76476d5554a1999a4db44732d117498ccee7e40c4081b2f1b704e98b7c4b4d55`；證據來自持久 candidate detail 投影，並非額外保存的原始 features 陣列。

畫面的數量標籤改為「本批次研究範圍」「本批次已分類」，避免 5,000 筆上限被誤讀為完整母體。初次 UI 掃描使用正規化邊界補丁前的工作樹；提交版本的重啟、本地讀取、顯示及帳戶狀態另見發布收據。已保存快照保持原版本，不重寫歷史。

正式保存的快照、原文核驗及新模型／帳戶檢查見 UI 快照收據（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/ui-snapshot-verification.json`）；最終 commit、GitHub 分支／main、README／程式 blob 與部署一致性見 發布收據（未隨公開版提供；原參考：`../../output/unmatched-catalog-investigation-20260912/release-verification.json`）。本輪工程驗收不將 M1、完整覆蓋或正期望值改為通過。
