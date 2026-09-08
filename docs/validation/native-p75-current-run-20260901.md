# P75 原生 Current Run／研究子任務驗收（2026-09-01）

## 範圍

使用本專案根目錄的 `開啟股市AI系統.command` 啟動 macOS 原生 `Stock AI Liquid Glass`；啟動器回報 instance `fae9545ae1cab455`、source `3242db039491fb6e`。透過 Computer Use 直接操作右側 Agent Dock，Provider 為遠端 OpenAI-compatible `gpt-oss:20b`。

## 修復內容

`agent.run_subtasks` 先前把模型給出的簡短 child focus（例如 `market.analyze_symbol 2887.TW`）當成獨立使用者目標重新分類。它會因此從父 Run 的 `market_information`／research-only 邊界錯誤升格為 `market_decision`，強迫 child 產生買賣決策並在 `host_grounded_market_decision` completion gap 重試。

現在 child Run 由 Host 帶入已驗證的父 task kind；research-only 父任務還會帶入不可繞過的「不產生買賣決策、不建訂單、不要求操作指示」邊界。這是執行上下文傳遞，非要求使用者在 prompt 中教系統怎麼回答。

## 實際流程與結果

- `AR-a7873b9215004c358974091aa5350d5e`：在原生 UI 輸入「分析 2887.TW，完成一筆本機紙上模擬買進 1 股；不得對外送單、建立自動化或操作真實帳戶」。Dock 可見 `market.analyze_symbol`、`paper.preview_order`、`paper.submit_order` 均完成；`paper_execution_count=1`、`live_execution_count=0`，最終摘要明確標示未送往實盤券商。
- `AR-b76aa376ff9d409f94ce66ccfcb4c1b6`：在同一原生 Dock 輸入「比較 2330.TW 與 2887.TW 的資料品質限制與主要風險；只做研究，不提供買賣決策、不建立訂單或自動化」。Dock 顯示執行中與可見 Plan，接著完成兩個 `market.analyze_symbol` Branch，沒有 `waiting_user_input`、`waiting_decision`、`continuation_requested` 或 `host_grounded_market_decision` completion gap。

## 自動化測試

```text
uv run pytest -q tests/test_agent_runtime_v2.py -k 'runtime_subtasks_link_parallel_child_runs_to_the_parent_forest or runtime_critic_enforces_enough_steps_for_evidence_and_synthesis'
2 passed

uv run pytest -q tests/test_agent_runtime.py -k 'host_fallback_market_decision_reflection_is_a_nonblocking_receipt or repeated_invalid_final_synthesis_escalates_with_a_failure_fingerprint or routine_market_wait_is_completed_without_teaching_the_agent'
3 passed
```

## OpenAI-compatible 工具信封回歸驗收

2026-09-01 的原生 UI 回歸首次重現一條不同根因：遠端 `gpt-oss:20b` 回傳
`assistant({tool: "market.research_pack", arguments: "…"})`，Host 把外層
`assistant` 寫進 durable Plan。該名稱不是已揭露能力，Plan compile 因而一直
失敗，最後以 `execution_guard_blocked` 終止；這不是使用者輸入不足。

修復後，再以同一個原生 App、新的自然語言輸入：

> 請分析台新新光金 2887.TW 的近期價格、技術面與資料限制，只做分析，不建立紙上或真實交易。

- launcher source：`83d7481698b0cb45`；Provider：遠端 OpenAI-compatible `gpt-oss:20b`。
- Run `AR-9d3cd75e7d0745449704f57ca14151de` 只建立一個 `market.analyze_symbol` Branch，先前的 `assistant` 假工具與 `agent.run_subtasks` 遞迴分支均未出現。
- Dock 依 Host 驗證收據終態顯示 `completed`：即時 quote 已過期，因此摘要直接列出 TWSE MIS `last_trade`、資料時間與 `quote_expired` 等限制，而不是猜測技術結論或等待使用者教它如何繼續。
- 結果明確為未建立 Automation、未建立紙上訂單、未向實盤券商送單。

本次相關測試：`tests/test_agent_runtime.py`、`tests/test_agent_runtime_v2.py`、
`tests/test_agent_workspace_api.py` 共 190 passed；完整套件為 1809 passed、5 skipped。
