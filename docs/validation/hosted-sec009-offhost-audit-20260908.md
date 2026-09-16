# SEC-009 immutable off-host audit evidence — 2026-09-08

SEC-009 is complete. Security and trading decisions now share the durable content-addressed authority used by Broker OMS state. The hosted drill exported that authority off host and reconstructed a complete order on an independent clean runner. No local or remote model was called, and no Agent Run control was operated.

## Candidate and hosted result

- Candidate commit: `b81c6228261d7183502d0fe1155e98f017883720`
- Workflow: `Hosted critical retention archive`
- Run: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34224155552>
- Export job `102054081610`: passed
- Clean-runner restore job `102054279485`: passed
- Export artifact `10054980595`: expires `2026-12-07T12:05:37Z`
- Restore artifact `10055006001`: expires `2026-12-07T12:05:37Z`

The candidate used the production Broker OMS writer to persist the exact sandbox intent and each lifecycle state. Before broker I/O, the Host risk decision was appended to the same SQLite retention authority with the intent SHA-256 as its evidence binding. The audit writer rejects sensitive field names, immutable identity conflicts and non-durable configuration.

The exported archive contained 16 critical records. A second GitHub-hosted runner downloaded the off-host artifact, verified every archive and record hash, restored it into a new SQLite authority and returned `PRAGMA quick_check=ok`. Using only the downloaded archive, it reconstructed order `BOI-HOSTED-AUDIT-1` as:

`CREATED → SUBMITTING → ACKNOWLEDGED → FILLED`

The reconstruction also recovered both security decisions: the explicit sandbox trading authorization and the central pre-trade risk approval. Both decisions were bound to the unchanged original intent hash. The reconstruction receipt, archive hash and restored authority were independently verified before the clean-runner job passed.

## Downloaded artifact verification

| File | SHA-256 |
| --- | --- |
| `critical-retention-archive.json` | `9a2ce3ede5e2097a64e8a71ac58a20636b9ecbccb533404adfe3059a6c48611d` |
| `critical-retention-export-evidence.json` | `55b69a01f1770c481d5cac801ba7582f69c22dd26c4f730c1bb3c907ebb29ccd` |
| `critical-retention-restore-receipt.json` | `5d1d2563edca5f6d4ab24e9771d0ce1f52ec3b2076961d2175890d7722bf1416` |
| `critical-retention-gate-receipt.json` | `ac6101be405462781f649089b34174b9a118dd7f8486ef3374be8096d6ffb7bc` |
| `order-audit-reconstruction-receipt.json` | `4e07c3b4fe431d44669cce77d42a524d36c4c9a11214fcc4fa3f73a12f346bed` |
| `hosted-security-audit-gate-receipt.json` | `3edbe8fa7c7ebf67a06c79fdfb59091a51f83389722a10e181ffc86f948b7076` |
| `restored-critical-retention.sqlite` | `4dfde50bbcdbf22c8417eb380fe044e750fcf3a3f9b56bd22e8a1fcfe5bc23a8` |

- Archive content SHA-256: `adb51a552f5139205de61f79494257003af87392967e06922ba62d0f89fb1a04`
- Restore receipt SHA-256: `b48ec737bdd701d39cb90537bd8ae689b99b443678e262ba23d58cbdfa7ff65d`
- Order reconstruction receipt SHA-256: `7c247ab727ba408251380994affa40a57890bdd2c8de1e875ce42da75fb88173`
- Hosted security gate receipt SHA-256: `d1eef1c82033b10f3068895bfde4355fa1603d0e3b39c30b8a040b3b291e9e9d`

## Local and desktop verification

The security audit, retention archive, Broker OMS, reconciliation, security controls and release status suites passed `120` tests. Python compilation, workflow YAML parsing, `git diff --check` and the no-model marker audit also passed.

The candidate was launched only through `./開啟股市AI系統.command`; `./驗證目前執行版本.command` reported managed service source, candidate `b81c6228261d`, matching local and served UI hashes, and `RESULT: VERIFIED CURRENT PROJECT INSTANCE`. In the native Stock AI Liquid Glass app, the exact candidate URL was visible. The home workspace, system data platform, individual-stock K-line chart and Agent Dock were opened successfully. No Run, send, provider or model control was activated.

Native SEC-009 validation（未隨公開版提供；原參考：`native-sec009-offhost-audit-20260908.png`）
