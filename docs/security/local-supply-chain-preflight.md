# 本機供應鏈 preflight

這份 preflight 是 GitHub hosted security workflow 的本機對等檢查。它不會觸發
GitHub CI，也不會把 SBOM、掃描結果或憑證寫入專案目錄。

## 必須使用 lockfile 匯出的第三方清單

`pip-audit` 若直接掃描目前環境，會嘗試向 PyPI 查詢本機專案
`stock-ai-ecosystem`，並以「本機套件不在 PyPI」結束。這不是漏洞掃描通過。

應先從 `uv.lock` 匯出且省略 project 本身，再掃描第三方依賴：

```bash
uv export --frozen --no-hashes --no-emit-project \
  --output-file /tmp/stock-ai-third-party-requirements.txt
uv run --with pip-audit pip-audit --strict --desc \
  --no-deps --disable-pip \
  -r /tmp/stock-ai-third-party-requirements.txt
```

然後執行 Bandit 高嚴重度 gate 與 deterministic CycloneDX SBOM：

```bash
uv run --with bandit bandit -q -r src -lll
uv run python scripts/build_sbom.py --output /tmp/stock-ai-sbom.cdx.json
shasum -a 256 /tmp/stock-ai-sbom.cdx.json
```

Hosted workflow 會將 CodeQL 的 SARIF 留成 artifact，並以
`scripts/check_codeql_sarif.py` 直接阻擋 security severity 7.0 以上的結果。這條路徑不依賴
GitHub code-scanning API 權限；缺少 SARIF、內容損壞或有 high／critical finding 都會失敗。
CodeQL action 的 analyze step 即使因未啟用 GitHub code scanning 而回報上傳錯誤，後續 gate 仍會
讀取它已產生的 SARIF；若 SARIF 沒有生成，gate 會失敗，不能被 `continue-on-error` 掩蓋。
CodeQL 僅分析 `src/` 與 `scripts/` 的第一方程式，`external/` 的 vendored upstream 不會混入
本專案的 gate。

## 2026-09-01 本機 receipt

在目前 lockfile 下，上述第三方依賴掃描回報 `No known vulnerabilities found`；
Bandit 高嚴重度 gate 通過。生成的 SBOM SHA-256 是
`8ca20c05ec095307fa36f491b08783de3aa3e555fbf584214ec91d03a7b7d5ef`。

這份本機 receipt 不取代 SEC-003～SEC-006 所需的 hosted scan、CVE exception
owner triage、已簽名 release SBOM 或 release signing identity；那些條件仍維持
partial，且不得因為本機 preflight 成功而被標記為完成。
