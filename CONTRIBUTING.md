# Contributing

Use this public repository for reproducible bugs, documentation, tests and reviewed changes. Work on a branch from current public main. Do not merge private development history, local-only outputs or archived credentials into this repository.

## Local setup

Install Python 3.12 and uv, then run `uv sync --frozen --extra dev`. Never use real broker credentials or personal account data in a test. Keep real settings outside version control; `.env.example` contains placeholders only. Review `git diff --cached` before committing and use the GitHub-provided noreply address for your author email.

## Required review

Changes must preserve PAPER-only defaults, model/Host permission boundaries and the distinction between research results and execution evidence. Do not disable guards, replace assertions with fixed successes, or widen network permissions merely to obtain passing tests. Keep third-party notices. Clearly identify fixtures, mocked responses, live-data observations and real model results.

Run `python scripts/validate_public_release.py --gitleaks /path/to/gitleaks --output output/public-validation.json` inside the installed environment. Public CI runs the same stated offline subset. For broader changes add relevant unit and integration coverage; the public subset alone does not certify application readiness. Do not turn on live trading or external automations in CI.

Include the tested revision, reproduction steps, expected/actual behavior and the exact test scope in a pull request. Screenshots and logs must use synthetic or de-identified information. Report sensitive issues using [SECURITY.md](SECURITY.md), not a public issue containing secrets.
