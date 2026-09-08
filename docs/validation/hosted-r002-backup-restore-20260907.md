# R-002 hosted off-host backup and clean restore evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted SQLite backup restore`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34114119395
- Jobs: `create-backup` (`101716844568`) and `clean-runner-restore` (`101717026576`)
- Result: `success`
- Exact candidate: `0a0245095d59c61cb81f94e44f93e783e523e8e3`
- Backup artifact ID: `10015554647`
- Restore evidence artifact ID: `10015577501`
- Retention expiry: `2026-10-07T10:58:07Z` and `2026-10-07T10:58:49Z`

The first job created an online SQLite backup from a WAL-enabled authoritative
database, recorded the source content, schema, integrity, backup bytes and policy
hashes, then uploaded the result to GitHub Actions artifact storage. The policy
receipt requires an off-host destination, a 24-hour interval and at least 14
retained copies. The workflow runs every day at `03:17 UTC`; each independent
artifact has an enforced 30-day retention period, which preserves more than the
required rolling-copy count while successful daily runs continue.

The second job ran on a separate fresh GitHub-hosted runner. It downloaded the
off-host artifact, required the restore directory to be absent, verified the
backup receipt before materializing `restored.sqlite`, compared every ordered
row with the source manifest, checked exact content hashes, ran SQLite
`quick_check`, and verified that a caller-owned target could not be overwritten.
No model or model endpoint was invoked.

## Independent downloaded-artifact verification

Both final artifacts were downloaded again after the workflow completed. The
embedded `open_stock_ai.hosted_backup_gate_receipt.v1` matched the exact commit
and run ID, reported `passed=true` with no blockers, and passed the independent
receipt verifier.

- Embedded gate receipt SHA-256: `0ce3793483ea6f7b42dff6e65bc8a0e217745489ceec340d791489e6f5ac5839`
- `backup-gate-receipt.json`: `fb6b56af99c3728e37e638628b6cd20ba24b9164794b7e0dd0223657a697cf67`
- SQLite backup SHA-256: `d50a65b217a8e45ef9c08bd7ea60810f11261128b8b7a733297c557464ddf4a2`
- Source/restored content SHA-256: `4386cb8fb0726a9340d8211ef9e8b37a501f0ef154ccc997b35fc15f374397b7`
- Restored `PRAGMA quick_check`: `ok`
- Existing-target overwrite attempt: refused

The backup artifact contains exactly `authoritative.backup.sqlite`,
`backup-receipt.json`, `backup-policy-decision.json` and
`source-manifest.json`. The restore evidence contains exactly
`backup-gate-receipt.json` and `restored.sqlite`. A prior successful run was not
accepted as final evidence because independent inspection found unnecessary
SQLite `-wal` and `-shm` sidecars. The final workflow removes sidecars and also
uses an explicit upload whitelist.

## Local tests and native desktop acceptance

The SQLite backup, policy and hosted-gate suites passed `10 passed`, including
tamper rejection, content equality, clean restore, overwrite refusal and exact
artifact membership. Python compilation and `git diff --check` also passed.

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`c6fbbc16d174`. Visible Computer Use inspection exercised the native
`Stock AI Liquid Glass` home page, instrument K-line chart, system settings and
Agent Dock collapse/re-expand controls without starting or changing an Agent Run
and without calling a model. Evidence:
`docs/validation/native-r002-hosted-backup-restore-20260907.jpg`.
