# 自主交易目標 M0 執行基準審計

本報告對應 2026-09-12 目標計畫的 M0／任務 01、02，並提供 M1／任務 03、04 的斷點證據。採集時間約為 2026-09-12 17:52–18:08（Asia/Taipei）。本次僅讀取版本、設定白名單、資料庫及既有執行收據；沒有啟動模型、重啟服務、重設帳戶、送單或改動啟用狀態。

**結論：目前服務確實執行 `270e59a6aec8`；已存在真模型研究收據，但自主帳戶尚無任何持久交易計畫、委託、成交、持倉或平倉成果，因此 M1 尚未完成。** 最新真模型紀錄早於目前版本，不能用其失敗或舊成功直接代表目前版本。當前版本已修正其中一個主要 Host 證據辨識斷點，離線重播通過，仍需真模型閉環驗收。

## 1. 服務及程式身分

| 項目 | 實際證據 |
| --- | --- |
| Git branch | `main` |
| Git HEAD | `270e59a6aec85995eb44a484627066e9bd2cefbc` |
| 提交時間 | 2026-09-12 17:14:12 +08:00 |
| 提交摘要 | `fix: preserve runtime identity after crash restart` |
| PID／port | `45757`／`8000` |
| instance_id | `fae9545ae1cab455` |
| 專案根 | `/Users/your-user/Desktop/AI股市系統-Agent測試版` |
| 實際 service cwd | `/Users/your-user/Library/Application Support/StockAI-System/fae9545ae1cab455/runtime/service-source` |
| `/health` | `status=ok`、`build_commit=270e59a6aec8`、同一 `instance_id`；`project_root` 為上述 service cwd |
| 既有驗證腳本 | `bash verify-stock-ai-instance.sh`：`VERIFIED CURRENT PROJECT INSTANCE` |
| 獨立程式驗證 | service-source 中 527 個受 Git 追蹤的 `.py/.yaml/.yml` 檔案與 HEAD Git blob 全數相符，0 缺檔、0 差異 |

上述獨立比對的 UTC 時間為 `2026-09-12T09:56:42.719216+00:00`，即台北 17:56:42。本報告不把後續工作區修改宣稱為已部署。工作開始前已有圖片刪除、`uv.lock` 修改及未追蹤項目，沒有清除它們。

現有 UI 檔案服務端及本地 SHA-256 同為 `6dc798bec9c71345a31385defd69df5eedeb804adfea2b79fc12d3e3bfe735e7`。啟動記錄位於專案 `logs/stock-ai-server.{pid,port,commit,root,instance}`。

## 2. 真正資料路徑與帳戶

| 用途 | 絕對路徑 | 採集時大小／schema |
| --- | --- | --- |
| 正在服務的交易／自主／前瞻帳本 | `/Users/your-user/Library/Application Support/StockAI-System/fae9545ae1cab455/runtime/service-source/output/open_stock_ai.sqlite` | 122,413,056 bytes；`user_version=47` |
| 正在服務的 Agent run／event／checkpoint | `/Users/your-user/Library/Application Support/StockAI-System/fae9545ae1cab455/runtime/agent-data/agent-runtime.db` | 4,809,265,152 bytes；`user_version=47` |
| 市場資料 | `/Users/your-user/Library/Application Support/StockAI-System/fae9545ae1cab455/runtime/market-data.db` | 由啟動器 `STOCK_AI_MARKET_DATA_DB` 明確指定；本輪未展開行情表 |
| 工作區另一份交易帳本 | `/Users/your-user/Desktop/AI股市系統-Agent測試版/output/open_stock_ai.sqlite` | 326,959,104 bytes；`user_version=47`；沒有 `autonomous_*` 表 |

`open-stock-ai.sh` 把服務 cwd 設成 managed service-source。其 `.env` 白名單中的 `OPEN_STOCK_AI_SQLITE_PATH=output/open_stock_ai.sqlite` 是相對路徑，所以解析到服務根的 output；該 output 與工作區 output 不是符號連結。不能在工作區直接採用預設相對路徑後宣稱已審查正式服務帳本。`AgentRuntimePaths.discover()` 則使用啟動器指定的 `STOCK_AI_AGENT_DATA_ROOT`。

自主流程固定使用合成紙上帳戶識別 `autonomous-paper-v1`，與舊 UI／手動實驗帳戶不同。本報告只保存必要的授權及計數，不複製完整帳戶快照。

| 帳戶狀態 | 既有服務帳本結果 |
| --- | --- |
| 模式 | 紙上；`PaperBrokerPort(PaperBrokerSimulator(PaperOMS))` |
| Campaign enabled | `true` |
| Model review enabled | `true` |
| 初始紙上額度 | TWD 1,000,000；目前未產生交易使用 |
| 計畫／Broker 委託／OMS 委託／成交／持倉／outcome | 全部為 0 |
| Research cycles | 5 |
| Retained evidence | 147 |
| Model reviews | 3：completed、partially_completed、cancelled 各 1 |
| 逐檔深入覆蓋紀錄 | 40；不能等同全市場深入研究 |

服務端 YAML 及 `.env` 白名單一致指定 `mode=paper`、`LIVE_TRADING_ENABLED=false`、外部 runtime／帳戶匯入／外部憑證能力皆 false。`governed_capability_promotion_receipts` 為 0。自主服務建構器直接固定 Paper broker，不存在本輪由自主流程切換實盤的證據。本輪沒有读取券商或模型憑證。

## 3. 模型選擇與設定指紋

真正服務使用 `/Users/your-user/Library/Application Support/StockAI-System/fae9545ae1cab455/runtime/service-source/output/agent_runtime_settings.json`；其中 `default_driver=codex`、`codex_model=gpt-5.6-sol`、`codex_reasoning_effort=medium`。自主複查保存的選擇及三份 SDK receipt 都吻合，`selection_source=sdk_resolved_host_receipt`／`resolution_source=sdk_thread_start`。每日上限 1 個接受或結果不明的 run、`max_steps=12`。

工作區 output 中另一份設定仍是 `default_driver=openai-compatible`、`openai_model=gpt-oss:20b`；它不是正在服務的自主模型設定。`config/open_stock_ai.yaml` 的 `llm.provider=openai-compatible` 只是該 settings class 的描述欄位，不能用來否定實際 Codex driver 收據。

| 服務設定 | SHA-256 |
| --- | --- |
| `config/open_stock_ai.yaml` | `609211de19b36660d69c42104eaa406060d4a615bf29cfebfc2d73a7cdc7c85c` |
| `config/agent_runtime.yaml` | `8a35431bf731e9da6f394345d3573e365aae9621f0b2a03939deb9c8f5a2ee11` |
| `output/agent_runtime_settings.json` | `a2b1a578cbfcd38aaddb0c5b58c8880df55db947b340b54b06b3a37fe09e9c97` |

## 4. 真模型收據、原始錯誤分類及預算

| run_id | 時間及終態 | 能證明的內容與限制 |
| --- | --- | --- |
| `AR-0ecd3fd778564747a24adcb0b37ba7c4` | 2026-09-11；cancelled | `CancelledError`，既有使用者取消；有 SDK 模型身分，不能算閉環成功 |
| `AR-77d1c802531e4ea4946ed3ee93d152e1` | 2026-09-11；completed | 研究兩檔、決定保留現金、啟用 campaign；0 紙上執行、0 實盤執行。模型把其他研究框架 blocked／正期望值未通過列為不提案原因；這不是持倉生命週期證明 |
| `AR-8d9ff823ef2847fe9fb6ae53551c3cf7` | 2026-09-12 15:51:43–15:53:49；partially_completed | 4 次 SDK 模型呼叫成功，10 個工具成功。完成缺項為 `retained_research_and_campaign_activation`，最後以 `cost_budget_exhausted` 結束；不是已證明「市場沒有機會」 |

最新 run 的工具及 token 概況如下。每工具耗時為持久 `started_at → finished_at`，含該 run 並行排程等待，不是單純資料源網路延遲。Token 值是 Host 的 `ceil(JSON characters / 4)` 估算計費單位，**不是 SDK 實際帳單 token、美元或額度**；SDK tokenUsage 事件的 usage 欄位為空。

| 模型 turn | 模型耗時 | Host 估算 token units | 工具／結果 |
| --- | ---: | ---: | --- |
| 1 | 25.071 秒 | 21,002 | `autonomy.research` 0.891 秒；`autonomy.status` 1.060 秒，均成功 |
| 2 | 34.552 秒 | 23,124 | `autonomy.evidence` 共 8 次，均成功：bulk 0.683 秒；7 份保留歷史依序 0.787、0.845、0.909、0.983、1.054、1.129、1.209 秒 |
| 3 | 30.025 秒 | 24,433（由保存累計差額推得） | 無新工具；Host legacy evidence feedback 阻擋完成 |
| 4 | 24.035 秒 | 24,985 | 無新工具；重複相同 Host evidence feedback |
| 5 | 未啟動模型 | 預計 prompt 24,669，未准入 | provider 已使用 93,544／96,000，剩餘 2,456，不足下一輪 |

最新 run 的 `tool_trace` 順序為：research-cycle、status-before、bulk-evidence、hist-2330、hist-2454、hist-2308、hist-3661、hist-2327、hist-2408、hist-2492。這些是事件內既有 call ID，只用於追蹤；報告不保存參數、行情原文或模型草稿。第 1、2 輪的 token 來自 safe checkpoints；第 4 輪及總數來自 run result。global cost units 93,554 包含另外 10 個工具單位；provider 只計 93,544 模型單位。

兩個有界 Host 斷點：

1. **歷史 evidence 誤判。** 第 3、4 輪 `policy.feedback` 說尚缺主系統行情證據，要求 `market.research_pack` 或 `market.analyze_symbol`，忽略了 10 筆已成功驗證的 autonomy 工具結果。HEAD 已在 `561c52226`（16:12:40）修正 `completion_policy.py:609–611` 接受 autonomy.research／evidence／status；本次從實際 service-source 抽取純 predicate，對保存 trace 做無模型、無寫入重播：`met=true`、10 筆 substantive。**這個舊失敗已具有 Host 修正，尚無修正後真模型 run；不應再次擴大預算來掩蓋它。**
2. **可選 plan patch 的節點識別不符。** 第 2 輪事件 78 為 `invalid_arguments`／`Unknown plan node: load_retained_evidence`，隨後事件 79 `invalid_optional_provider_plan_patch`；Host 忽略該可選 patch，後續 8 個 evidence 呼叫仍全部成功。它是 Agent 工作圖修訂問題，不是 TradingPlan 建立或委託失敗。可離線驗證節點 ID 對接，不能据此宣稱交易路徑已通。

## 5. 資料覆盖與前瞻政策

最新 cycle `AC-cac3764c9aa50db1e51f2466bf4854da917f01fa8040a459007e391716b5bb78` 建立於 16:36:50：證券母體 4,606、普通股 2,392、可用批次資料 1,953；本輪深入選取 20、成功 5。其餘 15 筆為 `history_identity_or_provenance_not_verified` 或 `insufficient_completed_history`。前一 cycle（15:51:04）20／20 成功。兩者皆為 Host research，`model_calls=0`；必須另以 model review run 連結，不能把這個欄位解讀成全系統未呼叫真模型。

已登記前瞻 protocol：`FP-040bdc7a8be619fb56c7743bb291391834798ebab67559884f2cfb03510522b9`，policy `d780f71077b562f64edd90e0f8b1532753f73fd76e988c1bc49d9a9d2aaec25e`。觀察期 2026-09-12 15:53 至 2027-03-05 14:35；120 次預定觀察、至少 120 觀察日及 30 筆平倉；`fixture=false`。目前只有 1 decision、5 deviation，沒有 trade／NAV 成果證据。

5 個 deviation 全為 `host_execution_policy_changed_during_forward_window`。已登記政策的 505 個 Python source hash 與目前 service-source 比對：495 相符、10 不同；`host_public_configuration` 是配置摘要 hash，不是缺少的檔案。不同檔案包括 completion policy、recovery policy、validators、autonomy tools、AutonomousCampaign、官方歷史載入、forward monitor、model review 及 autonomous service。這些偏離已記錄，不能把之前 policy 的結果直接累加成當前政策績效。

## 6. 證據鏈目前能連到哪裡

`autonomous_model_reviews.run_id` 可聯接 Agent DB 的 `agent_runs`、`agent_events`、`agent_checkpoints`；`cycle_id` 可聯接交易 DB 的 `autonomous_research_cycles`；模型 receipt、`environment_hash` 及前瞻 `policy_version/source_hashes` 皆已有保存。最新 run 的 environment hash 為 `16bd1b8ed2e1c6a1d2c8e0da8c2f5fab3444bfd69d0882a4cd099ba2a56ff8f8`。

但該 market run 的 environment snapshot 中 `project.exposed=false`；模型 receipt 沒有直接持久 `build_commit`／DB root／instance identity。部分前瞻政策具有 source hashes，尚不能因此說每個 run、每份工程測試都完整綁定提交與實際帳本。`scripts/verify_autonomous_closed_loop.py` 明確使用固定行情、不呼叫模型，只可列為工程可達性證據。

## 7. 最小可實作基準工具與下一步

沿用既有驗證脚本，增加一個唯讀收據匯出器即可，不建立另一套交易或排程平台：

1. 採集時間、HEAD、dirty tracked path 列表、`/health`、PID／instance、runtime source manifest hash、允許欄位的設定 hash。
2. 必填並保存交易 DB 及 Agent DB 的 resolved 絕對路徑、schema、必要帳戶 alias、cycle／run／policy ID；偵測工作區 DB 與服務 DB 不同時明確呈現。
3. 用同一規則匯出研究、模型、計畫、進場單、成交、出場單、剩餘部位、對帳／outcome 的存在性及 hash；缺哪一環就輸出 missing，不能從 completed run 推導成交。
4. 匯出 fixture／真市場／真模型標記；校驗 runtime source 與證據版本，分類 historical error、current code correction、not yet revalidated。
5. 在既有日額度、模型及風險參數內完成修正後的真模型觀察；若沒有交易決策，記錄可信不交易理由，同時另做隔離的委託／成交／退出工程验收。不能放寬風險、造單或把工程行情混進前瞻績效。

## 8. 唯讀採集限制

先審查 API authentication 及狀態端點的實作。本輪 HTTP 僅讀 `/health` 及既有驗證脚本使用的 static asset。**沒有呼叫 `GET /agent/autonomy/status`**：雖名為 status，它會建構 model review 並進行前瞻／run reconciliation，可能寫入證據，不符合本輪嚴格唯讀要求。

本機 Python SQLite 使用普通 `mode=ro` 查 WAL-mode 資料庫時遇到 `unable to open database file`。採集只在來源 `-wal` 不存在時，以 `mode=ro&immutable=1` 加 `PRAGMA query_only=ON` 讀取，且每批前後確認檔案大小、mtime_ns 不變及 WAL 仍不存在；每批採納資料均通過穩定性檢查。這是本次 checkpointed file 的有限讀取證據，**不是活躍 WAL 資料庫的一般 snapshot 方法**。正式工具應使用可保持一致性的唯讀備份／snapshot 流程，遇到活躍寫入時明確等待或報 unavailable，而非忽略 WAL。
