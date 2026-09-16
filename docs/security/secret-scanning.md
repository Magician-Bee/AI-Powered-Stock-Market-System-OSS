# Secret scanning gate

`Core research and Agent safety` checks the complete Git history with
[gitleaks](https://github.com/gitleaks/gitleaks).  It intentionally uses a
full clone (`fetch-depth: 0`): scanning only the changed checkout cannot find
a credential that was committed and later deleted.

Run the same release gate locally before publishing a branch:

```bash
gitleaks git . --log-opts='--all' --redact=100
```

The committed `.gitleaksignore` is an exact-fingerprint baseline for twelve
reviewed false positives in public vendored source snapshots plus two previous
test-fixture identifiers.  It is not a directory, rule, or regex allowlist.  A
new finding anywhere in the current tree or history fails the scan.  Never add
a real credential to this baseline.

When a real secret is found, revoke or rotate it before any Git history rewrite.
Then complete the repository migration with the owner: rewrite the affected
objects, force-push the protected refs under an agreed migration window, expire
old clones, and rerun the full-history scan until it reports zero findings.

## Recorded local full-history audit — 2026-08-21

The following non-destructive audit was run against the complete local Git
history.  The report path was an untracked temporary directory and redaction
was set to 100%, so no finding payloads were persisted in this repository.

```bash
gitleaks detect --source . --redact=100 --exit-code 0 \
  --report-format json --report-path "$(mktemp -d)/history.json" \
  --no-banner --no-color
```

Result: 544 commits (about 44.42 MB) scanned; zero Gitleaks findings.

This is evidence that the current local clone's history passed the configured
scanner at that time. It is **not** evidence that any previously issued
provider, broker, or user credential was revoked or rotated, nor that an owner
completed a protected-reference history rewrite and invalidated old clones.
Those owner-controlled actions remain required for SEC-001 to be complete.

## Recorded local full-history audit — 2026-08-26

After reviewing the exact historical Broker OMS fixture fingerprint, the same
non-destructive command scanned 680 commits (about 45.51 MB) and reported zero
findings.  This confirms the scanner baseline is precise; it remains **not**
evidence that historical credential rotation or protected-reference rewrite
has been completed.

## Recorded local full-history audit — 2026-08-28

The release command from this document was executed again against every ref in
the local clone:

```bash
gitleaks git . --log-opts='--all' --redact=100 --no-banner --no-color
```

Result: 731 commits (about 45.89 MB) scanned in 12.6 seconds; Gitleaks reported
`no leaks found`. This is a current local scanner receipt for SEC-002, with
redaction enabled and no report payload written to the repository. It remains
**not** evidence that an account owner revoked or rotated historical
credentials, or that protected references were rewritten. Those explicit
owner-controlled actions are still required before SEC-001 can become complete.

## Recorded local full-history audit — 2026-09-01

The same redacted release command was run again against every reachable ref:

```bash
gitleaks git . --log-opts='--all' --redact=100 \
  --report-format json --report-path - --exit-code 0 --no-banner --no-color
```

Result: 767 commits (about 46.24 MB) scanned in 12.2 seconds; Gitleaks reported
`no leaks found` and emitted an empty JSON finding list. This receipt is useful
for SEC-002 and for detecting a newly introduced history finding, including
vendored source snapshots. It is still **not** evidence that any historical
credential was revoked or rotated, that a protected-reference history rewrite
was owner-approved and force-pushed, or that old clones were invalidated. Those
owner-controlled actions remain the explicit SEC-001 completion blockers.
