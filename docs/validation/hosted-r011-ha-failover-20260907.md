# R-011 hosted HA failover evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted HA failover`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34106748368
- Job: `multi-process-failover`
- Result: `success` in 47s
- Exact candidate: `54581d26021a4f51a4cc9b237e9ab6608e9f0142`
- Artifact ID: `10012677079`
- Artifact retention expiry: `2026-10-07T09:34:52Z`

The workflow ran on an isolated GitHub Linux runner. For every service it started
separate primary and standby OS processes against the same durable SQLite lease
authority. The standby first had to observe a live-owner rejection. The harness
then sent `SIGKILL` to the primary and waited for the standby to claim the lease.
No model or model endpoint was invoked.

## Failover results

| Service | Primary PID | Standby PID | Epoch | Failover |
| --- | ---: | ---: | ---: | ---: |
| API | 2114 | 2115 | 1 → 2 | 0.919558 s |
| Agent runtime | 2116 | 2117 | 1 → 2 | 0.916828 s |
| Market data | 2118 | 2119 | 1 → 2 | 1.016898 s |
| Broker gateway | 2120 | 2121 | 1 → 2 | 0.515645 s |
| Scheduler | 2122 | 2123 | 1 → 2 | 0.919089 s |

All five services satisfied the required invariants: separate process identities,
observed primary death, standby acquisition, monotonic epoch advancement,
stale-owner fencing and durable-state recovery. Attempts to renew the old epoch
after takeover were rejected for every service.

## Independent receipt verification

The downloaded `open_stock_ai.hosted_ha_failover_receipt.v1` matched the exact
candidate SHA, contained every service in the canonical topology order, reported
`passed=true` with no blockers and independently verified its nested lease
receipt hashes. `PRAGMA quick_check` on `ha-leases.sqlite` returned `ok`.

- Embedded campaign receipt SHA-256: `0e007e8a0649725667575b61dc89381edd16789de2fdb1cff7a29d5b78ce5ff5`
- `ha-failover-receipt.json`: `c873a335c17aecd973b2516995b6be90bd8ad34921a7a35126347d2be9fae4e3`
- `ha-leases.sqlite`: `3dc3607ee87a903644e2b5a279d641c1f1a55cda834b8298b7fe89afd497f361`

## Native desktop acceptance

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`54581d26021a`; visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered without
overlap. Evidence:
`docs/validation/native-r011-hosted-ha-failover-20260907.jpg`.
