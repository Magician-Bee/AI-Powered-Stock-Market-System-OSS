# 股市AI系統可攜式啟動

整個專案資料夾可以搬到另一台 Windows 或 macOS 電腦。GitHub 的 **Download ZIP** 與 `git clone` 都是支援的取得方式；啟動器不依賴原電腦的絕對路徑，也不會跨作業系統重用舊 `.venv` 或 `.runtime`。

下載後看到的專案通常只有原始碼、介面資源與隨附外部專案。第一次啟動會另外建立 Python、套件、快取與工具，因此資料夾由數百 MB 增加到數 GB 是正常現象，不代表 ZIP 下載不完整。

## 建議的下載方式

### GitHub Download ZIP

1. 在 GitHub 專案頁選擇 **Code → Download ZIP**。
2. 完整解壓縮到一般可寫入的資料夾。
3. 不要直接在 ZIP 壓縮檔預覽視窗內執行。
4. Windows 雙擊 `開啟股市AI系統.bat`；macOS 雙擊 `開啟股市AI系統.command`。

ZIP 下載沒有 `.git` 歷史，啟動器會將它識別為 `archive-or-working-copy`；這不影響系統功能。

### Git clone

```bash
git clone https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS.git
cd AI-Powered-Stock-Market-System
```

私人儲存庫需要先完成 GitHub 驗證。`git clone` 的優點是之後可以使用 `git pull` 更新，但不是正常啟動的必要條件。

## Windows

雙擊 `開啟股市AI系統.bat`。

Windows 啟動器會：

- 強制使用 UTF-8，支援中文、空白與一般 Unicode 專案路徑；
- 為目前專案路徑建立唯一 instance ID；
- 驗證健康狀態、目前 commit／working copy、專案根目錄與受保護 API；
- 從首頁取得 `X-Stock-AI-Session` 後再檢查 `/api/codex/capabilities`，不會把安全機制的 403 誤判成啟動失敗；
- 若上一次啟動留下同一專案的舊程序，先安全停止再重新啟動；
- 若 `8000` 已被其他程式使用，自動改用 `8001` 至 `8999` 的可用連接埠。

不需要系統管理員權限，也不需要預先安裝 Python。支援 64 位元 Windows 10/11 與內建 Windows PowerShell 5.1 或更新版本。

## macOS

雙擊 `開啟股市AI系統.command`。

Windows 的 `.bat` 是 Windows 命令檔，macOS Finder 無法原生執行，因此 macOS 必須使用 `.command`。

若檔案是經由不保留執行權限的雲端硬碟或壓縮工具搬移，第一次可在 macOS「終端機」執行：

```bash
chmod +x 開啟股市AI系統.command 停止股市AI系統.command 啟動n8n執行層.command open-stock-ai.sh stop-stock-ai.sh scripts/start-n8n-headless.sh
```

之後即可直接雙擊。

## 第一次啟動會做什麼

1. 在專案內的 `.runtime` 安裝固定版本的 `uv`。
2. 自動下載與使用 Python 3.12，不需要預先安裝 Python。
3. Windows 建立 `.runtime/venv-windows`，並將受管理的 Python 放在 `.runtime/python`。
4. macOS 依 Intel 或 Apple Silicon 建立 `.runtime/venv-macos-<架構>`。
5. 依 `uv.lock` 安裝固定版本的必要套件。
6. 從 OpenAI 官方 Codex 發行頁自動安裝或更新相容的 Codex CLI。
7. 從 `8000` 起自動尋找未被占用的本機埠並啟動服務。
8. Windows 自動開啟預設瀏覽器；macOS 26 或更新版本自動安裝並開啟隨附的 Apple AppKit Liquid Glass 原生 App，不需要先安裝 Xcode。

如果 `8000` 已被其他程式使用，啟動器會自動改用 `8001` 至 `8999` 之間的可用埠，並開啟正確的網址。健康檢查會確認它是目前這份專案、目前版本與目前路徑所啟動的服務，不會把其他剛好使用相同網址的程式誤認成股市系統。

macOS 若從 exFAT/FAT 外接磁碟啟動，由於磁碟不支援 Python 環境需要的符號連結，啟動器會自動將執行環境放在 Mac 的 `~/Library/Caches/StockAI-System/` 下；專案檔案仍保留在外接磁碟，不需要手動搬移。

Apple 真正的 Liquid Glass 是 macOS 原生框架能力，不是跨平台網頁 CSS。macOS 26 原生 App 使用系統的 `NSGlassEffectView` 與 `NSGlassEffectContainerView`；Windows 與舊版 macOS 仍使用功能完整的網頁介面。技術邊界與官方來源記錄在 [`apple-liquid-glass.md`](apple-liquid-glass.md)。

第一次安裝執行環境需要網路，並可能下載大量 Python 套件與工具。安裝完成後，之後啟動會重用已安裝的環境。Codex 暫時下載失敗時，股市系統仍會正常開啟，之後可在設定頁重新連接。

## 跨電腦與跨系統搬移

搬移專案時不需要攜帶舊電腦的 `.runtime`。Windows 與 macOS 的虛擬環境、Python 執行檔及原生工具不可互用；新電腦會自動建立自己的環境。

建議只同步原始專案，不要用雲端硬碟把正在使用中的 `.runtime`、`logs` 或資料庫檔案在兩台電腦間即時覆蓋。Git 已透過 `.gitignore` 排除這些本機執行內容。

新電腦上的 ChatGPT/Codex 登入狀態不會從舊電腦複製。第一次開啟後，依畫面登入一次即可。

## 啟動失敗時

先查看：

- `logs/stock-ai-server.out.log`
- `logs/stock-ai-server.err.log`

若舊版 Windows 啟動器曾顯示 `The server did not become ready`，但日誌同時顯示 `Uvicorn running`，請先雙擊 `停止股市AI系統.bat`，再使用更新後的啟動器。新版會攜帶正確的 runtime session header、清理失敗後的殘留程序，並且不會把成功啟動誤判為失敗。

只有在套件環境確定損壞時，才需要關閉系統後刪除 `.runtime`，再重新啟動讓它完整重建。不要刪除專案原始碼、`.env` 或需要保留的資料庫。

公司防火牆、離線環境、防毒軟體或作業系統阻止官方下載時，第一次自動安裝仍可能需要允許網路存取。這是新電腦唯一無法由專案檔案本身繞過的外部限制。

## Headless n8n 執行層

只有跨服務、每日／每週或長期流程需要 n8n；高頻行情、價格穿越與短延遲條件仍由 Stock AI 的內部 Runtime 處理。雙擊「啟動n8n執行層.command」會把固定版本 n8n 安裝到本專案的 .runtime/n8n/，只監聽 127.0.0.1:5678，並以專案私有的加密金鑰保存其資料。

第一次啟動會由 `scripts/setup-n8n-local-owner.sh` 在 loopback 實例建立本機 owner、最小 workflow scopes 的 API key，以及獨立的 callback secret，並只寫入 Git 忽略的 `.runtime/n8n/data/stock-ai-gateway.env`（權限 600）。若本機 n8n owner 已存在、但該檔中的 gateway API key 已失效，bootstrap 會使用同一個受限本機 owner session 重新建立替代的 scoped workflow key；缺少本機 owner recovery credentials 時會 fail-closed，要求 owner 手動提供新 key。Stock AI 只載入 gateway URL／token／callback secret；owner 密碼不會匯入 App process。callback wire token 由獨立 callback secret 單向衍生，因此 API key 可獨立輪替；執行 `scripts/rotate-n8n-callback-secret.sh` 後必須重新部署既有 workflow。若 bootstrap 或 readiness 失敗，Agent 會保留待送出的 durable submission，不會假稱已啟用。
headless n8n 同時關閉成功／錯誤／逐節點／手動 execution payload 留存，啟用 pruning，並將完成 execution 的保留上限設為 168 小時或 2,000 筆（先到者為準）；Stock AI 自身仍保留已 redacted 的 durable callback／Agent receipts。

預設只允許 `local` 模式的 loopback n8n。若在受控環境明確設定 `N8N_AUTOMATION_DEPLOYMENT_MODE=remote`，必須同時提供 HTTPS URL、`N8N_AUTOMATION_REMOTE_HOST_ALLOWLIST`、遠端 leaf certificate 的 SHA-256 pin（`N8N_AUTOMATION_REMOTE_TLS_CERTIFICATE_SHA256`）；選擇 mTLS 時，client certificate 與 key 必須成對設定。遠端端點不得 redirect，callback 只是一個最小化的 wake-up envelope，不能攜帶下單、權限或其他 Agent 指令。

## 停止系統

- Windows：雙擊 `停止股市AI系統.bat`
- macOS：雙擊 `停止股市AI系統.command`

macOS 停止腳本同時辨識此專案目錄及其唯一 managed `service-source`，並驗證程序命令包含 `uvicorn stock_ai.main:app`；其他副本或其他服務不在停止範圍。可在停止後用 `lsof -nP -iTCP:<原服務埠> -sTCP:LISTEN` 確認埠已釋放，重新啟動後執行 `./驗證目前執行版本.command` 核對 SHA 與 managed source。

需要保留歷史供桌面驗收、但暫時不能自動呼叫模型時，使用：

```bash
./停止股市AI系統.command
STOCK_AI_AGENT_BACKGROUND_PAUSED=1 ./開啟股市AI系統.command
```

此程序環境設定會傳入 launchd 與 fallback 啟動路徑，跳過 Agent 背景恢復、worker 啟動與排程，並拒絕 Automation reanalysis 喚醒；不修改已保存的排程或使用者暫停狀態。這不會停掉獨立的 n8n 服務，也不是所有模型 API 的總開關：驗收時不得手動送出 Agent、模型測試或模型分析。設定變更必須先停止服務，不能重用既有程序。解除限制後先停止，再執行一般啟動指令即可恢復。
