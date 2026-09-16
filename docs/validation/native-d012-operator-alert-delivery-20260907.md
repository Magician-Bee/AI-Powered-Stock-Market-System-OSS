# D-012 桌面資料告警投遞驗收

## 範圍

- 啟動入口：`./開啟股市AI系統.command`
- 桌面程式：Stock AI Liquid Glass
- 路徑：系統 → 資料品質與血緣 → 資料來源可觀測性與自動告警
- 模型與 Agent Run：未呼叫、未操作

## 驗收契約

桌面 launcher 以 `STOCK_AI_OPERATOR_ALERT_SINKS=desktop` 啟用本機 macOS Notification Center。使用者按下「評估並通知」後才投遞；每個目前告警都保存 append-only notification receipt。Provider receipt 的 `acknowledgement=os_request_accepted_only` 只表示作業系統接受請求，不宣稱人員已讀。

通知文案只使用 source、dataset、severity 與 code；alert details 不會進入 `osascript` 參數。非 macOS、命令錯誤或 OS 拒絕都回傳 failed receipt。

## 原生驗收結果

- 功能驗收 commit：`c4fb41dfee754b1532f8a4f24b58d029fd343cf7`
- 版本檢查：分支 `codex/fix-ui-responsive-system-20260907-1154`，Git commit、launcher 記錄 commit 與 managed runtime UI SHA 一致；結果為 `VERIFIED CURRENT PROJECT INSTANCE`。
- 原生操作：以 Stock AI Liquid Glass 開啟「系統 → 資料平台」，在「資料來源可觀測性與自動告警」按下「評估並通知」。
- 畫面結果：SLO 狀態 `breach`；自動告警 11 項；通知已投遞 11、失敗 0、未設定 0。畫面同時顯示 SLO receipt 與前三筆 notification receipt SHA-256。
- 資料庫核對：11 筆 `delivered` 收據的 provider sink 均為 `macos_notification_center`，acknowledgement 均為 `os_request_accepted_only`；歷史 27 筆 `not_configured` 收據保留，沒有覆寫或刪除。
- 截圖：native-d012-operator-alert-delivery-20260907.png（未隨公開版提供；原參考：`native-d012-operator-alert-delivery-20260907.png`）

## 自動化驗證

```text
uv run pytest tests/test_operator_notifications.py tests/test_data_observability_notifications.py tests/test_static_ui.py tests/test_production_requirement_status.py tests/test_api.py tests/test_portable_launcher_contract.py tests/test_launcher.py -q
141 passed, 1 warning
```

Release gate 已重新計算為 `complete=69`、`partial=55`、`unverified=0`；D-012 已離開 blocking requirement 清單。其他需要正式資料帳戶、外部服務、安全證據或仍未完成的項目維持 partial，沒有以本機通知驗收取代外部條件。
