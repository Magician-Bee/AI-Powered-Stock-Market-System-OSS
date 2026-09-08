# 五券商 API 整合：架構、完成狀態與驗收邊界

更新日期：2026-08-31

本文件是台新 Nova、富邦 Fubon Neo、永豐 Shioaji、元大 SPARK 與元富 Nova 的實作狀態來源。任何能力只有取得官方 SDK、真實 Session、真實事件或官方測試環境 Receipt 後，才可標示為已驗證。

## 台新參考接入（目前優先路徑）

帳戶持有人已選定台新作為未來的參考券商，因此「設定 → 券商與連線」會優先顯示台新唯讀接入步驟。它由 `GET /api/brokers/taishin/readonly-onboarding` 提供，第一階段**只限 Python 行情 SDK 的唯讀驗證**：不讀取帳務、不建立委託、不送單，也不接受帳密、OTP 或憑證內容。

台新的官方首頁與 Python 行情手冊是這條路徑唯一採用的外部來源。官方手冊指定帳戶本人下載 `PY_TradeD` wheel，Worker 只會載入其中的 `PY_Trade_package`、`MarketDataMart`、`Sol_D` 與 `RCode`，不會從 PyPI 猜測或安裝替代套件。帳戶本人完成官方申請、簽署、驗證、SDK 下載及 macOS Keychain 安全引用後，Host 才能建立隔離的唯讀行情 probe：以 Host 授權收據及 Keychain reference 登入，訂閱一個格式受限的 `TWSE:代號`、等待一個有對應代號的官方 callback，然後必定取消訂閱並斷線。回傳值只含事件種類、可公開欄位名稱與 callback SHA-256，不含行情數值、帳號、密碼、OTP 或憑證；逾時或清理失敗一律 fail closed。這個技術 probe 本身不會把能力標為已驗證，只有 SDK、Session 與帳戶本人檢視的 immutable integration receipt 都存在後才可更新唯讀狀態。交易能力不會因這個 probe 自動開啟。

## 安全邊界

- 券商 API 補強即時行情、私人帳務及交易閉環，不取代 TWSE、TPEx、MOPS、TAIFEX、TDCC 等官方公開資料。
- 行情來源與下單券商分離；多家行情不做價格平均。
- 真實交易預設關閉，OMS Kill Switch 預設開啟。
- 模型只能建立 `USER_APPROVAL_PENDING` 的本機提案，不能自行批准或直接送單。
- 登入、OTP、憑證、API Key、風險文件及固定 IP 由帳戶本人完成。
- 專案、UI 與一般問答任務不能存取券商帳務；持倉／交易規劃仍需主機針對該次 Agent run 核發明確授權。
- 密碼、OTP、Key 與憑證內容不得進入 Prompt、Log、Git 或網頁回應。

## 資料流

```text
Official SDK/API
  -> dedicated broker worker process
     (Host 隔離已驗證；各家官方 SDK 尚待安裝與真實驗證)
  -> BrokerCapabilityRegistry
  -> UnifiedBrokerGateway
  -> CanonicalFinancialEventBus
  -> reconciliation / account / OMS
  -> StockEvidencePack / Agent / UI
  -> CentralBrokerRiskGate
  -> Paper OMS
  -> explicitly authorized live OMS (目前關閉)
```

行情事件同時保留券商、商品、交易所、Market Session、交易所時間、Sequence、接收時間、原始 Payload Hash。融合順序是交易所時間、Sequence、Feed Health、接收時間；相同交易所座標出現不同值時標記 `conflict` 並禁止交易，不產生平均價。

## 目前完成

| 範圍 | 狀態 | 可驗證產出 |
| --- | --- | --- |
| 官方來源鎖定 | 部分完成 | `config/broker_sources.lock.yaml` 保存官方文件 URL、頁面 Hash、下載時間與 SDK 待驗證狀態；只有富邦公開頁面可確認 2.2.8，SDK 二進位 Hash 尚未取得 |
| 合規登錄 | 完成安全骨架 | `config/broker_compliance.yaml` 鎖定官方條款入口；未經帳戶本人與券商確認的授權範圍、行情再利用、固定 IP 及 Sandbox 權限保持 `null` |
| SDK／憑證驗證 | 完成 Host 驗證入口 | SDK 必須留在專案外，以串流方式計算 SHA-256；沒有官方 Checksum 不允許安裝。憑證只接受安全儲存引用及 Host Metadata，不讀取或回傳憑證內容 |
| 五家 Adapter 契約 | 台新唯讀 SDK loader 已接線；其餘完成安全骨架 | 台新 Worker 只可透過 Keychain reference 載入帳戶本人安裝的官方 `PY_TradeD` quote SDK，登入後仍不宣稱行情能力；五家所有能力預設 `null`，沒有 Receipt 不得設為 `true` |
| 人工授權流程 | 完成安全入口 | 使用者點擊後才開啟官方頁，狀態停在帳戶本人 Checkpoint |
| Secret Store | 完成引用契約 | 只接受 Keychain、Credential Manager、Keyring 或 Vault URI，不保存秘密值 |
| 即時事件契約 | 完成安全骨架 | Canonical event、Raw event/hash、Event Bus |
| 多來源融合 | 完成核心規則 | 去重、Exchange time/Sequence 排序、衝突可見、不平均、衝突禁交易、Feed Score 與選源切換稽核 |
| Feed 復原與限流 | 完成 Host 核心 | Sequence Gap 逐來源追蹤、Snapshot Recovery Gate、每券商／端點獨立流量閘門、429 Retry-After 與指數退避；真實斷線及官方限制仍待實機驗證 |
| 帳務契約與對帳 | 完成 Host 核心 | 遮罩帳號、缺值保留 `null`、券商／帳戶類型／幣別隔離；現金、交割、庫存數量、未成交、成交與交割日程對帳，不相符時禁止交易 |
| OMS／中央風控 | 完成核心防線 | Idempotency、Host/Broker Mapping、Callback Raw Evidence、部分成交、原單／替代單關係、Unknown state 禁重送、主動 Snapshot 對帳、券商確認刪單、Kill Switch、Live Gate、人工批准檢查 |
| 監控與告警 | 完成安全骨架 | 行情延遲／Sequence Gap／SDK Crash 產生 Warning；未知委託或帳務差異停止新委託；多券商失聯或重複委託風險啟動 Kill Switch |
| Broker Worker 邊界 | 完成 Host 隔離 | 券商網路／帳務工具使用 `broker` Worker，固定載入目前專案程式並以獨立子程序 PID 執行；Host-only Worker Protocol 提供 Health、Version、Login、Market、Account、Order 類別，泛用接口刻意不開放直接 Place Order |
| Agent 工具 | 完成受限工具面 | 規格要求的 12 個工具；沒有 raw login、secret/certificate read、direct submit 或 bank transfer |
| StockEvidencePack | 完成安全投影 | 即時來源、延遲／健康、衝突與選定事件可投影；原始 Payload 不進模型 Context，帳務預設關閉且需該次 Run 的主機授權 |
| 設定 UI | 完成第一階段 | 五家授權、SDK、Worker 與唯讀驗證狀態；官方申請頁需使用者點擊 |
| Sandbox Matrix | 完成誠實登錄 | `config/broker_sandbox_matrix.yaml` 將五家 Sandbox、Paper 與測試帳號保持 `null`，直到帳戶本人與官方 SDK Probe 產生證據 |
| TEST-B01～B20 稽核 | 完成機器可讀矩陣 | `config/broker_requirement_status.yaml` 分開記錄離線實作狀態、真實驗收狀態、證據與外部阻擋原因 |

## 尚未完成，不能宣稱已整合

目前未持有五家帳戶授權、憑證、API Key、SDK 安裝包及測試帳號，因此下列項目一律是「未驗證」：

- 五家官方 SDK 二進位 Hash、各 SDK 專屬環境及已載入官方 SDK 的真實 Worker。
- 五家唯讀登入 Session Receipt。
- 即時成交、五檔、快照、K 線、零股、期貨及選擇權的真實事件。
- 帳戶、庫存、未成交、損益、額度、交割資訊及每日對帳。
- 各家 Sandbox 的 ROD、IOC、FOK、改單、刪單、部分成交、拒絕、重連與回報重送。
- StockEvidencePack 的真實券商事件接入，以及 Feed Score、重連、限流、監控與告警的實機驗證。
- 單一券商、小額、人工批准的實盤驗收。這必須排在所有唯讀與 Sandbox 驗收之後。

靜態 UI、Mock、Manifest 或單元測試都不能把上述項目變成完成。

## 帳戶本人接續流程

1. 在「設定 → 券商 API」選擇一家券商並開啟官方頁。
2. 本人完成開戶、API 聲明、憑證、Key、測試帳號、固定 IP 或營業員開通。
3. 只把秘密放入作業系統安全儲存；專案只保存安全引用。
4. 下載官方 SDK 後記錄版本、下載時間及檔案 SHA-256。
5. 建立該券商獨立環境與 Worker，再依序完成唯讀登入、行情、帳務及 Sandbox Probe。
6. 把真實 Receipt 寫入能力驗證紀錄；只有實測成功的欄位可從 `null` 改為 `true`，官方明確不支援才標示 `false`。

## 驗證指令

```bash
uv run pytest -q tests/test_broker_framework.py
node --check src/stock_ai/ui/static/js/features/broker-gateway.js
```

目前券商框架離線測試共 42 項。自動化 Browser E2E 已確認五張券商卡完整顯示、沒有真實 Feed 時明確顯示「尚無真實 Feed」，且未點擊時不會開啟外部頁面；可稽核報告與本機截圖 SHA 記錄於 `docs/validation/five-broker-browser-e2e-20260726.json`。真人帳戶的官方授權頁、等待點與完成後恢復仍須由帳戶本人驗收，不能以靜態畫面取代。

## 本階段產出與邊界

- 修改檔案：Broker OMS、Order/Account Contracts、Event Bus、Account Reconciliation、Feed Quality、Sequence Tracker、Rate Limit Governor、Reconnect Planner、Worker Protocol、Sandbox／Requirement 狀態檔與測試。
- SDK 版本與 Hash：沒有新增未經驗證值；仍以 `config/broker_sources.lock.yaml` 為準。
- Broker Capability Profile：五家能力仍全部為 `null`，沒有 Receipt 不改為 `true` 或 `false`。
- 人工授權狀態：五家仍為 `requires_user_action`。
- 真實登入 Receipt、真實行情樣本、真實帳務對帳、模擬委託回報：尚未取得。
- OMS 狀態遷移：離線測試已涵蓋 ACK、PARTIALLY_FILLED、FILLED、CANCEL_PENDING、CANCELLED、UNKNOWN；仍待官方 Sandbox 回條驗證。
- Browser E2E Artifact：已產生未登入／未連線狀態的自動化畫面；真人官方授權與恢復流程尚未執行。
- 秘密掃描：目前工作樹已移除 vendored Shioaji 範例中的自動登入與嵌入憑證；既有 Git 歷史仍可追溯舊值，需另行完成經授權的歷史重寫及發布前 Git／Log／Prompt 掃描，才能通過 TEST-B18。
- 正式交易：仍由 Live Gate 與預設啟用的 Kill Switch 阻擋。
