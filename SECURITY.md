# Security policy

## Supported scope

This is an experimental local research and paper-trading workstation. The public source preview is not a multi-user hosted service or a production live-trading product. Keep the UI/API on loopback, do not expose it directly to the internet, and keep real credentials out of repository files and test artifacts.

## Reporting

Use GitHub's private vulnerability reporting option when it is available. Otherwise open an issue containing only a non-sensitive request for a private disclosure channel; wait for the maintainer's response before sending reproduction data. Do not include tokens, account identifiers, private logs, personal holdings or exploitable details in public reports. No response-time guarantee is made.

## Areas requiring review

Review model/Host trust boundaries, untrusted news and web content, tool authorization, credential handling, file/network access, durable state, dependency integrity and paper-order idempotency. Prompts alone are not a security boundary. A successful source scan does not prove these controls are complete. Never test suspected broker issues against live accounts as part of this project's public validation.

Confirmed exposed credentials must be revoked or rotated by their owner. Deleting a file does not revoke a credential or erase earlier copies. Public fixes should include bounded regression tests with synthetic data and a clear description of the affected versions.
