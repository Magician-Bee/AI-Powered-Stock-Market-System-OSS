# Public source release · 2026-09-16

This public source preview is derived from maintained source snapshot `a0b76d5b258de1ae8313d07f1a5fa34f29eade06`. The private development repository and its main branch are not published or rewritten by this distribution. The public package does not contain private Git history, runtime databases, account records, raw execution logs, development screenshots, machine shortcuts, environment secrets, or model weights.

## Changes for public distribution

Personal home paths and model-server addresses were replaced by documented examples. macOS application identifiers use the project's public namespace. Imported broker/API examples use placeholders; synthetic idempotency fixtures preserve their original values. Notebook execution outputs and nonessential metadata were cleared. Only reviewed application artwork and icons were retained; image metadata was removed. PowerShell text retains UTF-8 BOM compatibility. AI-Trader source is omitted pending a complete license notice; other components retain their upstream licenses and available notices.

Original project code is MIT licensed. The license does not cover third-party data, credentials, models or services. See [third-party notices](THIRD_PARTY_NOTICES.md).

## Evidence and readiness

[Source review](docs/evidence/privacy-source-review.json) records the exported source checks. [Public validation](docs/evidence/public-validation.json) records the release tree's static checks, Gitleaks scan and selected offline risk/paper/broker-contract tests. These are real executions of the stated checks, not a full application or model benchmark. No live order, broker API, model inference or external notification is used for release validation. The PAPER capability gate remains unchanged.

The large historical development overview is preserved in [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md). Private or omitted evidence is labeled as not included instead of being exposed as a broken download. Historical test numbers are not newly certified results.

The release starts a reviewed public source history with GitHub noreply author identity. This does not erase provider-retained old objects, Actions metadata or copies previously obtained by others. The privacy scan is heuristic, not proof that every possible secret or identifying fact is absent.
