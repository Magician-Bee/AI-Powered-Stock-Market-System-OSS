# M0–M1：決策與成交來源接線

本輪直接推進使用者計畫 01、03、04、23：讓真模型提案及每筆紙上成交能回查當次來源，並修補成交後中斷的恢復。沿用既有 Agent Runtime、AutonomousCampaign、PaperBroker、PaperOMS、cash_ledger 及 autonomous_evidence，沒有另設帳戶、交易帳本或排程平台。

基於已發布的 `65c6a14c4ed0b8c9447d3775b1e8af30e767988d`，工作分支為 `codex/fix-autonomous-fill-provenance-20260912-1843`。此文件本身不作為最新部署已運行的證明；部署後另核對服務與 GitHub。

## 實際斷點與修復

PaperBroker 先呼叫 OMS 提交成交、現金與持倉，之後才保存含行情的 broker event。送單時的行情只代表第一次送單，不能證明後续部分成交的行情。因此，第二次成交提交後若程序在 event 前中斷，原本可能只剩成交帳本。

現在每筆成交的 `cash_ledger.metadata_json.fill_evidence` 在原 OMS transaction 內保存實際 fill 的身分與金額、完整當次 `market_context`、Host 注入的撮合設定與部署引用，並計算內容雜湊。證據寫入失敗時，該筆現金、持倉及成交一起回滾；相同 fill id 的重複回報返回原資料，不覆寫來源。BrokerPort 在同一讀取快照中取得 fill 與現金紀錄，分別呈現完整性及來源未知狀態。

故障測試另揭露：OMS 已全數成交、broker 尚未更新時，恢復後可能仍顯示部分成交。現在讀單與撮合前先核對同帳戶、同委託的 OMS 契約與實際成交量，再修正派生的 broker 狀態並保存 `fills_reconciled`；不新增成交、價格或現金。IOC 的部分成交恢復為已取消剩餘量，不能在下一筆行情繼續成交。

新增的來源投影不納入 v1 財務成果的雜湊；broker 與原始成交表讀取同一筆成交時，原成果收據保持一致。完整來源仍由 fill id 關聯現金帳本，另行驗證，不刪除或覆寫已保存的來源。

模型工具呼叫另保存 `model_invocation_context`。Host 以實際 run／node 尋找當前工具呼叫，核對既有模型與 SDK turn 事件，保留白名單 ID、時間、狀態、部署及資料庫位置雜湊；不保存提示詞、完整工具參數或憑證。缺少或無法唯一關聯時保持 unknown，且不以證據服務失敗阻斷既有持倉保護。提案的既有冪等鍵不變，重試保留原 definition 與決策來源。

ASGI 在背景任務啟動前固定一份來源檔案快照。來源雜湊集合另存一次，決策和成交只附帶精簡部署引用，包括宣告 commit、instance、PID、快照引用、帳戶及交易資料庫位置雜湊。直接使用 library 的首次快照明確標記 `first_autonomous_use`，不冒稱程序啟動時即已觀察。

## 證據界線

- 雜湊證明資料完整性；不能單憑 `is_realtime`、provider 名稱或雜湊宣稱行情為真。
- 部署快照是 Host 當時觀察到的檔案，不是 Python 所有已載入程式碼、相依套件或遠端模型的獨立認證；宣告 commit 仍需部署比對。
- 資料庫位置雜湊可區別不同路徑的同名帳戶，不能證明檔案內容、重設历史或帳戶所有權。不同 DB 的讀取不是跨庫原子快照。
- 紙上撮合設定與實際券商成交來源分開；保留完整撮合證據也不等於真實市場成交。
- 舊紀錄缺收據、損壞、身分不符或不唯一，均不從送單行情、鄰筆成交或現在版本補成通過。
- 生命週期審核器維持 `m1_verified=false` 與 `positive_ev_qualified=false`。工程 fixture 與來源完整性通過不能代替真模型、真行情及全帳戶成果的驗收。

## 本輪驗證

最終整合回歸 **781 passed、0 failed**（99.55 秒），包含 18 項成交來源故障測試、36 項唯讀 audit、14 項模型來源測試與 6 項部署接線測試。Python 編譯、`git diff --check`、neutrality CI 通過；Starlette 留有既有 httpx 棄用警告。成果與前瞻的原有條件保持不變。

隔離閉環以 `/tmp/stock-ai-provenance-loop-20260912-1908.sqlite` 執行，1 計畫、2 筆紙上成交、1 個成果，平倉與重複對帳通過。新增審核器逐筆確認兩份 cash receipt 完整，並同時標示 fixture、來源 unknown、部署缺失、`m1_verified=false`。工程輸出位於 `output/autonomous-provenance-engineering-loop-20260912.json`，正確帳戶的審核為 `output/autonomous-provenance-engineering-account-audit-20260912.json`；它們不作真模型或市場績效。

19:08–19:09 實際啟動服務並在「投資組合 → 模擬帳戶」操作「更新自主帳戶狀態」，刷新完成後按鈕恢復可用。自主與手動帳戶各自呈現，數值與下述正式帳本一致。正式服務 PID 61622 的 ASGI 啟動已保留部署收據，532 個來源檔案與 runtime 鏡像全部一致，0 個缺檔或讀取錯誤；本次待提交來源 fingerprint 為 `32b5f4cb507fa95e`，宣告 commit 尚為父提交 `65c6a14c4ed0`，不以父提交名稱代替待提交來源。提交後重新部署並另保存最新 commit 核對。

正式帳本唯讀 audit 為 `output/autonomous-provenance-live-audit-20260912.json`：0 計畫、M1／正期望值皆 false。部署收據首次保留失敗的隔離測試亦確認，下次會重試初始化，不會讓背景工作拿到缺少接線的 campaign。

## 正式帳本觀察與下一步

本輪開始仍為週六，正式自主紙上帳戶持有 TWD 1,000,000，0 計畫、0 成交；已核准每日模型預算 1／1 已用完。未加開模型、增加步數、提高風險或建立測試委託。

舊前瞻 protocol `FP-040bdc7a8be619fb56c7743bb291391834798ebab67559884f2cfb03510522b9` 已在正式 evaluations 表保存 abandoned：`frozen_policy_changed; start_new_prospective_trial_after_account_is_flat`。這解釋了舊 protocol 不再追加觀察，並非可把舊政策重新當作本輪驗證。原偏離與門檻持續保留。

下一個直接驗收點仍是原授權與預算下的新真模型決策、持久提案、自然觸發進出場及逐筆對帳。M7 的 120 日觀察、30 筆平倉與成本後統計資格未達成；M8 正式券商與實盤授權、M9 美股及其他市場也仍為未完成範圍。
