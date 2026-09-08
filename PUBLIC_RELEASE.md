# Public source release

This is the first public source distribution of AI-Powered Stock Market System, maintained by Magician-Bee. It starts a new Git history from the maintained development snapshot dated 2026-09-08. Earlier private development history is not included. There are no external adoption or download claims for this initial release.

The core application provides Taiwan-market data research, charts, model-provider adapters, auditable Agent workflows, strategy research, risk checks and paper-trading records. Real broker order execution is disabled. Implementation and validation limitations remain documented in the main README and requirement matrix.

## Distribution differences

- Original project code is released under MIT; third-party notices remain under their original licenses.
- Hardcoded credentials in imported broker and Hugging Face examples are replaced with explicit placeholders. All bundled notebooks have execution output and nonessential metadata removed.
- AI-Trader source is omitted pending a complete upstream license notice; its optional adapters remain.
- Private model-server addresses are replaced with the documentation-only address `192.0.2.1`, and personal macOS home paths use `/Users/your-user`. These are placeholders, not service endpoints.
- Private development validation screenshots are omitted. Historical validation notes describe development evidence, not fresh test results for this public distribution.
- The public history has no secret-scanner suppression baseline.

See THIRD_PARTY_NOTICES.md and the existing portable-launch instructions for details. Optional external sources are described in config/external_sources.lock.yaml; scripts/bootstrap_external.py supports selecting individual projects.
