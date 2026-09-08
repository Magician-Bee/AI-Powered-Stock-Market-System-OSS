# R-010 hosted chaos recovery evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted chaos recovery`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34105444323
- Job: `staging-fault-injection`
- Result: `success` in 42s
- Exact candidate: `857472882735566510ecafae9e8ec77a16780624`
- Artifact ID: `10012175884`
- Artifact retention expiry: `2026-10-07T09:21:05Z`

The workflow ran on an isolated GitHub Linux runner. The runner guard requires
Linux, `GITHUB_ACTIONS=true` and the explicit `--allow-privileged` flag. No model
or model endpoint was invoked.

## Injected faults and recovery invariants

| Scenario | Real injected fault | Observed recovery evidence |
| --- | --- | --- |
| Process kill | `SIGKILL` after a committed SQLite checkpoint | Child exited `-9`; checkpoint survived restart |
| Network partition | Kernel `iptables OUTPUT REJECT` against a local TCP endpoint | Connection was blocked; rule was removed and connectivity recovered |
| Disk full | Mounted a 1 MiB `tmpfs` and wrote until `ENOSPC` | 1,048,576 bytes filled the filesystem; mount was cleaned up |
| Timeout | Applied a 0.1 second deadline to a long-running child | Timeout was observed and the child was terminated |

Every scenario independently recorded all three required invariants:

- durable state recovered;
- new orders remained blocked until the system was safe;
- an operator receipt was written.

The shared safety database remained at `new_orders_blocked=1`. The campaign
receipt and all scenario receipts were reopened from disk after the injections.
`PRAGMA quick_check` returned `ok` for both retained SQLite databases.

## Independent receipt verification

The downloaded `open_stock_ai.hosted_chaos_gate_receipt.v1` was verified against
the exact candidate SHA. It reported `passed=true`, all four scenarios in the
required order and no blockers. Its embedded receipt SHA-256 is
`05a939fb36e4020062bcce24bae7cb54979d9a49b02bec98558b8f587f26f903`.

Retained artifact hashes:

- `chaos-gate-receipt.json`: `863deef3c44f80719e2410ccff65d3790860f1a0d0131863a4264bce0c9163f9`
- `chaos-receipts.sqlite`: `4684c59617b768c3faf0badba6f9ccbda997a4b501ee318cf2776396d41c15a9`
- `safety-state.sqlite`: `29d816bcdd1a640f7b20ebab486206261ea189a05353da23be4d8589148727da`
- `operator-process_kill.json`: `68e880e86a6f6718515bd00bc5f9775bfd9c2ff52bb8fb8c8460994cecf19512`
- `operator-network_partition.json`: `1c427c5bea8fc4cb98af3fce0bd084481a007511215e3425e27b51172b84730e`
- `operator-disk_full.json`: `bbabcbfaa217173f2e7bad893ca447bf0475cd10c29fe01b54d20f0ca575a605`
- `operator-timeout.json`: `55e8a47fe837c3c0dccc5e5156926da49e73ba8a44db5cd829f4ef98c356b021`

## Native desktop acceptance

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`857472882735`; visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered without
overlap. Evidence:
`docs/validation/native-r010-hosted-chaos-gate-20260907.jpg`.
