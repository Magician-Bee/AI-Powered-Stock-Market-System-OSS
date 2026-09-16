# GOV-005 hosted critical retention evidence — 2026-09-08

## Scope and status

GOV-005 remains `partial`. This batch proves that execution-critical retention records can be exported as a content-addressed archive, moved off the producing runner, independently verified and reconstructed in a fresh SQLite authority. The hosted artifact retention is 90 days, so the receipt explicitly records `scope=hosted_drill_only` and `production_years_satisfied=false`. It is not evidence that a production order can be reconstructed years later.

No Agent Run, model provider, broker account or live order was invoked. The archive contains deterministic paper/sandbox examples for the three retention schemas already emitted by the runtime writer paths.

## GitHub-hosted run

- Workflow: `Hosted critical retention archive`
- Run: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34141401316>
- Export job: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34141401316/job/101803976893>
- Clean-runner restore job: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34141401316/job/101804118353>
- Candidate commit: `9198eb6e51ed6d2ad58e60ef4406a0c2cfad9384`
- Result: `success`
- Archive artifact ID: `10026036651`
- Archive artifact: `stock-ai-critical-retention-9198eb6e51ed6d2ad58e60ef4406a0c2cfad9384`
- Restore artifact ID: `10026057479`
- Restore artifact: `stock-ai-critical-retention-restore-9198eb6e51ed6d2ad58e60ef4406a0c2cfad9384`
- Artifact expiry: `2026-12-06T16:03:32Z`

The first runner pruned its ordinary signal projection to the declared bound and exported only critical records. The second job used a separate fresh runner, downloaded the off-host artifact, required a nonexistent restore target, verified every nested hash, restored a new SQLite database and independently verified the outer gate receipt.

## Downloaded artifact verification

The artifacts were downloaded after the hosted run and verified again with the exact candidate code:

| Evidence | SHA-256 |
| --- | --- |
| Critical archive file | `6c4f0b4f2957863b9345c2a79b18e7cb1088e97f02b8aa84d7a04ba107fe8664` |
| Critical archive content | `9c4170a2ea0337cbc726c25d40888cbee2846b74d1a7bb72c9475c6ba71b0e78` |
| Export evidence file | `eaef4aeb55ec0d9dc15a4d9104dffe4bd6443792b5cdea084458971c390c8abd` |
| Restore receipt file | `6f1e486b04a5b354d9d6c2b3477a672593e92a53b0a0669afdf7659e1e22efb3` |
| Gate receipt content | `fb1e262196b22cded66cddb8f6be01ebe186debc85a7e69456f9c20a37ef12c0` |
| Gate receipt file | `385e7761ef1eb4fa94bcd4e5ecac8687962c9db260a748abd422b984e42dd341` |
| Restored SQLite file | `b441d68488055e4a47b85f5a1349ed89e13b10159c5e9f1611daa0443f496328` |

The restored database returned `PRAGMA quick_check=ok` and contained exactly three immutable critical rows:

- `paper-oms-execution-PAPER-1` — `stock_ai.paper_oms_execution_snapshot.v1`
- `paper-broker-execution-PAPER-1-filled` — `stock_ai.paper_broker_execution_snapshot.v1`
- `oms-state-live-disabled-example` — `stock_ai.broker_oms_execution_state.v1`

Archive or payload tampering, an altered outer receipt, missing schemas, an existing restore target, a failed SQLite check, or any attempt to claim production-years retention makes verification fail closed.

## Local verification

```text
uv run pytest tests/test_retention_archive.py tests/test_content_retention.py tests/test_runtime_governance.py tests/test_paper_oms.py::test_trade_store_retains_immutable_critical_oms_snapshot_and_reuses_it tests/test_paper_broker.py::test_paper_broker_retains_every_lifecycle_projection_across_restart tests/test_broker_framework.py::test_oms_persists_immutable_execution_state_to_critical_retention_ledger tests/test_paper_training_integration.py::test_runtime_paper_broker_factory_uses_the_authoritative_retention_ledger -q
17 passed
```

Python compilation and `git diff --check` also passed. `ruff` is not installed in the project environment, so no ruff result is claimed.

## Native desktop validation

The project was stopped and launched only through `./停止股市AI系統.command` and `./開啟股市AI系統.command`. `./驗證目前執行版本.command` confirmed branch `codex/feat-gov005-offhost-retention-20260907-2351`, commit `9198eb6e51ed`, managed runtime source, and matching local/served UI hashes. Computer Use then raised the native **Stock AI Liquid Glass** app; the home workspace, market list, chart controls and Agent Dock were visible, and the URL was bound to `stock_ai_commit=9198eb6e51ed`. No Run, send, provider or model control was activated.

Native GOV-005 validation（未隨公開版提供；原參考：`native-gov005-offhost-retention-20260908.jpg`）

## Remaining blocker

`production_years_archive_retention_and_remaining_execution_critical_writers_are_not_yet_connected`

The full release gate remains closed at 83 complete and 41 partial requirements. Its post-update ledger SHA-256 is `382bda4d8b4f25e4783ae941afb4489714585bd20182b526bc77c3c4bdc29097`.
