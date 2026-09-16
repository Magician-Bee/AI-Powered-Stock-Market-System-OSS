# Public validation evidence

[Source privacy review](privacy-source-review.json) describes export-time checks. [Release validation](public-validation.json) is the machine-readable result for the released source. [Source manifest](SOURCE-SHA256.json) binds the final files by SHA-256; the manifest itself is excluded from its own hash list.

The public release validation compiles Python source, checks repository hygiene and cleared notebooks, runs Gitleaks against a tracked-files-only export, and executes selected risk, portfolio, paper-account, broker-contract and transport-guard regressions with socket connections disabled. Test inputs are local fixtures. No application service, model endpoint, market feed, real broker or notification service is started.

The selected tests are not the entire application's suite and do not establish live-trading readiness, valid market data, general model success, profitability or cross-platform installation. Historical statements elsewhere in the repository remain maintainer-recorded development evidence. Files marked not included are not public downloadable proof and have not been reconstructed or fabricated.

Manifest format: each JSON record is `[sha256, relative_path]`. These are content checksums, not credentials.
