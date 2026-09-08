# Final Agent Runtime（P0–P105）

本文件記錄 P0–P105 最終規格在正式程式中的責任邊界。系統最高層實體是 Session；Run 只是 Session 內一次可恢復的執行，模型文字不是進度、工具成功或完成狀態的真相。

## 核心執行模型

- P0–P9：Objective 使用不可變版本；每個 Run 建立持久 Recursive Task Forest。Branch 是一級實體，具有自己的 Local Plan、Step、依賴、Budget、Checkpoint、Result 與生命週期；Branch 內有序、Branch 間可平行，Join 會檢查衝突。`RuntimeForestAuthority`／`ForestExecutor` 現在會接收 Durable Runtime 先建立的 SQLite Forest、root Branch 與 Objective version IDs，checkpoint recovery 也拒絕用不同 in-memory Forest 覆蓋該 identity。child Run 與 UI branch control 的 lifecycle transition 已統一經 `DurableForestStore.transition_branch` 原子寫入 Branch、Local Plan、Join 與 transition provenance；延遲 callback 不得將 terminal child 重新開啟。能力 executor 的 in-memory scheduling snapshot 仍是 checkpoint transport，而非第二個 UI state source。
- P10–P18：Reflection、Clarification、Decision、Approval 與 Proposal Arbitration 是不同互動；Decision Card 必須包含 Agent 建議、替代方案與自由輸入。執行中的訊息先經 Steering Router 分成 soft steer、hard steer、fork、branch control、artifact change 或一般問題。
- P19–P22：只有 Host Runtime Event 是狀態真相。Capability 由多個 intent 組合，並按 research、decision、automation、confirmation 階段揭露。
- P23–P28：External Research 採 evidence-first loop，保留來源、時間、freshness、support／conflict edge 與 provenance。Artifact 點擊綁定精確版本與節點，修改採 optimistic conflict check。
- P29–P34：Automation 由語意 Intent 動態編譯，經 policy、validate、dry-run 後才能 activate。Internal Scheduler 處理本機條件與時間；n8n 只作 headless cross-system execution，不能成為第二個 Agent。
- P35–P42：Error Receipt、Failure Fingerprint、禁止 identical retry 與 L0–L9 recovery ladder 只修復受影響 Branch；失敗可形成 `partially_completed`，不必終止整棵 Forest。
- P43–P54：Memory 分 working、session、user preference、project state、episodic、procedural 六層。候選需評分、衝突、合併或 supersede；一次錯誤不能直接成為永久 procedural rule。金融風控、交易 Approval、Clarification 與 Automation 權限互相分離。
- P55–P61：Agent Dock 提供 Chat、Task Forest、Runtime Timeline、Decision Card、Evidence Graph、Artifact Canvas／Diff 與 Automation Semantic View。固定 Host 狀態由前端格式化，不消耗模型 token；模型工作摘要保持自由生成。
- P62–P70：Token Budget、Context Broker 2.0、Branch Result compression、durable lifecycle、Automation／Notification dedup、provenance 與 KPI collector 都是 Host 能力。
- P71–P87：SQLite schema v39 保存上述領域；HTTP API 直接讀寫 Session、Forest、Branch、Interaction、Artifact、Automation 與 Event，不從聊天文字反推。Host Completion Gate 驗證必要 Branch、Evidence、Blocking Question、Approval、Critical Tool Failure 與 Final Result。
- P88–P105：普通問答、複雜持股決策、使用者協作、不安全提案、JSON／網站失敗、mid-run steering、Artifact 修改、Automation trigger、Memory 與 Session History 都有情境測試；另有五類 Provider model matrix、14 種 chaos fault 與成功 KPI 契約。

## API

正式入口位於 `/api/agents`：

```text
POST /sessions
GET  /sessions/{session_id}
POST /sessions/{session_id}/messages
GET  /sessions/{session_id}/forest
GET  /branches/{branch_id}
POST /branches/{branch_id}/pause|resume|cancel
POST /interactions/{interaction_id}/respond
GET  /artifacts/{artifact_id}
POST /artifacts/{artifact_id}/select|propose-change|restore
POST /automations/preview
POST /automations
PATCH /automations/{automation_id}
POST /automations/{automation_id}/pause|resume
GET  /events/stream
```

Current Run 輸入固定送到 Session Message endpoint，不再一律呼叫 replan。所有改變狀態的 API 仍受既有本機 session token、Origin、Policy、Approval 與金融風控邊界保護。

## 驗收證據

- Domain／persistence：`tests/test_agent_runtime_final_interaction.py`、`tests/test_agent_memory_final.py`、`tests/test_agent_automation_final.py`
- Repair／research／chaos／KPI：`tests/test_agent_runtime_final_repair_research.py`
- P88–P105 scenarios／model matrix：`tests/test_agent_final_system_scenarios.py`
- API／durable restart：`tests/test_agent_runtime_api.py`、`tests/test_durable_agent_runtime.py`
- UI／browser：`tests/test_agent_dock_state.py`、`tests/e2e/test_agent_dock_browser.py`

真實交易仍停用；Automation 不能繞過交易 Approval，也不能把使用者偏好當成硬規則。

## 回答後提案與認知檢查契約

- `reflection` 先以 Host 本 Run 的 tool call／node／validation receipt 核對 Evidence ID，再物化為正式 `ReflectionCheckpoint`；偽造或不屬於本 Run 的 ID 不得出現在 Decision Card。
- `interaction_proposals` 最多三筆，且不改變已完成 parent Run。`ask`／`follow_up`／`create_artifact` 使用 `arguments.objective`；`draft_automation` 使用 JSON 編碼的 `arguments.intent_json`，避免 strict Provider schema 接受任意執行欄位。
- Host 將每個有效提案寫成真正的 durable Interaction。接受一般提案才建立同 Session child Run；略過沒有副作用。Automation 必須另外經使用者確認、Policy、compile、validate 與 dry-run。
- Proposal Evaluation 使用專用 `open_stock_ai.proposal_evaluation.v1` strict output schema，不能再套用禁止 structured result 的一般 market-information schema。
