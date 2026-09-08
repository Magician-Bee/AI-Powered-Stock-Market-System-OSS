# FinRL / Qlib runtime smoke — 2026-08-28

這份收據記錄桌面專案本機隔離 runtime 的實際執行驗收。它驗證的是研究模型
工作流與 artifact receipt；不是投資建議、真實券商連線或交易授權。

## 執行環境與命令

在專案根目錄、既有的 `.runtime/external-workflows/` 隔離環境中執行：

```bash
STOCK_AI_RUN_EXTERNAL_MODEL_E2E=finrl \
  uv run pytest -q tests/integration/test_external_model_runtime.py -k finrl

STOCK_AI_RUN_EXTERNAL_MODEL_E2E=qlib \
  uv run pytest -q tests/integration/test_external_model_runtime.py -k qlib
```

兩項 release smoke 都預設跳過，只有明確設定對應環境變數才會建立隔離 subprocess
並執行；一般 UI 分析不會觸發它們。

## 驗收結果

- FinRL：使用 vendored FinRL `DRLAgent.get_model/train_model` 以 PPO 訓練 64
  timesteps，保存 Stable-Baselines3 policy，於新的隔離程序載入 policy 後完成
  11 個 action inference steps。測試核對訓練與重載的 policy SHA-256 相同。
- Qlib：以 `config/qlib/workflow_smoke_linear_Alpha158.yaml` 與本機
  `.runtime/external-workflows/qlib-data/cn_data` provider 實際執行
  `qlib.cli.run.workflow`。receipt 含 Dataset、`params.pkl`、`pred.pkl`、
  Signal Analysis 與 Portfolio Analysis artifacts，且每個 artifact 都帶有
  SHA-256 與 size。
- 兩個 worker receipt 都使用
  `open_stock_ai.external_worker_runtime_fingerprint.v2`，擷取 Python build、
  非識別性 hardware metadata 與 package-lock SHA-256，避免把舊版 v1 schema
  誤當作最新 runtime evidence。

## 安全邊界

這兩項驗收只寫入本機 `.runtime/external-workflows/` 與本機 immutable model
registry；不會呼叫券商 SDK、不會讀取帳戶憑證、不會傳送實盤訂單，也不會提升任何
模型輸出為交易權限。研究模型的 production promotion、deterministic-kernel
attestation 與任何真實券商驗證仍須各自的外部證據，不能由本收據推論為完成。
