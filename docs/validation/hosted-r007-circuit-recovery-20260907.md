# R-007 hosted circuit breaker recovery evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted circuit breaker recovery`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34107840277
- Job: `failure-storm-recovery`
- Result: `success` in 37s
- Exact candidate: `ea303ae937201b97210d297268a5277dad492b04`
- Artifact ID: `10013098361`
- Artifact retention expiry: `2026-10-07T09:46:25Z`

The workflow started an isolated HTTP upstream in a separate process and sent
real requests to provider, source and broker paths. Each path returned three
HTTP 503 responses, then switched to HTTP 200 only after the open-circuit
backoff. No model or external account was used.

## Failure storm and recovery result

Each scope produced the same verified transition:

1. Three admitted failures moved the circuit through `closed`, `closed`, `open`.
2. A request during backoff returned `open_backoff_active` before network I/O.
3. The upstream request count remained 3 while the circuit was open.
4. A peer scope remained independently admitted.
5. One half-open probe was admitted after backoff and received HTTP 200.
6. The success closed the circuit and the final upstream request count became 4.

This sequence passed for:

- `provider:hosted-failure-storm`
- `source:hosted-failure-storm`
- `broker:hosted-failure-storm`

## Independent receipt verification

The downloaded `open_stock_ai.hosted_circuit_gate_receipt.v1` matched the exact
candidate SHA, contained all three scopes in canonical order and reported
`passed=true` with no blockers. Every nested circuit-breaker decision receipt
was independently hash-verified.

- Embedded campaign receipt SHA-256: `15c4ee7944826ade48ffaa56ac8fac0bd0af794df5d6c4da4714e4d1277e4487`
- `circuit-gate-receipt.json`: `2400d9f7cce3a18b0f6d8ab17979bf10b73e5903fac2fc3e4ece633164363a14`
- `provider-failure-storm.json`: `a81085c9f5d4800ab08f586adb385738b09902d977c92aabaf74a40935eb83b6`
- `source-failure-storm.json`: `c7781fc2f081e1b006ed00b91c876870ab09898a891f0dd49860fb0054d5daca`
- `broker-failure-storm.json`: `1cf88988100d3892727a65a4fec8d1d40df131ccb90d732618e8d0cba161b57d`
- `server-requests.jsonl`: `ab16e227fa7c625afdd042a8435fbbe52c8235f115b84a48032e92ad5bbac0ee`

## Native desktop acceptance

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`ea303ae93720`; visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered without
overlap. Evidence:
`docs/validation/native-r007-hosted-circuit-gate-20260907.jpg`.
