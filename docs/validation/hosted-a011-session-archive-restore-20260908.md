# A-011 hosted Session archive restore evidence — 2026-09-08

A-011 is complete. The production archive and materialization code path was exercised across two independent GitHub-hosted Linux runners without calling a local or remote model and without operating an Agent Run.

## Candidate and hosted result

- Candidate commit: `3271691b4a67dff2b965938fcb10510385d18bee`
- Workflow: `Hosted Session archive restore`
- Run: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34222048542>
- Export job `102047222974`: passed
- Clean-runner restore job `102047427916`: passed
- Export artifact `10054154567`: expires `2026-12-07T11:41:49Z`
- Restore artifact `10054187124`: expires `2026-12-07T11:41:49Z`

The exported archive contained one Session, one title-history entry, two messages and one completed decision folder snapshot. Its immutable envelope retained event, evidence, artifact and version lineage. The clean runner materialized one Session row, one folder row, two message rows and one full archive-envelope row into a new SQLite database. The loaded envelope exactly matched the export, `PRAGMA quick_check` returned `ok`, and a second materialization attempt was rejected before overwrite.

The deliberate secret marker `must-not-leak` did not appear anywhere in the downloaded archive.

## Downloaded artifact verification

| File | SHA-256 |
| --- | --- |
| `session-archive.json` | `4a9b320891fde397804a39b8d3fe05bc92b892e73498462a4a00fe5342f2d94c` |
| `session-archive-export-evidence.json` | `afca4bd0d0df63db2be85264a7e3d249e32b081fc1a68a105557cc6a02fc5810` |
| `session-archive-gate-receipt.json` | `dd47e23c50f20178d82851d3f637f124fa9dd2a8406299fcf54dc75de6b93f79` |
| `session-archive-restore-receipt.json` | `b6d0e4b7fe8f3da89ed5e1b82ee66977855f24e1b129c8727e8a87748bd53684` |
| `restored-session.sqlite` | `de9a29ab61ebc38dd58e328d0faf059de0217bc3c8d31a256f39b72613915bd0` |

- Archive receipt SHA-256: `c102f0f0a92ecb3ded51cf79d3a27f4dd7a6d640d91949bdc45fd64dc4388b49`
- Lineage SHA-256: `3d31e5b80a2efbb2721086b5f77fa2da9efeea87e1789a5a242a2084d919266e`
- Gate receipt SHA-256: `46c7638ef4647373471972df81eb2dbcfb52626e4b749e75ce982ef2036b1b56`

## Local and desktop verification

The hosted gate and existing Session archival suites passed `7` tests. Python compilation and `git diff --check` passed. The candidate was launched through `./開啟股市AI系統.command`; `./驗證目前執行版本.command` reported the managed source, candidate `3271691b4a67`, matching UI hashes and `RESULT: VERIFIED CURRENT PROJECT INSTANCE`.

The native Stock AI Liquid Glass app visibly showed candidate `3271691b4a67`, the home workspace and the Agent Dock. No Run, send, provider or model control was activated.

Native A-011 validation（未隨公開版提供；原參考：`native-a011-session-restore-20260908.png`）
