# Third-party notices

The root MIT license covers original Magician-Bee project code and documentation. It does not replace the licenses or notices of third-party code, assets, datasets, models, or services. Preserve the notices in each component when redistributing it.

| Component | License evidence in this distribution |
| --- | --- |
| FinGPT | external/FinGPT/LICENSE (MIT) |
| FinRL | external/FinRL/LICENSE (MIT) |
| FinRL-Trading | external/FinRL-Trading/LICENSE (Apache-2.0) |
| FinRobot | external/FinRobot/LICENSE and NOTICE (Apache-2.0) |
| TradingAgents | external/TradingAgents/LICENSE (Apache-2.0) |
| Qlib | external/qlib/LICENSE (MIT) |
| Glin UI | external/glinui/LICENSE (MIT) |
| liquidGL | external/liquidGL/package/LICENSE (MIT) |
| FreeFrontend / CodePen examples | src/stock_ai/ui/static/vendor/freefrontend-liquid-glass/LICENSE and MANIFEST.md |

Exact upstream revisions are listed in docs/integration/external_sources_manifest.md and config/external_sources.lock.yaml. Local example-credential and test-fixture edits in FinRL and FinGPT, and an argument-formatting edit in FinRobot, are changes made for this public distribution; upstream copyright notices are retained. Imported notebooks have also been cleared of execution outputs and nonessential metadata, and hardcoded Hugging Face token examples are replaced with explicit placeholders.

The AI-Trader source snapshot is not included: its package metadata names MIT, but the pinned source has no top-level license text. Its integration adapters and source references remain, and workflows needing that source report a missing prerequisite until it is installed separately. Review upstream licensing before installing or redistributing it.

Downloaded market data and third-party API access are governed by their providers' terms; this software license does not grant redistribution rights to those materials.
