# R-009 hosted load regression evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted API load regression`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34104172227
- Job: `stock-ai-api-load-gate`
- Result: `success` in 1m11s
- Exact candidate: `9cc62cac0801fad307150d534b47bbad57bb54a5`
- Artifact ID: `10011715225`
- Artifact retention expiry: `2026-10-07T09:07:50Z`

The job started `stock_ai.main:app` on an isolated GitHub runner with temporary
SQLite paths and `STOCK_AI_AGENT_BACKGROUND_PAUSED=1`. It verified `/health`
reported `stock-ai-system` and the exact candidate commit before applying load.
No model or model endpoint was invoked.

## Profile and result

The profile used 20 requests/second, concurrency 8 and two independent 15-second
measurement intervals. Each interval required at least 240 completed samples.
The absolute gates were p95 <= 250 ms, p99 <= 500 ms, error rate 0 and throughput
>= 15 requests/second. Relative candidate latency and throughput regression was
also bounded to 50% of the fresh baseline.

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Samples | 300 | 300 |
| p95 latency | 2.968 ms | 2.815 ms |
| p99 latency | 3.821 ms | 3.019 ms |
| Error rate | 0.0% | 0.0% |
| Throughput | 19.9998 req/s | 19.9998 req/s |

Result: `passed=true`, with no blockers. The downloaded receipt independently
verified under `open_stock_ai.hosted_load_gate_receipt.v1`; its embedded receipt
SHA-256 is `8df8787695a43995695072634f7ad99a7fdbbcd0c2e955672e11ee33e5d6cbcb`.

## Retained artifact hashes

- `load-gate-receipt.json`: `c4f5589e91a925a2781cfb5f004d3268ff4672d48729aa58c997fed070715380`
- `load-profile.json`: `9add9afc91d0146bae79bc92861a9b877cfab13e2dce42fafad597393ae83432`
- `load-report.json`: `4cefd475be155835414eba2d6bd34edbeb7ecc4c84c05af78dc5897a8b9ba394`
- content-addressed baseline artifact: `d931cd7d067ef4277ce831a4a69e9fb980cba916450297d0816ec063e534e6af`

## Native desktop acceptance

The branch was stopped and launched only through `停止股市AI系統.command` and
`開啟股市AI系統.command`. `驗證目前執行版本.command` confirmed the managed
runtime at commit `9cc62cac0801`; visible Computer Use inspection confirmed the
native `Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered
without overlap. Evidence: `docs/validation/native-r009-hosted-load-gate-20260907.jpg`.
