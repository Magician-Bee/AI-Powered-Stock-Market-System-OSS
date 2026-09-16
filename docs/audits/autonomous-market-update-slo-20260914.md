# 自主市場更新 SLO 驗收

## 目標與版本

- 直接推進 M2／08：全市場主檔增量更新、共享快取、受控並行、來源限流與事前時效驗收。
- 執行程式提交：`8d781cd979f132aa8a37a4bd2f445cf8a3b3da28`。
- 原生服務 instance：`fae9545ae1cab455`；PID `59042`；port `8000`。
- 服務健康資訊、啟動器記錄、受管 Python source mirror、本機與伺服 UI SHA 均綁定上述提交；`verify-stock-ai-instance.sh` 回報 `VERIFIED CURRENT PROJECT INSTANCE`。

## 實作

市場掃描現在會在第一個來源請求前保存固定政策及 hash，要求 `twse_isin_listed`、`tpex_isin_otc`、`tpex_isin_emerging`、`twse_official_master`、`tpex_official_master`、`tpex_delisted_history` 六分區於 300 秒內全數 `succeeded` 或 `skipped_fresh`，嘗試失敗率上限 0%。稽核器從正式交易與市場 SQLite 以 `mode=ro`、`query_only=on`、一庫一個 `BEGIN` 讀取，並驗證政策 hash、時間順序、deadline、分區狀態、來源失敗率與新鮮快取的 checkpoint／cursor／TTL。

TPEx 大型 JSON 在官方回應中同時有精確 Content-Length、`Accept-Ranges: bytes` 及 ETag 或 Last-Modified 才啟用區間恢復。每段要求 206、精確 Content-Range、相同 validator 和精確長度；版本變動、超過 64 MiB、缺口或無法證明區間能力就停止。TPEx 指數請求依 Host 的同來源一秒間隔序列執行，兩個交易所工作者上限為兩個。

TWSE 官方權證原始清單中 4,656 筆無目前發行證據的身分全數為已到期歷史發行。它們繼續保留於 raw、checkpoint 與歷史缺口帳本，不阻擋目前市場基準。任何當期、未來或身分衝突權證仍 fail closed。

## 正式循環

| 項目 | 冷啟動全量 | 隨後增量 |
| --- | ---: | ---: |
| cycle | `AC-dacba3fa4d083c4e6dc7454256c8247d2f51e2112ad9a972b97d4252610d1d87` | `AC-3273b5bc7a97f3ac85c0772cc4961722e9756541c2ea9c67fb9fe890ea7b0b03` |
| 證券總數 | 49,262 | 49,262 |
| 新增部位商品門檻 | 1,936 | 1,936 |
| 六分區 | 6 succeeded | 6 skipped_fresh |
| 嘗試失敗率 | 0% | 0% |
| 主檔更新時間 | 639.873 秒 | 42.656 秒 |
| 300 秒 SLO | breach | met |
| 模型呼叫 | 0 | 0 |

冷啟動的主要時間為 TWSE ISIN 196.639 秒與 TWSE 官方主檔 260.394 秒。此結果沒有被後來的增量成功覆寫。增量收據逐一核對六個成功 checkpoint、完整 cursor、最後成功時間與 21,600 秒 TTL，並確認它們在 SLO 宣告時仍新鮮。

增量循環的單檔深查選到 `2426.TW`，因 `insufficient_completed_history` 失敗。該失敗保留在逐檔帳本，不改寫六個市場主檔分區的驗收。

## 稽核與帳戶證據

- [冷啟動 SLO breach 收據](autonomous-market-update-cold-20260914.json)：內容 hash `f57cc7a2eb437f0f821330d846d6f8f2d88f8f4fcae7a7b62ed4df22fecaa53a`。
- [增量 SLO pass 收據](autonomous-market-update-incremental-20260914.json)：內容 hash `a02d25059f037a8b1d9143686809283c52db3be0b4c8d7746ce1af91a442fa49`。
- [逐檔覆蓋帳本收據](autonomous-research-coverage-20260914.json)：49,262 列、0 重複 entity、0 列 hash 錯誤、0 缺 cycle 或 evidence reference，收據 hash `3b3ff58dbf43f85abc160de7e0c9d048e22202034847cfe8adb543efad376bca`。
- [交易生命週期收據](autonomous-lifecycle-20260914.json)：0 計畫，`m1_verified=false`、`positive_ev_qualified=false`，收據 hash `8969eec40a99d0c869482bd117501a7432603e7798d8138692cb77a15614fdaa`。

正式研究前後的 `autonomous-paper-v1` 材料狀態精確一致：現金、可用現金與總權益均為 TWD 1,000,000，0 計畫、0 未成交委託、0 成交、0 持倉。模型供應者收據維持 Codex `gpt-5.6-sol`、medium；2026-09-14 模型複查已用 0／1、剩餘 1。當時為臺北 10:45，不在 14:30 以後的自然複查時段，因此沒有手動提前呼叫、改日期、增加預算或強制交易。

原生 App 的實際可存取樹顯示相同 instance 與 commit URL，自主紙上帳戶為 TWD 1,000,000，逐檔覆蓋 49,262、可新增部位 1,936、深入 28、失敗 2、尚未深入 49,217，帳本時間 `2026-09-14T02:43:38.002486+00:00`。頁面同時明示多數價格、財務、新聞事件、委託簿與跨市場領域尚需更新，因此這次不宣稱 M2 全部完成。

## 驗證邊界

完整測試套件為 3,456 passed、5 skipped、0 failed，另有三個既有棄用警告。此證據只通過 M2／08 的正常增量市場主檔更新驗收；冷啟動全量路徑仍超過 300 秒，逐檔深查與其他資料領域仍有實際缺口。M1 真模型進出場閉環、M7 的 120 日／30 筆平倉、M8 真實券商授權及 M9 其他市場均未完成。
