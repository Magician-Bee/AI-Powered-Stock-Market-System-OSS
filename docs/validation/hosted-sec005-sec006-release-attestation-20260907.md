# SEC-005／SEC-006 hosted release attestation 驗收

- 日期：2026-09-07（Asia/Taipei）
- Requirements：`SEC-005`、`SEC-006`
- 最終候選：`fe41ed5a59427a9c389a32b025b3f7f029bf1c71`
- GitHub run：[34137501131](https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34137501131)
- 結果：`success`

## 實際簽署與驗證

`build-and-sign` job（`101791761628`）在 GitHub-hosted Linux runner 建立 exact-commit source archive、由釘定 `uv.lock` 產生 deterministic CycloneDX SBOM，並建立綁定 commit、source artifact SHA-256 與 signer identity 的 provenance。Workflow 使用釘定的 `sigstore/cosign-installer` commit `6f9f17788090df1f26f669e9d70d6ae9567deba6` 與 cosign `v3.0.5`，透過 GitHub Actions OIDC／Fulcio keyless certificate 分別簽署 source、provenance、SBOM；沒有建立或保存長效私鑰。

`clean-runner-verify` job（`101791880993`）只在簽署 job 完成後啟動。它從 GitHub artifact 下載輸出，重新產生 SBOM 與 provenance、逐位元比較，檢查相對路徑 `SHA256SUMS`，再對三份 bundle 執行 `cosign verify-blob`，固定要求：

- certificate identity：`https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/.github/workflows/release-attestation.yml@refs/heads/main`
- OIDC issuer：`https://token.actions.githubusercontent.com`
- transparency log：`https://rekor.sigstore.dev`

三次驗證均回報 `Verified OK`，才建立 `stock_ai.hosted_release_attestation_gate.v1` receipt。

## Artifact 與不可變識別

- signed release artifact `10024560115`：`stock-ai-signed-release-fe41ed5a59427a9c389a32b025b3f7f029bf1c71`，51,767,397 bytes，保存至 `2026-12-06T15:17:04Z`
- clean verification artifact `10024572617`：`stock-ai-release-verification-fe41ed5a59427a9c389a32b025b3f7f029bf1c71`，1,248 bytes，保存至 `2026-12-06T15:17:04Z`
- source archive SHA-256：`d9051f26cc9ca42d6afa4a1370e915eaf62f4a4fa1c0b42f79485cdad3c42b11`
- provenance SHA-256：`ee79bd734fad76df03b0a1778e655d2bcc4d1c62b359d51f98a854338f981a0d`
- SBOM SHA-256：`a549d7755a922327fdbf85c1d36412f8533e2fb88795ba8b656a97c5f50b39af`
- source bundle SHA-256：`e7b798cbc431e302758f644b23200698dc002f6fd6992fc01d641f1648e0eb37`
- provenance bundle SHA-256：`0a2ba17063cd09c6af5167080af73a8e9700a59f7c1b0994d92f6b7cb1971bd0`
- SBOM bundle SHA-256：`5db91b65f052e12c550ca083e2c1965e5714fc1e58ce46677744f7e319a423f4`
- gate receipt file SHA-256：`6a2eb03e82de5b30faea5ddc596e2a0111fc3fc8a6cb4f143653a44dd931704f`
- gate receipt self-hash：`103a02b84d60ce055894d0a1de255f433d2f8f48afcff5734a5b4c05df9871b8`

Sigstore v0.3 bundles 各含一筆 transparency-log inclusion：source Rekor index `2752076614`、provenance `2752076738`、SBOM `2752076853`。

## 失敗路徑未被誤報

初版 run [34118886214](https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34118886214) 使用 GitHub Artifact Attestations API，但 GitHub 明確拒絕 user-owned private repository。該 run 結果為 `failure`，沒有產生完成證據，也沒有被納入 requirement gate。最終實作改用 Sigstore 的 GitHub Actions OIDC keyless signing；成功證據只來自 run `34137501131`。

## 本機與原生桌面驗收

- 測試：`uv run pytest tests/test_release_attestation_workflow.py tests/test_security_controls.py -q` → `12 passed`
- `./開啟股市AI系統.command` 啟動分支 `codex/fix-private-sigstore-attestation-20260907-1954`
- `./驗證目前執行版本.command` 驗證 commit `fe41ed5a5942`、managed runtime source 與 local／served UI SHA 一致，結果為 `VERIFIED CURRENT PROJECT INSTANCE`
- Computer Use 在可見的原生 `Stock AI Liquid Glass` App 驗收首頁與 Agent Dock；未操作 Agent Run 控制、未送出訊息、未呼叫本地或遠端模型。

SEC-005／SEC-006 原生桌面首頁與 Agent Dock 驗收（未隨公開版提供；原參考：`native-sec005-sec006-release-attestation-20260907.png`）
