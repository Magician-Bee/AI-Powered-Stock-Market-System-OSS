# 專案協作、分支與提交指南

本文件適用於人類開發者、ChatGPT、Codex，以及其他獲得此 Repository 授權的 Agent。任何人或 Agent 在修改、建立分支、提交或推送前，都必須先閱讀本文件。

## 2026-07-18 Git 歷史重寫

本 Repository 已在 2026-07-18 清除舊歷史中的本機 runtime、回測輸出、研究報告、瀏覽器截圖與大型暫存二進位檔。重寫後的 `main` 基準 commit 是：

```text
97a42891f7cc9bf3124d4849743e4f6412656868
```

它與重寫前 `f911a553301a35d1da6309d43f44f8b371e6b293` 的最新專案 tree 完全相同；改變的是舊 commit SHA，不是該版本的程式內容。

如果 Clone 建立於上述日期之前，不要直接把舊歷史 force-push 回 GitHub，也不要把舊 `main` 合併進新 `main`。最安全的做法是保留舊資料夾作備份，重新 Clone，再只轉移自己的來源碼修改：

```bash
cd ..
mv AI股市系統-Agent測試版 AI股市系統-Agent測試版-pre-rewrite
git clone https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS.git AI股市系統-Agent測試版
cd AI股市系統-Agent測試版
git switch main
git pull --ff-only
```

若舊資料夾仍有未提交修改，先在舊資料夾輸出 patch，並另外列出未追蹤檔案：

```bash
git status --short
git diff --binary > ../stock-ai-working-tree.patch
git diff --cached --binary > ../stock-ai-staged.patch
git ls-files --others --exclude-standard
```

在新 Clone 中逐一檢查後再套用：

```bash
git apply --check ../stock-ai-working-tree.patch
git apply ../stock-ai-working-tree.patch
git apply --check ../stock-ai-staged.patch
git apply ../stock-ai-staged.patch
```

若 patch 有衝突，停止自動套用並人工搬移自己的來源檔變更；不得用舊分支覆蓋新 `main`。專案維護者持有重寫前的完整 Git bundle，可在意外時離線還原，但該備份不得重新推回遠端。

## 開始修改

先確認遠端、身份、分支與工作目錄：

```bash
git remote -v
git status -sb
git fetch origin --prune
git switch main
git pull --ff-only
```

每項工作使用獨立分支。Codex 建議使用 `codex/<簡短名稱>`；其他自動化 Agent 可使用 `agent/<簡短名稱>`；人類開發者可使用 `<帳號>/<簡短名稱>`：

```bash
git switch -c codex/example-change
```

不得在含有不明修改的工作目錄直接開始，也不得靜默覆蓋其他人或其他 Agent 的變更。若 `git status` 不乾淨，先確認每個檔案的擁有者與用途。

## 可提交與不可提交的內容

只提交來源碼、測試、長期有效文件、設定範例與必要靜態資產。以下內容只屬於本機，不得提交：

- `.runtime/`：Python、uv、Codex、編譯後 App、外部 workflow runtime
- `logs/`：PID、port、server log、instance metadata
- `output/`：SQLite、回測、研究報告、匯出、驗收截圖
- `artifacts/`、`.playwright-cli/`：一次性瀏覽器與除錯資料
- `.env`、API key、Bearer token、Cookie、GitHub token 或其他憑證
- 模型權重、安裝檔、壓縮檔及可從上游或建置流程重建的大型二進位檔

需要長期保存的小型測試資料，應匿名化並放進 `tests/fixtures/`。外部研究專案必須依 `config/external_sources.lock.yaml` 的來源與 commit 管理，不得任意把下載快取或模型權重塞回 Git。

提交前必須明確檢查暫存區；不要在未檢查時使用 `git add -A`：

```bash
git add path/to/intended-file
git diff --cached --check
git diff --cached --stat
git diff --cached --name-only
```

這個檢查正常情況下不應輸出任何路徑：

```bash
git diff --cached --name-only |
  rg '(^|/)(\.runtime|logs|output|artifacts|\.playwright-cli)(/|$)'
```

## 必須通過的驗證

第一次開發先安裝依賴：

```bash
uv sync --extra dev
```

一般提交至少執行與修改範圍相關的測試。準備推送或修改核心 Agent、風控、資料、設定、前端載入順序時，必須執行完整檢查：

```bash
uv run python scripts/check_repo_hygiene.py
uv run python -m compileall -q src scripts
find src/stock_ai/ui/static -type f -name '*.js' -print0 |
  xargs -0 -n1 node --check
uv run pytest -q
git diff --check
```

若修改啟動器或首次開啟流程，還要實際執行：

```bash
/bin/bash stop-stock-ai.sh
/bin/bash open-stock-ai.sh
curl -fsS http://127.0.0.1:8000/health
/bin/bash stop-stock-ai.sh
```

不得因為測試太慢而刪除測試、縮小安全邊界、關閉 Codex 原有能力或把失敗結果寫成成功。

## Commit、Push 與 Pull Request

Commit 應只包含同一項目的變更，訊息清楚描述目的：

```bash
git commit -m "fix: describe the repaired behavior"
```

推送前重新取得 `main`。自己的工作分支可 rebase；不要 rebase 或 force-push 別人的分支：

```bash
git fetch origin --prune
git rebase origin/main
git push -u origin "$(git branch --show-current)"
```

建立 Pull Request 至 `main`，並在說明中列出：

- 修改內容與根因
- 使用者與系統影響
- 執行過的測試
- 尚未驗證的風險
- 是否涉及資料遷移、權限、秘密或外部服務

只有分支擁有者在確定沒有覆蓋他人提交時，才能對自己的 PR 分支使用：

```bash
git push --force-with-lease
```

不得 force-push `main`、不得重寫共享歷史、不得刪除遠端分支；除非 Repository 擁有者針對確切分支與操作給出明確授權。

## ChatGPT、Codex 與其他 Agent

私人 Repository 可透過 ChatGPT Apps／GitHub Connector 授權給指定帳號或 Agent。這種授權不代表 Repository 對外公開，也不代表其他未授權 AI 可以讀取。

Agent 在開始工作前必須：

1. 讀取 `README.md`、本文件及與任務相關的架構文件。
2. 顯示並理解 `git status -sb`、目前分支與遠端。
3. 保留現有權限與 Codex 原生能力；只能在任務範圍內加強，不得私自削弱。
4. 不得讀回、輸出或提交 Keychain、環境變數與登入憑證。
5. 修改後提供可驗證的測試、commit、遠端 SHA 與 CI 結果，不得只口頭宣稱已同步。

若出現歷史分歧、無共同 merge base、未知生成檔、秘密掃描警告或無法判斷修改擁有者，應停止推送並交由 Repository 擁有者確認。
