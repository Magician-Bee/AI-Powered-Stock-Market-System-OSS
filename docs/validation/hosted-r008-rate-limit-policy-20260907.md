# R-008 hosted rate-limit policy evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted rate limit policy`
- Run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34108758767
- Job: `policy-and-429-recovery`
- Result: `success` in 36s
- Exact candidate: `baa52fced2017c0d286fc20499b62d8091c11aee`
- Artifact ID: `10013470557`
- Artifact retention expiry: `2026-10-07T09:56:48Z`

The workflow started an isolated HTTP upstream in a separate process. It returned
a real HTTP 429 with `Retry-After` on the first request for every verified scope,
then HTTP 200 after the governor's backoff. No model or external account was used.

## Verified policies and admission results

Each endpoint, tool and provider policy fixed these reviewed limits:

- two requests per 0.25-second window;
- maximum concurrency of one;
- `policy_verified=true`;
- a content-addressed policy receipt.

The hosted campaign covered:

- `endpoint:hosted-market-data`
- `tool:hosted-research`
- `provider:hosted-primary`
- `provider:hosted-alternate`

Every scope rejected a second in-flight admission with
`maximum_concurrency_reached`, observed HTTP statuses `[429, 200, 200]`, blocked
during `backoff_active`, and rejected a third recovered request with
`verified_window_limit_reached`. Exactly three upstream requests occurred per
scope. A separate unregistered tool scope proved
`unverified_policy_conservative_interval` and then recovered after the interval.

## Independent receipt verification

The downloaded `open_stock_ai.hosted_rate_limit_gate_receipt.v1` matched the exact
candidate SHA, reported `passed=true` with no blockers and independently verified
every policy and admission-decision SHA-256.

- Embedded campaign receipt SHA-256: `b5e5f655470ef4f462972667ff28bf77f16516be0758e2559dee62cc070c5f31`
- `rate-limit-gate-receipt.json`: `b38d5ef30708d6fa0263ffeded278090ae33574ce91e46bc9c0989fba08d3ad1`
- `endpoint-hosted-market-data.json`: `1bd94760879581f65f0d55d1e0ef06fa17f5851d7dabb80d476497267e133315`
- `tool-hosted-research.json`: `1812656509e395b54f32cfadf9795df4180c838af0a2254810fb2f54546be83f`
- `provider-hosted-primary.json`: `d4cbe1431b9004464a84c286797eabf3fc1a847cb11819732eacf02e59c00e73`
- `provider-hosted-alternate.json`: `455c5e9c11bde78a8e04d0a4c7c9832588b643d7c8b8291b9a3f1697dcbf0599`
- `unknown-policy.json`: `af820b4f1c6707eb42aceb018b7aadcd514b8d86bd2cfceb061d56c0685e7f3d`
- `server-requests.jsonl`: `7b6f8c3fe68f4ad961ae3c61dd165aa8a0ff8c9c22ea7ef05c267044577d5d69`

## Native desktop acceptance

The candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`baa52fced201`; visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered without
overlap. Evidence:
`docs/validation/native-r008-hosted-rate-limit-20260907.jpg`.
