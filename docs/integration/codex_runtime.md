# Codex 核心整合

股市AI系統直接以 `openai-codex` SDK 連接本機 Codex App Server。Codex thread
以專案根目錄為 `cwd`，可讀取完整程式、設定、測試、文件與外部來源鎖定資料，
不是 iframe 或獨立聊天視窗。

## 啟動與登入

```powershell
uv sync --extra dev
uv run python -m uvicorn stock_ai.main:app --host 127.0.0.1 --port 8000
```

開啟 <http://127.0.0.1:8000/>。未登入時，全畫面帳號入口會要求使用
ChatGPT 瀏覽器登入或裝置碼登入；登入狀態由 Codex App Server 管理，不由前端
保存 ChatGPT 密碼。

Windows 的 `開啟股市AI系統.bat` 與 macOS 的 `開啟股市AI系統.command`
會自動安裝 Python、鎖定套件與 OpenAI 官方最新 Codex CLI，並將其絕對路徑
以 `STOCK_AI_CODEX_BIN` 傳入後端，不依賴使用者先手動安裝 Codex。

## 系統能力

- `POST /api/codex/project/sync`、`POST /api/codex/run`：僅供明確啟用的開發者診斷；預設停用，不能作為使用者 Agent 任務入口。
- `POST /api/codex/market-radar`：僅供開發者診斷，不是首頁正式資料來源。
- `POST /api/agents/market-radar/runs`：首頁正式入口；使用目前選定 Provider，且必須提供明確 Universe。
- `GET /api/codex/account`：讀取 ChatGPT 帳號登入與方案狀態。
- `GET /api/codex/capabilities`：顯示 App Server、Computer Use 與專案範圍。

網頁操作使用具有網址驗證的 Chrome CUA；非瀏覽器 Windows 程式才使用原生
Computer Use。App Server 的命令、檔案、權限、使用者輸入與 MCP elicitation
請求由後端集中處理，Computer Use 權限只在該次明確要求操作 UI 的 turn 啟用。

## 完整能力保留與 Agent Runtime 加強

原生 Codex 的專案操作能力保留給明確開啟的開發者診斷：持久 thread 使用完整專案 `cwd`，可保留 full-access sandbox、命令、檔案、瀏覽器與 Computer Use。一般使用者任務固定經 `/api/agents/runs`，不會建立可見的原生 Codex 聊天。

當 Codex 作為 `codex` Agent driver 時，主系統會以 ChatGPT 帳號登入的 App Server 建立 ephemeral 結構化 turn，請 Codex 選擇 Stock AI 工具，再由主系統立即執行行情、研究、帳戶、風控與 Paper Broker 工具。這個路徑不會呼叫 `/api/codex/run`、不建立可見的原生 Codex 聊天，也不會把 UI 任務貼回 Codex 對話。

TradingAgents 與 FinRobot 也使用同一個帳號真相。Host 針對每個外部 workflow 建立只監聽 `127.0.0.1` 的短期 OpenAI-compatible bridge 與隱藏 Codex thread；外部 LangGraph／AutoGen process 只取得不可重用的隨機 bearer token。Codex thread 固定 `read_only`、`deny_all`，框架工具仍由外部 workflow 自己執行；模型請求、SDK turn/item/token-usage 與選用工具名稱回到同一個 Agent activity stream。這不是將問題轉送到可見 Codex 聊天，也不會把 ChatGPT/Codex 憑證交給外部 process。

原生 `/api/codex/run` 的 full-access project thread（終端機、檔案、瀏覽器、Skills、MCP 與 Computer Use）仍保留給明確的原生 Codex 診斷；它不是共享 Agent Runtime 的中繼工具。一般 UI 任務一律由 Stock AI Runtime 使用 Codex 作為目前推理 Provider，未來 provider 只能替換推理層，不能改變工具、記憶、UI 或風控。

相關模組：

- `src/stock_ai/codex_runtime.py`：原生完整 Codex runtime。
- `src/stock_ai/codex_llm_bridge.py`：run-scoped loopback 模型橋接與稽核證據。
- `src/stock_ai/agent_drivers.py`：Codex 與其他可替換 Agent driver。
- `src/stock_ai/agent_tools.py`：主系統持有的實際 Stock AI 工具。
- `src/open_stock_ai/agent_runtime/`：供應商無關主控迴圈。

詳細協定與安全邊界見 [可替換 Agent Runtime 與模擬交易](../architecture/agent-trading.md)。

## 市場與執行邊界

首頁市場雷達預設分析六檔觀察清單，結果快取五分鐘；「更新判斷」可強制重跑。
Codex 不能把未通過 `RiskEngine` 的買進訊號標示為「現在可買」，也不能把非
賣出來源訊號改寫為「現在可賣」。目前執行仍是 paper-only，沒有真實券商下單。

## 驗證

```powershell
uv run pytest -q
find src/stock_ai/ui/static/js -name '*.js' -exec node --check {} \;
uv run python -m compileall -q src/stock_ai
```

瀏覽器驗收產生的截圖與暫存輸出屬於 `output/`，不納入版本控制。若新增可重複執行的瀏覽器測試，測試程式應放在 `tests/browser/`，固定輸入則放在 `tests/fixtures/`。
