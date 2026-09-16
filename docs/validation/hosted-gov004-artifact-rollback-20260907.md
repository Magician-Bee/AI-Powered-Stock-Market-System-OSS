# GOV-004 hosted artifact rollback evidence — 2026-09-07

## Candidate and hosted execution

- GitHub workflow: `Hosted artifact rollback drill`
- Successful run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34110052083
- Initial fail-closed run: https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34109725703
- Job: `strategy-model-rollback`
- Successful result: `success` in 45s
- Exact candidate: `11f52014209f7a5a6ddf2046ca5f4ef0513cfafd`
- Artifact ID: `10013972568`
- Artifact retention expiry: `2026-10-07T10:11:22Z`

The first run correctly failed because the drill omitted the process-local Stock
AI runtime session and the real API returned HTTP 403. The successful candidate
loaded the session token injected into the index, supplied the required same-origin
headers, and called the running Stock AI governance API over loopback HTTP. This
preserves the same local security boundary used by the native desktop app.

No strategy or model was executed. The drill exercised artifact identity,
approval, activation, rollback and durable recovery only.

## Rollback results

The drill wrote four distinct content-addressed artifact files and approved them
as `strategy-v1`, `strategy-v2`, `model-v1` and `model-v2`. It then verified:

- changing the hash of an existing artifact ID was rejected;
- activating an unapproved artifact was rejected;
- a rollback with `approved_by=agent` was rejected by the authenticated API;
- strategy rollback restored `strategy-v1` while model stayed at `model-v2`;
- model rollback restored `model-v1` while strategy stayed at `strategy-v1`;
- both restored versions remained current after the API process stopped and the
  durable SQLite registry was reopened;
- `PRAGMA quick_check` returned `ok`.

The strategy rollback receipt SHA-256 is
`3d31fc96df4712e2098b45efb69c948232f716d66ab3e9daca0f3da86d1bc633`;
the model receipt SHA-256 is
`3eaf3a9d6e3c84ff1787b88e9f703bfe2674fe84a0a6e561a4dd528640db4969`.

## Independent receipt verification

The downloaded `open_stock_ai.hosted_rollback_gate_receipt.v1` matched the exact
candidate SHA, reported `passed=true` with no blockers and independently verified
both nested rollback receipts.

- Embedded campaign receipt SHA-256: `f3c6f3da2c400735749397e10d7464fa3de47265afd86dd41324e30426c928f5`
- `rollback-gate-receipt.json`: `29cb2c796e1a0235b65b122604657a88adbd7f8d9486775b6737bfe7dd44749b`
- `governance.sqlite`: `9f18391e26220c5e962a18f62bca2c4074ad5b963b4837cd9ca06a49337da21d`
- `strategy-v1.json`: `6a4e96e7cb4e8eddddce605cddfb7d6b1d104a404dafdc4efa3e03ffb6994361`
- `strategy-v2.json`: `c6060bbd244884583fe4dafe6c5e296a055239ba11de664762436aa515c6a49d`
- `model-v1.json`: `2c71645742f426a0b8c6e3a9273b62dba3926fd84af39e6fede27a76b5deffb3`
- `model-v2.json`: `746a1a9d5af8cf4766cf92e63442bef86d7753d82ac3933c3c313448a27a749b`

## Native desktop acceptance

The successful candidate was stopped and launched only through
`停止股市AI系統.command` and `開啟股市AI系統.command`.
`驗證目前執行版本.command` confirmed the managed runtime at commit
`11f52014209f`; visible Computer Use inspection confirmed the native
`Stock AI Liquid Glass` home workspace and expanded Agent Dock rendered without
overlap. Evidence:
`docs/validation/native-gov004-hosted-rollback-20260907.jpg`.
