# GOV-005 critical execution writer evidence — 2026-09-08

GOV-005 remains `partial`. This batch connects the remaining identified paper-account and reconciliation writers to the durable content-addressed retention ledger. The hosted artifact lasts 90 days and explicitly records `production_years_satisfied=false`; the remaining blocker is production-years off-host retention.

## Candidate and hosted result

- Candidate commit: `71b0527bddf19a30b7b10e0cec1d4b96332ed6f2`
- Workflow: `Hosted critical retention archive`
- Run: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34220764365>
- Export job `102043076334`: passed
- Clean-runner restore job `102043265393`: passed
- Export artifact `10053650903`: expires `2026-12-07T11:27:13Z`
- Restore artifact `10053677958`: expires `2026-12-07T11:27:13Z`

The production writer drill generated and restored these ten critical schemas:

1. `stock_ai.paper_oms_execution_snapshot.v1`
2. `stock_ai.paper_broker_execution_snapshot.v1`
3. `stock_ai.broker_oms_execution_state.v1`
4. `open_stock_ai.paper_corporate_action_execution.v1`
5. `open_stock_ai.paper_borrow_fee_batch.v1`
6. `open_stock_ai.paper_settlement_batch.v1`
7. `open_stock_ai.settlement_pnl_feed.v1`
8. `stock_ai.broker_reconciliation_execution.v1`
9. `stock_ai.account_reconciliation_receipt.v1`
10. `stock_ai.account_reconciliation_execution.v1`

The clean runner restored 10 critical rows into a new SQLite authority and returned `PRAGMA quick_check=ok`. The gate receipt passed with `artifact_retention_days=90`, `scope=hosted_drill_only`, and `production_years_satisfied=false`.

## Downloaded artifact verification

| File | SHA-256 |
| --- | --- |
| `critical-retention-archive.json` | `29d2a03b904a9bc97912965bdbd0c1017fa8f6501a65079f905ea3fd6bfbe846` |
| `critical-retention-export-evidence.json` | `30abd8203f1913c6058d945018c034d480555a7b58bf978537afd9ad03202021` |
| `critical-retention-gate-receipt.json` | `533e5941b8e2afdd1cfee91b57e82fe2ee1406f26ad1910ede0b3eb156b95479` |
| `critical-retention-restore-receipt.json` | `9cf9a7d9e5efe84b38dc7eb821e50c20b62c2db0b65f15690c889a87a2ff31f1` |
| `restored-critical-retention.sqlite` | `6ba2f458c0c446f7991b85f4f8fc5236d22e4912b60b5acfc885ca7707a1637c` |

The verified gate receipt SHA-256 is `5a9fc91df5e2065abcf0e13c1fd07d60b0fafb9d941acbca007ec15dd289578a`.

## Local verification

`104 passed` across the retention archive, corporate action, borrow fee, Taiwan settlement, settlement P&L, order/account reconciliation, Paper OMS and Paper Broker suites. Python compilation and `git diff --check` also passed. No local or remote model was called.

The candidate was launched only through `./開啟股市AI系統.command`; `./驗證目前執行版本.command` reported the managed service source, candidate SHA `71b0527bddf1`, matching local/served UI hashes, and `RESULT: VERIFIED CURRENT PROJECT INSTANCE`. The native Stock AI Liquid Glass app visibly showed the same candidate URL, the home workspace and Agent Dock. No Run, send, provider or model control was activated.

Native GOV-005 critical-writer validation（未隨公開版提供；原參考：`native-gov005-critical-writers-20260908.png`）

## Remaining blocker

`production_years_archive_retention_is_not_yet_provisioned`
