# External Source Manifest

Generated after cloning the approved source repositories into `external/`.

These repositories are reference and adapter sources only. They do not own the
main application flow, data model, risk gate, or execution path.

| Project | Local path | Origin | Branch | Verified HEAD |
| --- | --- | --- | --- | --- |
| TradingAgents | `external/TradingAgents` | `https://github.com/TauricResearch/TradingAgents.git` | `main` | `85946c2f60768ab2dae23a5a36cd927662feef94` |
| FinRobot | `external/FinRobot` | `https://github.com/AI4Finance-Foundation/FinRobot.git` | `master` | `6a8161ff5cfa66ec3df9c11a0bf7a84a1ac11f01` |
| FinGPT | `external/FinGPT` | `https://github.com/AI4Finance-Foundation/FinGPT.git` | `master` | `608a496781faf2705b9a59c89f80e6c04b15d76e` |
| FinRL-Trading | `external/FinRL-Trading` | `https://github.com/AI4Finance-Foundation/FinRL-Trading.git` | `master` | `e65d6f0483ead7d2ef4a5fc940cdf960392a25c1` |
| FinRL | `external/FinRL` | `https://github.com/AI4Finance-Foundation/FinRL.git` | `master` | `220f9e490996a6e5c84cfad914ff14f2e0c42d22` |
| qlib | `external/qlib` | `https://github.com/microsoft/qlib.git` | `main` | `d5379c520f66a39953bad76234a7019a72796fd0` |
| AI-Trader | `external/AI-Trader` | `https://github.com/HKUDS/AI-Trader.git` | `main` | `d03ff6c056b32ced735adf7c19ed8175adb1c8df` |

Verification command:

```powershell
git -C external/<project> remote get-url origin
git -C external/<project> branch --show-current
git -C external/<project> rev-parse HEAD
```

Runtime verification endpoint:

```text
GET /api/open-stock-ai/external-sources
GET /api/open-stock-ai/external-source-lock
```

The endpoint is backed by `open_stock_ai.external_sources.registry.ExternalProjectRegistry`
and returns each project's local path, origin URL, branch, HEAD commit,
requirements/config files, and capability files discovered from the cloned
source tree.

`GET /api/open-stock-ai/external-source-lock` returns the machine-checkable
`open_stock_ai.external_source_lock.v1` contract. It includes the exact
approved `git clone` command for every external project, the expected branch and
HEAD from this manifest, the current local origin/branch/HEAD, and per-project
`origin_verified`, `head_verified`, and `lock_verified` flags.
