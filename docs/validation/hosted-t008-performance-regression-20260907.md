# T-008 hosted performance regression evidence — 2026-09-07

## Scope and status

T-008 remains `partial`. This batch closes the missing hosted evidence for the database, API and backtest runtime surfaces. Model execution was disabled throughout the batch, so the model runtime surface remains an explicit blocker and no mock or fixture is counted as model performance evidence.

## GitHub-hosted run

- Workflow: `Hosted non-model performance regression`
- Run: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34139640836>
- Job: <https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34139640836/job/101798487477>
- Candidate commit: `6bf136649a64f62e16005fba7e25adc08b42369f`
- Result: `success`
- Artifact ID: `10025392453`
- Artifact: `stock-ai-t008-performance-6bf136649a64f62e16005fba7e25adc08b42369f`
- Artifact size: 3,052 bytes
- Artifact expiry: `2026-12-06T15:42:07Z` (90-day workflow retention)
- Receipt content hash: `ea61eeadb4d106e26f727e6eaad197010dc3005c01711a4f9644ccf7f766d6ae`
- Receipt file SHA-256: `0c1f6e3272aa50c4b4cad6ce699cf769ddc1eb4ed62f10fa0000076f605bd0e6`
- Independent verification: passed

The workflow starts the exact candidate service on loopback with Agent background work paused. The benchmark script never imports or calls an Ollama, OpenAI-compatible or other model provider. The retained receipt records `model_execution: disabled` and `deferred_surfaces: [model_runtime]`.

## Retained measurements

Each surface captured 100 baseline samples followed by 100 candidate samples. All candidate error rates were zero.

| Surface | Baseline p95 | Baseline p99 | Candidate p95 | Candidate p99 | Candidate throughput | Baseline artifact | Report |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Database | 0.621366 ms | 0.892373 ms | 0.520184 ms | 0.612147 ms | 2152.572255/s | `450b3a77893da2aabda089e26d81a59e320aeb5abcef8f0a979a8d464e61c830` | `58e9ed26e04625d919e292302183c1e9138b9bf517fbc147d2d0c2196e6f1dc9` |
| API | 1.942066 ms | 2.124944 ms | 2.366466 ms | 3.027015 ms | 566.332363/s | `fb8a7778a2a4c612056214618276f915004853dec29e4d524f95f2994b2a6b86` | `5e94a0caa2b2e09ca3394f90621efbb1e6c053038539ca3c029293c47db9d691` |
| Backtest | 7.877189 ms | 8.115575 ms | 8.639945 ms | 9.068059 ms | 127.272752/s | `65937e632a6dbbb333a8971752ced7e5afb5fce892e53cfea947a5548512122a` | `a0097ef368876123b76fef0534dfee5c5128f2af74a7da13985062ee70a59116` |

Missing surfaces, samples, altered baseline artifacts, altered candidate metrics, report mismatches and threshold regressions all fail closed.

## Local verification

The managed desktop runtime was stopped and launched only with the project `.command` files. `驗證目前執行版本.command` confirmed branch `codex/feat-t008-hosted-performance-20260907-2339`, commit `6bf136649a64`, the managed runtime source, and identical local/served UI hashes.

The same performance gate ran locally against `http://127.0.0.1:8000/health` with 20 baseline and 20 candidate samples per non-model surface. The receipt passed independent verification with `database,api,backtest` covered and `model_runtime` deferred.

Relevant automated checks:

```text
uv run pytest tests/test_hosted_performance_gate.py tests/test_performance_regression.py tests/test_hosted_load_gate.py tests/test_security_controls.py tests/test_release_attestation_workflow.py -q
25 passed
```

## Native desktop validation

Computer Use raised the native **Stock AI Liquid Glass** app and verified the URL was bound to `stock_ai_commit=6bf136649a64`. The home workspace, chart controls and Agent Dock were visible. No Agent Run, send, provider or model control was activated.

![Native T-008 validation](native-t008-hosted-performance-20260907.jpg)

## Remaining blocker

`model_runtime_baseline_and_candidate_samples_are_deferred_while_model_execution_is_disabled`

The full release gate therefore remains closed at 83 complete and 41 partial requirements. Its post-update ledger SHA-256 is `5d6ef9ce3f876a334f5b604370e7b5e84629f451791217e008a4300b5268676d`.
