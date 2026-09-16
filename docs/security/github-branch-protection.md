# GitHub branch-protection evidence

## Recorded remote audit — 2026-08-21

The authenticated GitHub API was asked for the protection settings on
`Magician-Bee/AI-Powered-Stock-Market-System` branch `main`:

```bash
gh api repos/Magician-Bee/AI-Powered-Stock-Market-System/branches/main/protection
```

GitHub returned HTTP 403: the current private-repository plan must be upgraded
or the repository made public to enable branch protection. Consequently, no
branch-protection rules or required Core Safety status checks can be asserted
as enabled for this repository.

This is a recorded limitation, not a substitute for the required protection.
T-001 can only be completed after the repository owner chooses an eligible
GitHub plan or visibility, configures branch protection for `main`, and records
the required Core Safety evidence set. No additional GitHub CI workflow was
added by this audit.

## Recorded remote audit — 2026-08-28

The same read-only check was repeated while authenticated as the repository
owner. The token has `repo` scope, so this result is not caused by an anonymous
request:

```bash
gh api -i repos/Magician-Bee/AI-Powered-Stock-Market-System/branches/main/protection
```

GitHub again returned HTTP 403 with the explicit message: upgrade to GitHub Pro
or make this repository public to enable this feature. No protection rule was
created or changed, and no workflow was dispatched. T-001 therefore remains
`partial`: the required Core Safety branch-protection gate cannot be enabled
until the repository owner changes the eligible plan or repository visibility.
