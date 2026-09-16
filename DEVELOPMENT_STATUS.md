# Historical development status

> Maintainer-recorded implementation notes, not fresh public-release acceptance. Some private evidence is intentionally omitted. See [public validation](docs/evidence/README.md).

# AI 股市系統

以台股為核心的本機市場研究、可替換模型 Agent 分析與紙上交易工作站。

本專案以自主交易為產品目標，整合官方市場資料、即時行情、新聞事件、基本面、籌碼、策略研究、中央風控、委託與持倉帳本。目前以紙上券商驗證同一套研究、交易計畫與持倉管理流程；真實券商尚未啟用。

本次開發主線依 [M0–M9 最終目標計畫](docs/plans/autonomous-trading-goal-20260912.zh-TW.md) 推進，初始版本與帳戶見 [M0 執行基準](docs/audits/autonomous-goal-m0-baseline-20260912.md)，後續修復與缺項見 [執行進度與驗收](docs/plans/autonomous-goal-progress.zh-TW.md)。未成交出場現在保存退出意圖與剩餘風險告警；模型可明示等待秒數、替換次數與最低賣價，Host 在進場前把最低價納入既有損失預算，撤單確認後才可重掛。這些工程修復不代表真模型買賣閉環或獲利資格已驗收。

自主決策另保存 Host 模型呼叫與部署收據；每筆紙上成交的當次行情、撮合設定與版本關聯隨現金帳本一起提交，以免中斷後只留下成交卻遺失來源。收據完整性、真實來源與 M1 驗收分別判定，舊成交不回填為已驗證；接線及限制見 [成交來源證據報告](docs/audits/autonomous-lifecycle-provenance-20260912.md)。

官方歷史請求遇到來源中斷時，已驗證快取仍可繼續讀取；解析拒絕的原文另留取得證據。模型研究摘要保留錯誤原因及完整回查收據，不增加原輸出預算。實際來源探測與工程證據見 [歷史恢復報告](docs/audits/autonomous-history-recovery-20260912.md)。

模型現在可在同一任務指定台股補查，再用新研究版本提出自己的計畫；固定候選評估失敗會獨立記錄，不再遮蔽合格歷史。原風控與研究預算維持，詳見 [按需研究報告](docs/audits/autonomous-on-demand-research-20260912.md)。

商品身分另由官方 ISIN 名錄及保存原文核驗，研究範圍與新增部位資格分開。未知、ETF、ETN、存託憑證及特別股仍可保留研究；自主計畫新增部位只接受已核驗的普通板普通股。送單前重讀主檔，既有買單資格失效時取消剩餘買量，已成交部位繼續按原計畫管理。存量身分、原文回讀及缺項見 [商品身份報告](docs/audits/autonomous-product-identity-20260912.md)；真模型買賣閉環仍待實際成交與對帳驗收。

主檔讀取會核對代碼有效期與目前交易所，轉板後不再因舊來源較晚寫入而選回舊代碼；查不到唯一有效身分時保留 unknown 缺項。已找回 39 檔普通股的正確研究代碼，並將先前 2,071 個名錄缺口依官方 ISIN、venue 與真正掛牌日寫入正式主檔：1,320 筆為 unknown，751 筆為 pre-listing。這些列可進入研究範圍，但在取得行情與生命週期證據前不會冒充 active 或取得進場資格；證券主檔搜尋會顯示生命週期，只有 active 身分可開啟圖表，其他結果明示為僅供研究。本機讀取上限為 100,000；全市場研究後會依不可變 entity 身分保存逐檔帳本，分開呈現 11 個資料領域的可用性、時效、來源、到期及下次更新需求，以及深入研究的 current／stale／failed／never／not-attributed 狀態。帳本存在不代表資料完整或策略有效。歷史缺口分析見 [目前掛牌與覆蓋報告](docs/audits/current-listing-coverage-20260912.md)，正式身分導入見 [名錄身分與研究範圍報告](docs/audits/catalogue-identity-research-ingest-20260913.md)，帳本契約見 [逐檔研究覆蓋報告](docs/audits/autonomous-security-research-coverage-20260913.md)。

權證新發行改以核驗過的官方 ISIN 與交易所辨識，保留舊 ID、真正掛牌日及歷史有效期；展延不會拆成新商品，未來掛牌也不會自動變成 active 或報價。來源失敗可重試，逐筆身分缺項保存在既有 ingestion 紀錄。真實資料的唯讀與離線重播已驗證 38 組代碼重用分離及 42,505 個原 ID 沿用；先前缺失名錄現已經由正式 UI 同步，後續 API 清冊只在精確發行證據一致時補上到期與生命週期資料。設計前置見 [權證發行身分報告](docs/audits/warrant-issuance-identity-20260912.md)，正式寫入證據見 [名錄身分與研究範圍報告](docs/audits/catalogue-identity-research-ingest-20260913.md)。

已接入台股主檔批次掃描、可追溯策略驗證、模型部位與條件／定時交易計畫介面，以及持續管理紙上委託與持倉的執行器。工程回歸與實際研究已有驗證；正期望值、完整模型成交閉環與真實券商執行仍需各自取得證據。操作、成本假設與驗證資格見[自主交易指南](docs/guides/autonomous-trading.zh-TW.md)。

模型可安排未來日期、時間與突破／回檔進場，實際部位與原始資金要求分開保存。Host 分別回報計畫建立、送單與績效資格；休市或其他研究框架的限制不會直接否決有效的未來計畫。前瞻驗證另凍結模型、提示、工具、程式與成本，事前登記觀察期並持續收集等待、拒絕、提案、每日淨值及平倉結果；資格僅適用於相符的紙上策略與成本情境。

<!-- capability-status:start -->
### 交易能力分級（自動產生）

唯一來源：`config/capability_status.yaml`。目前核准最高等級為 **PAPER**（`paper`）；
券商實單、模型直接下單與跨級環境變數啟用均為關閉。
升級必須依序通過 RESEARCH → PAPER → SHADOW → BROKER_SANDBOX → RESTRICTED_LIVE → PRODUCTION_LIVE，
且需要簽章人工核准與該等級全部驗收證據。
P0–P105 真相登錄：`config/production_requirement_status.yaml`；目前共有 **124** 項要求，
由 release gate 依阻擋項目逐項判定；P0 尚未完成 `Q-003`、`D-002`、`D-003`、`E-006`、`E-007`、`E-008`、`E-009`、`SEC-001` 等 9 項，任何未驗證項目都不得因為程式碼或 UI 存在而視為完成。
完整 P0–P105 release checklist：**85/124**；P1/P2 未驗收項目同樣會阻擋 release。
Quant P0 基礎門檻為 **8/9**；研究結果作為執行證據目前為 **關閉**，
待 `Q-003` 的阻擋條件全部驗收後才能開啟。
<!-- capability-status:end -->

## 目前能力與邊界

已實作：

- TWSE、TPEx、MOPS、TDCC、TAIFEX 與 Yahoo 資料整合
- 台股即時行情、可重建 1／5／15／30／60 分 K、可指定日期範圍的歷史日 K（原始／前復權／後復權、OHLCV／成交金額）、新聞與事件；實際歷史覆蓋依來源與驗證結果呈現缺口
- 互動式個股圖表：蠟燭 K 線／美國線／折線／面積圖／Heikin-Ashi 切換、十字游標 OHLCV、滾輪縮放、拖曳平移，以及可持久保存的趨勢線、水平價位與交點標記
- 六個正式工作區：首頁、市場、個股、投資組合、研究、系統；所有子功能改由工作區分頁承接
- 技術、情緒、基本面與研究證據整合
- 策略訊號、研究驗證、中央風控與紙上執行
- SQLite 模擬帳戶、委託、成交、持倉、損益與學習紀錄
- Paper Broker 的每次成交都可保存 queue／latency／probability／market-impact，以及明確的 `market_volume`、`participation_rate`、`volume_cap` 流動性上限；缺少成對輸入或超出範圍時 fail closed。這些是可重播的本地 scenario inputs，不代表歷史 order book 或真實券商成交校準。
- Codex 帳號登入、每個 run 獨立隱藏 thread、App Server 可稽核事件與預設停用的開發者診斷路徑
- Stock AI Agent Runtime 預設使用 Codex，也可在設定頁實際切換至 OpenAI-compatible API／本機 gateway 或外部 Agent Framework；三種驅動器共用同一套工具、權限、驗證、風控與事件紀錄
- 使用者明確要求獨立 Critic／反方分支時，只有 Host 可以建立唯一的 read-only Critic child；父 Run 僅在 child 的 durable receipt 實際 `completed` 並 Join 後顯示已完成反方驗證，未完成時會如實保留限制
- 外部網頁、新聞與市場工具觀測在送入 provider context 前會標示為 `open_stock_ai.untrusted_content.v1` data-only envelope；外部文字沒有 instruction authority，後續工具權限只能由 Host validator 決定，完整 hostile corpus／真實 provider runtime 仍依 P0–P105 矩陣另行驗收
- Durable memory candidate 會保存 evidence trust／provenance；外部或低信任內容先 quarantine、不可主動取代既有 memory，須 Host verification 才能進入 active retrieval，完整 malicious-memory corpus／provider-to-memory receipts 仍依矩陣驗收
- Mutating tool validation 會產生 `open_stock_ai.mutation_receipt.v1`，以 SHA-256 綁定 exact request、result 與 state transition；receipt 竄改會失敗，但 legacy surface／真 provider 的 strict migration 仍依 P0–P105 矩陣驗收
- Agent dispatch 會由 Host 原子套用 global／session／provider／tool cost budgets，並以 execution guard 阻擋遞迴深度、節點、重複 tool signature、總呼叫數與無進度失控；目前仍缺 reviewed provider cost schedule 與真 provider runaway chaos receipts
- Provider 失敗預設 fail-closed；只有明確 allowlist 的替代 Provider 才能在 advisory／read-only Run 切換，Host 會記錄模型品質變更通知與可驗證 receipt；紙上、專案、外部及 full execution 不允許降級，目前仍缺真實 outage／alternate runtime receipts
- Authoritative Store Matrix 以 `config/authoritative_store_matrix.yaml` 明確標示每個 durable domain 的唯一 owner 與 read-only projection；能力狀態 API 會回傳 matrix hash，Forest、Artifact、Experiment 與 live OMS 的 runtime writer 都必須透過各自的 durable authority，不能用 process-local state 形成第二個真相
- Provider-neutral PlanGraph、Host Plan Compiler、Validator、Recovery、Checkpoint、風險導向 Policy／Approval、Memory、Artifact、可複製 Workflow 與 Worker Supervisor；Host 會實際派送 reasoning、tool、subtask／subagent、approval、schedule、workflow、validation、checkpoint 與 finalize 節點
- Session-first Final Agent Runtime：不可變 Objective Version、Recursive Task Forest、Branch Local Plan／Join、Reflection／Decision Checkpoint、mid-run soft／hard／fork steering、Evidence Graph、可定位版本的 Artifact、六層 Memory、自我修復與動態 Automation lifecycle；執行中的自然修正會由 Host 自動併入 Session，並在根目標已過期時取消舊 Run、建立可追溯的替代 Run，不要求使用者指定內部執行方式。架構摘要見 [Final Agent Runtime](docs/architecture/final-agent-runtime-p0-p105.md)，逐項實作、測試與待驗收狀態見 [P0–P105 實作追溯矩陣](docs/architecture/P0-P105-IMPLEMENTATION-MATRIX.zh-TW.md)
- Host 只派送 PlanGraph 中依賴已完成的 ready node；工具結果必須通過 schema、語意、依工具類型核對的 mutation receipt、檔案 hash 轉移與節點 postcondition 驗證，單獨回傳 `success: true` 不會成為完成證據
- 專案、終端機、瀏覽器與外部框架使用真實隔離子程序；逾時或取消會終止整個 process group，不把同一個 API process 內的函式呼叫偽裝成 Worker
- 後端常駐的 durable run：關閉或重新載入 UI 不會取消任務，可依 `run_id` 重播事件、重連、查詢與取消
- Agent 每次 provider 執行、關鍵 API request，以及本機 paper-training 的已驗證報價／OMS lifecycle 都會將實測延遲與終態寫入同一個 runtime SQLite 的不可變 SLO ledger。系統 → 資料平台的「評估資料新鮮度」會以每個來源的真實 `latest_success_at`（取最舊成功時間）寫入資料新鮮度收據；缺資料、逾時或來源告警都會 fail closed。Agent 的 `broker.market.subscribe` 只能經由 runtime Gateway：事件先連結已驗證的官方 SDK integration receipt、寫入不可變 sequence receipt，才以交換所時間、實際接收延遲與 sequence 分類記錄 `broker.feed` SLO；沒有官方 integration evidence、gap、重複與亂序都不會成為成功資料。Dock 的 Observability 可展開查看 hash-verified SLO evidence；未接線的 broker feed 仍維持 `no_data`／非全綠，不會用 Agent 成功率代替其他服務的量測。
- 使用者明確授權的紙上模擬下單，會以 Host 驗證的精確預覽與持久成交收據作為完成條件；即使研究資料不足，也會在單次紙上執行後結束，不會要求外部驗收、重複規劃或重複送出同一筆模擬訂單。收據一旦齊備，Host 直接完成 Run，不會為了要求模型再寫一段結語而消耗額外 provider 預算，也不會把續跑草稿自動塞回使用者輸入框。一般「分析某標的的技術面與風險」預設是無交易的市場資訊需求，不必額外寫「只做分析」；資料不足時，`market.analyze_symbol` 的 Host 驗證 `data_blocked` receipt 會直接產生資料來源、時間與限制的無交易結論，不會讓模型重複呼叫同一工具直到步驟、成本或 token 預算耗盡。OpenAI-compatible 模型若將真正工具包成 `assistant({tool, arguments})` 巢狀信封，Host 會在計畫建立前解開並逐一核對白名單；未知工具不會寫入 durable Plan，也不會阻塞後續可用分析工具。一般市場研究未明確要求獨立 Critic 時不會暴露遞迴子任務。盤中行情暫不可用而有簽章可驗證的官方收盤價時，Agent 可完成標示資料時間與限制的 advisory 分析；該資料永遠保留為 fallback，不能成為決策候選、紙上訂單或任何實盤價格。自然語言的公司名稱＋技術／基本面／風險比較會由 Host 路由為市場研究，先自行解析名稱與查證公開資料；模型不能把「請使用者提供公開市場資料」或「告訴我該如何回答」當成一般分析的阻塞決策，也不能把 `Requesting…`／「請提供…」等進度文字當作最終答案。Host 會自行處理一般市場研究中的資訊澄清與修復邊界；只有真正需要使用者授權、帳戶／憑證或外部實際操作的 gate 才會顯示互動卡。已完成的純分析 Run 可以再次執行；`paper_execute`、`project_execute`、`external_execute`、`full_execute`，或已記錄 mutating tool 的 Run 則只會建立新的「安全分析草稿」Session，強制回到 advisory，且不複製舊交易／寫入指令。若舊版曾把已具 Host finalization、驗證通過與紙上 receipt 的 Run 誤留在 recovery，啟動時只會依這三份 durable 事件收斂為完成，其他 partial Run 不會被自動改綠或重播。
- SQLite durable advisory scheduler：一次、週期、cron、事件及條件任務不依賴 UI；市場、新聞、持倉與 UI command/state 事件先寫入持久事件匣，再由租約中的 Runtime 觸發，重啟或瞬間斷線不會遺失；每次觸發皆為可稽核的新 Run，排程不得啟用專案修改或模擬送單權限
- 真實 Skills 指令載入、Codex App Server MCP schema 發現／健康檢查／安全綁定、SQLite durable UI command bridge
- Host-owned Playwright 瀏覽器與 Telegram／LINE 通知工具；瀏覽器可讀取 JavaScript 頁面，但封鎖 localhost、私有網段、下載與密碼欄位
- Codex App Server 已登入 Connector 的真實 MCP schema 會依 read-only、外部寫入、破壞性寫入三層權限動態綁定，不以固定工具數假裝整合
- 專案內 sandboxed terminal 與 run／capability／專案路徑／過期時間綁定的原生 Codex approval
- 生產 `StrategyEngine` 的 point-in-time exact replay，包含 walk-forward、OOS、purged CV、成本、滑價、延遲與 strategy/data SHA-256
- 三層學習記憶與 proposal → exact evaluation → shadow paper → 人工 promotion 門檻
- 七個固定金融外部專案各有一個可稽核的隔離執行工具，可由 Agent 直接呼叫實際 vendored source
- TradingAgents LangGraph 與 FinRobot AutoGen 預設沿用已登入的 Codex App Server；TradingAgents 的真實 vendored graph 也可在明確 opt-in 的隔離研究 runtime 透過 OpenAI-compatible provider（目前已驗證遠端 `gpt-oss:20b`）執行，兩者都不具下單權限
- FinRL 已執行 policy 訓練／保存／重新載入／推論的合成價格 smoke test；Qlib 已執行 cn_data／SH000300 的 Dataset／Model／Recorder／Signal／Portfolio Analysis workflow。這些框架整合測試不代表台股策略已訓練完成或具有正期望值
- Web 工作站，以及 macOS 26 的原生 Liquid Glass 外殼

尚未完成或刻意停用：

- 真實券商 API 與正式下單
- 外部框架的隔離 runtime 與資料集不隨核心依賴或 Git clone 自動安裝；缺少時會回報實際 prerequisite
- FinGPT 本地 base model／LoRA 入口與來源保留，但目前依設定停用；預設模型使用 Codex，不會下載或載入本地 FinGPT 權重
- 可供生產升級的完整台股 point-in-time 基本面、籌碼、新聞歷史資料集；引擎已實作，但沒有該資料時一律不宣稱 empirical valid
- 任何獲利保證或自動實盤交易

五券商共用安全層已建立台新 Nova、富邦 Neo、永豐 Shioaji、元大
SPARK 與元富 Nova 的固定 Registry、人工授權入口、標準行情／帳務／委託
契約、資料品質、對帳、OMS、Worker 隔離及預設關閉的正式交易 Gate；這不
代表五家真實帳戶已連線。官方 SDK、Session、行情、帳務、Sandbox 與受控
實盤的逐項證據及阻擋原因，請以
[五券商整合狀態](docs/integration/five-broker-gateway.md) 與
`config/broker_requirement_status.yaml` 為準。

目前的參考券商是**台新數位 API**。在「系統 → 券商與連線」可展開台新唯讀接入步驟；第一階段只驗證 Python 行情 SDK 與隔離唯讀行情 probe，不讀取帳務、不建立委託、不送單，也不在專案或頁面接收帳密、OTP 或憑證內容。帳戶本人完成台新官方申請與 macOS Keychain 安全引用後，Host 才會產生可稽核的唯讀驗證收據；交易能力仍維持關閉。

## 快速啟動

### macOS

雙擊 `開啟股市AI系統.command`。停止時雙擊 `停止股市AI系統.command`。

停止腳本會核對 Uvicorn 程序及其工作目錄，支援此專案的 Desktop 目錄與 Application Support managed runtime；PID 紀錄失效時也能找回本專案服務，不會停止其他副本。維護期間若不可自動呼叫模型，先停止，再執行 `STOCK_AI_AGENT_BACKGROUND_PAUSED=1 ./開啟股市AI系統.command`：暫停 Agent 啟動恢復、排程與 Automation 模型喚醒，保留原有 Run 與排程資料。這是背景工作暫停，手動模型操作仍須避免；恢復正常執行前同樣先停止，再以一般 `.command` 重啟。

### Windows

雙擊 `開啟股市AI系統.bat`。停止時雙擊 `停止股市AI系統.bat`。

啟動器會準備固定版本的 `uv`、Python 3.12、專案依賴與 Codex CLI，並從本機連接埠 `8000` 起尋找可用埠。Agent SQLite 啟動路徑只做有界的資料庫開啟與 migration catalog 可讀性檢查，不會因資料庫成長到數 GB 就在開啟 UI 前同步掃描全部資料頁；完整 `quick_check` 仍保留為明確的維護操作。完整搬移與首次啟動說明見 [可攜式啟動指南](docs/guides/portable-launch.md)。

本機市場資料位於 `.runtime/market-data.db`。資料修訂雜湊只包含業務內容；ETF 使用交易所＋代號作為證券身分，發行人的統編只保留為 metadata，不會把同一發行人的不同 ETF 合併後反覆產生假 revision。市場頁的一般 GET 只讀取本地 warehouse，官方證券主檔同步由明確的 refresh 操作觸發。

Agent 資料庫維護前會按檔案系統加總備份與重寫的預估尖峰空間，納入 SQLite 邏輯大小、WAL 與安全餘量；不足或無法查證時先阻擋。備份使用獨立檔名與私有 `.partial` 檔，通過 `quick_check`、計算 SHA-256 並 fsync 後才成為正式世代，並先保存完整性收據再依保留政策移除舊副本。詳見 [Agent 儲存維護防線](docs/guides/agent-storage-preflight.zh-TW.md)。

執行資料採有界保留：已處理的 Runtime inbox 保留最新 2,000 筆完整 payload，較舊去重事件只留 idempotency tombstone；Signal ledger 保留最新 500 筆完整可回放契約，Decision ledger 長期保存決策欄位但不再複製同一份大型研究 payload。桌面服務啟動時及之後每小時會執行一次 SQLite 耐久保留維護，並留下不可竄改收據；重啟後會從最後收據接續，且不會刪除 execution-critical 內容。可用 `RETENTION_MAINTENANCE_INTERVAL_SECONDS` 調整本機間隔（最小 1 秒）。`.runtime/` 與 `output/` 都不提交 Git。`.runtime/n8n/package` 約數 GB 是固定版本 n8n 的真實執行依賴，不應當作快取誤刪；callback secret 與 n8n API key 分開保存，可使用 `scripts/rotate-n8n-callback-secret.sh` 獨立輪替，輪替後要重新部署既有 workflow；資料庫清理前仍應先做可還原備份。

### 開發模式

```bash
uv sync --extra dev
uv run python -m uvicorn stock_ai.main:app --host 127.0.0.1 --port 8000
```

啟動後可使用：

- Web UI：<http://127.0.0.1:8000/>
- OpenAPI：<http://127.0.0.1:8000/docs>
- 健康檢查：<http://127.0.0.1:8000/health>

### P0–P105 架構與本機驗收入口

Final Agent Runtime 的正式資料流是：

```text
Agent Dock / Session Message
  -> DurableAgentRuntime
  -> AgentOrchestrator + Host PlanGraph
  -> FinalAgentRuntime
       -> Objective Version + Recursive Task Forest
       -> Interaction / Steering / Repair / Checkpoint
       -> Research Evidence / Memory / Artifact / Automation
  -> SQLite durable events
  -> Agent Dock Task Forest / Decision / Evidence / Artifact views
```

P0–P105 每一項的實作模組、測試檔與尚待實機／provider 驗收內容，統一記錄在 [P0–P105 實作、測試與驗收追溯矩陣](docs/architecture/P0-P105-IMPLEMENTATION-MATRIX.zh-TW.md)。矩陣中的「測試落點」只表示已有自動化覆蓋，不代表目前工作樹已重跑通過；真實 provider、外部 n8n／通知、完整 Chaos 與 `.command` UI 流程沒有證據時一律維持「需驗收」。

完成回答後，Provider 可回傳 0–3 個不阻塞目前答案的 `interaction_proposals`。一般下一步使用 `arguments.objective`；Automation 使用 JSON 編碼的語意 `arguments.intent_json`。Host 會把提案物化成真正可持久化的 Decision Card，只有使用者確認後才建立同 Session follow-up Run 或進入 Automation validate／dry-run／activate；模型輸出的 Reflection Evidence ID 也必須先通過 Host receipt 核對。

macOS UI 驗收必須雙擊本專案根目錄的 `開啟股市AI系統.command`，或從 Finder 確認開啟的是：

```text
/Users/your-user/Desktop/AI股市系統-Agent測試版/開啟股市AI系統.command
```

啟動後至少親自操作 Session 建立／續接、running 狀態輸入、Task Forest 展開、Decision 回覆、Evidence／Artifact 點擊定位與版本修改、Automation preview／確認，以及停止後重新啟動的 durable recovery。完整逐步清單與證據欄位見追溯矩陣的「macOS `.command` 實機 UI 驗收」。

Agent Runtime 重點測試入口：

```bash
uv run pytest -q \
  tests/test_agent_runtime_final_interaction.py \
  tests/test_agent_forest_executor.py \
  tests/test_agent_forest_projection.py \
  tests/test_agent_durable_checkpoint.py \
  tests/test_agent_runtime_final_repair_research.py \
  tests/test_agent_memory_durable.py \
  tests/test_agent_automation_final.py \
  tests/test_agent_final_system_scenarios.py \
  tests/test_agent_provider_matrix_chaos.py \
  tests/test_durable_agent_runtime.py \
  tests/test_agent_runtime_api.py \
  tests/test_agent_dock_state.py
uv run pytest -q tests/e2e/test_agent_dock_browser.py
uv run pytest -q
```

## 系統資料流

所有可執行的股票決策都必須走同一條核心流水線：

```text
市場與事件來源
  -> MarketDataHub           資料正規化、來源與品質契約
  -> IntelligenceHub        技術、新聞、情緒、財報與反思
  -> StrategyEngine          策略訊號與結構化決策
  -> ResearchEngine          回測、因子與研究有效性
  -> RiskEngine              唯一中央風控批准入口
  -> ExecutionEngine         僅紙上交易
  -> SQLite                  訊號、決策、委託、成交與持倉
```

Agent 的觀察清單掃描、比較與頁面重新整理只會執行分析，不會自動建立訂單。紙上訂單必須通過明確的執行入口；真實交易在設定與程式邊界上都維持停用。

### 中立分析與 AI 真實性

首頁不再要求使用者先輸入股票或選擇 Universe。第一次開啟會以 TWSE／TPEx 全市場官方批次資料建立可持久化的 `MarketIntelligenceSnapshot`；再次開啟先在一秒內回傳上一份快照並於背景依事件條件更新。中央圖表預設沿用最後查看股票，沒有歷史 Context 時顯示台灣市場基準；股票卡、圖表、詳細分頁與 Agent 共用同一份 `WorkspaceContext`。自選、Screener 與 Agent 仍透過同一個 Universe Resolver 解析持倉、明確代號、產業與官方成交量排名，並保留來源、篩選條件及建立時間。

條件選股採用 allowlisted typed DSL，而不是字串關鍵字或隱藏分數：可使用例如 `close >= 100`、`change_percent > 0`、`volume >= 100000`、`exchange == TWSE`、`realtime == true`，也可使用已快取官方資料的 `revenue_yoy > 15`、`institutional_buy_5d > 0`、`pe_percentile < 30`、`avg_turnover_20d > 50000000` 與欄位對欄位的 `close > sma_60`。所有條件為 AND；每筆結果都保存條件、觀測值、比較值、資料來源與通過收據。未知或無型別欄位會被 API 明確拒絕，缺即時或尚未快取的官方資料不會以 0 或預設值冒充通過，也不會在篩選請求中暗自啟動網路回補。

首頁是「AI 全市場決策中心」：中央顯示市場環境、可投資 Universe、已分類數量、現在可買、持續觀察、未來可買、暫不介入與持倉可賣／減碼；左側是同一份決策清單的篩選器，右側是常駐 Agent Dock。每檔股票只會落在一個主要市場分類，持倉行動另以 `HOLD`／`ADD`／`REDUCE`／`EXIT` 表示，並公開進出場區間、正反證據、觸發條件、失效條件、資料時間、來源與品質。零檔時會列出清楚的空狀態與最接近條件者，不會把候選冒充為買進訊號。全市場先由確定性多因子掃描，模型只處理候選漏斗、持倉、重大事件與排名變化；只有成功 receipt 才顯示 `AI VERIFIED`，模型失敗時仍保留全市場分類與上一份成功快照。

目前正式 UI 契約固定為首頁加五個工作區、共 32 個子頁，Workspace Context、深連結、Agent Dock 與 UI Action Registry 均以同一份版本化契約驗收。完整規格、遷移表與差距稽核見 [UI Contract V1](docs/architecture/ui-contract-v1.md)、[工作區資訊架構遷移紀錄](docs/architecture/workspace-migration-ledger.md) 與 [UI Contract V1 差距分析](docs/audits/ui-contract-v1-gap-analysis.md)。

`GET /api/workspace/bootstrap` 會在同一份回應提供持久市場快照、最後選取標的的預載圖表、自選與提醒摘要、持倉動作、全市場成交量／漲跌／產業／異常排行、Agent Context 與布局偏好。前端會先把預載圖表放入 L1 single-flight 快取，再建立中央圖表，因此不會為同一個首頁首次顯示重複讀取圖表。首頁搜尋會先查快照，再查正式證券主檔；只有找不到可直接開啟的證券時，才把產業名稱或自然語言指令交給 Agent，證券搜尋失敗不會被誤送成對話問題。法人排行若本輪缺少同日期可比較欄位，會明確顯示不可用，不以規則分數冒充法人買賣超。

量化規則、模型分析、Host 風控與執行狀態使用分離契約。只有 durable run、內層結果、completion validation、真實 invocation receipt 與正式 `MarketRadarResult` 同時成功，首頁才會顯示模型卡片；`max_steps_reached` 或前端仍在輪詢都不會冒充成功。未校準規則分數與模型自述信心也不會描述為成功機率。詳細邊界與驗證證據見 [Market Radar Runtime](docs/architecture/market-radar-runtime.md)、[Neutral Analysis Runtime](docs/architecture/neutral-analysis-runtime.md)、[Migration v10](docs/migration/neutral-runtime-v10.md) 與 [Definition of Done](docs/refactor/definition-of-done-evidence.md)。

交易預覽、量化規則參考與風控工作區在未指定股票時會直接顯示空狀態，不會送出分析請求或自行補入標的。J 頁固定標示為「非模型規則彙整」，操作名稱為「更新規則參考」；只有具備成功模型呼叫憑證的結果才能使用 AI／MODEL 標示。

統一資料平台由 `MarketDataPlatform` 提供唯一標準化讀取入口；原始 Response 以 SHA-256 保存，正規化資料保留修訂、六種時間座標、來源、品質、備援狀態與轉換血緣。歷史查詢同時限制 `available_at` 與 `acquired_at`，避免回測讀取當時尚未取得的資料。Source Registry v2 另將正式資料來源、授權狀態、更新頻率、可靠度、資料集端點、欄位與失敗／備援策略集中在 `config/market_data_sources.yaml`；讀取器只使用資料集 ID，不自行保存上游網址。架構與漸進遷移範圍見 [Unified Market Data Platform](docs/architecture/unified-market-data-platform.md)、[Migration v11](docs/migration/unified-market-data-v11.md) 與 [Source Registry v2](docs/migration/source-registry-v2.md)。

### Agent 不只是聊天

頂部常駐對話列仍是任何頁面都能使用的統一 Agent 輸入入口；所有回覆與執行狀態集中在右側常駐的 Agent Dock。首頁、研究、Market Radar 與模擬交易工作區的 Agent 動作都建立相同的 durable Session／Run，不再各自保存或輪詢第二份回答。右側工作區可調整寬度、收合或最大化，並以 Chat、Tasks、Artifacts 分頁呈現目前 Context、固定 Plan、步驟樹、工具、Skill、批准與產物。首頁的「直接交給 AI Agent」也使用同一條 `/api/agents` 執行管線，不會把內容轉送到另一個 Codex 聊天任務。`/api/codex/run` 只保留為預設停用的開發者診斷 API，不在一般 Agent 路徑使用。

桌面配置預設將左側功能列縮為僅保留可辨識的功能名稱，右側 Agent Dock 也採較窄寬度，優先把中央空間留給行情卡、K 線與主要操作畫面。自然語言問題若需要多步執行，Codex 會依當次目標自行建立並修訂可視化 Plan，而不是套用固定步驟；每一步完成後都會留下「本步結果、仍缺資訊、下一步」的公開進度摘要。固定 Plan 依實際先後順序排列且可收合，收合後仍保留完成數與 Revision 進度。Chat 中的 Skill、Package、工具與 Host 驗證預設只顯示小型操作字卡，使用者需要時才展開完整參數與結果；珍珠白與日光藍等淺色主題會提高正文對比，但仍保留執行中、成功、警告、失敗、Skill、Package 與 MCP 的狀態色。首頁預覽、個股完整圖表、工作區分頁、繪圖工具、決策摘要與 Agent 圖形區共用同一份 light-theme surface／chart token，切換淺色主題時不會殘留獨立的黑色畫布或面板。最終回答則依問題自然選擇編號段落、清單及比較表格的結構，不固定章節名稱或順序，也不把行情、技術、新聞與風險全部擠成一段文字。對話依事件時間由上往下排列，完成回答會出現在最後一筆執行活動之後並自動捲入視野。

每段對話先建立 durable `session_id`，每個任務再建立 `run_id` 與 PlanGraph。Codex 驅動器會建立綁定目前專案 root 的隱藏 App Server thread；其他驅動器則直接呼叫已設定的模型／Agent endpoint。無論選哪一個 Provider，真正的工具仍由 Stock AI Host 執行。使用者明確要求建立本機純文字 Artifact 時，若 Provider 遺漏唯一已揭露的 `artifact.create_text`，Host 會編譯該受限、可稽核的本機操作並保留批准卡；否定式「不下單／不操作真實帳戶」不會被誤判成實盤要求。UI 只是訂閱者：中斷串流、切頁或重新開啟不會取消後端任務；重連時會依事件 sequence 從 SQLite 續播，並先用 Run Snapshot 重建 Plan、Step、Tool Call、Approval、Artifact、Environment Snapshot 與最後結果。WKWebView 長串流若暫停送達，前端會以相同 sequence 週期補讀 durable events，不會重複執行工具。事件採用具 `event_id`、sequence、Session／Run／Plan／Step／Tool／Approval 關聯欄位的統一 envelope，寫入 SQLite 與送到前端前都會遮罩密鑰。UI 狀態、待執行 command 與結果也保存在 SQLite，後端或 WebView 重啟後不會遺失。工作台顯示 Provider lifecycle、可稽核計畫摘要與修訂、遮罩後的工具參數、Skills／MCP／套件、驗證、批准、checkpoint、復原、結果與排程；Codex SDK 重複的 started/completed/token-usage 事件會合併成一張「模型執行紀錄」，完整明細仍可展開。系統不偽造狀態，也不曝露任何模型私有的逐字 chain-of-thought。批准遭拒不是整個 Run 失敗：Runtime 會保存拒絕證據，從安全 checkpoint 恢復並要求目前 Provider 修改計畫；同一組參數不能重送。

排程也屬於 durable runtime：`schedule.create/list/cancel` 可建立後端 advisory 任務，即使 Web／WKWebView 沒有開啟也會執行。市場行情、新聞、持倉，以及 UI state／command 完成事件會先進入 SQLite `agent_runtime_events`，由 Runtime 以租約取出後比對 event／condition schedule；事件內容具有去重鍵，程序重啟後可續處理而不會在同一時間窗重複觸發。為了避免背景任務繞過互動授權，scheduled run 固定是 `advisory`，不會背景修檔或送模擬單。

Capability Registry 是工具 manifest、routing 和狀態的單一真相來源。Agent 可讀取 market、portfolio、paper broker、project、web、Browser、通知、Skills、MCP、UI 與外部金融專案能力。Skills 會實際載入完整 `SKILL.md` 到當次 run；MCP 由已登入的 Codex App Server 取得真實 schema，並依 `readOnlyHint`／`destructiveHint` 分層綁定。自己的 UI 由 action bus 操作，不依賴固定 port、Chrome 或 Computer Use，因此 Web、WKWebView 和自動選埠都能使用；外部網站則由隔離的 Playwright browser context 實際載入 JavaScript 頁面。

專案變更須明確選擇 `project_execute`；瀏覽器點擊／填寫、通知發送與非破壞性 Connector 寫入須選擇 `external_execute`；模擬送單須選擇 `paper_execute`，並在同一輪先預覽完全相同的委託；破壞性 Connector 寫入只允許 `full_execute`。`terminal.run` 是無 shell、argv allowlist、清洗環境、無網路的專案 sandbox，不是任意 zsh。原生 Codex command／file approval 預設拒絕，只能由當次 run、capability、專案 root 與過期時間完整匹配的明確 grant 開放。每次批准使用短效、一次性 challenge，且工具名稱與完整參數摘要不可替換。專案寫入、取代、移動與刪除另要求呼叫者提供先前讀取的 SHA-256；檔案若已被其他程序修改，操作會拒絕而不是覆蓋新內容。Python、JSON、TOML 與 YAML 寫入會在原子替換前先解析，語法錯誤時原檔保持不變。所有模式都不會開啟真實券商交易。

### 本機 Web 安全邊界

應用仍只設計為 loopback 本機工作站，但 localhost 並不等於自動可信。首頁會為目前後端程序簽發隨機 session token；同來源的 `/api/*` 請求必須攜帶該 token，所有會改變狀態的請求另外驗證 `Origin`。後端重新啟動後舊頁面會自動重新載入以取得新 token。啟動器也會先讀取首頁 token，再執行受保護的能力健康檢查。

網站讀取與 Browser 工具會在每次連線及重新導向後重新驗證目的位址，拒絕 loopback、私有網段、link-local 與不安全 scheme；工具參數、事件與錯誤輸出會遮罩常見 API key、token、cookie、Authorization 與密碼內容。

## 專案結構

```text
.
├── src/
│   ├── stock_ai/                 應用層：FastAPI、資料服務、Codex、UI API
│   │   ├── agent_*.py            Agent 驅動器、durable run、UI bridge、儲存與 HTTP API
│   │   ├── capability_registry.py Capability manifest、路由與狀態的單一來源
│   │   ├── tool_providers/        Skills、MCP、UI 等可插拔工具供應器
│   │   ├── sandbox_executor.py    無 shell 的專案命令 sandbox
│   │   └── ui/static/            瀏覽器前端；JS/CSS 依 core、shell、features 分區
│   └── open_stock_ai/            領域核心：資料、情報、策略、研究、風控、執行
│       └── agent_runtime/         供應商無關的 Agent 合約與主控迴圈
├── tests/                         單元、整合、API、UI 與安全邊界測試
├── config/                        系統設定、資料目錄、外部來源鎖定
├── docs/
│   ├── architecture/             系統結構、Agent 與市場連動設計
│   ├── data/                     資料來源、即時行情與資料政策
│   ├── guides/                   啟動、桌面外殼與操作指南
│   ├── integration/              Codex、Open Stock AI、外部專案整合
│   └── reference/                查詢能力與參考資料
├── skills/                        專案內 Codex 市場分析技能
├── external/                      固定來源版本的第三方研究專案
├── macos/                         macOS 原生外殼來源與發行包
├── scripts/                       維護與外部專案 bootstrap 工具
├── output/                        本機資料庫與生成結果，不提交 Git
├── logs/                          本機服務日誌與狀態，不提交 Git
└── .runtime/                      本機 Python、uv、Codex 與 App runtime，不提交 Git
```

更完整的模組責任與依賴規則見 [專案結構與維護定位](docs/architecture/project-structure.md)。
前端檔案、載入順序與頁面責任見 [前端架構](docs/architecture/frontend.md)。
Agent 的責任邊界、持久化、事件、批准與恢復規格見 [Stock AI Agent Runtime contract](docs/architecture/agent-runtime-contract.md)、[Final Agent Runtime（P0–P105）](docs/architecture/final-agent-runtime-p0-p105.md) 與 [P0–P105 實作追溯矩陣](docs/architecture/P0-P105-IMPLEMENTATION-MATRIX.zh-TW.md)。

## 問題應該修在哪裡

| 問題類型 | 優先檢查位置 |
| --- | --- |
| API 路由、HTTP 狀態、頁面資料缺欄位 | `src/stock_ai/main.py`、`src/open_stock_ai/api.py` |
| 股票搜尋、摘要、歷史 K 線、新聞與市場服務 | `src/stock_ai/services.py`、`realtime_quotes.py`、`phase1_data.py` |
| Codex 登入、thread、prompt 與 Computer Use | `src/stock_ai/codex_api.py`、`codex_runtime.py`、`codex_market.py` |
| Agent 實際工具、Codex provider 與自主迴圈 | `src/stock_ai/capability_registry.py`、`agent_tools.py`、`agent_drivers.py`、`src/open_stock_ai/agent_runtime/` |
| Durable run、重連、取消、持久化事件與事件排程匣 | `src/stock_ai/durable_agent_runtime.py`、`agent_run_store.py`、`agent_event_bus.py`、`agent_api.py` |
| Agent 專案、安全終端機與網站工具 | `src/stock_ai/agent_general_tools.py`、`sandbox_executor.py` |
| Skills、MCP 與自有 UI 動作橋接 | `src/stock_ai/tool_providers/`、`agent_ui_bridge.py`、`ui/static/js/features/agent-ui-bridge.js` |
| 原生 Codex SDK、隱藏 thread 與 scoped approvals | `src/stock_ai/codex_runtime.py`、`approval_policy.py`、`codex_api.py` |
| Agent 外部金融專案隔離執行器 | `src/stock_ai/external_project_tools.py`、`external_runtime_worker.py` |
| Agent 模擬交易工作區 | `src/stock_ai/agent_trading_api.py`、`paper_training_api.py` |
| 前端畫面、事件綁定或圖表 | `src/stock_ai/ui/static/index.html`、`ui/static/js/`、對應 CSS |
| 資料來源、品質、即時性或 execution eligibility | `src/open_stock_ai/data/`、`src/stock_ai/source_policy.py` |
| 技術／新聞／基本面情報 | `src/open_stock_ai/intelligence/` |
| 策略訊號與買賣判斷 | `src/open_stock_ai/strategy/` |
| 回測、研究報告與因子 | `src/open_stock_ai/research/` |
| 風控拒絕、曝險、停損與部位上限 | `src/open_stock_ai/risk/`、`central_risk_adapter.py` |
| 紙上委託、成交、現金與持倉 | `src/open_stock_ai/execution/` |
| SQLite schema、migration、訊號與交易保存 | `src/open_stock_ai/storage/` |
| 外部 FinGPT／FinRL／Qlib 等契約 | `src/open_stock_ai/external_sources/`、`config/external_sources.lock.yaml` |
| macOS 啟動、埠、runtime 或 Liquid Glass | `open-stock-ai.sh`、`macos/`、`build-macos-liquid-glass.sh` |
| Windows 啟動與停止 | `open-stock-ai.ps1`、`stop-stock-ai.ps1` |

修補時不要在 UI 或 Agent prompt 內複製策略、風控或帳戶規則；這些規則必須留在對應核心模組，避免形成第二套真相。

## 設定與資料

主要設定位於：

- `config/open_stock_ai.yaml`：paper mode、風控門檻、SQLite、watchlist、外部專案路徑
- `config/agent_runtime.yaml`：預設 Provider、可替換模型介面與固定安全政策
- `output/agent_runtime_settings.json`：保存非秘密的 Provider、端點與模型偏好，權限為 `0600` 且不提交 Git；API key／Bearer token 只進 macOS Keychain 或後端環境變數
- `config/data_catalog.yaml`：資料目錄
- `config/market_data_sources.yaml`：Source Registry v2 與 Cache Policy V2；來源授權、頻率、可靠度、資料集端點、TTL、失效條件、欄位與失敗／備援策略的唯一真相來源
- `config/data_quality_rules.yaml`：Data Quality Service v2；必填欄位、數值、非負值、時間對齊與一致性規則
- `config/data_reconciliation_rules.yaml`：Reconciliation Engine V1；價格、財務與事件的跨來源比較方法及容許誤差
- `config/external_sources.lock.yaml`：第三方專案來源、分支與 commit
- `.env`：本機憑證與環境覆寫，不會提交 Git
- `.env.example`：可提交的環境變數範例

### 台股證券清單、歷史 K 線與即時同步

台股代號不是寫死在前端。`GET /api/securities/master` 由三個可恢復的官方分區同步 TWSE／TPEx 上市、上櫃、興櫃、ETF、權證、指數及下市資料；快取到期後才重讀，`POST /api/securities/master/refresh` 可強制刷新。Incremental Loader V2 會在每個成功批次後提交 cursor；中斷或受控暫停後只從最後提交位置續傳，成功且仍在 TTL 內時不再呼叫來源。`GET /api/data/ingestion/runs` 可稽核每次更新與批次。Revision History V1 以不可變 trigger 保護每個 normalized revision；`POST /api/data/snapshots` 能固定完整 point-in-time revision manifest，之後可依相同版本重建資料。Data Quality Service V2 依台北市場日對每個資料集掃描缺值、統計／契約異常、時間錯位、重複 revision payload 與既有來源衝突；`POST /api/data/quality/daily` 產生可追溯的每日報告及問題明細，相同資料狀態不會重複建立報告。內部 `ENT-*` 以統編及正式代號／名稱解析，不以 `.TW`、`.TWO` 當主鍵；轉板、下市與權證到期保存為生命週期事件。`GET /api/data/entity-registry/resolve` 可用來源代號、正式顯示代號、統編、內部 ID 與 `as_of` 時間點解析同一標的；代號重用或同時歧義會明確回傳候選，不會靜默猜測。`GET /api/data/sources` 同時提供每個正式來源與資料集的授權、頻率、可靠度、端點模板、欄位和失敗策略，資料目錄可直接檢視。Data Envelope V2 會為 payload 的每個值保存實際來源、來源／取得／更新時間、品質、原始資料 ID／路徑與轉換輸入；Temporal Contract V1 再把交易日、財務期間、公告時間、來源可用時間、系統取得時間與有效時間分離。Raw Data Lake V1 保存 API／CSV／JSON 的原始 bytes、wire SHA-256、媒體類型與長度，並以不可變 trigger 防止覆寫；`POST /api/data/raw/{raw_payload_id}/reprocess` 可從原始物件重跑清洗並稽核輸入／輸出 hash。Standard Market Warehouse V1 再將相同 revision 同步投影到價格、財務、籌碼、事件與總體五張標準表；`GET /api/data/warehouse/{domain}` 與通用查詢會讀到相同 revision 與 payload。`POST /api/data/query` 可分別傳入 `knowledge_at` 與 `effective_at`，歷史研究不會讀到尚未公告或尚未取得的 revision；既有 `as_of` 仍可同時設定兩個時間點。資料目錄可反查欄位血緣、時間基準與原始物件並操作重跑。個股頁與 Agent 仍使用相容的主動股票／ETF 清單。詳細 migration 見 [Security Lifecycle v12](docs/migration/security-lifecycle-v12.md)、[Entity Registry v13](docs/migration/entity-registry-v13.md)、[Source Registry v2](docs/migration/source-registry-v2.md)、[Data Envelope V2 Field Provenance v14](docs/migration/data-envelope-v2-field-provenance-v14.md)、[Temporal Contract V1 / schema v15](docs/migration/temporal-contract-v15.md)、[Raw Data Lake V1 / schema v16](docs/migration/raw-data-lake-v16.md)、[Standard Market Warehouse V1 / schema v17](docs/migration/standard-market-warehouse-v17.md)、[Incremental Loader V2 / schema v18](docs/migration/incremental-loader-v18.md)、[Revision History V1 / schema v19](docs/migration/revision-history-v19.md) 與 [Data Quality Service V2 / schema v20](docs/migration/data-quality-v20.md)。

Cache Policy Service V2 會依資料種類套用不同 TTL 與明確失效原因，區分 `fresh`、`stale_while_revalidate`、`expired`、`invalidated`；超過 stale window 的資料不會標成可服務。資料庫 refresh lease 讓同一來源／資料集／分區同時只發出一個上游請求，其他請求會明確回報 refresh 進行中。`GET /api/data/cache` 可稽核狀態，`POST /api/data/cache/{dataset}/invalidate` 會留下失效事件。詳細設計見 [Cache Policy V2 / schema v21](docs/migration/cache-policy-v21.md)。

Source Failover V1 會依 Source Registry 審核過的重試、HTTP 狀態與備援資料集順序執行故障切換。每次主來源與備援嘗試都保存 run、實際 `source_id`、資料集、端點、錯誤 Response、原始 payload 與 revision；備援 revision 固定標示 `is_fallback=true`，主來源恢復後兩個來源仍分開保存，查詢會優先選擇非備援資料。若共享傳輸保護器因併發、退避或 circuit 狀態拒絕官方來源讀取，已具持久化資料契約的法人、融資券與月營收讀取會回傳既有官方快取，而不是把預期的限流拒絕升級成 UI 的 HTTP 500；未知程式錯誤仍會保留為錯誤。`GET /api/data/failover` 與 `/api/data/failover/runs` 可稽核切換狀態。詳細設計見 [Source Failover V1 / schema v22](docs/migration/source-failover-v22.md)。

Reconciliation Engine V1 會在指定 `knowledge_at` 對每個來源只取最新 revision，依價格數值、財務絕對／相對誤差、事件類型／正規化文字／時間容許範圍進行交叉校驗。每次 run 與衝突證據會保存於 schema v23；差異只會標記為 `open` conflict，不會覆寫任一來源，後續值收斂時則留下 `resolved` 生命週期。`GET /api/data/reconciliation`、`POST /api/data/reconciliation/runs` 與衝突查詢 API 可稽核狀態，資料目錄 UI 可直接執行並檢視來源差異。詳細設計見 [Reconciliation Engine V1 / schema v23](docs/migration/reconciliation-engine-v23.md)。

Unified Data API V1 將首頁、資料庫、選股、新聞、籌碼、基本面、個股圖表與即時監控所需的市場資料集中到版本化 `/api/data/ui/v1/*` 邊界。53 條 façade 路由共用既有後端 service 與 `MarketDataPlatform`，前端不再持有分散的舊市場資料路徑，更不會直接存取來源 connector；舊路徑只保留相容用途。`GET /api/data/ui/v1/contract` 可查詢完整路由、頁面 consumer 與邊界狀態。詳細設計見 [Unified Data API V1](docs/migration/unified-data-api-v1.md)。

CHIP-003／CHIP-004 將借券賣出、還券、借券餘額與當沖量／當沖比分開保存及顯示。`GET /api/data/ui/v1/flow/chip/short-daytrade` 依使用者明確輸入的 `.TW` 或 `.TWO` 代號，讀取 TWSE／TPEx 官方歷史資料；非交易日不補零，近期當沖數值標示 T+2 修訂風險，熱度只採公開機械區間且不作為投資訊號。完整契約見 [Borrowed Short and Day-Trade History V1](docs/data/short-daytrade-history-contract.md)。

CHIP-005 透過 `GET /api/data/ui/v1/flow/chip/tdcc-history` 讀取 TDCC 官方 OpenAPI 最新每週持股分級，保存每週快照、17 級原始列與 SHA-256，並分開顯示股東總人數、1–10,000 股顯示分群、千張大戶及 400 張以上比例。集中度直接採 400,001 股以上比例，是公開的機械式顯示指標而非模型分數或投資訊號；OpenAPI 只供最新快照，因此本地歷史逐週累積且不宣稱已自動回補完整一年。完整契約見 [TDCC Holding Distribution History V1](docs/data/tdcc-holding-history-contract.md)。

Complete Data Lineage V1 以 schema v24 保存不可變的衍生指標與結論 artifact，並將每個輸入欄位連回 revision、原始 payload、wire SHA-256 與實際來源。結論寫入採 fail-closed：任何輸入若無法完整回溯到通過完整性檢查的原始資料就拒絕保存。`GET /api/data/lineage/{target_id}` 會回傳完整多階段圖與缺口；資料目錄直接顯示「原始來源 → revision → 指標 → 結論」。詳細設計見 [Complete Data Lineage V1 / schema v24](docs/migration/complete-data-lineage-v24.md)。

Realtime Quote V1 以同一份 `stock_ai.realtime_quote.v1` 契約正規化 TWSE MIS 五秒快照與 Fugle 授權行情，完整揭露最新成交／單筆量、最佳買賣、委買賣五檔、累計成交量、交易狀態、交易所時間、接收時間、新鮮度與來源授權屬性。`GET /api/realtime/quote/{symbol}` 提供快照，`GET /api/realtime/stream/{symbol}` 以具重連提示、序號與禁用代理緩衝的 SSE 持續更新；個股 UI 僅接受這份契約更新畫面。詳細設計與來源界線見 [Realtime Quote V1](docs/data/realtime-quote-contract.md)。

Intraday Candle V1 以 schema v25 保存不可變 1 分 K revision 與來源批次 receipt，並由台北時間 09:00 錨定重建 1、5、15、30、60 分 K。指定交易日只讀已保存分鐘資料，缺少分鐘會揭露數量而不向前填值；`complete` 表示來源批次完整，盤中批次或 quote 樣本才標為 `partial`。`as_of` 可重建當時已知版本。個股頁可選週期及交易日，顯示來源根數、重建根數、完整狀態與可用日期。詳細設計、來源授權界線與驗證方式見 [Intraday Candle V1](docs/data/intraday-candle-contract.md)。

個股圖表保留上述來源、修訂與完整性契約，只在顯示層提供互動。使用者可在蠟燭 K 線、美國線、收盤折線、收盤面積圖與 Heikin-Ashi 之間切換，並可快速查看 1 日、5 日、1／3／6 個月、1／3／5 年或全部資料；十字游標仍顯示來源資料的日期、開高低收與成交量，不會把 Heikin-Ashi 顯示值冒充原始成交價。滑鼠滾輪可縮放，游標模式拖曳可平移，鍵盤方向鍵也能逐根檢視。趨勢線、水平價位與交點標記依「股票代號＋週期」保存在本機瀏覽器，支援復原、清除與重設視圖，不會修改或回寫任何市場資料。

首頁預設的 `^TWII` 等市場指數也使用同一套互動圖表，但會走獨立的指數歷史路徑；切換指數時不會誤送個股即時五檔、財報、籌碼或估值請求。Workspace 更新串流使用可攜帶本機工作階段驗證標頭的串流連線，重連時會保留目前快照與圖表。

資料依用途分層，避免把不同時效混成「全部即時」：

- 證券主檔：TWSE／TPEx 官方資料，每小時重新同步。
- 全市場最近收盤：TWSE `STOCK_DAY_ALL` 與 TPEx `tpex_mainboard_quotes`，五分鐘更新快取。
- 歷史日 K：上市使用 TWSE `STOCK_DAY`、上櫃使用 TPEx「個股日成交資訊」；`start`／`end` 可查任意完整日期範圍，最多每頁 5,000 根並以游標無缺口接續。OHLCV 與成交金額以不可變 revision 保存，逐月揭露官方覆蓋與錯誤，不再裁成最近 160 根。`price_basis` 可明確選擇原始未復權、前復權或後復權；復權價格與累積因子由 TWSE／TPEx 官方除權息參考價重建並保存，因子覆蓋不完整時預設拒絕用於回測。詳見 [Complete Daily History V1](docs/data/daily-history-contract.md) 與 [Price Adjustment V1](docs/data/price-adjustment-contract.md)。
- 盤中選定標的：本機以 TWSE MIS 約五秒網頁快照更新目前開啟的上市／上櫃股票。這不是可同時轉散布數千檔的授權低延遲行情。
- 盤中分 K：有 Fugle key 時保存授權 1 分 K；未設定時可用明確標為研究用途的 Yahoo 最近七日 1 分 K。五種週期一律由保存的 1 分 K 重建，沒有成交時不以買賣中價造 K。
- 歷史來源暫時失敗時：可用 Yahoo 歷史資料維持圖表連續性，但 API 會標示 fallback；盤中成交與執行資格不會因此升級。
- 公司行動：股利、增減資、正反分割、合併換股與庫藏股以 schema v28 不可變修訂保存；只有具 TWSE／MOPS／TPEx 官方網址、條款完整且已到期的事件能透過明確操作同步紙上帳戶。配息現金、股數、成本與價格同交易更新，增資只建立待人工認購權、不自動扣款，庫藏股明確為持有人 no-op。詳見 [Corporate Action Ledger V1](docs/data/corporate-action-contract.md) 與 [schema v28](docs/migration/corporate-actions-v28.md)。
- 交易限制：注意股、處置股、停牌、恢復交易與當日漲跌停以 schema v29 官方來源修訂保存，並在預覽、送單、掛單撮合及 OMS 最終成交套用同一規則。注意股只提示；處置股須明確限價；停牌阻擋成交直到官方恢復；超出漲跌停或無流動性證據的鎖死市價單會被拒絕。詳見 [Trading Restriction Contract V1](docs/data/trading-restriction-contract.md) 與 [schema v29](docs/migration/trading-restrictions-v29.md)。
- 流動性：`GET /api/data/ui/v1/market/{symbol}/liquidity` 使用官方未復權日成交量／成交額、TWSE MIS 或 Fugle 即時最佳買賣價，以及 TWSE／TPEx 官方已發行普通股數，計算平均量、平均成交額、換手率、即時買賣價差、委託量占比與透明的滑價模型估計。缺少即時五檔、官方股數或委託股數時欄位保持 `null` 並標為資料不足，不以歷史價格或假股數補值。schema v30 會保存不可變股數 revision 與每次評估輸入。詳見 [Liquidity Assessment Contract V1](docs/data/liquidity-assessment-contract.md) 與 [schema v30](docs/migration/liquidity-assessments-v30.md)。
- 異常交易：個股頁可用官方未復權日 K 掃描爆量、跳空、急漲急跌與價量背離。每個事件保存原始日 K、來源、指標、公開門檻與 detector version；重掃只追加觀察，確認、解除與重開也以 append-only 動作追蹤。缺少官方資料時不以 fallback 或 AI 推測造事件。詳見 [Trading Anomaly Event Contract V1](docs/data/trading-anomaly-contract.md) 與 [schema v31](docs/migration/trading-anomalies-v31.md)。
- 月營收歷史：基本面頁可從 MOPS 官方封存增量保存 2010 年起的逐月資料，包含當月營收、月增、年增、累計營收與累計年增。每月保存原始 CP950 HTML、欄位血緣、不可變 revision 與 checkpoint；封存未提供原始公告時間時明確保留 `published_at=null`，不以月份冒充發布日。詳見 [Monthly Revenue History Contract V1](docs/data/monthly-revenue-history-contract.md) 與 [schema v33](docs/migration/monthly-revenue-history-v33.md)。
- 損益表歷史：基本面頁可從 MOPS 官方 IFRS 歷史站增量保存 2013-Q1 起的營收、毛利、營業利益、稅後利益與 EPS，預設可完整涵蓋十年以上。主欄保存官方年初至今累計值；Q2/Q3 另存官方單季值，Q4 年報未直接揭露單季時保持 `null`，不以相減方式製造 EPS。每季保存原始 UTF-8 HTML、POST 參數、欄位血緣、不可變 revision 與 checkpoint；歷史頁未提供原始申報時間時明確保留 `published_at=null`。詳見 [Income Statement History Contract V1](docs/data/income-statement-history-contract.md) 與 [schema v34](docs/migration/income-statement-history-v34.md)。
- 資產負債表歷史：基本面頁可逐季保存 MOPS 官方 IFRS 現金、總資產、總負債、股東權益、存貨與應收款，並以相鄰已保存季度計算差額及變動率。原始 HTML、POST 參數、欄位標籤、不可變 revision 與 checkpoint 均保留，缺值與零分母不製造比較值。詳見 [Balance Sheet History Contract V1](docs/data/balance-sheet-history-contract.md) 與 [schema v35](docs/migration/balance-sheet-history-v35.md)。
- 現金流量表歷史：基本面頁逐季保存官方營業、投資、融資現金流與資本支出，依 `營業現金流 − |資本支出|` 計算自由現金流，並與同期間官方稅後利益計算現金轉換率及獲利品質。缺少任一來源時保持 `null`／資料不足，不跨期間拼接。詳見 [Cash Flow History Contract V1](docs/data/cash-flow-history-contract.md) 與 [schema v36](docs/migration/cash-flow-history-v36.md)。
- 財務比率歷史：以同期間官方損益表及資產負債表自動計算毛利率、營益率、淨利率、ROE、ROA 與負債比；Q1-Q3 累計淨利依季度年化，ROE/ROA 優先使用相鄰季度平均餘額。每列公開公式、實際輸入、分母基準及兩張官方報表連結，缺值與零分母維持 `null`。詳見 [Financial Ratio History Contract V1](docs/data/financial-ratio-history-contract.md)。
- 財報修訂與當時版本：基本面頁可查詢月營收、損益表、資產負債表與現金流量表的不可變版本鏈，逐欄顯示同一來源修訂前後差異；指定 `knowledge_at` 後只選取當時已公告、可用且已取得的版本，普通財報、財務比率與成長率查詢也沿用相同截止時間，避免歷史研究前視偏誤。平行來源不會被誤判為修訂，未保存第二版時也不宣稱「沒有重編」。詳見 [Financial Revision History Contract V1](docs/data/financial-revision-history-contract.md)。
- 產業特有指標：基本面頁依證券主檔選擇金融、半導體、航運或營建的獨立分析配方。金融控股使用 TWSE 專用損益與資產負債 OpenAPI，不套製造業毛利模板；其餘產業分別呈現資本支出、船隊資產效率、營建存貨與現金回收等指標。缺少官方必要欄位時顯示不可用，未知產業不回退成通用模板。詳見 [Industry-specific Metrics Contract V1](docs/data/industry-specific-metrics-contract.md)。
- 財測與指引：基本面頁讀取 TWSE 自願財測的預測區間及同期間會計師查核／核閱實際數，顯示區間內、優於或低於財測及達成率。法說指引與管理層展望沿用具來源契約；沒有量化區間時保持不可比較，沒有自願財測時也不以法人預估或 AI 預測替代。詳見 [Financial Guidance Tracking Contract V1](docs/data/financial-guidance-tracking-contract.md)。
- 財報異常：以兩個連續且三表期間一致的官方季度，自動檢查應收成長相對營收、存貨資產比、營業現金流轉換、毛利率變化與一次性損益。每項公開觀察值與門檻；官方欄位缺漏時標示不可判斷。詳見 [Financial Anomaly Flags Contract V1](docs/data/financial-anomaly-contract.md)。
- 基本估值：在同一交易所估值日，以官方收盤價、不可變股數版本及同期間三張 MOPS 財報一致計算 PE、PB、PS、EV/EBITDA 與 FCF Yield，另保留交易所公布殖利率及 PE／PB 交叉核對；缺值不以零或總負債替代。詳見 [Basic Valuation Contract V1](docs/data/basic-valuation-contract.md)。
- 歷史估值分位：逐月保存 TWSE／TPEx 官方 PE、PB 與殖利率的最後可用交易日原始列及雜湊，使用 midrank 經驗分位計算 1／3／5／10 年相對位置，公開每個視窗的實際樣本數與涵蓋日期。官方歷史未提供 PS、EV/EBITDA、FCF Yield 時明確列為不支援，不以目前值回填。詳見 [Valuation Percentile Contract V1](docs/data/valuation-percentile-contract.md)。
- 同業比較：只接受官方證券主檔中產業代碼完全相同、仍交易中的普通股，且比較名單必須由使用者明確指定；逐家公司保留交易所估值日期與相同財報期間，並比較 PE、PB、殖利率、獲利率、ROE 與負債比。候選排序只供瀏覽，不會暗中加入比較或把高低直接標為優劣。詳見 [Peer Comparison Contract V1](docs/data/peer-comparison-contract.md)。
- DCF、情境與敏感度：基本面頁提供可調整基期營收、毛利率、FCF 轉換率、成長率、折現率、終值成長、淨負債、股數與預測年數的完整模型；逐年現金流與公式公開，只輸出悲觀／中性／樂觀範圍，不提供單一目標價，並同時呈現成長率×折現率及毛利率×終值的 5×5 矩陣。詳見 [DCF Scenario and Sensitivity Contract V1](docs/data/dcf-scenario-sensitivity-contract.md)。
- 模型選擇與證據分層：依產業、獲利、自由現金流、股利、資產密集度與成長特性決定適用及排除模型並公開理由；歷史事實、公司指引、具名分析師預估與使用者模型假設維持四個獨立層級，不混成單一數字。詳見 [Valuation Model and Evidence Contract V1](docs/data/valuation-model-evidence-contract.md)。
- 法人與融資券歷史：籌碼頁回補並保存官方 T86 交易日，分開呈現外資、投信、自營商的每日／累計淨額與連續買賣超；MI_MARGN 歷史保留融資融券餘額、增減、限額、使用率、券資比及資券互抵，非交易日不以零填補。詳見 [Institutional and Margin History Contract V1](docs/data/chip-flow-history-contract.md)。
- 成長率歷史：月 MoM／YoY／累計 YoY 直接使用官方揭露；季 QoQ 僅比較連續且有官方單季值的季度，Q4 未直接揭露單季時保持 `null`；年度 YoY 與 3／5／10 年及可用區間 CAGR 使用官方年度端點並公開起訖來源。詳見 [Growth Metric History Contract V1](docs/data/growth-metric-history-contract.md)。

近期上市股票若實際交易日不足，圖表只顯示資料量足夠的 MA／BOLL／MACD，並揭露有效 K 線根數；不會捏造上市前價格。若產品要對大量使用者同時提供全市場逐筆或低延遲行情，仍須接入交易所或授權資訊商，詳見 [授權盤中即時行情源接入](docs/data/authorized-market-data.md)。

價格與執行資格會分開標記。研究資料可以作為背景，但不能把延遲、fallback 或模擬價格偽裝為可交易即時價格。核心資料契約會揭露來源、交易所時間、接收時間、即時性、fallback、模擬狀態、執行資格與阻擋原因。

`open_stock_ai.source_envelope.v3` 不再依賴來源名稱字串猜測信任等級，而是使用固定 `provider_id`、`connector_id`、`quote_kind`、`exchange_timestamp`、`received_at`、`max_age_seconds`、`authorized`、`realtime`、`delayed`、`official_close` 與完整性 signature。Intraday 只接受已授權、新鮮的 realtime last trade；swing／after-market 可接受新鮮即時成交或官方收盤；research 可讀延遲或 Yahoo，但固定不可執行。

## 紙上帳戶與執行邊界

首頁、Agent 工作區、模擬交易頁面與 Codex context 共用同一個 SQLite Paper Account。帳戶的現金、持倉、掛單與成交資料是唯一真相，UI 展示卡不是另一套帳本。

一般 Codex 問答與 advisory Agent 不得暗中建立委託。只有使用者明確選擇自主模擬執行時，Agent 才能在同一輪完成精確預覽後進入 Paper Broker；模擬帳戶不得超支，也不得賣出未持有部位。

Agent 的預覽有 30 秒有效期，送出前會重新取得伺服器價格與 source-envelope signature；價格或來源改變必須重新預覽。Agent order 另外強制 Central RiskEngine 通過；人工 paper-training lab 仍可以明確作為受限制的實驗來記錄被拒絕或風險建議，但不會改變 Agent 的強制邊界。

### 實證與學習升級

`ExactStrategyReplay` 會在嚴格時點資料上逐期呼叫生產的 `StrategyEngine`，信號在下一根 bar 才執行，並計入交易成本、滑價和延遲。報告同時包含 walk-forward folds、樣本外結果、purged cross-validation、策略版本 hash 和資料版本 hash。精確回放與僅研究用途的 MA baseline 共用同一套 ledger-derived 績效契約，輸出 CAGR、Sharpe、Sortino、Calmar、最大回撤、曝險、總／平均換手、hit ratio、profit factor 與 tail loss；無法從資料算出的比率會明確為 `null` 並附 warning，不會補零冒充結果。任何 feature `available_at` 晚於 signal time、缺失 PIT intelligence 或樣本不足都會停止 exact/empirical 資格；系統不會退回簡單 MA 後宣稱生產策略通過。

策略升級同時必須通過統計顯著性與 regime robustness：系統從逐期 ledger 報酬重算 Bootstrap Sharpe CI、DSR、Holm 校正，並以決策當下可得的價格、成交量與具 `available_at` 的財報事件，分別產出 bull、bear、高波動、低流動性與財報期的獨立 OOS 指標。任一類別樣本不足、財報事件晚於決策時間或分層績效未達閘門，都會留下 blocker，不能以整體平均報酬升級策略。

Exact replay 也必須逐個決策時間驗證 historical universe membership：上市、下市與代號對應會以有效區間還原，而 membership 資料必須有原始 `available_at` 與本地 `ingested_at`，且兩者均不晚於當時決策。今日的現存證券名單、或只有今日擷取時間的 archive，都會被標記為 unavailable；系統不會把它們回填成歷史可知 universe，也不會忽略已下市股票。

策略 proposal 的 promotion 另有獨立統計 gate：回放必須提供完整 period returns，並以固定 seed 的 bootstrap Sharpe 信賴區間、Deflated Sharpe 與 Holm-Bonferroni 多重比較校正建立 receipt。樣本少於 40、Sharpe 信賴區間未嚴格為正、校正後 p-value 大於 0.05，或 Deflated Sharpe probability 小於 0.95 時，proposal 必定拒絕，不能開始 shadow 或由人工 promotion。

學習分為 run memory、episodic memory 和 policy proposal memory。Reflection 的 `next_rules` 只會建立 inactive proposal；proposal 必須通過 exact replay、walk-forward、OOS、purged CV、成本／滑價／延遲、Risk review，再經過至少 20 樣本的 shadow paper 且無風控違規，最後只能由非 Agent／非 Codex 的人工核准 promotion。

詳細流程見 [Agent Trading Workspace](docs/architecture/agent-trading.md)。

## Agent Runtime API

```text
GET  /api/agents          驅動器、工具與不可變安全邊界
GET  /api/agents/tools    可執行工具 schema
GET  /api/agents/providers 啟用與停用的 provider 真實狀態
GET  /api/agents/environment 目前 UI、帳戶、Git、能力與風險環境快照
GET  /api/agents/settings Provider 設定與是否已配置；永不回傳密鑰
POST /api/agents/settings 切換 `codex`、`openai-compatible` 或 `external-agent`
GET  /api/agents/providers/{provider_id}/health 測試目前 Provider 連線
POST /api/agents/providers/{provider_id}/conformance 實際測試結構輸出、工具、繁中與長上下文
GET  /api/agents/market-radar/universe-options 列出真實自選、持倉與 Workflow Universe
POST /api/agents/market-radar/runs 建立正式多股票 Market Radar run
GET  /api/agents/market-radar/runs/{run_id} 僅在 receipt、驗證與正式結果全通過時回傳模型卡片
POST /api/agents/sessions 建立持久 Agent 對話
GET  /api/agents/sessions 列出對話
GET  /api/agents/sessions/{session_id}/messages 讀取持久訊息
POST /api/agents/sessions/{session_id}/messages 執行中輸入經 Steering Router 局部追加、修正或 fork，不一律重跑
GET  /api/agents/sessions/{session_id}/forest 讀取 Recursive Task Forest
GET  /api/agents/branches/{branch_id} 讀取 Branch、Local Plan 與 Step
POST /api/agents/branches/{branch_id}/pause|resume|cancel 局部控制 Branch
POST /api/agents/interactions/{interaction_id}/respond 回覆 clarification、decision 或 approval
POST /api/agents/sessions/{session_id}/archive 封存對話
POST /api/agents/sessions/{session_id}/runs 在同一對話建立 run
POST /api/agents/runs     建立不綁定 HTTP 連線的 durable run
GET  /api/agents/runs     列出最近 run
GET  /api/agents/runs/{run_id} 查詢持久化狀態與結果
GET  /api/agents/runs/{run_id}/snapshot 一次重建完整 Run 工作區
GET  /api/agents/runs/{run_id}/stream 依 sequence 重播／追蹤 SSE 事件
POST /api/agents/runs/{run_id}/pause 安全暫停後端任務
POST /api/agents/runs/{run_id}/cancel 明確取消後端任務
POST /api/agents/runs/{run_id}/resume 從安全 checkpoint 繼續
POST /api/agents/runs/{run_id}/retry 從安全 checkpoint 重試失敗步驟
POST /api/agents/runs/{run_id}/replan 向執行中的 PlanGraph 加入修訂要求
GET  /api/agents/runs/{run_id}/plan 目前計畫與歷次修訂
GET  /api/agents/runs/{run_id}/events 持久事件
GET  /api/agents/runs/{run_id}/artifacts 產出檔案
GET  /api/agents/artifacts/{artifact_id} 讀取 Artifact 與版本
POST /api/agents/artifacts/{artifact_id}/select|propose-change|restore 版本綁定的局部修改
GET  /api/agents/runs/{run_id}/checkpoints 安全恢復點
GET  /api/agents/approvals 待批准動作
POST /api/agents/approvals/{approval_id}/approve 批准精確工具＋參數摘要
POST /api/agents/approvals/{approval_id}/deny 拒絕動作
POST /api/agents/workflows 保存版本化 workflow
GET  /api/agents/workflows 列出 workflow
POST /api/agents/schedules 建立 advisory-only 一次／定期任務；錯過執行只支援可驗證的 run_once
GET  /api/agents/schedules 列出排程、下次執行時間與最後 run_id
POST /api/agents/automations/preview 產生語意 Automation 提案
POST /api/agents/automations 建立、驗證、試跑並啟用 Automation
PATCH /api/agents/automations/{automation_id} 建立新的 Automation Intent／Artifact 版本
POST /api/agents/automations/{automation_id}/pause|resume 控制 Automation lifecycle
GET  /api/agents/events/stream 跨 Run 的 Host Runtime Event SSE
DELETE /api/agents/schedules/{schedule_id} 停用排程
POST /api/agents/run      相容舊客戶端；內部仍建立 durable run
POST /api/agents/run/stream 相容舊串流；斷線不取消 run
POST /api/agents/ui/state 由 Web／WKWebView 發布介面狀態
GET  /api/agents/ui/commands UI 訂閱待執行 action
POST /api/agents/ui/commands/{command_id}/result UI 回傳真實操作結果
```

即時活動包含 Codex App Server 實際 turn/item/token-usage 生命週期、可驗證的計畫摘要、經遮罩的工具參數、技能／MCP／套件標記、工具結果、政策檢查與排程資訊。它不輸出模型私有的逐字 chain-of-thought；UI 中的「計畫摘要」是可稽核的決策摘要，不偽裝成私有推理。

同一個 Session 的 child Agent Run 會綁定父 Task Forest 的實際 Branch。其 `ready`、`running`、等待、完成、部分完成、失敗與取消都經同一個 SQLite Forest transition boundary 原子寫入 Branch、Local Plan、Join 與 transition provenance；延遲 callback 不能將終態 Branch 重新打開。因此 Dock 顯示的是 Host 持久化狀態，不依賴模型回覆文字或使用者在輸入框補充工程規則。

### Capability 真實狀態

`GET /api/agents` 的 `capability_registry` 同時驅動 UI 顯示、模型 manifest 和實際 routing。存在但尚未綁定或無法完整執行的能力會明確標示為 blocked／inventory-only，不會因為設定頁看得到名稱就宣稱已整合。外部框架使用 capability-level 狀態：例如 FinRL 的 environment 可就緒，但 policy training／loading／inference 仍可分別為 false。

MCP 不從 `config.toml` 名稱猜測可用性。每個 run 會向當前 Codex App Server 取得 server status 與真實工具 schema；唯讀工具可在 advisory 使用、一般外部寫入需 `external_execute`、標示 destructive 的工具只允許 `full_execute`。`node_repl` 與 Computer Use 不會直接映射成任意程式執行能力。因此工具數會依使用者的登入、已安裝 Connector、連線健康與實際 schema 改變，而不是 README 寫死的數字。

`browser.open/read_page/wait_for` 可由 Agent 真正開啟並讀取 JavaScript 網站；`browser.click/fill` 需要外部操作權限。每個 run 使用隔離 context，結束時只在本機 `.runtime/agent-browser/` 保存權限為 `0600` 的 browser storage state，工具輸出不包含 cookie、storage、密碼、隱藏欄位或檔案內容。`notifications.channels/previews/send` 直接使用既有 Telegram／LINE 後端，只有 `send` 需要外部操作權限，且任何 API 回應都不回傳憑證。使用者確認後啟用的 Automation 也會透過同一 transport 交給 canonical `NotificationManager`，由它統一處理去重、冷卻、fallback 與 durable receipt；外部 channel 未配置或拒絕時不會假報送達。

Agent 會先分類一般問答、即時資訊、專案任務、市場資料與股票決策。非股票決策的 `decision` 固定為 `null`，不附加預設股票或信心分數；只有明確詢問買賣、進出場或持有判斷時才使用股票決策格式。穩定的一般知識由 Codex 直接回答，不會為了湊證據而虛構或呼叫終端機；即時、冷門、專案、UI 與交易問題則必須有 Host 驗證過的實際工具證據。即時或冷門問題會優先使用專用官方工具，否則以 `web.research` 完成多搜尋來源發現與實際頁面讀取。單獨的搜尋結果不能當成完成證據，頁面失敗或沒有欄位時必須換查詢或來源。臺股期貨外資未平倉則直接使用 `market.taifex_foreign_open_interest` 讀取期交所 OpenAPI。

模型提供者：

- `codex`：以使用者已登入的 ChatGPT 帳號直接執行 App Server 結構化工具規劃，不需 API key、不會轉送成 Codex 聊天；原生 `/api/codex/run` 完整能力仍獨立保留。

- `openai-compatible`：直接呼叫使用者設定的 `/chat/completions`，可接相容雲端 API、Ollama gateway、vLLM、OpenRouter 或自訂服務；支援無金鑰本機服務。Ollama 可直接填入根位址（例如 Tailscale 裝置的 `http://host:11434`），設定頁會讀取 `/v1/models` 與 `/api/tags`、列出該服務已載入的模型供選擇，並將實際推論端點正規化為 `/v1`；未提供模型清單的相容服務仍可手動輸入模型 ID。

- `external-agent`：直接呼叫 `open_stock_ai.provider_turn.v1` endpoint，可接 Hermes、LangGraph、AutoGen、CrewAI 或自訂 Agent server。

「測試目前連線」只確認服務與所選模型可連線；完整 Agent Run 還會另外驗證 JSON 協定、繁體中文與工具呼叫能力。參數量很小或未針對工具使用調校的本機模型可能可以聊天，卻無法建立可執行計畫；此時應改用具備結構化輸出與工具呼叫能力的模型，或切回 Codex。相容層會接受常見的 `status/state` 與 `tool/name` 欄位差異，但不會替模型捏造工具、證據或完成狀態。

在 Agent 輸入區或「系統 → Agent 與模型」可展開 Codex 風格的模型與推理選單，所選項目會顯示勾號。模型清單由目前帳號的 App Server `model/list` 取得，推理選項依各模型的 `supportedReasoningEfforts` 更新。兩項皆選預設時沿用 Codex 設定；指定模型且推理選預設時，使用該模型建議的強度。輸入區選擇後即儲存，設定頁則按「儲存 Agent 設定」；新任務會等待儲存成功才送出。執行中與恢復的任務保留原設定，執行紀錄保存 App Server 實際使用的模型與推理強度。這些設定只屬於股市系統，不會改寫全域 Codex 設定。

專案使用 `openai-codex>=0.147.0`，避免舊 SDK 因不認得 `max`／`ultra` 等新增推理等級而在收到回覆前失敗。macOS 啟動器與後端優先採用 `STOCK_AI_CODEX_BIN` 明確指定的執行檔，其次是已安裝的 ChatGPT.app／Codex.app，再使用可攜式或 PATH 後備版本，讓應用更新後不會被舊 PATH 版本遮蔽。[Codex 官方模型清單契約](https://learn.chatgpt.com/docs/app-server#list-models-modellist)

Host 傳給模型的工具證據會保留有長度上限的內容、價量指標、行情時間與來源正文；被裁切的部分明確標示省略。資料仍保留來源與不可信內容標記，不能把工具回傳文字當成執行指令。這避免工具已有資料、模型卻只收到 schema／股票代碼而誤稱資料不存在。

設定頁會實際重建 Provider Registry，之後的新 Run、Workflow 與 Schedule 都使用所選驅動器；沒有完成端點／模型設定的 Provider 不能成為操作員。`local` 透過 OpenAI-compatible 介面接入，Anthropic／Gemini 可透過相容 gateway 或外部 Agent 接入。密鑰只以 write-only 欄位送至本機後端並保存於 macOS Keychain，API、事件、設定 JSON 與頁面都不會讀回明文。FinGPT 的非模型資料轉換契約保留，但本地 base model／LoRA executor 固定停用，不下載權重。詳細協定見 [Agent Trading Workspace](docs/architecture/agent-trading.md)。

第一次開啟時可選「不使用 Codex 繼續」，先使用不需要模型的市場與研究功能；之後可在「設定 → Agent、模型與 API」登入 Codex 或設定其他 Provider。略過 Codex 不會刪除或降低 Codex 原有能力。

## 外部研究專案

`external/` 包含 TradingAgents、FinGPT、FinRobot、FinRL、FinRL-Trading、Qlib 與 AI-Trader 等固定版本來源。原有 `external_sources` adapter 仍負責契約、schema、prompt 與研究投影；Agent Runtime 另外透過隔離 subprocess 實際載入並執行下列外部原始碼入口，每次結果都回傳檔案路徑、函式名稱與 SHA-256，不會在失敗時退回本機假結果：

| Agent 工具 | 實際外部入口 |
| --- | --- |
| `external.tradingagents.model_capabilities` | TradingAgents `get_capabilities` |
| `external.tradingagents.investment_decision` | TradingAgents `parse_rating` 五級投資決策 |
| `external.fingpt.sentiment_consensus` | FinGPT `summarize_market_sentiment` |
| `external.finrobot.report_quality` | FinRobot `TextUtils.check_text_length` |
| `external.finrobot.market_data` | FinRobot `YFinanceUtils.get_stock_data` 真實行情來源 |
| `external.finrl.walk_forward_windows` | FinRL `calc_train_trade_starts_ends_if_rolling` |
| `external.finrl.simulate_environment` | FinRL `StockTradingEnv.reset/step` 資產與交易成本模擬 |
| `external.finrl_trading.information_ratio` | FinRL-Trading `compute_information_ratio` |
| `external.finrl_trading.regime_signals` | FinRL-Trading `compute_slow_regime_signals` |
| `external.qlib.align_signals` | Qlib `SingleData.add` |
| `external.qlib.factor_dataset` | Qlib `SepDataFrame` 因子／標籤對齊與 IC |
| `external.ai_trader.variant_metrics` | AI-Trader `variant_summary` |
| `external.ai_trader.score_signal` | AI-Trader `score_signal_quality` 與 prediction extraction |

需要完整模型／Agent workflow 時使用下列高階工具；這些工具不會在失敗時退回公式或模板：

| Agent 工具 | 真實上游 workflow |
| --- | --- |
| `external.tradingagents.analyze_symbol` | `TradingAgentsGraph.propagate` 完整分析、辯論、風險討論與決策圖 |
| `external.fingpt.run_sentiment_model` | 保留 `FinoGridSentimentAnalyzer.load/score_batch`；目前由政策停用本地模型，不下載權重 |
| `external.finrl.train_policy` | FinRL `DRLAgent.get_model/train_model`，保存 Stable-Baselines3 policy artifact |
| `external.finrl.predict_actions` | 從 artifact 重新載入 policy 並在 vendored `StockTradingEnv` 產生 actions |
| `external.qlib.train_factor_model` | `qlib.cli.run.workflow` 的 Dataset／Model／Recorder 訓練 |
| `external.qlib.backtest_model` | 含 Signal／Portfolio Analysis record 的完整 Qlib workflow |
| `external.finrobot.generate_financial_report` | FinRobot `SingleAssistant` AutoGen 報告流程，code execution 固定關閉 |

FinRL／Qlib 的正式模型研究不會在一般 UI 分析時自動啟動。只有帶有
`research_model_runtime.enabled=true` 的 point-in-time dataset 研究請求，才會透過隔離 worker
執行模型；receipt 會綁定 PIT manifest/data SHA-256、FinRL policy SHA-256、Qlib workflow config SHA-256 與 Recorder artifacts。
重放必須載入相同資料 hash 的既有 artifact，禁止在 out-of-sample 階段重新擬合。
每次成功的 opt-in 執行也會寫入主 SQLite 的不可變 Model Registry：FinRL policy 與 Qlib artifact-set
各自取得內容定址 version，Experiment receipt 連結 PIT 資料、設定、模型版本與完整 worker receipt；每個實驗可依標的或框架查詢與比較。Champion/challenger 部署另有不可變的人類 promotion receipt，明確限於 shadow 或 production 邊界，且不會授予下單權限。
隔離 worker 會回傳 Python、OS／machine 與完整 package lock 的雜湊；紀錄因而可標為 `environment_captured`。
即使如此，在 framework 尚未證實 deterministic kernels 前仍會明確標示並非 bitwise reproducible，不能誤當成完全可重現。

核心工具可直接由目前的 Codex Agent 選用；建議先讀取 `market.research_pack`，再把已驗證的行情、技術資料或分析送入外部核心工具。TradingAgents 與 FinRobot 在獨立 Python process 內保留原生 LangGraph／AutoGen 工作流，但它們的模型請求會送到 Host 建立的 run-scoped、loopback-only Codex 橋接。Qlib 的 global patch、AI-Trader 的暫存資料庫與 FinRL 環境不會污染 API server；AI-Trader 評分使用一次性 SQLite，不會寫入主系統 Paper Account。

`external.workflow.analyze_verified_pack` 是高階組合工具：一次對已驗證 observations 執行 TradingAgents rating parser、FinGPT sentiment consensus 與 AI-Trader signal-quality scorer，每步都保留 source lock、module path、function 與 SHA-256。`external.runtime.health` 會分開回報 source present、environment ready、model loaded、training ready、inference executed 與 full workflow ready；組合 workflow 成功不等於未安裝的 foundation model 已推論。

高階 workflow 需要 `external_execute`，在獨立 Python process 內執行，模型、Recorder 與 policy artifact 只寫入 `.runtime/external-workflows/`。Host 會驗證上游 module 位於 source-lock 目錄並核對 module/artifact SHA-256。TradingAgents／FinRobot worker 只收到該 run 隨機產生的短期 loopback bearer token，不會收到 Codex／ChatGPT 帳號 token、GitHub token 或其他使用者憑證；證據也不保存該 bearer token。FinGPT 本地模型依政策停用；真實下單固定停用。

可依框架建立不進 Git 的隔離 runtime：

```bash
uv run python scripts/bootstrap_external_workflows.py finrl
uv run python scripts/bootstrap_external_workflows.py tradingagents qlib finrobot
```

FinGPT runtime 只在未來明確決定使用本地模型時才另外執行 `bootstrap_external_workflows.py fingpt`，目前正常啟動與 Agent workflow 不會執行它。

runtime 綁定保存於 `.runtime/external-workflows/runtime-bindings.json`（`0600`）。使用 `external.runtime.health` 取得每個框架的實際 interpreter、缺少依賴、設定、最近訓練／推論與 artifact 狀態。逐項完成度與 release smoke 見 [Agent Runtime 完成度與證據矩陣](docs/architecture/agent-runtime-completion.md)。

需要重新建立固定來源時：

```bash
python scripts/bootstrap_external.py --runtime
```

版本與授權稽核見 [外部來源 manifest](docs/integration/external_sources_manifest.md)。

## 測試與驗證

### 右側 Agent Dock 的可驗證執行狀態

右側 Dock 是 Agent 對話、動態計畫、Tasks Tree、Approval 與 Artifacts 的唯一完整工作區；首頁只保留快速入口與簡短狀態，不再複製回答或完整活動紀錄。每個 Run 以 `session_id + run_id` 隔離訊息、步驟、工具呼叫、Skill／Package／MCP 稽核資料與產物。

`max_steps_reached` 代表「已達本次步驟上限但尚未完成」，不會被投影成完成。介面會保留未完成與阻塞資訊，並提供「繼續執行」、「增加步驟上限」、「重新規劃」及「建立新 Run」。收合的固定計畫卡會顯示 `已完成項目 / 總項目` 與目前執行或等待的任務；Tasks 分頁可查看父子節點、依賴、平行分支、負責 Agent、工具呼叫、阻塞原因、完成條件與 Revision 差異。

長時間 Run 的 Chat feed 使用 keyed DOM 與視窗化載入，預設只渲染最近紀錄；「載入較早紀錄」會維持閱讀位置。只有使用者已接近底部時，新事件才會自動捲到底。Artifacts 僅顯示目前 Session／Run 的產物，並提供開啟、下載、來源 Step、Evidence 與 URI。

針對這些互動的真實 Chromium／Chrome 驗收可執行：

```bash
uv run pytest -q tests/e2e/test_agent_dock_browser.py
```

完整 Python 測試：

```bash
uv run pytest -q
```

研究與執行安全測試：

```bash
uv run pytest -q tests/test_research_safety.py tests/test_execution_boundary.py
```

前端 JavaScript 語法檢查：

```bash
find src/stock_ai/ui/static/js -name '*.js' -exec node --check {} \;
node --check src/stock_ai/ui/static/agent-trading-workspace.js
node --check src/stock_ai/ui/static/paper-training.js
```

Python 匯入與編譯檢查：

```bash
uv run python -m compileall -q src/stock_ai src/open_stock_ai
```

## 版本控制與生成物規則

所有人類開發者與 AI Agent 在建立分支、提交或推送前，必須先閱讀 [專案協作、分支與提交指南](CONTRIBUTING.md)。該文件包含 2026-07-18 Git 歷史重寫後的舊 Clone 遷移方式、分支與 PR 流程、禁止提交項目及必要驗證。

以下目錄只保存本機執行狀態，不應提交 Git：

- `.runtime/`：下載的工具、Python、虛擬環境與編譯後 App
- `logs/`：PID、port、server log 與 instance metadata
- `output/`：SQLite、回測 JSON、研究報告、匯出與 UI 截圖
- `artifacts/`：一次性瀏覽器除錯複本

需要保存可重現的研究範例時，應先匿名化並移到 `tests/fixtures/`；需要保存瀏覽器驗收程式時，應放在 `tests/browser/`，不要放進 `output/`。

安全列出或清除可重建生成物：

```bash
python scripts/clean_generated.py          # 只列出
python scripts/clean_generated.py --apply  # 實際清除
```

這個工具不會刪除 SQLite 帳戶資料或 `.runtime/`。

## 文件索引

完整文件分類請從 [docs/README.md](docs/README.md) 開始。
