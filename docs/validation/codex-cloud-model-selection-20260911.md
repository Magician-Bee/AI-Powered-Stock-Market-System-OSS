# Codex 雲端相容性與模型選單驗收

2026-09-11，macOS 原生 Stock AI 視窗。

## 問題與修正

更新後的 Codex App Server 回傳 `reasoningEffort=ultra`，專案原本的 `openai-codex 0.1.0b2` 無法解析，在模型開始回答前就拋出 `ThreadStartResponse` enum 驗證錯誤。PATH 的 Codex 0.144.6 也會遮蔽應用內較新的 0.153.4。

- 更新 SDK 與 lock 中的 Codex 套件至 0.147.0，加入對新增推理等級的實際協定測試。
- macOS 保留明確 `STOCK_AI_CODEX_BIN` 覆寫，否則優先選擇 ChatGPT.app／Codex.app；啟動器將所選路徑傳給後端。
- 依賴同步失敗時，只有通過必要 SDK import 和新增推理等級解析的既有 runtime 才可作後備，舊 SDK 不會被宣稱相容。
- 模型清單由 App Server `model/list` 動態取得，推理選項依該模型的支援清單產生；沒有寫死模型目錄。
- 設定頁和 Agent 輸入區提供模型／推理選單、目前選擇勾號、鍵盤操作、載入與保存錯誤處理。原生選單避開 AppKit 頂部工具列。
- 設定讀取採 request/revision 檢查，儲存完成前發出的舊讀取結果或錯誤不會覆蓋剛保存的選擇。
- 新任務等待設定保存成功；既有任務、checkpoint 恢復、context compact 與同任務外部框架橋接保留原設定。
- 模型執行紀錄使用 App Server 實際回傳的模型／推理強度；切換下一任務設定不會改寫舊任務顯示。

實測另發現 Host 的工具交接只保留少數 schema 的正文，`market.research_pack`、`web.fetch` 等結果被壓成 `null`。已加入有界正文投影，優先保存技術指標、交易／接收時間、最近價量與多個來源正文，保留遮罩與 untrusted-data 邊界；第二層 prompt 壓縮也保留工具識別與重新計算後的內容 hash。

另修正完成條件將「未虛構持倉或成交」誤當交易主張的判斷。否定作用限於所在子句，實際成交、委託、持倉仍需有效的 Host 證據；行情的交易日／交易所／成交量描述不誤要求紙上交易工具。錯月份的財務數值與虛構價格仍會被拒絕；完整日期／時間戳不再被當成月度數值或秒數價格，技術指標窗口與指標數值分開處理。此規則並非完整的日期、來源與敘述語意驗證。

恢復任務時，Host 的 plan/revision 與逐字完成條件會獨立放在 prompt 的上下文壓縮區之外，避免模型只看到任務摘要而遺失既有條件；不替模型捏造條件結果，也不跳過證據檢查。

「研究包回傳 N 筆／點」的明確筆數敘述，會從同任務、已綁定且驗證通過的 `market.research_pack.recent_history` 長度核對。只處理該筆數短語，不讓同值價格或成交量取得豁免；錯誤筆數仍被拒絕。裸八位數如 `20260910` 保持數值驗證，只有附帶時鐘的緊湊交易所時間戳才作日期處理。

兩項皆選預設時沿用 Codex 的有效設定；指定模型而強度選預設時使用該模型建議的強度。不修改全域 Codex 設定。一般 Agent、市場分析與框架橋接使用此設定；預設停用的舊 `/api/codex/run` 開發者診斷路徑仍維持原生持久 thread 的設定。

官方契約：[List models](https://learn.chatgpt.com/docs/app-server#list-models-modellist)。

## 驗證

實際操作透過專案 `開啟股市AI系統.command` 啟動的原生視窗。已完成：模型清單載入、設定頁選擇 GPT-5.6 Sol／medium 並儲存、輸入區鍵盤切換 Astra／ultra，以及 Sol 任務成功回覆與實際模型紀錄一致。

- 非瀏覽器完整回歸：2,039 passed、5 skipped（160.91 秒，3 項既有 deprecation warnings），包含上述最後的歷史筆數與日期解析修正。真實帳號 SDK 相容性測試另有 1 passed。
- 實際模型目錄包含 Astra、Sol、Terra、Luna、Daybreak Blue、GPT-5.5 和 Codex Spark，與使用者提供的 Codex 選單一致；清單由帳號取得。
- 原生視窗確認設定頁與輸入區的模型／強度選單、目前選擇勾號、重新啟動後保存設定，以及切換新任務為 Sol／medium 時，已完成的 Astra／ultra 任務仍顯示原實際模型。
- Sol／medium 的真實一般問答正確完成單利與複利計算；中斷的股市任務在切換新任務偏好後仍以原 Sol／medium 恢復至完成。該舊任務曾交付 Host 備援摘要，因此不將它算作完整技術分析品質通過。
- 最後新建的原生股市任務以 Sol／medium 在約 89 秒、2 個步驟內完成，兩個市場工具均通過 Host 驗證，完成檢查通過。1,653 字最終回答與模型最後回覆逐字相同，包含價格、均線、RSI、成交量、資料期間與來源衝突限制；未被數字拒絕流程替換成備援摘要。途中曾有資料不足的 Host 完成嘗試，最終仍經下一步取得完整模型回答。
- 實際雲端推論涵蓋 Astra 與 Sol，沒有宣稱七個模型都逐一完成推論測試。紙上帳戶完整 JSON 與測試前一致，全域 Codex 設定 SHA-256 也保持一致。
- 修改檔案通過 Python AST、JavaScript syntax、shell syntax 與差異空白檢查；neutrality 檢查通過。
- Repository hygiene 仍因既有追蹤檔案約 156 MiB（上限 100 MiB）與 14 張超過個別大小限制的既有驗收 PNG 而失敗。本次未加入大型圖檔，也未調寬該檢查，不能宣稱整個 repository 的所有檢查皆通過。

驗收只測 Codex 雲端推論，沒有啟動本地 LLM 或真實下單。自動回歸曾包含既有 headless Chromium 測試，後续已依要求排除；前端補充邏輯驗證使用 Node VM，畫面手動驗收使用原生視窗。

本次驗收證明模型連接與選擇功能，不能據此推論交易策略具有獲利正期望值。整體策略、風控、歷史績效與排程成熟度另有本機審查報告；既有資料不隨本次程式提交公開。
