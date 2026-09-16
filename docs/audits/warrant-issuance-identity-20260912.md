# 權證發行身分與來源恢復驗收（2026-09-12）

本輪推進 M2／任務 05、07：修正權證代碼重用會覆寫已到期商品的風險，並接通既有官方名錄、生命週期及 Entity Registry。基準為 `6da549fe4f97493b35a5eeb51caddc0b5c05c7b9`，工作分支為 `codex/fix-warrant-issuance-identity-20260912-2320`；受測工作樹以各收據的來源檔案雜湊識別，最終提交／部署見發布收據。**M1 真模型買賣閉環、完整市場覆蓋及正期望值資格仍未通過。**

## 身分規則與實際來源

原權證 UUID 使用 `warrant:{code}`，即使新名稱與舊 entity 不符，仍會生成同一 ID。先前已在 38 筆未來掛牌、沿用到期代碼的官方名錄項目重現此問題。新權證改以 venue 與官方 ISIN 建立穩定 ID；真正掛牌日界定代碼有效期，不使用履約開始日、可變到期日或名稱作為新 UUID 的依據。

真正的 TWSE／TPEx 權證 API 保存資料均不含 ISIN；TWSE 的「履約開始日」也不是掛牌日。新 `catalogue_identity` 投影精準回讀既有三個 ISIN checkpoint、取得收據及完整原文，沿用原始位元組、URL、HTTP、parser 與雜湊核驗。v1 商品分類 receipt 不新增欄位，避免破壞既有收據的完整性比較。掛牌日期另採嚴格日曆解析，保留 day precision，台灣日初轉為 UTC；這不構成歷史公開時刻的證明。

來源取得最多六小時、來源日期最多落後台灣曆日一日；未來取得、失敗 checkpoint、重複 ISIN、日期或名稱衝突均拒絕建立新發行身分。未來掛牌保留 `pre_listing`；權證清冊不再自行產生 `quote_present=true`。來源缺失時保存原文與逐筆原因，不能由「新目錄沒有這筆」推定舊商品下市。

舊 code-only entity 只在同 venue、代碼、名稱及已知存續期間支持官方原始掛牌日時沿用；缺失或無效到期日、相交期間的無法解釋更名、複數候選均保留 unresolved。已知相同 ISIN 的更名、到期日更正或展延沿用身分。實際 `03007X`、`03010X` 的兩個保存版本各有原文「展延」說明：到期由 2026-09-02 改為 2027-03-02，而官方掛牌日仍為 2014-07-31；不能把到期日變化當成另一次發行。

識別碼沿用既有唯一鍵，以真正掛牌日與舊空起日並存，不放寬跨 owner 寫入拒絕。查詢現在即使未指定 `as_of` 也核對有效期；不同權證 ID 不再因「較新、active」而自動合併。舊空起日無可信結束時間而與新發行重疊時，解析保持歧義。舊錯誤履約期間的啟動修復遇到唯一鍵衝突也會保留原列並回報缺項。

商品分類另核對權證 owner 的商品類型與已知 ISIN，避免新代碼收據落到舊權證而取得普通股進場資格。新抓取的相同發行證據更新當前原文／checkpoint，不會僅因取得時間或 receipt hash 改變而新增相同業務 revision；完整來源證據仍保存。

## 來源失敗與恢復

名錄先於生命週期載入；新建普通股／其他商品亦可在同輪接到已核驗、已達掛牌日的精確分類，避免等待下一次目錄下載。名錄只是身分證據，不偽裝成公司、價格或完整研究資料。

隔離的實際 loader 測試另重現並修正兩項問題：強制刷新失敗後，舊成功快取會讓普通重試誤判 fresh；同份原文先 HTTP 503、後 HTTP 200 時，共用位元組物件會保留首次 503 而使新成功取得無法核驗。現在失敗會使相應名錄或主檔快取失效；每次 HTTP 的不可變取得收據負責該次傳輸真實性，共用物件負責來源與位元組完整性，首次錯誤記錄保留不改寫。

部分身分已提交而仍有 unresolved 時，來源批次不會被認證為完整成功。既有 ingestion run／batch 保存逐筆原因及 raw ID；下一次成功覆蓋最新 checkpoint 後，原失敗批次仍能回查。這是來源身分缺項紀錄，**尚非任務 10 的完整逐領域研究覆蓋帳本**。

## 真實資料唯讀與離線重播

所有正式 DB 查詢使用 SQLite URI `mode=ro`、`query_only` 與讀取 transaction；不建構會遷移／修復資料的平台物件，也不使用 `immutable`。所有 probe 的網路呼叫皆被禁止。

| 證據範圍 | 結果與限制 |
|---|---|
| 正式 checkpoint 與完整保留原文 | 47,334 筆對應核驗，含 44,566 權證；38 筆再發行的 ISIN、掛牌日、名稱及原文雜湊均一致 |
| 47,783 筆真實保存權證 API rows | 42,505 筆由現行 normalizer／consolidation 沿用原 ID；5,278 筆因較新目錄無精確對應而明列 unresolved |
| 2,061 筆只有名錄、缺 API row | 明確使用 identity-only snapshot，`expiry=None`；證明 38 個新 ID 不碰撞舊 ID，其餘新增身分亦不覆寫既有 input；不聲稱已通過缺失 API 的正常匯入 |
| 751 筆未來掛牌權證 | 離線保留 pre_listing、無報價；其餘 1,310 個只有名錄的權證保留 unknown，不猜到期或 active |
| 兩個真實展延案例 | 舊／新來源版本獨立重播均保留同一 ID |

**本輪未將這些離線產物寫入正式市場庫，未補造 2,061 筆缺失 API 資料，亦未完成 2,071 個名錄缺口。** ETF `00838B`、其他證券 `01111S`–`01114S`、五檔興櫃及逐領域覆蓋仍需具來源依據的後續導入。現有 5,000 筆研究批次上限與深入研究缺口仍保留；台股以外的目標市場未完成。

正式唯讀收據（未隨公開版提供；原參考：`../../output/warrant-identity-investigation-20260912/retained-issue-bindings-final-v1.json`） 的檔案 SHA-256 為 `9bebcc49d85d2b68039e229ea6896a35e3ed837fdd34896b58c4f5e130ce6b07`、receipt hash `772c1e11f063feac5bdbb52807ab3e96d6f0c26a2b4ea3d3e8ba1d2adebb781a`。離線重播 v2（未隨公開版提供；原參考：`../../output/warrant-identity-investigation-20260912/warrant-identity-replay-v2.json`） 的檔案 SHA-256 為 `d039ed44411c39d13a501c604dd173130b331e5be4ec5f975e1482586f9681d4`、receipt hash `0555103872089bd9314cc426d0947d539cb3d022561ba7c54b8396bfa60485bc`。檔案 hash 與移除頂層 receipt 欄位後的 canonical hash 不可互換。

兩份定稿收據核對真正 import 路徑、執行前後 source hash 及當前工作樹皆一致。較早的 `retained-issue-bindings-preliminary-v1.json` 因執行期間原始碼改動而整體 failed，原失敗證據保留；不以後來通過覆寫它。完整重現程式及說明位於 ignored 證據目錄（未隨公開版提供；原參考：`../../output/warrant-identity-investigation-20260912/READ_ONLY_FINDINGS.md`）。

## 回歸、UI 與發布

最終回歸、實際 UI 操作、版本及 GitHub 核對由同目錄的 `final-regression.xml`、`ui-verification.json`、`release-verification.json` 保存。本輪未以測試 fixture 或離線重播當成真模型買賣／獲利證據。

整批定稿回歸為 **513 passed、0 failed、0 skipped，223.03 秒**（`final-regression-v2.xml`）。前一份完整回歸保留 511 passed／1 failed：取消測試只替換了舊「主檔先載入」的入口，未替換新增的名錄先載入入口；更新後分別驗證取消名錄／主檔時完成當前清理且不啟動後續批次或掃描，不修改原取消執行語義。各次較小回歸與重跑不累加為新的測試總數。

實際 UI 使用「系統 → 資料平台 → 解析標的」。修正前 `085974.TW` 回傳已到期的「華邦電統一57購01」；工作樹部署後顯示「查無標的」，但直接查 `ENT-256e7be18ee55fe894d51fd0b9e6896f` 仍回傳同一到期商品歷史。展延的 `03007X.TW` 仍解析為 active「元展07」、`ENT-1eceaaa1acfc5eaf84f4045fc1578813`。部署實例 `fae9545ae1cab455`、working PID `4605`、source fingerprint `fc04ae3d7a1e048c` 通過既有實例核對；最終提交版本另由發布收據核對。這些操作只查詢本地主檔，沒有觸發市場刷新、模型任務或交易。
