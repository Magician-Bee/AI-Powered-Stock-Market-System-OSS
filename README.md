# AI 股市系統 · Open Stock AI

**以台股為核心的本機市場研究、模型可替換 Agent 與紙上交易工作站。**

本專案把市場資料、研究證據、風控、模擬委託與持倉帳本放在同一個可追溯流程中，讓開發者研究 AI 如何提出、執行及核對交易計畫。原創程式碼採 MIT 授權，第三方元件保留各自授權。

> 公開原始碼預覽版。最高執行等級為 **PAPER**；真實券商送單與模型直接下單保持關閉。公開發布不代表已通過全部產品驗收、已證明獲利，或適合無人監督的真金交易。

[公開版本說明](PUBLIC_RELEASE.md) · [驗證證據](docs/evidence/README.md) · [貢獻指南](CONTRIBUTING.md) · [安全政策](SECURITY.md) · [隱私](docs/PRIVACY.md) · [授權](LICENSE)

## 專案提供什麼

| 領域 | 內容 |
| --- | --- |
| 台股資料與圖表 | 官方證券主檔、歷史與即時行情來源接頭、互動 K 線、新聞、基本面與籌碼資料整合。資料的實際可用性依來源與設定而定。 |
| AI 研究工作台 | 可替換的模型與 Agent 驅動器、工具呼叫、研究證據、持久任務及可觀察事件。Host 負責權限、成本、執行與驗證邊界。 |
| 交易計畫 | 研究範圍掃描、策略評估、部位與條件／定時進出場計畫；不把模型結語直接當成獲利或完成證據。 |
| 紙上交易 | 中央風控、模擬委託／成交、持倉、現金與損益帳本，以及來源收據與對帳。模擬成本不是券商實際費率。 |
| 開發與評測 | 原始碼、合成測試、離線風控／紙上帳戶回歸、資料來源追溯與公開檢查報告。 |

## 執行邊界

`config/capability_status.yaml` 是能力等級的唯一設定來源；本版保留 PAPER、停用實單與禁止模型直接向券商送單的設定。Python 單元測試、資料來源實測、模型買賣閉環、策略績效與實盤驗收是不同證據，不能互相替代。

部分外部研究框架、通知、n8n、券商 SDK 及模型服務需要使用者自行安裝、設定及授權。未通過資料／風控／研究資格的流程應明示缺項，不以固定答案或隱藏降級冒充成功。AI-Trader 原始碼未隨本版散布，相關選用流程可能顯示缺少前置條件。

## 取得與啟動

```bash
git clone https://github.com/Magician-Bee/AI-Powered-Stock-Market-System-OSS.git
cd AI-Powered-Stock-Market-System-OSS
```

Windows：雙擊 `開啟股市AI系統.bat`；停止使用 `停止股市AI系統.bat`。macOS：雙擊 `開啟股市AI系統.command`；停止使用 `停止股市AI系統.command`。啟動器會準備執行依賴並開啟本機工作台；首次啟動需要網路。完整步驟見[可攜式啟動指南](docs/guides/portable-launch.md)。

開發環境安裝 Python 3.12 與 uv 後：

```bash
uv sync --frozen --extra dev
uv run python -m uvicorn stock_ai.main:app --host 127.0.0.1 --port 8000
```

開啟 `http://127.0.0.1:8000`。金鑰與帳戶設定只放在本機設定／秘密管理中；`.env.example` 僅提供空白範本。不要把工作台裸露到公網，不要匯入真實券商憑證作為公開測試資料。市場查詢或模型功能會連線到選定來源，可能需要權限或產生費用。

## 公開驗證

[本版驗證報告](docs/evidence/public-validation.json)記錄實際執行的原始碼／機密規則檢查與指定的離線回歸測試。回歸程序阻擋 socket 連線，未啟動應用程式服務、未呼叫真實模型或券商。它不是全套功能、真模型自主交易、獲利或全平台安裝認證。

```bash
uv run python scripts/validate_public_release.py --gitleaks /path/to/gitleaks --output output/public-validation.json
```

公開版檢查包含固定測試範圍，原始完整開發狀態與歷史驗收敘述保留於 [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md)。未公開的私人資料、執行截圖、日誌與原始研究報告不會被標示為可下載的公開證據。

## 參與及授權

由 Magician-Bee 維護。歡迎以合成資料提出可重現問題、測試與修正；本版不主張已存在大規模外部使用或下載數據。程式碼使用 [MIT](LICENSE)；[第三方聲明](THIRD_PARTY_NOTICES.md)與資料供應商條件各自適用。這是研究與軟體開發工具，不構成投資建議，也不保證獲利。

<details>
<summary>能力登錄（由來源設定產生）</summary>

<!-- capability-status:start -->
### 交易能力分級（自動產生）

唯一來源：`config/capability_status.yaml`。目前核准最高等級為 **PAPER**（`paper`）；
券商實單、模型直接下單與跨級環境變數啟用均為關閉。
升級必須依序通過 RESEARCH → PAPER → SHADOW → BROKER_SANDBOX → RESTRICTED_LIVE → PRODUCTION_LIVE，
且需要簽章人工核准與該等級全部驗收證據。
P0–P105 真相登錄：`config/production_requirement_status.yaml`；目前共有 **124** 項要求，
由 release gate 依阻擋項目逐項判定；P0 尚未完成 `Q-003`、`D-002`、`D-003`、`E-006`、`E-007`、`E-008`、`E-009`、`SEC-001` 等 9 項，任何未驗證項目都不得因為程式碼或 UI 存在而視為完成。
完整 P0–P105 release checklist：**85/124**；P1/P2 未驗收項目同樣會阻擋 release。
Quant P0 基礎門檻為 **8/9**；研究結果作為執行證據目前為 **關閉**，
待 `Q-003` 的阻擋條件全部驗收後才能開啟。
<!-- capability-status:end -->
</details>
