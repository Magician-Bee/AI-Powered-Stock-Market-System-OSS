# P100 禁止實作方式：負向驗收清單

本文件把 P100 的 19 項禁止項目連到可執行的負向測試。它是本次 release 的追溯清單，不代表任何一項可以不經後續變更再次驗證。

| 禁止項目 | 防線與驗收測試 |
| --- | --- |
| 所有問題分類寫死 | 多意圖 Router 保留負向限制：`tests/test_neutrality_contracts.py::test_multi_intent_router_does_not_activate_market_for_a_no_lookup_constraint`。 |
| 每股票一個專門 Prompt | 空問題／一般問題不得選股：`tests/test_neutrality_contracts.py::test_empty_search_and_general_question_never_choose_a_stock`。 |
| 每提醒條件一個固定 Workflow | 語意化 Intent 且拒絕執行技術：`tests/test_agent_automation_final.py::test_intent_is_semantic_and_rejects_execution_technology`。 |
| 前端按鈕寫死 | UI 由 durable runtime event reducer 投影：`tests/test_agent_dock_state.py::test_runtime_reducer_projects_forest_interaction_artifact_automation_and_evidence`。 |
| 分支只是 UI 分類 | 真實 durable branch／join event：`tests/test_agent_forest_projection.py::test_durable_runtime_emits_real_branch_and_join_events`。 |
| Branch 沒有自己的 Plan | 每 branch local plan 有序、branch 平行：`tests/test_agent_forest_executor.py::test_branches_run_truly_in_parallel_while_each_local_plan_remains_ordered`。 |
| 使用者輸入直接視為事實 | Proposal arbitration 對高影響輸入不得盲套用：`tests/test_agent_runtime_final_interaction.py::test_proposal_arbitration_never_blindly_applies_high_impact_input`。 |
| 反問只因缺資料 | Reflection、decision、waiting state 分開：`tests/test_agent_runtime_final_interaction.py::test_reflection_precedes_decision_and_waiting_states_are_distinct`。 |
| Current Run 輸入全部 replan | composer 以 Session Message 精確作用範圍：`tests/test_agent_dock_state.py::test_current_run_composer_posts_a_session_message_with_precise_selection`。 |
| Tool 一次失敗即 Run failed | Local failure 不取消 sibling，join 可回傳 partial：`tests/test_agent_forest_executor.py::test_local_failure_does_not_cancel_siblings_and_join_returns_partial_result`。 |
| 同樣錯誤重複 retry | failure fingerprint / repair campaign：`tests/test_agent_provider_matrix_chaos.py::test_fault_occurrence_is_exact_and_does_not_repeat_accidentally`。 |
| n8n 再接 Agent 重解需求 | Headless n8n pipeline 以已確認 intent + credential reference 執行：`tests/test_agent_automation_final.py::test_headless_n8n_pipeline_requires_permission_and_credential_reference`。 |
| 模型任意生成危險 n8n Workflow | Automation policy 把監控、外部訊息、交易核准分離：`tests/test_agent_automation_final.py::test_policy_separates_monitoring_external_message_and_trade_approval`。 |
| Research 只在內部 API 壞掉才用 | Research task 由 capability / evidence host validation 驗收：`tests/test_agent_final_system_scenarios.py::test_scenarios_a_b_g_and_l_use_one_session_and_real_recursive_branches`。 |
| 模型聲稱工具成功就視為成功 | completion gate 需要 required evidence：`tests/test_agent_forest_executor.py::test_completion_gate_rejects_model_completion_without_required_evidence`。 |
| 圖表僅裝飾、不能互動 | Artifact 選取採 version pin、可版本修改：`tests/test_agent_final_system_scenarios.py::test_scenario_h_artifact_selection_is_version_bound_and_conflict_safe`。 |
| 所有對話永久寫入 Memory | market price 僅進 working memory：`tests/test_agent_memory_durable.py::test_completed_market_run_keeps_prices_in_working_memory_only`。 |
| 每個問題都建立 Automation | 明確拒絕時絕不 proposal／建立：`tests/test_agent_automation_final.py::test_opportunity_detector_honors_explicit_no_automation_constraint`；並已經原生 UI 驗收。 |
| 每個問題啟動所有工具 | 非股市指令不分類為市場資訊：`tests/test_agent_runtime_v2.py::test_explicit_non_stock_instruction_is_not_classified_as_market_information`。 |

## 本次本機驗收

使用桌面專案的 `開啟股市AI系統.command` 啟動原生 App，以遠端 `gpt-oss:20b` 實際輸入「討論 n8n 自主監控架構；不要建立自動化、不要查市場、不要交易」。結果是一般回覆、`market: false`、沒有 Automation decision card，也沒有新增 automation durable record。此驗收特別覆蓋上表第 18 項。

## P83–P105 長期有效性閘門

單次成功率或全生命週期累計值不能作為 release 結論。Observability 必須同時保存原始事件並可重算 `24h`、`7d`、`30d`、`all` 四個窗口；有限窗口以事件 UTC 時間做閉區間篩選，`all` 不帶易變的查詢時間，因此重啟前後快照必須完全相同。

| 驗收風險 | 必要負向證據 |
| --- | --- |
| 同一 Run 的 `result.final` 與 `run.completed` 被算成兩個任務 | Task 分母以 terminal `run_id` 去重；測試同時寫入兩種成功事件仍只算一個樣本。 |
| 沒有樣本卻顯示 0%，被誤讀為完美 | 每個比例帶 `metric_samples`／`metric_status`；分母為零時 UI 顯示 `—`。 |
| 舊成功流量永久掩蓋最近回歸 | 儀表板可切換 24 小時、7 天、30 天與全期間，且顯示 task／execution／failure event 樣本數。 |
| Error Rate 只看工具、忽略模型失敗 | 分母包含 tool 與 model execution attempts，另保留 `tool_error_rate` 供工具層診斷。 |
| 不同失敗全部混成 `unknown` | P104 十四類 fault 使用固定 taxonomy；Dashboard 顯示分類計數，未知類別仍保留而不靜默丟棄。 |
| Branch recovery 以所有完成 Branch 當分母 | 以 branch-scoped failure ledger cohort 為分母；沒有 ledger 時只接受明確 recovery success／failure 樣本。 |
| 舊資料今天被讀取就被誤判為新鮮 | Evidence freshness 優先以 `published_at` 計算，缺少發布時間時才退回 `observed_at`，並保存採用的時間基準。 |
| Reflection 一次拋出過多問題 | 記錄 questions/checkpoint 與 question-budget breach rate；每個 checkpoint 超過 3 題即形成 breach 樣本。 |
| deterministic provider fixture 被寫成真實 provider 通過 | P103 contract matrix 與 live provider 驗收分開記錄；沒有端點與實際執行證據時維持「需真實 provider」。 |
| Chaos campaign 只證明 fault 名稱存在 | 每種 P104 fault 必須有注入 trace、恢復策略與 invariant；相同 seed 重跑結果必須一致，重複事件不得重複增加 KPI。 |

對應測試集中在 `tests/test_agent_provider_matrix_chaos.py`：涵蓋 terminal Run 去重、四個長期窗口、P104 失敗分類、事件冪等、P83 問題預算、無樣本狀態及前端窗口／樣本／分類投影。這些 deterministic 測試是 release gate 的一部分，但不取代規格矩陣列出的 macOS 原生 UI、真實 provider、Browser／Provider process crash 與外部服務驗收。
