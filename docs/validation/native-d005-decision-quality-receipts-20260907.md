# D-005 決策資料品質收據驗收

## 範圍

- 啟動入口：`./開啟股市AI系統.command`
- 桌面程式：Stock AI Liquid Glass
- 路徑：首頁 → 股票決策 → 證據
- 模型與 Agent Run：未呼叫、未操作

## 驗收契約

每個由 Market Intelligence 新建的股票決策都必須保存 `stock_ai.decision_data_quality_receipt.v1`。收據包含 freshness、completeness、anomaly、source disagreement、confidence 與 certification，payload 由 SHA-256 綁定，SQLite trigger 禁止更新或刪除。

宣告 v1 receipt schema 的 snapshot 若缺少任何收據、數量不符，或 receipt 綁到不同 snapshot／symbol，整筆保存會失敗並回滾。ledger 回讀時重新驗證 payload hash 與資料列 identity；同一收據也不能改綁另一個 decision surface。

未取得同日獨立來源時，`source_disagreement_status=not_observed` 且 `certification_status=partial`。這代表系統已依規格安全降級，並不宣稱來源一致。

## 原生驗收結果

- 功能驗收 commit：`337366161a44f3f210c65c10fb47cff88487340e`
- 版本檢查：分支 `codex/fix-data-evidence-controls-20260907-1212`，Git commit、launcher 記錄 commit 與 managed runtime UI SHA 一致；結果為 `VERIFIED CURRENT PROJECT INSTANCE`。
- 原生操作：以 Stock AI Liquid Glass 開啟首頁，選取「建榮 5340.TWO」決策卡並聚焦品質收據。
- 畫面結果：決策顯示「待交叉驗證」、資料完整度「部分資料」、quality receipt `DQDR-4c8b9032c11ad339353c7e622d1c7a10`、certification `partial`、freshness `passed`、completeness `passed`、confidence `warning`；畫面同時說明尚未取得獨立同日行情，不建立新部位。
- 最新快照 `MIS-d362d42e61054ca6b23df5de63117f04` 的 durable ledger 有 2,395 個決策、2,395 個不同股票與 2,395 筆收據；以正式回讀程式重新驗證全部 2,395 筆 hash。當中 1,974 筆明確保存 `not_observed`／`partial` 降級結果。
- 截圖：[native-d005-decision-quality-receipts-20260907.png](native-d005-decision-quality-receipts-20260907.png)

## 自動化驗證

```text
uv run pytest tests/test_shared_decision_quality_receipts.py tests/test_market_summary_quality_receipts.py tests/test_market_intelligence_workspace.py tests/test_screener_conditions.py tests/test_production_requirement_status.py tests/test_static_ui.py tests/test_api.py -q
150 passed, 1 warning
```

Release gate 已重新計算為 `complete=70`、`partial=54`、`unverified=0`；D-005 已離開 blocking requirement 清單。D-010、D-011 與需要正式外部證據的項目維持 partial。
