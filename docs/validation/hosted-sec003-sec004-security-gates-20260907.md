# SEC-003／SEC-004 hosted security gate 驗證

- 日期：2026-09-07（Asia/Taipei）
- 工作分支：`codex/fix-security-evidence-20260907-1221`
- 掃描 commit：`41cab35851163ac575a10ea4b9308d59fb3d8de5`
- GitHub Actions run：<https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS/actions/runs/34083835719>
- 結果：`success`

## Hosted job 結果

- `codeql`：成功；第一方 `src/`、`scripts/` 產生 SARIF，內建 gate 以 security severity `7.0` 為阻擋門檻。
- `static-and-dependency`：成功；Bandit high-severity gate、從 `uv.lock` 匯出的釘定第三方依賴稽核、CycloneDX SBOM 與 artifact 上傳均成功。
- pip-audit：53 個釘定依賴、0 個已知漏洞、0 個修復項目。

## 不可變證據

- CodeQL gate receipt SHA-256：`bb9de9c8a481cc2f9704bb6e0e8cc62512368c88163bdf9d3cc0566efe87b37d`
- CodeQL SARIF SHA-256：`e31ba62b9e88320ed78938c942bb696447dc29b2186d00064cbffbc32492cb99`
- CodeQL 結果數：45；high／critical：0。
- `codeql-security-receipt` artifact ID：`10004609071`。
- pip-audit JSON SHA-256：`5b302ddfc6684fb9dd6c0a2d5326c470ed013bc673bfb5549a0b6c35f0cee4d3`。
- CycloneDX SBOM SHA-256：`a549d7755a922327fdbf85c1d36412f8533e2fb88795ba8b656a97c5f50b39af`。
- `stock-ai-supply-chain-receipts` artifact ID：`10004539478`。

`scripts/check_codeql_sarif.py` 對缺少或損壞的 SARIF 採 fail-closed，並在任何 security severity 7.0 以上結果存在時令 workflow 失敗。`pip-audit` 使用 `--no-deps --disable-pip` 直接核對完整釘定清單，避免 PyPI 已撤下套件版本造成安裝解析假失敗。

SEC-005 維持 partial：本次保存的是 hosted workflow SBOM artifact，尚未具備 production signing identity 與簽名 release SBOM。

## 原生桌面驗收

以 `./停止股市AI系統.command` 停止舊服務，再由 `./開啟股市AI系統.command` 啟動 commit
`e448a3e022b5`。啟動器回報 managed runtime、instance `fae9545ae1cab455`、port `8000`；
Computer Use 在可見的原生「Stock AI Liquid Glass」中開啟「系統 → 資料平台」，確認資料平台卡片、
Source Observability、Agent Dock 與導覽控制正常顯示。驗收畫面：
`docs/validation/native-sec003-sec004-security-gates-20260907.jpg`。
