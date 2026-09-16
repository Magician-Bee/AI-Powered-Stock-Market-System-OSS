# R-003 hosted migration retention evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted migration retention`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34115642136
- Jobs: `migration-and-cleanup` (`101721673315`) and `clean-runner-verify` (`101721819971`)
- Result: `success`
- Exact candidate: `0a98652eed52632c3ebe54e1c7b4a5b8d0edd654`
- Retained migration artifact ID: `10016122282`
- Clean-runner verification artifact ID: `10016143673`
- Both artifacts expire: `2026-12-06T11:14:56Z`

The first GitHub-hosted Linux runner created a real version-zero legacy SQLite
database containing a decision signal, then opened it through `SQLiteStore`.
Startup created and verified the pre-migration backup before applying the
contiguous v1 through v47 manifest. The completed receipt, current schema
version and original signal row were checked after commit.

The drill materialized historical completed pairs and one prepared forensic
pair around that verified backup. Cleanup first emitted a dry-run, then executed
only when the exact same two expired completed pairs remained valid. It retained
the two newest completed pairs, the prepared pair and an unknown caller-owned
file. The retained migration pairs were packaged into a weekly, 90-day GitHub
Actions artifact. No user database, model or model endpoint was used.

The second job ran on another fresh runner. It downloaded the off-host artifact
and independently verified every retained backup receipt, bytes hash, schema,
SQLite integrity and original `2330.TW` content before creating the final gate
receipt.

## Independent downloaded-artifact verification

Both artifacts were downloaded again after completion. The downloaded file
sets were exact: eight allowlisted retention files and two verification files,
with no SQLite sidecars or unknown output. Every retained database returned
`PRAGMA quick_check=ok`; statuses were exactly two `completed` and one
`prepared`.

- Migration manifest SHA-256: `377d68f5f3e62766e093a75179e75282bf0b7b31ad3679f7b7e9bf18f54406fd`
- Dry-run report SHA-256: `41ccfd17cb58476d0bee5a1b4cd0eb148fe573a4b38ef00e204f56bf8cd58f19`
- Executed cleanup report SHA-256: `92f2fbfafc446c4d1c44ec26fc69f649516a4f8ca20a5fe6adcd478cfee7b15e`
- Retained backup SHA-256: `3d22050ed446c172982e4ea7a4ce9edd2fbf5a9a61c0d131e7fe32b0045d01cc`
- Embedded gate receipt SHA-256: `0ad74c1c470a3444faf569789896390903d7067a2f88de4f8ed413f2c882cfb4`
- `migration-gate-receipt.json`: `62b7030692af3c47e276b7d049b23e8ec95d5193a18a28b00cadfde0f3a63df4`

The cleanup execution report's `deleted` array is byte-for-byte equal to the
dry-run report's `planned` array. Both content-addressed reports passed the
independent verifier with no blockers.

## Local tests and native desktop acceptance

Migration safety, SQLite backup, backup policy and hosted migration suites
passed `15 passed`. They cover manifest continuity, migration-before-backup,
rollback, dry-run/execution equality, protected-pair preservation, cleanup
receipt tampering and retained-backup tampering. Python compilation and
`git diff --check` also passed.

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`0a98652eed52`. Visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home and system/data-platform workspaces rendered with
the expanded Agent Dock. No Agent Run control or model was invoked. Evidence:
`docs/validation/native-r003-hosted-migration-retention-20260907.jpg`.
